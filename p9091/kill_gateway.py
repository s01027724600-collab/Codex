"""Kill-only gateway 鈥?minimal rescue panel on port 9091.

Independent of the main gateway. Shares only auth.json for password.
Kills at OS level 鈥?no dependency on the main gateway's job state.
"""

import argparse
import ctypes
import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
import uuid
from ctypes import wintypes
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from typing import List, Optional, Tuple

APP_NAME = "claude-code-gateway-kill"
VERSION = "0.1.1"
SESSION_COOKIE = "gateway_session"
SHELL_THIS_PC = "shell:this_pc"
DESKTOP_PUBLIC = "desktop:public"
SHELL_OPEN_PREFIX = "shellopen:"


def is_windows() -> bool:
    return sys.platform.startswith("win")


def ensure_background_stdio() -> None:
    if sys.stdin is None:
        sys.stdin = open(os.devnull, "r", encoding="utf-8", errors="replace")
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8", errors="replace")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8", errors="replace")


ensure_background_stdio()


_ORIGINAL_SUBPROCESS_POPEN = subprocess.Popen


def _windows_hidden_subprocess_defaults(kwargs: dict) -> dict:
    if not is_windows():
        return kwargs

    creationflags = int(kwargs.get("creationflags") or 0)
    creationflags |= getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    creationflags &= ~getattr(subprocess, "CREATE_NEW_CONSOLE", 0x00000010)
    kwargs["creationflags"] = creationflags

    startupinfo = kwargs.get("startupinfo")
    if startupinfo is None:
        startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = getattr(subprocess, "SW_HIDE", 0)
    kwargs["startupinfo"] = startupinfo
    return kwargs


def _hidden_subprocess_popen(*popenargs, **kwargs):
    return _ORIGINAL_SUBPROCESS_POPEN(*popenargs, **_windows_hidden_subprocess_defaults(kwargs))


if is_windows():
    subprocess.Popen = _hidden_subprocess_popen


# ----------------------------------------------------------------
#  Minimal auth (same password as main gateway)
# ----------------------------------------------------------------

def load_auth_config(auth_file: str) -> dict:
    try:
        with open(auth_file, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def b64url_decode(s: str) -> bytes:
    s = s.replace("-", "+").replace("_", "/")
    padding = 4 - len(s) % 4
    if padding != 4:
        s += "=" * padding
    import base64
    return base64.b64decode(s)


# ----------------------------------------------------------------
#  Taskbar-window listing (Windows only)
# ----------------------------------------------------------------

def list_taskbar_apps(query: str = "", limit: int = 80) -> list:
    """Return visible top-level app windows, not background process noise."""
    if not is_windows():
        return []

    from ctypes import wintypes

    class Rect(ctypes.Structure):
        _fields_ = [
            ("left", ctypes.c_long),
            ("top", ctypes.c_long),
            ("right", ctypes.c_long),
            ("bottom", ctypes.c_long),
        ]

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    try:
        dwmapi = ctypes.windll.dwmapi
    except Exception:
        dwmapi = None

    enum_proc_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows.argtypes = [enum_proc_type, wintypes.LPARAM]
    user32.EnumWindows.restype = wintypes.BOOL
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsWindowVisible.restype = wintypes.BOOL
    user32.IsIconic.argtypes = [wintypes.HWND]
    user32.IsIconic.restype = wintypes.BOOL
    user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user32.GetWindowTextLengthW.restype = ctypes.c_int
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowTextW.restype = ctypes.c_int
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
    user32.GetWindow.restype = wintypes.HWND
    user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.GetWindowLongW.restype = ctypes.c_long
    user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(Rect)]
    user32.GetWindowRect.restype = wintypes.BOOL
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    if dwmapi:
        dwmapi.DwmGetWindowAttribute.argtypes = [
            wintypes.HWND,
            wintypes.DWORD,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        dwmapi.DwmGetWindowAttribute.restype = ctypes.c_long

    gw_owner = 4
    gwl_exstyle = -20
    ws_ex_toolwindow = 0x00000080
    ws_ex_appwindow = 0x00040000
    dwmwa_cloaked = 14
    process_query_limited_information = 0x1000
    ignored_names = {
        "searchhost.exe",
        "shellexperiencehost.exe",
        "startmenuexperiencehost.exe",
        "textinputhost.exe",
        "widgets.exe",
        "widgetservice.exe",
    }
    protected_names = {
        "explorer.exe",
        "applicationframehost.exe",
    }
    ignored_titles = {
        "undefined",
        "default ime",
        "msctfime ui",
    }

    def get_title(hwnd: int) -> str:
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return ""
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buffer, length + 1)
        return buffer.value.strip()

    def is_cloaked(hwnd: int) -> bool:
        if not dwmapi:
            return False
        value = ctypes.c_int(0)
        result = dwmapi.DwmGetWindowAttribute(
            hwnd, dwmwa_cloaked, ctypes.byref(value), ctypes.sizeof(value)
        )
        return result == 0 and bool(value.value)

    def window_area(hwnd: int) -> int:
        if user32.IsIconic(hwnd):
            return 0
        rect = Rect()
        if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return 0
        width = max(0, int(rect.right - rect.left))
        height = max(0, int(rect.bottom - rect.top))
        return width * height

    def process_path(pid: int) -> str:
        handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
        if not handle:
            return ""
        try:
            size = wintypes.DWORD(32768)
            buffer = ctypes.create_unicode_buffer(size.value)
            if kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
                return buffer.value
            return ""
        finally:
            kernel32.CloseHandle(handle)

    rows = {}

    def callback(hwnd: int, lparam: int) -> bool:
        if not user32.IsWindowVisible(hwnd):
            return True
        if is_cloaked(hwnd):
            return True
        title = get_title(hwnd)
        if not title:
            return True
        if title.strip().lower() in ignored_titles:
            return True

        exstyle = int(user32.GetWindowLongW(hwnd, gwl_exstyle))
        is_app_window = bool(exstyle & ws_ex_appwindow)
        if (exstyle & ws_ex_toolwindow) and not is_app_window:
            return True
        if user32.GetWindow(hwnd, gw_owner) and not is_app_window:
            return True
        area = window_area(hwnd)
        if not is_app_window and area < 40000:
            return True

        pid = wintypes.DWORD(0)
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        pid_value = int(pid.value)
        if pid_value <= 0:
            return True

        path = process_path(pid_value)
        name = Path(path).name if path else f"pid-{pid_value}"
        if name.lower() in ignored_names:
            return True
        existing = rows.get(pid_value)
        if existing and int(existing.get("area") or 0) >= area:
            return True
        rows[pid_value] = {
            "pid": pid_value,
            "name": name,
            "title": title,
            "hwnd": f"0x{int(hwnd):X}",
            "area": area,
        }
        return True

    user32.EnumWindows(enum_proc_type(callback), 0)
    needle = query.strip().lower()
    out = []
    current_pid = os.getpid()
    for row in sorted(rows.values(), key=lambda item: (str(item["title"]).lower(), int(item["pid"]))):
        haystack = " ".join([str(row["pid"]), row["name"], row["title"], row["hwnd"]]).lower()
        if needle and needle not in haystack:
            continue
        row.pop("area", None)
        name_lower = str(row.get("name") or "").lower()
        row["closeable"] = True
        row["killable"] = row["pid"] not in (0, 4, current_pid) and name_lower not in protected_names
        if name_lower in protected_names:
            row["protected_reason"] = "Windows shell process"
        out.append(row)
        if len(out) >= max(1, min(int(limit or 80), 200)):
            break
    return out


def kill_process(pid: int) -> bool:
    try:
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
        return True
    except Exception:
        return False


def kill_taskbar_app(pid: int) -> bool:
    visible = list_taskbar_apps(limit=200)
    visible_pids = {int(row.get("pid") or 0): row for row in visible}
    row = visible_pids.get(int(pid))
    if not row:
        raise ValueError("pid is not a visible taskbar app")
    if not row.get("killable"):
        raise ValueError("refusing to force-kill a protected Windows shell process")
    if int(pid) in (0, 4, os.getpid()):
        raise ValueError("refusing to kill a protected process")
    return kill_process(int(pid))


def close_taskbar_window(hwnd_text: str) -> bool:
    if not is_windows():
        raise ValueError("window closing is only supported on Windows")
    try:
        hwnd = int(str(hwnd_text), 16)
    except (TypeError, ValueError):
        raise ValueError("invalid hwnd")
    visible_hwnds = {str(row.get("hwnd") or "").lower() for row in list_taskbar_apps(limit=200)}
    if str(hwnd_text).lower() not in visible_hwnds:
        raise ValueError("hwnd is not a visible taskbar window")
    user32 = ctypes.windll.user32
    wm_close = 0x0010
    return bool(user32.PostMessageW(hwnd, wm_close, 0, 0))


def known_folder_path(guid_text: str) -> Path:
    if not is_windows():
        raise OSError("known folders are only available on Windows")

    class GUID(ctypes.Structure):
        _fields_ = [
            ("Data1", ctypes.c_ulong),
            ("Data2", ctypes.c_ushort),
            ("Data3", ctypes.c_ushort),
            ("Data4", ctypes.c_ubyte * 8),
        ]

    value = uuid.UUID(guid_text)
    guid = GUID(
        value.time_low,
        value.time_mid,
        value.time_hi_version,
        (ctypes.c_ubyte * 8).from_buffer_copy(value.bytes[8:]),
    )
    out = ctypes.c_wchar_p()
    hr = ctypes.windll.shell32.SHGetKnownFolderPath(
        ctypes.byref(guid),
        0,
        None,
        ctypes.byref(out),
    )
    if hr != 0:
        raise OSError(f"SHGetKnownFolderPath failed: {hr}")
    try:
        return Path(out.value).resolve()
    finally:
        ctypes.windll.ole32.CoTaskMemFree(out)


def user_desktop_root() -> Path:
    try:
        root = known_folder_path("B4BFCC3A-DB2C-424C-B029-7FE99A87C641")
    except Exception:
        root = Path.home() / "Desktop"
    if not root.exists():
        root.mkdir(parents=True, exist_ok=True)
    return root.resolve()


def public_desktop_root() -> Optional[Path]:
    candidates = []
    try:
        candidates.append(known_folder_path("C4AA340D-F20F-4863-AFEF-F87EF2E6BA25C"))
    except Exception:
        pass
    public_home = os.environ.get("PUBLIC", "")
    if public_home:
        candidates.append(Path(public_home) / "Desktop")
    for path in candidates:
        try:
            resolved = path.resolve()
            if resolved.exists() and resolved.is_dir():
                return resolved
        except OSError:
            continue
    return None


def desktop_roots() -> list:
    roots = [("user", user_desktop_root())]
    public_root = public_desktop_root()
    if public_root and public_root != roots[0][1]:
        roots.append(("public", public_root))
    return roots


def desktop_root() -> Path:
    return user_desktop_root()


def is_visible_desktop_entry(entry: Path) -> bool:
    if entry.name.lower() == "desktop.ini":
        return False
    try:
        attrs = getattr(entry.stat(), "st_file_attributes", 0)
        hidden = bool(attrs & 0x2)
        system = bool(attrs & 0x4)
        return not hidden and not system
    except OSError:
        return False


def split_desktop_path(path_text: str) -> Tuple[str, List[str]]:
    text = str(path_text or "").replace("\\", "/").strip()
    if text == DESKTOP_PUBLIC:
        return "public", []
    if text.startswith(DESKTOP_PUBLIC + "/"):
        rest = text[len(DESKTOP_PUBLIC) + 1 :]
        parts = [part for part in rest.split("/") if part and part != "."]
        if any(part == ".." for part in parts):
            raise ValueError("parent path traversal is not allowed")
        return "public", parts
    if re.match(r"^[A-Za-z]:", text):
        raise ValueError("absolute paths are not allowed")
    while text.startswith("/"):
        text = text[1:]
    parts = [part for part in text.split("/") if part and part != "."]
    if any(part == ".." for part in parts):
        raise ValueError("parent path traversal is not allowed")
    return "user", parts


def desktop_root_for_source(source: str) -> Path:
    if source == "public":
        root = public_desktop_root()
        if not root:
            raise ValueError("public desktop is not available")
        return root
    return user_desktop_root()


def resolve_desktop_path(path_text: str = "") -> Path:
    source, parts = split_desktop_path(path_text)
    root = desktop_root_for_source(source)
    target = (root / Path(*parts)).resolve() if parts else root
    try:
        target.relative_to(root)
    except ValueError:
        raise ValueError("path is outside the Desktop root")
    return target


def path_to_desktop_relative(path: Path) -> str:
    resolved = path.resolve()
    for source, root in desktop_roots():
        try:
            rel = resolved.relative_to(root)
        except ValueError:
            continue
        text = rel.as_posix()
        if source == "public":
            return DESKTOP_PUBLIC if text == "." else f"{DESKTOP_PUBLIC}/{text}"
        return "" if text == "." else text
    return ""


def is_shell_open_path(path_text: str) -> bool:
    return str(path_text or "").strip().startswith(SHELL_OPEN_PREFIX)


def shell_open_target(path_text: str) -> str:
    target = str(path_text or "").strip()[len(SHELL_OPEN_PREFIX) :]
    if not target:
        raise ValueError("shell target is required")
    return urllib.parse.unquote(target)


def shell_execute_open(file_path: str, parameters: str = "", directory: str = "") -> bool:
    if not is_windows():
        raise ValueError("opening files is only supported on Windows")
    shell32 = ctypes.windll.shell32
    result = shell32.ShellExecuteW(
        None,
        "open",
        str(file_path),
        str(parameters or None) if parameters else None,
        str(directory or None) if directory else None,
        1,
    )
    code = int(result)
    if code <= 32:
        raise RuntimeError(f"ShellExecuteW failed with code {code}")
    return True


def shell_invoke_folder_item(path: Path) -> bool:
    if not is_windows():
        raise ValueError("opening shell items is only supported on Windows")
    script = r"""
$ErrorActionPreference = 'Stop'
$OutputEncoding = [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$shell = New-Object -ComObject Shell.Application
$folder = $shell.Namespace($env:CCG_SHELL_FOLDER)
if (-not $folder) { throw "Shell folder not found: $($env:CCG_SHELL_FOLDER)" }
$item = $folder.ParseName($env:CCG_SHELL_NAME)
if (-not $item) { throw "Shell item not found: $($env:CCG_SHELL_NAME)" }
$item.InvokeVerb()
"""
    env = os.environ.copy()
    env["CCG_SHELL_FOLDER"] = str(path.parent)
    env["CCG_SHELL_NAME"] = path.name
    proc = subprocess.run(
        ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden", "-Command", script],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=10,
        check=False,
        env=env,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise RuntimeError(f"Shell item invoke failed: {detail or proc.returncode}")
    return True


def _process_image_path(pid: int) -> str:
    if not is_windows() or not pid:
        return ""
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(0x1000, False, int(pid))
    if not handle:
        return ""
    try:
        buffer = ctypes.create_unicode_buffer(32768)
        size = wintypes.DWORD(len(buffer))
        if kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return buffer.value
    finally:
        kernel32.CloseHandle(handle)
    return ""


def _same_windows_path(left: str, right: str) -> bool:
    if not left or not right:
        return False
    return os.path.normcase(os.path.abspath(left)) == os.path.normcase(os.path.abspath(right))


def _window_text(hwnd) -> str:
    user32 = ctypes.windll.user32
    length = user32.GetWindowTextLengthW(hwnd)
    if length <= 0:
        return ""
    buffer = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buffer, length + 1)
    return buffer.value or ""


def bring_window_to_front(hwnd) -> bool:
    if not is_windows() or not hwnd:
        return False
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
    user32.AttachThreadInput.restype = wintypes.BOOL
    user32.SetActiveWindow.argtypes = [wintypes.HWND]
    user32.SetActiveWindow.restype = wintypes.HWND
    user32.SetFocus.argtypes = [wintypes.HWND]
    user32.SetFocus.restype = wintypes.HWND
    kernel32.GetCurrentThreadId.restype = wintypes.DWORD

    hwnd = wintypes.HWND(int(hwnd))
    current_thread = kernel32.GetCurrentThreadId()
    foreground = user32.GetForegroundWindow()
    foreground_pid = wintypes.DWORD()
    foreground_thread = (
        user32.GetWindowThreadProcessId(foreground, ctypes.byref(foreground_pid))
        if foreground
        else 0
    )
    target_pid = wintypes.DWORD()
    target_thread = user32.GetWindowThreadProcessId(hwnd, ctypes.byref(target_pid))
    attached = []
    try:
        if hasattr(user32, "AllowSetForegroundWindow"):
            user32.AllowSetForegroundWindow(-1)
        for thread_id in {int(foreground_thread or 0), int(target_thread or 0)}:
            if thread_id and thread_id != int(current_thread):
                if user32.AttachThreadInput(current_thread, thread_id, True):
                    attached.append(thread_id)
        user32.ShowWindow(hwnd, 5)
        user32.ShowWindow(hwnd, 9)
        user32.BringWindowToTop(hwnd)
        user32.SetWindowPos(hwnd, -1, 0, 0, 0, 0, 0x0001 | 0x0002)
        user32.SetWindowPos(hwnd, -2, 0, 0, 0, 0, 0x0001 | 0x0002)
        if hasattr(user32, "SwitchToThisWindow"):
            user32.SwitchToThisWindow(hwnd, True)
        user32.SetActiveWindow(hwnd)
        user32.SetFocus(hwnd)
        user32.SetForegroundWindow(hwnd)
    finally:
        for thread_id in attached:
            user32.AttachThreadInput(current_thread, thread_id, False)
    time.sleep(0.15)
    return bool(user32.IsWindowVisible(hwnd))


def activate_windows_for_executable(exe_path: str, timeout: float = 6.0) -> bool:
    if not is_windows() or not exe_path:
        return False
    target = str(exe_path)
    if not Path(target).exists():
        return False
    user32 = ctypes.windll.user32
    try:
        dwmapi = ctypes.windll.dwmapi
    except Exception:
        dwmapi = None
    enum_proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    ignored_titles = {"default ime", "msctfime ui", "undefined"}
    gwl_exstyle = -20
    ws_ex_toolwindow = 0x00000080
    dwmwa_cloaked = 14

    class Rect(ctypes.Structure):
        _fields_ = [
            ("left", ctypes.c_long),
            ("top", ctypes.c_long),
            ("right", ctypes.c_long),
            ("bottom", ctypes.c_long),
        ]

    def is_cloaked(hwnd) -> bool:
        if not dwmapi:
            return False
        value = ctypes.c_int(0)
        result = dwmapi.DwmGetWindowAttribute(
            hwnd, dwmwa_cloaked, ctypes.byref(value), ctypes.sizeof(value)
        )
        return result == 0 and bool(value.value)

    def window_area(hwnd) -> int:
        rect = Rect()
        if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return 0
        return max(0, int(rect.right - rect.left)) * max(0, int(rect.bottom - rect.top))

    def show_window(hwnd) -> bool:
        return bring_window_to_front(hwnd)

    deadline = time.time() + max(0.1, timeout)

    while time.time() < deadline:
        candidates = []

        def callback(hwnd, _lparam):
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if not _same_windows_path(_process_image_path(int(pid.value)), target):
                return True
            title = _window_text(hwnd).strip()
            if not title or title.lower() in ignored_titles:
                return True
            if is_cloaked(hwnd):
                return True
            exstyle = int(user32.GetWindowLongW(hwnd, gwl_exstyle))
            if exstyle & ws_ex_toolwindow:
                return True
            area = window_area(hwnd)
            if area < 20000:
                return True
            candidates.append((area, title, hwnd))
            return True

        user32.EnumWindows(enum_proc(callback), 0)
        if candidates:
            candidates.sort(reverse=True)
            if show_window(candidates[0][2]):
                return True
        time.sleep(0.25)
    return False


_OPEN_ACTIVATION_LOCK = threading.Lock()
_OPEN_ACTIVATION_TOKEN = 0


def next_open_activation_token() -> int:
    global _OPEN_ACTIVATION_TOKEN
    with _OPEN_ACTIVATION_LOCK:
        _OPEN_ACTIVATION_TOKEN += 1
        return _OPEN_ACTIVATION_TOKEN


def is_current_open_activation(token: int) -> bool:
    with _OPEN_ACTIVATION_LOCK:
        return token == _OPEN_ACTIVATION_TOKEN


def cancel_pending_open_activation() -> None:
    next_open_activation_token()


def schedule_activation_for_shortcut(exe_path: str) -> None:
    if not is_windows() or not exe_path:
        return
    token = next_open_activation_token()

    def worker() -> None:
        try:
            deadline = time.time() + 5.0
            while time.time() < deadline and is_current_open_activation(token):
                if activate_windows_for_executable(exe_path, 0.5):
                    return
                time.sleep(0.2)
        except Exception:
            pass

    thread = threading.Thread(target=worker, name="ccg-open-activate", daemon=True)
    thread.start()


def taskbar_window_snapshot() -> dict:
    snapshot = {}
    try:
        for row in list_taskbar_apps(limit=200):
            hwnd = str(row.get("hwnd") or "").lower()
            if hwnd:
                snapshot[hwnd] = str(row.get("title") or "")
    except Exception:
        pass
    return snapshot


def file_title_needles(file_path: Path) -> list:
    names = []
    stem = str(file_path.stem or "").strip()
    name = str(file_path.name or "").strip()
    if stem:
        names.append(stem.lower())
    if name and name.lower() not in names:
        names.append(name.lower())
    return [name for name in names if name]


def title_mentions_file(title: str, file_path: Path) -> bool:
    lower = str(title or "").lower()
    return any(needle in lower for needle in file_title_needles(file_path))


def activate_window_for_opened_file(file_path: Path, before: dict, timeout: float = 7.0) -> bool:
    deadline = time.time() + max(0.2, timeout)
    while time.time() < deadline:
        candidates = []
        try:
            rows = list_taskbar_apps(limit=200)
        except Exception:
            rows = []
        for row in rows:
            hwnd_text = str(row.get("hwnd") or "")
            hwnd_key = hwnd_text.lower()
            if not hwnd_text:
                continue
            title = str(row.get("title") or "")
            old_title = before.get(hwnd_key)
            is_new = hwnd_key not in before
            changed = old_title is not None and title != old_title
            mentions_file = title_mentions_file(title, file_path)
            if not (is_new or changed or mentions_file):
                continue
            score = 0
            if mentions_file:
                score += 100
            if is_new:
                score += 50
            if changed:
                score += 25
            candidates.append((score, hwnd_text, title))
        if candidates:
            candidates.sort(reverse=True)
            try:
                hwnd = int(candidates[0][1], 16)
            except ValueError:
                hwnd = 0
            if hwnd and bring_window_to_front(hwnd):
                return True
        time.sleep(0.25)
    return False


def schedule_activation_for_opened_file(file_path: Path, before: dict) -> None:
    token = next_open_activation_token()

    def worker() -> None:
        try:
            deadline = time.time() + 7.0
            while time.time() < deadline and is_current_open_activation(token):
                if activate_window_for_opened_file(file_path, before, 0.5):
                    return
                time.sleep(0.2)
        except Exception:
            pass

    thread = threading.Thread(target=worker, name="ccg-open-file-activate", daemon=True)
    thread.start()


def shortcut_info(path: Path) -> dict:
    if path.suffix.lower() != ".lnk":
        return {}
    script = r"""
$ErrorActionPreference = 'Stop'
$OutputEncoding = [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($env:CCG_SHORTCUT_PATH)
[pscustomobject]@{
  target = [string]$shortcut.TargetPath
  arguments = [string]$shortcut.Arguments
  working_directory = [string]$shortcut.WorkingDirectory
} | ConvertTo-Json -Compress
"""
    env = os.environ.copy()
    env["CCG_SHORTCUT_PATH"] = str(path)
    try:
        proc = subprocess.run(
            ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden", "-Command", script],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=8,
            check=False,
            env=env,
        )
        if proc.returncode != 0 or not proc.stdout.strip():
            return {}
        data = json.loads(proc.stdout)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def is_this_pc_path(path_text: str) -> bool:
    text = str(path_text or "").replace("\\", "/").strip()
    return text == SHELL_THIS_PC or text.startswith(SHELL_THIS_PC + "/")


def split_this_pc_path(path_text: str) -> list:
    text = str(path_text or "").replace("\\", "/").strip()
    if text == SHELL_THIS_PC:
        return []
    if not text.startswith(SHELL_THIS_PC + "/"):
        raise ValueError("invalid This PC path")
    rest = text[len(SHELL_THIS_PC) + 1 :]
    parts = [part for part in rest.split("/") if part and part != "."]
    if any(part == ".." for part in parts):
        raise ValueError("parent path traversal is not allowed")
    return parts


def drive_label(letter: str) -> str:
    if not is_windows():
        return f"{letter}:"
    try:
        from ctypes import wintypes

        root = f"{letter}:\\"
        volume_name = ctypes.create_unicode_buffer(261)
        fs_name = ctypes.create_unicode_buffer(261)
        serial = wintypes.DWORD()
        max_component = wintypes.DWORD()
        flags = wintypes.DWORD()
        ok = ctypes.windll.kernel32.GetVolumeInformationW(
            root,
            volume_name,
            len(volume_name),
            ctypes.byref(serial),
            ctypes.byref(max_component),
            ctypes.byref(flags),
            fs_name,
            len(fs_name),
        )
        if ok and volume_name.value:
            return f"{volume_name.value} ({letter}:)"
    except Exception:
        pass
    return f"本地磁盘 ({letter}:)"


def list_this_pc_drives() -> list:
    entries = []
    if is_windows():
        mask = ctypes.windll.kernel32.GetLogicalDrives()
        letters = [chr(ord("A") + index) for index in range(26) if mask & (1 << index)]
    else:
        letters = []
    for letter in letters:
        entries.append(
            {
                "name": drive_label(letter),
                "path": f"{SHELL_THIS_PC}/{letter}",
                "type": "drive",
                "is_dir": True,
                "openable": False,
                "size": 0,
                "modified": 0,
                "suffix": "",
            }
        )
    return entries


def resolve_this_pc_path(path_text: str) -> Path:
    parts = split_this_pc_path(path_text)
    if not parts:
        raise ValueError("This PC root is virtual")
    drive = str(parts[0]).upper()
    if not re.fullmatch(r"[A-Z]", drive):
        raise ValueError("invalid drive")
    drive_root = Path(f"{drive}:\\")
    if not drive_root.exists():
        raise ValueError("drive does not exist")
    target = (drive_root / Path(*parts[1:])).resolve()
    try:
        target.relative_to(drive_root.resolve())
    except ValueError:
        raise ValueError("path is outside the drive root")
    return target


def path_to_this_pc_relative(path: Path) -> str:
    resolved = path.resolve()
    drive = (resolved.drive or "").rstrip(":").upper()
    if not re.fullmatch(r"[A-Z]", drive):
        return SHELL_THIS_PC
    root = Path(f"{drive}:\\").resolve()
    try:
        rel = resolved.relative_to(root)
    except ValueError:
        return SHELL_THIS_PC
    parts = [SHELL_THIS_PC, drive]
    if str(rel) != ".":
        parts.extend(rel.parts)
    return "/".join(str(part).replace("\\", "/") for part in parts)


def entry_from_shell_item(item: dict) -> Optional[dict]:
    name = str(item.get("Name") or "").strip()
    raw_path = str(item.get("Path") or "").strip()
    if not name:
        return None
    lower_name = name.lower()
    lower_path = raw_path.lower()
    if lower_name == "desktop.ini" or lower_path.endswith("\\desktop.ini"):
        return None

    if "{20d04fe0-3aea-1069-a2d8-08002b30309d}" in lower_path or name == "此电脑":
        return {
            "name": name,
            "path": SHELL_THIS_PC,
            "type": "this_pc",
            "is_dir": True,
            "openable": False,
            "size": 0,
            "modified": 0,
            "suffix": "",
            "source": "shell",
        }

    if raw_path and not raw_path.startswith("::"):
        try:
            target = Path(raw_path).resolve()
            if target.exists():
                stat = target.stat()
                is_link = target.is_symlink()
                is_dir = target.is_dir() and not is_link
                rel = path_to_desktop_relative(target)
                if not rel:
                    rel = path_to_this_pc_relative(target)
                return {
                    "name": name,
                    "path": rel,
                    "type": "directory" if is_dir else "file",
                    "is_dir": is_dir,
                    "openable": not is_dir,
                    "size": 0 if is_dir else int(stat.st_size),
                    "modified": int(stat.st_mtime),
                    "suffix": target.suffix.lower(),
                    "source": "shell",
                    "shell_type": str(item.get("Type") or ""),
                }
        except OSError:
            pass

    if raw_path:
        return {
            "name": name,
            "path": SHELL_OPEN_PREFIX + urllib.parse.quote(raw_path, safe="{}:-_./\\"),
            "type": "shell",
            "is_dir": False,
            "openable": True,
            "size": 0,
            "modified": 0,
            "suffix": "",
            "source": "shell",
            "shell_type": str(item.get("Type") or ""),
        }
    return None


def shell_desktop_entries() -> list:
    if not is_windows():
        return []
    script = r"""
$ErrorActionPreference = 'SilentlyContinue'
$OutputEncoding = [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$shell = New-Object -ComObject Shell.Application
$desktop = $shell.Namespace(0)
$items = @()
foreach ($item in $desktop.Items()) {
  $items += [pscustomobject]@{
    Name = [string]$item.Name
    Path = [string]$item.Path
    IsFolder = [bool]$item.IsFolder
    Type = [string]$item.Type
  }
}
$items | ConvertTo-Json -Depth 4 -Compress
"""
    try:
        proc = subprocess.run(
            ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden", "-Command", script],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=8,
            check=False,
        )
        if proc.returncode != 0 or not proc.stdout.strip():
            return []
        data = json.loads(proc.stdout)
        if isinstance(data, dict):
            data = [data]
        entries = []
        seen = set()
        for item in data:
            entry = entry_from_shell_item(item)
            if not entry:
                continue
            key = (entry.get("name"), entry.get("path"))
            if key in seen:
                continue
            seen.add(key)
            entries.append(entry)
        return entries
    except Exception:
        return []


def shell_folder_entries(folder: Path) -> list:
    if not is_windows():
        return []
    script = r"""
$ErrorActionPreference = 'SilentlyContinue'
$OutputEncoding = [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$shell = New-Object -ComObject Shell.Application
$folder = $shell.Namespace($env:CCG_SHELL_FOLDER)
if (-not $folder) { @() | ConvertTo-Json -Compress; exit 0 }
$items = @()
foreach ($item in $folder.Items()) {
  $items += [pscustomobject]@{
    Name = [string]$item.Name
    Path = [string]$item.Path
    IsFolder = [bool]$item.IsFolder
    Type = [string]$item.Type
  }
}
$items | ConvertTo-Json -Depth 4 -Compress
"""
    env = os.environ.copy()
    env["CCG_SHELL_FOLDER"] = str(folder)
    try:
        proc = subprocess.run(
            ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden", "-Command", script],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=8,
            check=False,
            env=env,
        )
        if proc.returncode != 0 or not proc.stdout.strip():
            return []
        data = json.loads(proc.stdout)
        if isinstance(data, dict):
            data = [data]
        entries = []
        seen = set()
        for item in data:
            entry = entry_from_shell_item(item)
            if not entry:
                continue
            key = (entry.get("name"), entry.get("path"))
            if key in seen:
                continue
            seen.add(key)
            entries.append(entry)
        return entries
    except Exception:
        return []


def desktop_listview_hwnd() -> Optional[int]:
    if not is_windows():
        return None
    user32 = ctypes.windll.user32
    enum_proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    progman = user32.FindWindowW("Progman", None)
    defview = user32.FindWindowExW(progman, None, "SHELLDLL_DefView", None) if progman else None
    if not defview:
        found = []

        def enum_windows(hwnd, _lparam):
            child = user32.FindWindowExW(hwnd, None, "SHELLDLL_DefView", None)
            if child:
                found.append(child)
                return False
            return True

        user32.EnumWindows(enum_proc(enum_windows), 0)
        defview = found[0] if found else None
    if not defview:
        return None
    listview = user32.FindWindowExW(defview, None, "SysListView32", None)
    return int(listview) if listview else None


def desktop_visible_icon_names() -> list:
    listview = desktop_listview_hwnd()
    if not listview:
        return []

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    lvm_first = 0x1000
    lvm_getitemcount = lvm_first + 4
    lvm_getitemtextw = lvm_first + 115
    lvif_text = 0x0001
    process_vm_operation = 0x0008
    process_vm_read = 0x0010
    process_vm_write = 0x0020
    process_query_information = 0x0400
    mem_commit = 0x1000
    mem_reserve = 0x2000
    mem_release = 0x8000
    page_readwrite = 0x04

    class LVITEMW(ctypes.Structure):
        _fields_ = [
            ("mask", wintypes.UINT),
            ("iItem", ctypes.c_int),
            ("iSubItem", ctypes.c_int),
            ("state", wintypes.UINT),
            ("stateMask", wintypes.UINT),
            ("pszText", ctypes.c_void_p),
            ("cchTextMax", ctypes.c_int),
            ("iImage", ctypes.c_int),
            ("lParam", wintypes.LPARAM),
            ("iIndent", ctypes.c_int),
            ("iGroupId", ctypes.c_int),
            ("cColumns", wintypes.UINT),
            ("puColumns", ctypes.c_void_p),
            ("piColFmt", ctypes.c_void_p),
            ("iGroup", ctypes.c_int),
        ]

    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(wintypes.HWND(listview), ctypes.byref(pid))
    count = int(user32.SendMessageW(wintypes.HWND(listview), lvm_getitemcount, 0, 0))
    if pid.value <= 0 or count <= 0:
        return []

    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.VirtualAllocEx.restype = ctypes.c_void_p
    handle = kernel32.OpenProcess(
        process_vm_operation | process_vm_read | process_vm_write | process_query_information,
        False,
        pid.value,
    )
    if not handle:
        return []

    remote_text = None
    remote_item = None
    try:
        text_bytes = 520
        remote_text = kernel32.VirtualAllocEx(
            handle, None, text_bytes, mem_commit | mem_reserve, page_readwrite
        )
        remote_item = kernel32.VirtualAllocEx(
            handle, None, ctypes.sizeof(LVITEMW), mem_commit | mem_reserve, page_readwrite
        )
        if not remote_text or not remote_item:
            return []

        names = []
        for index in range(count):
            item = LVITEMW()
            item.mask = lvif_text
            item.iItem = index
            item.iSubItem = 0
            item.pszText = remote_text
            item.cchTextMax = 260
            written = ctypes.c_size_t()
            kernel32.WriteProcessMemory(
                handle, remote_item, ctypes.byref(item), ctypes.sizeof(item), ctypes.byref(written)
            )
            user32.SendMessageW(wintypes.HWND(listview), lvm_getitemtextw, index, remote_item)
            buffer = ctypes.create_unicode_buffer(260)
            read = ctypes.c_size_t()
            kernel32.ReadProcessMemory(handle, remote_text, buffer, text_bytes, ctypes.byref(read))
            name = buffer.value.strip()
            if name:
                names.append(name)
        return names
    except Exception:
        return []
    finally:
        if remote_text:
            kernel32.VirtualFreeEx(handle, remote_text, 0, mem_release)
        if remote_item:
            kernel32.VirtualFreeEx(handle, remote_item, 0, mem_release)
        kernel32.CloseHandle(handle)


def activate_desktop_icon(icon_names: list) -> bool:
    listview = desktop_listview_hwnd()
    if not listview:
        return False
    names = desktop_visible_icon_names()
    if not names:
        return False

    wanted = {str(name).strip().lower() for name in icon_names if str(name).strip()}
    index = -1
    for idx, name in enumerate(names):
        if str(name).strip().lower() in wanted:
            index = idx
            break
    if index < 0:
        return False

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    lvm_first = 0x1000
    lvm_setitemstate = lvm_first + 43
    lvm_ensurevisible = lvm_first + 19
    lvm_getitemposition = lvm_first + 16
    lvif_state = 0x0008
    lvis_focused = 0x0001
    lvis_selected = 0x0002
    process_vm_operation = 0x0008
    process_vm_read = 0x0010
    process_vm_write = 0x0020
    process_query_information = 0x0400
    mem_commit = 0x1000
    mem_reserve = 0x2000
    mem_release = 0x8000
    page_readwrite = 0x04
    wm_keydown = 0x0100
    wm_keyup = 0x0101
    vk_return = 0x0D
    wm_lbuttondown = 0x0201
    wm_lbuttonup = 0x0202
    wm_lbuttondblclk = 0x0203
    mk_lbutton = 0x0001

    class LVITEMW(ctypes.Structure):
        _fields_ = [
            ("mask", wintypes.UINT),
            ("iItem", ctypes.c_int),
            ("iSubItem", ctypes.c_int),
            ("state", wintypes.UINT),
            ("stateMask", wintypes.UINT),
            ("pszText", ctypes.c_void_p),
            ("cchTextMax", ctypes.c_int),
            ("iImage", ctypes.c_int),
            ("lParam", wintypes.LPARAM),
            ("iIndent", ctypes.c_int),
            ("iGroupId", ctypes.c_int),
            ("cColumns", wintypes.UINT),
            ("puColumns", ctypes.c_void_p),
            ("piColFmt", ctypes.c_void_p),
            ("iGroup", ctypes.c_int),
        ]

    class Point(ctypes.Structure):
        _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(wintypes.HWND(listview), ctypes.byref(pid))
    if pid.value <= 0:
        return False
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.VirtualAllocEx.restype = ctypes.c_void_p
    handle = kernel32.OpenProcess(
        process_vm_operation | process_vm_read | process_vm_write | process_query_information,
        False,
        pid.value,
    )
    if not handle:
        return False

    remote_item = None
    remote_point = None
    try:
        remote_item = kernel32.VirtualAllocEx(
            handle, None, ctypes.sizeof(LVITEMW), mem_commit | mem_reserve, page_readwrite
        )
        remote_point = kernel32.VirtualAllocEx(
            handle, None, ctypes.sizeof(Point), mem_commit | mem_reserve, page_readwrite
        )
        if not remote_item or not remote_point:
            return False

        item = LVITEMW()
        item.mask = lvif_state
        item.iItem = index
        item.iSubItem = 0
        item.state = lvis_selected | lvis_focused
        item.stateMask = lvis_selected | lvis_focused
        written = ctypes.c_size_t()
        kernel32.WriteProcessMemory(
            handle, remote_item, ctypes.byref(item), ctypes.sizeof(item), ctypes.byref(written)
        )
        user32.SendMessageW(wintypes.HWND(listview), lvm_setitemstate, index, remote_item)
        user32.SendMessageW(wintypes.HWND(listview), lvm_ensurevisible, index, 0)

        user32.SendMessageW(wintypes.HWND(listview), lvm_getitemposition, index, remote_point)
        point = Point()
        read = ctypes.c_size_t()
        kernel32.ReadProcessMemory(handle, remote_point, ctypes.byref(point), ctypes.sizeof(point), ctypes.byref(read))
        x = max(1, int(point.x) + 24)
        y = max(1, int(point.y) + 24)
        lparam = (y << 16) | (x & 0xFFFF)

        hwnd = wintypes.HWND(listview)
        user32.PostMessageW(hwnd, wm_lbuttondown, mk_lbutton, lparam)
        user32.PostMessageW(hwnd, wm_lbuttonup, 0, lparam)
        user32.PostMessageW(hwnd, wm_lbuttondblclk, mk_lbutton, lparam)
        user32.PostMessageW(hwnd, wm_lbuttonup, 0, lparam)
        user32.PostMessageW(hwnd, wm_keydown, vk_return, 0)
        user32.PostMessageW(hwnd, wm_keyup, vk_return, 0)
        return True
    except Exception:
        return False
    finally:
        if remote_item:
            kernel32.VirtualFreeEx(handle, remote_item, 0, mem_release)
        if remote_point:
            kernel32.VirtualFreeEx(handle, remote_point, 0, mem_release)
        kernel32.CloseHandle(handle)


def desktop_icon_names_for_path(target: Path) -> list:
    names = [target.name]
    if target.suffix.lower() in {".lnk", ".url"}:
        names.insert(0, target.stem)
    return names


def is_desktop_root_item_path(path_text: str) -> bool:
    try:
        _source, parts = split_desktop_path(path_text)
    except Exception:
        return False
    return len(parts) == 1


def desktop_entries_from_visible_icons() -> list:
    names = desktop_visible_icon_names()
    if not names:
        return []
    shell_entries = shell_desktop_entries()
    by_name = {}
    for entry in shell_entries:
        by_name.setdefault(str(entry.get("name") or ""), []).append(entry)

    entries = []
    for name in names:
        candidates = by_name.get(name) or []
        if candidates:
            entries.append(candidates.pop(0))
            continue
        entries.append(
            {
                "name": name,
                "path": SHELL_OPEN_PREFIX + urllib.parse.quote(name),
                "type": "shell",
                "is_dir": False,
                "openable": True,
                "size": 0,
                "modified": 0,
                "suffix": "",
                "source": "desktop-listview",
            }
        )
    return entries


def list_this_pc_items(path_text: str) -> dict:
    parts = split_this_pc_path(path_text)
    if not parts:
        return {
            "root": "This PC",
            "path": SHELL_THIS_PC,
            "display_path": "此电脑",
            "parent": "",
            "entries": list_this_pc_drives(),
        }

    folder = resolve_this_pc_path(path_text)
    if not folder.exists():
        raise ValueError("path does not exist")
    if not folder.is_dir():
        raise ValueError("path is not a directory")

    entries = shell_folder_entries(folder)
    used_shell_entries = bool(entries)
    if not entries:
        for entry in folder.iterdir():
            if not is_visible_desktop_entry(entry):
                continue
            try:
                stat = entry.stat()
                is_link = entry.is_symlink()
                is_dir = entry.is_dir() and not is_link
                entries.append(
                    {
                        "name": entry.name,
                        "path": path_to_this_pc_relative(entry),
                        "type": "directory" if is_dir else "file",
                        "is_dir": is_dir,
                        "openable": not is_dir,
                        "size": 0 if is_dir else int(stat.st_size),
                        "modified": int(stat.st_mtime),
                        "suffix": entry.suffix.lower(),
                    }
                )
            except OSError:
                continue

    if not used_shell_entries:
        entries.sort(key=lambda item: (0 if item["is_dir"] else 1, str(item["name"]).lower()))
    rel_path = path_to_this_pc_relative(folder)
    parent = SHELL_THIS_PC
    if folder.parent != folder:
        parent = path_to_this_pc_relative(folder.parent)
    return {
        "root": "This PC",
        "path": rel_path,
        "display_path": "此电脑\\" + rel_path[len(SHELL_THIS_PC) + 1 :].replace("/", "\\"),
        "parent": parent,
        "entries": entries,
    }


def list_desktop_items(path_text: str = "") -> dict:
    if is_this_pc_path(path_text):
        return list_this_pc_items(path_text)

    folder = resolve_desktop_path(path_text)
    if not folder.exists():
        raise ValueError("path does not exist")
    if not folder.is_dir():
        raise ValueError("path is not a directory")

    entries = []
    rel_path = path_to_desktop_relative(folder)
    source, _parts = split_desktop_path(path_text)
    if not rel_path:
        visible_entries = desktop_entries_from_visible_icons()
        if visible_entries:
            return {
                "root": str(desktop_root()),
                "roots": [
                    {"source": root_source, "path": str(root_path)}
                    for root_source, root_path in desktop_roots()
                ],
                "path": "",
                "display_path": "桌面",
                "parent": "",
                "entries": visible_entries,
            }

    if rel_path:
        entries = shell_folder_entries(folder)
        used_shell_entries = bool(entries)
        scan_roots = [] if entries else [(source, folder)]
    else:
        used_shell_entries = False
        scan_roots = desktop_roots()

    for scan_source, scan_root in scan_roots:
        for entry in scan_root.iterdir():
            if not is_visible_desktop_entry(entry):
                continue
            try:
                stat = entry.stat()
                is_link = entry.is_symlink()
                is_dir = entry.is_dir() and not is_link
                rel = path_to_desktop_relative(entry)
                entries.append(
                    {
                        "name": entry.name,
                        "path": rel,
                        "type": "directory" if is_dir else "file",
                        "is_dir": is_dir,
                        "openable": not is_dir,
                        "size": 0 if is_dir else int(stat.st_size),
                        "modified": int(stat.st_mtime),
                        "suffix": entry.suffix.lower(),
                        "source": scan_source,
                    }
                )
            except OSError:
                continue

    if not used_shell_entries:
        entries.sort(key=lambda item: (0 if item["is_dir"] else 1, str(item["name"]).lower()))
    if not rel_path:
        entries.insert(
            0,
            {
                "name": "此电脑",
                "path": SHELL_THIS_PC,
                "type": "this_pc",
                "is_dir": True,
                "openable": False,
                "size": 0,
                "modified": 0,
                "suffix": "",
                "source": "shell",
            },
        )
    parent = ""
    source_root = desktop_root_for_source(source)
    if folder != source_root:
        parent = path_to_desktop_relative(folder.parent)
    return {
        "root": str(desktop_root()),
        "roots": [
            {"source": root_source, "path": str(root_path)}
            for root_source, root_path in desktop_roots()
        ],
        "path": rel_path,
        "display_path": "桌面" + (("\\" + rel_path.replace("/", "\\")) if rel_path else ""),
        "parent": parent,
        "entries": entries,
    }


def open_desktop_item(path_text: str) -> bool:
    cancel_pending_open_activation()
    if is_shell_open_path(path_text):
        if not is_windows():
            raise ValueError("opening shell items is only supported on Windows")
        target = shell_open_target(path_text)
        return shell_execute_open(target)
    if is_this_pc_path(path_text):
        target = resolve_this_pc_path(path_text)
    else:
        target = resolve_desktop_path(path_text)
    if not target.exists():
        raise ValueError("path does not exist")
    if target.is_dir():
        raise ValueError("directories are opened by navigating in the browser")
    if not is_windows():
        raise ValueError("opening files is only supported on Windows")
    if target.suffix.lower() == ".lnk":
        info = shortcut_info(target)
        shortcut_target = str(info.get("target") or "")
        if shortcut_target and Path(shortcut_target).exists():
            try:
                if activate_windows_for_executable(shortcut_target, 0.4):
                    return True
            except Exception:
                pass
            try:
                shell_execute_open("explorer.exe", str(target), "")
                schedule_activation_for_shortcut(shortcut_target)
                return True
            except Exception:
                pass
            raise RuntimeError("failed to hand shortcut to Windows Explorer")
    before = taskbar_window_snapshot()
    ok = shell_execute_open(str(target), "", str(target.parent))
    if ok:
        schedule_activation_for_opened_file(target, before)
    return ok


# ----------------------------------------------------------------
#  HTML
# ----------------------------------------------------------------

PAGE = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Kill 鎺у埗鍙?:9091</title>
<style>
*{box-sizing:border-box}body{margin:0;padding:16px;background:#0f0f0f;color:#e0e0e0;font:14px/1.5 system-ui,sans-serif}
h1{font-size:18px;margin:0 0 12px;color:#ff6b6b}.panel{background:#1a1a1a;border:1px solid #333;border-radius:6px;padding:14px;margin-bottom:14px}
.row{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
input,button{font:inherit;padding:8px 12px;border-radius:4px;border:1px solid #444;background:#222;color:#e0e0e0}
button{cursor:pointer;font-weight:650;min-width:80px}button.kill{background:#b42318;border-color:#b42318;color:#fff}button.ok{background:#175cd3;border-color:#175cd3;color:#fff}
button:disabled{opacity:.4;cursor:not-allowed}.proc{display:flex;gap:10px;align-items:center;padding:6px 0;border-bottom:1px solid #222}
.proc-info{flex:1;min-width:0}.proc-name{font-weight:650;color:#fff}.proc-meta{font-size:12px;color:#888;word-break:break-all}
#state{display:inline-block;padding:2px 10px;border-radius:10px;font-size:12px;background:#333;color:#aaa}
#state.ok{background:#067647;color:#fff}.loading{opacity:.5}
</style>
</head>
<body>
<h1>Kill 鎺у埗鍙?:9091</h1>
<div class="panel">
  <div class="row">
    <input id="password" type="password" placeholder="缃戝叧瀵嗙爜" style="width:200px">
    <button id="loginBtn" class="ok" onclick="login()">鐧诲綍</button>
    <button id="logoutBtn" onclick="logout()" style="display:none">閫€鍑?/button>
    <span id="state">鏈櫥褰?/span>
  </div>
</div>
<div class="panel">
  <div class="row" style="margin-bottom:10px">
    <strong>杩涚▼鍒楄〃</strong>
    <button onclick="refresh()" id="refreshBtn" disabled>鍒锋柊</button>
  </div>
  <input id="filter" placeholder="绛涢€?PID / 鍚嶇О / 鍛戒护琛?.." style="margin-bottom:10px" oninput="renderList()">
  <div id="list">璇峰厛鐧诲綍</div>
</div>
<script>
var loggedIn = false;
var processes = [];
var el = function(id){return document.getElementById(id);};
var headers = function(){var h={'Content-Type':'application/json'};return h;};
var setState = function(text,ok){var s=el('state');s.textContent=text;s.className=ok?'ok':'';};

async function api(path,options){
  var res = await fetch(path,Object.assign({credentials:'same-origin'},options));
  var body = await res.json().catch(function(){return{};});
  if(!res.ok){var e=new Error(body.error||('HTTP '+res.status));e.status=res.status;throw e;}
  return body;
}

async function login(){
  el('loginBtn').disabled=true;
  try{
    await api('/login',{method:'POST',headers:headers(),body:JSON.stringify({password:el('password').value})});
    el('password').value='';
    loggedIn=true;
    setState('宸茬櫥褰?,true);
    el('loginBtn').style.display='none';
    el('logoutBtn').style.display='';
    el('refreshBtn').disabled=false;
    await refresh();
  }catch(e){
    setState('鐧诲綍澶辫触: '+(e.body&&e.body.error||e.message),false);
  }
  el('loginBtn').disabled=false;
}

async function logout(){
  await fetch('/logout',{method:'POST',credentials:'same-origin'});
  loggedIn=false;
  processes=[];
  el('list').innerHTML='璇峰厛鐧诲綍';
  setState('宸查€€鍑?,false);
  el('loginBtn').style.display='';
  el('logoutBtn').style.display='none';
  el('refreshBtn').disabled=true;
}

async function refresh(){
  try{
    var data=await api('/processes',{headers:{}});
    processes=data.processes||[];
    renderList();
  }catch(e){
    if(e.status===401){setState('璇峰厛鐧诲綍',false);loggedIn=false;}
    else{el('list').innerHTML='<div style="color:#ff6b6b">鍔犺浇澶辫触: '+e.message+'</div>';}
  }
}

function renderList(){
  var f=el('filter').value.toLowerCase();
  var filtered=processes.filter(function(p){
    return !f||(p.pid+'').indexOf(f)>=0||(p.name||'').toLowerCase().indexOf(f)>=0||(p.command||'').toLowerCase().indexOf(f)>=0;
  });
  if(!filtered.length){
    el('list').innerHTML='<div style="color:#888">鏃犲尮閰嶈繘绋?/div>';
    return;
  }
  var html='';
  for(var i=0;i<filtered.length;i++){
    var p=filtered[i];
    html+='<div class="proc">';
    html+='<div class="proc-info">';
    html+='<div class="proc-name">'+esc(p.name||'?')+' <span style="color:#aaa;font-weight:400">PID '+p.pid+'</span></div>';
    html+='<div class="proc-meta">'+esc(p.command||p.title||'')+'</div>';
    html+='</div>';
    html+='<button class="kill" onclick="doKill('+p.pid+',\''+esc(p.name||'')+'\')">缁撴潫杩涚▼</button>';
    html+='</div>';
  }
  el('list').innerHTML=html;
}

function esc(s){
  return (s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;');
}

async function doKill(pid,name){
  if(!confirm('纭寮哄埗缁撴潫 '+name+' (PID '+pid+')?')) return;
  try{
    var data=await api('/kill',{method:'POST',headers:headers(),body:JSON.stringify({pid:pid})});
    setState(data.ok?'宸茬粨鏉?PID '+pid:'缁撴潫澶辫触',data.ok);
    await refresh();
  }catch(e){
    setState('澶辫触: '+e.message,false);
  }
}

el('password').addEventListener('keydown',function(e){if(e.key==='Enter') login();});
el('filter').addEventListener('keydown',function(e){if(e.key==='Enter') refresh();});
setInterval(refresh,5000);
</script>
</body>
</html>"""

PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>浠诲姟鏍忕粨鏉熸帶鍒跺彴 :9091</title>
<style>
:root{color-scheme:light;--bg:#f8fafb;--panel:#fff;--line:#e5e7eb;--text:#4b5563;--muted:#9ca3af;--blue:#3b82f6;--red:#ef4444;--green:#10b981}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:14px/1.5 system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
header{padding:18px 20px;border-bottom:1px solid var(--line);background:#fff}.title{font-size:18px;font-weight:750;color:#374151}.sub{margin-top:3px;color:var(--muted);font-size:12px}
main{max-width:980px;margin:0 auto;padding:16px}.panel{background:var(--panel);border:1px solid var(--line);border-radius:8px;margin-bottom:14px;overflow:hidden}.panel h2{margin:0;padding:10px 12px;border-bottom:1px solid var(--line);font-size:13px;color:#374151;background:#fbfdff}.body{padding:12px}
.row{display:flex;gap:8px;align-items:center;flex-wrap:wrap}input,button{font:inherit}input{border:1px solid var(--line);border-radius:7px;background:#fff;color:var(--text);padding:9px 10px;outline:none}input:focus{border-color:var(--blue);box-shadow:0 0 0 2px rgba(59,130,246,.12)}
button{border:1px solid var(--line);border-radius:7px;background:#fff;color:#374151;padding:8px 12px;font-weight:650;cursor:pointer}button.primary{background:var(--blue);border-color:var(--blue);color:#fff}button.kill{border-color:#fecaca;color:var(--red)}button:disabled{opacity:.45;cursor:not-allowed}
.hint{color:var(--muted);font-size:12px;margin-top:7px}.state{display:inline-flex;align-items:center;border:1px solid var(--line);border-radius:999px;padding:5px 10px;color:var(--muted);font-size:12px}.state.ok{border-color:#bbf7d0;color:var(--green)}.state.err{border-color:#fecaca;color:var(--red)}
.apps{display:grid;gap:7px;max-height:calc(100vh - 260px);overflow:auto}.app{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:10px;align-items:center;border:1px solid var(--line);border-radius:8px;background:#fff;padding:9px 10px}.app-title{font-weight:700;color:#374151;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.app-meta{margin-top:2px;color:var(--muted);font-size:12px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.empty{color:var(--muted);padding:8px 0}
@media(max-width:720px){main{padding:10px}.app{grid-template-columns:1fr}.row input{width:100%}}
</style>
</head>
<body>
<header><div class="title">浠诲姟鏍忕粨鏉熸帶鍒跺彴 :9091</div><div class="sub">鍙樉绀轰换鍔℃爮鍙绐楀彛锛屽悗鍙拌繘绋嬩笉浼氬嚭鐜板湪杩欓噷銆?/div></header>
<main>
<section class="panel"><h2>鐧诲綍</h2><div class="body"><div class="row"><input id="password" type="password" autocomplete="current-password" placeholder="缃戝叧瀵嗙爜"><button id="loginBtn" class="primary" type="button">鐧诲綍</button><button id="logoutBtn" type="button" style="display:none">閫€鍑?/button><span id="state" class="state">鏈櫥褰?/span></div></div></section>
<section class="panel"><h2>浠诲姟鏍忕▼搴?/h2><div class="body"><div class="row"><input id="filter" placeholder="鎸夌獥鍙ｆ爣棰樸€佽繘绋嬪悕銆丳ID 鎴?hwnd 绛涢€? style="flex:1;min-width:240px"><button id="refreshBtn" type="button" disabled>鍒锋柊</button></div><div class="hint">鐐瑰嚮缁撴潫浼氬己鍒剁粨鏉熸墍閫夌▼搴忕殑 PID 杩涚▼鏍戯紱涓嶅湪浠诲姟鏍忛噷鐨?PID 浼氳鎷掔粷銆?/div><div id="list" class="apps"><div class="empty">鐧诲綍鍚庢樉绀轰换鍔℃爮绋嬪簭銆?/div></div></div></section>
</main>
<script>
const $ = (id) => document.getElementById(id);
let loggedIn = false;
let apps = [];
function headers(){return {'Content-Type':'application/json'};}
function setState(text,kind=''){const s=$('state');s.textContent=text;s.className='state '+kind;}
async function api(path,options={}){
  const res = await fetch(path,Object.assign({credentials:'same-origin'},options));
  const body = await res.json().catch(()=>({}));
  if(!res.ok){const err=new Error(body.error||('HTTP '+res.status));err.status=res.status;err.body=body;throw err;}
  return body;
}
async function login(){
  $('loginBtn').disabled=true;
  try{
    await api('/login',{method:'POST',headers:headers(),body:JSON.stringify({password:$('password').value})});
    $('password').value=''; loggedIn=true; setState('宸茬櫥褰?,'ok');
    $('loginBtn').style.display='none'; $('logoutBtn').style.display=''; $('refreshBtn').disabled=false;
    await refresh();
  }catch(err){setState('鐧诲綍澶辫触锛?+(err.body&&err.body.error||err.message),'err');}
  finally{$('loginBtn').disabled=false;}
}
async function logout(){
  await fetch('/logout',{method:'POST',credentials:'same-origin'});
  loggedIn=false; apps=[]; renderList(); setState('宸查€€鍑?,'');
  $('loginBtn').style.display=''; $('logoutBtn').style.display='none'; $('refreshBtn').disabled=true;
}
async function refresh(){
  if(!loggedIn) return;
  try{
    const q=encodeURIComponent($('filter').value.trim());
    const data=await api('/processes?limit=80'+(q?'&query='+q:''));
    apps=data.processes||data.apps||[];
    renderList();
    setState('灏辩华','ok');
  }catch(err){
    if(err.status===401){loggedIn=false; setState('璇峰厛鐧诲綍','err'); $('refreshBtn').disabled=true;}
    else{$('list').innerHTML='<div class="empty">鍔犺浇澶辫触锛?+escapeHtml(err.message)+'</div>'; setState('鍔犺浇澶辫触','err');}
  }
}
function renderList(){
  const list=$('list'); list.textContent='';
  if(!apps.length){list.innerHTML='<div class="empty">'+(loggedIn?'娌℃湁鎵惧埌浠诲姟鏍忓彲瑙佺▼搴忋€?:'鐧诲綍鍚庢樉绀轰换鍔℃爮绋嬪簭銆?)+'</div>'; return;}
  for(const app of apps){
    const row=document.createElement('div'); row.className='app';
    const info=document.createElement('div');
    const title=document.createElement('div'); title.className='app-title'; title.textContent=app.title||app.name||'鏈懡鍚嶇獥鍙?;
    const meta=document.createElement('div'); meta.className='app-meta'; meta.textContent=(app.name||'杩涚▼')+' | PID '+app.pid+' | '+(app.hwnd||'');
    info.append(title,meta);
    const kill=document.createElement('button'); kill.type='button'; kill.className='kill'; kill.textContent='缁撴潫'; kill.disabled=!app.killable;
    kill.addEventListener('click',()=>doKill(app.pid,app.title||app.name||'绋嬪簭'));
    row.append(info,kill); list.appendChild(row);
  }
}
function escapeHtml(text){return (text||'').replace(/[&<>"']/g,(c)=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
async function doKill(pid,name){
  if(!confirm('纭寮哄埗缁撴潫 '+name+' (PID '+pid+')锛?)) return;
  try{
    const data=await api('/kill',{method:'POST',headers:headers(),body:JSON.stringify({pid})});
    setState(data.ok?'宸茬粨鏉?PID '+pid:'缁撴潫澶辫触',data.ok?'ok':'err');
    await refresh();
  }catch(err){setState('缁撴潫澶辫触锛?+(err.body&&err.body.error||err.message),'err');}
}
$('loginBtn').addEventListener('click',login);
$('logoutBtn').addEventListener('click',logout);
$('refreshBtn').addEventListener('click',refresh);
$('password').addEventListener('keydown',(event)=>{if(event.key==='Enter') login();});
$('filter').addEventListener('input',()=>{if(loggedIn) refresh();});
setInterval(refresh,5000);
</script>
</body>
</html>"""

PAGE = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>浠诲姟鏍忕獥鍙ｆ帶鍒跺彴 :9091</title>
<style>
:root{color-scheme:light;--bg:#f8fafb;--panel:#fff;--line:#e5e7eb;--text:#4b5563;--muted:#9ca3af;--blue:#3b82f6;--red:#ef4444;--green:#10b981}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:14px/1.5 system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
header{padding:18px 20px;border-bottom:1px solid var(--line);background:#fff}.title{font-size:18px;font-weight:750;color:#374151}.sub{margin-top:3px;color:var(--muted);font-size:12px}
main{max-width:980px;margin:0 auto;padding:16px}.panel{background:var(--panel);border:1px solid var(--line);border-radius:8px;margin-bottom:14px;overflow:hidden}.panel h2{margin:0;padding:10px 12px;border-bottom:1px solid var(--line);font-size:13px;color:#374151;background:#fbfdff}.body{padding:12px}
.row{display:flex;gap:8px;align-items:center;flex-wrap:wrap}input,button{font:inherit}input{border:1px solid var(--line);border-radius:7px;background:#fff;color:var(--text);padding:9px 10px;outline:none}input:focus{border-color:var(--blue);box-shadow:0 0 0 2px rgba(59,130,246,.12)}
button{border:1px solid var(--line);border-radius:7px;background:#fff;color:#374151;padding:8px 12px;font-weight:650;cursor:pointer}button.primary{background:var(--blue);border-color:var(--blue);color:#fff}button.close{border-color:#bfdbfe;color:var(--blue)}button:disabled{opacity:.45;cursor:not-allowed}
.hint{color:var(--muted);font-size:12px;margin-top:7px}.state{display:inline-flex;align-items:center;border:1px solid var(--line);border-radius:999px;padding:5px 10px;color:var(--muted);font-size:12px}.state.ok{border-color:#bbf7d0;color:var(--green)}.state.err{border-color:#fecaca;color:var(--red)}
.apps{display:grid;gap:7px;max-height:calc(100vh - 260px);overflow:auto}.app{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:10px;align-items:center;border:1px solid var(--line);border-radius:8px;background:#fff;padding:9px 10px}.app-title{font-weight:700;color:#374151;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.app-meta{margin-top:2px;color:var(--muted);font-size:12px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.badge{display:inline-block;margin-left:6px;border:1px solid #fde68a;border-radius:999px;padding:1px 7px;color:#92400e;background:#fffbeb;font-size:11px}.empty{color:var(--muted);padding:8px 0}
@media(max-width:720px){main{padding:10px}.app{grid-template-columns:1fr}.row input{width:100%}}
</style>
</head>
<body>
<header><div class="title">浠诲姟鏍忕獥鍙ｆ帶鍒跺彴 :9091</div><div class="sub">杩欓噷鍏抽棴鐨勬槸鍏蜂綋绐楀彛锛屼笉寮烘潃 Windows 妗岄潰 Shell銆?/div></header>
<main>
<section class="panel"><h2>鐧诲綍</h2><div class="body"><div class="row"><input id="password" type="password" autocomplete="current-password" placeholder="缃戝叧瀵嗙爜"><button id="loginBtn" class="primary" type="button">鐧诲綍</button><button id="logoutBtn" type="button" style="display:none">閫€鍑?/button><span id="state" class="state">鏈櫥褰?/span></div></div></section>
<section class="panel"><h2>浠诲姟鏍忕獥鍙?/h2><div class="body"><div class="row"><input id="filter" placeholder="鎸夌獥鍙ｆ爣棰樸€佽繘绋嬪悕銆丳ID 鎴?hwnd 绛涢€? style="flex:1;min-width:240px"><button id="refreshBtn" type="button" disabled>鍒锋柊</button></div><div class="hint">榛樿鎸夐挳鏄€滃叧闂獥鍙ｂ€濓紝涓嶄細 taskkill銆俥xplorer.exe 灞炰簬 Windows 妗岄潰/浠诲姟鏍?Shell锛屽凡绂佹寮烘潃銆?/div><div id="list" class="apps"><div class="empty">鐧诲綍鍚庢樉绀轰换鍔℃爮绐楀彛銆?/div></div></div></section>
</main>
<script>
const $ = (id) => document.getElementById(id);
let loggedIn = false;
let apps = [];
function headers(){return {'Content-Type':'application/json'};}
function setState(text,kind=''){const s=$('state');s.textContent=text;s.className='state '+kind;}
async function api(path,options={}){
  const res = await fetch(path,Object.assign({credentials:'same-origin'},options));
  const body = await res.json().catch(()=>({}));
  if(!res.ok){const err=new Error(body.error||('HTTP '+res.status));err.status=res.status;err.body=body;throw err;}
  return body;
}
async function login(){
  $('loginBtn').disabled=true;
  try{
    await api('/login',{method:'POST',headers:headers(),body:JSON.stringify({password:$('password').value})});
    $('password').value=''; loggedIn=true; setState('宸茬櫥褰?,'ok');
    $('loginBtn').style.display='none'; $('logoutBtn').style.display=''; $('refreshBtn').disabled=false;
    await refresh();
  }catch(err){setState('鐧诲綍澶辫触锛?+(err.body&&err.body.error||err.message),'err');}
  finally{$('loginBtn').disabled=false;}
}
async function logout(){
  await fetch('/logout',{method:'POST',credentials:'same-origin'});
  loggedIn=false; apps=[]; renderList(); setState('宸查€€鍑?,'');
  $('loginBtn').style.display=''; $('logoutBtn').style.display='none'; $('refreshBtn').disabled=true;
}
async function refresh(){
  if(!loggedIn) return;
  try{
    const q=encodeURIComponent($('filter').value.trim());
    const data=await api('/processes?limit=80'+(q?'&query='+q:''));
    apps=data.processes||data.apps||[];
    renderList();
    setState('灏辩华','ok');
  }catch(err){
    if(err.status===401){loggedIn=false; setState('璇峰厛鐧诲綍','err'); $('refreshBtn').disabled=true;}
    else{$('list').innerHTML='<div class="empty">鍔犺浇澶辫触锛?+escapeHtml(err.message)+'</div>'; setState('鍔犺浇澶辫触','err');}
  }
}
function renderList(){
  const list=$('list'); list.textContent='';
  if(!apps.length){list.innerHTML='<div class="empty">'+(loggedIn?'娌℃湁鎵惧埌浠诲姟鏍忓彲瑙佺獥鍙ｃ€?:'鐧诲綍鍚庢樉绀轰换鍔℃爮绐楀彛銆?)+'</div>'; return;}
  for(const app of apps){
    const row=document.createElement('div'); row.className='app';
    const info=document.createElement('div');
    const title=document.createElement('div'); title.className='app-title'; title.textContent=app.title||app.name||'鏈懡鍚嶇獥鍙?;
    if(!app.killable){const badge=document.createElement('span'); badge.className='badge'; badge.textContent='鍙椾繚鎶?; title.appendChild(badge);}
    const meta=document.createElement('div'); meta.className='app-meta'; meta.textContent=(app.name||'杩涚▼')+' | PID '+app.pid+' | '+(app.hwnd||'');
    info.append(title,meta);
    const close=document.createElement('button'); close.type='button'; close.className='close'; close.textContent='鍏抽棴绐楀彛'; close.disabled=!app.closeable;
    close.addEventListener('click',()=>closeWindow(app.hwnd,app.title||app.name||'绐楀彛'));
    row.append(info,close); list.appendChild(row);
  }
}
function escapeHtml(text){return (text||'').replace(/[&<>"']/g,(c)=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
async function closeWindow(hwnd,name){
  if(!confirm('鍏抽棴绐楀彛锛?+name+'锛?)) return;
  try{
    const data=await api('/close',{method:'POST',headers:headers(),body:JSON.stringify({hwnd})});
    setState(data.ok?'宸插彂閫佸叧闂獥鍙ｈ姹?:'鍏抽棴澶辫触',data.ok?'ok':'err');
    setTimeout(refresh,600);
  }catch(err){setState('鍏抽棴澶辫触锛?+(err.body&&err.body.error||err.message),'err');}
}
$('loginBtn').addEventListener('click',login);
$('logoutBtn').addEventListener('click',logout);
$('refreshBtn').addEventListener('click',refresh);
$('password').addEventListener('keydown',(event)=>{if(event.key==='Enter') login();});
$('filter').addEventListener('input',()=>{if(loggedIn) refresh();});
setInterval(refresh,5000);
</script>
</body>
</html>"""


# Final clean UI. Keep this assignment closest to the handler so older archived
# PAGE drafts above cannot become the active interface again.
PAGE = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>任务栏窗口控制台 :9091</title>
<style>
:root{color-scheme:light;--bg:#f8fafb;--panel:#fff;--soft:#f3f6f8;--text:#4b5563;--muted:#9ca3af;--line:#e5e7eb;--blue:#3b82f6;--red:#ef4444;--green:#10b981}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:14px/1.5 system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei",sans-serif}
header{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:12px 18px;background:#fff;border-bottom:1px solid var(--line)}.title{font-size:18px;font-weight:750}.sub{color:var(--muted);font-size:12px}.wrap{max-width:980px;margin:0 auto;padding:14px;display:grid;gap:14px}.panel{background:#fff;border:1px solid var(--line);border-radius:8px;overflow:hidden}.panel h2{margin:0;padding:10px 12px;border-bottom:1px solid var(--line);background:var(--soft);font-size:13px}.body{padding:12px}.row{display:flex;gap:8px;align-items:center;flex-wrap:wrap}input,button{font:inherit}input{flex:1;min-width:220px;border:1px solid #d1d5db;border-radius:6px;padding:9px 10px;outline:none}input:focus{border-color:var(--blue);box-shadow:0 0 0 2px rgba(59,130,246,.14)}button{border:1px solid #d1d5db;border-radius:6px;background:#fff;color:var(--text);padding:8px 12px;font-weight:650;cursor:pointer}button.primary{background:var(--blue);border-color:var(--blue);color:#fff}button.close{border-color:#bfdbfe;color:#2563eb}button:disabled{opacity:.5;cursor:not-allowed}.hint{margin-top:7px;color:var(--muted);font-size:12px}.state{font-size:12px;color:var(--muted)}.state.ok{color:var(--green)}.state.err{color:var(--red)}.apps{display:grid;gap:7px;max-height:calc(100vh - 245px);overflow:auto}.app{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:10px;align-items:center;border:1px solid var(--line);background:#fff;border-radius:8px;padding:9px 10px}.app-title{font-weight:700;color:#374151;word-break:break-word}.app-meta{color:var(--muted);font-size:12px;word-break:break-all}.badge{display:inline-block;margin-left:6px;border:1px solid #fde68a;background:#fffbeb;color:#92400e;border-radius:999px;padding:1px 6px;font-size:11px}.empty{color:var(--muted);padding:12px}
</style>
</head>
<body>
<header><div><div class="title">任务栏窗口控制台 :9091</div><div class="sub">默认关闭具体窗口，不强杀 Windows 桌面 Shell。</div></div><span id="state" class="state">未登录</span></header>
<main class="wrap">
<section class="panel"><h2>登录</h2><div class="body"><div class="row"><input id="password" type="password" autocomplete="current-password" placeholder="网关密码"><button id="loginBtn" class="primary" type="button">登录</button><button id="logoutBtn" type="button" style="display:none">退出</button></div><div class="hint">9090 和 9091 使用同一个登录 Cookie，默认保留 90 天。</div></div></section>
<section class="panel"><h2>任务栏窗口</h2><div class="body"><div class="row"><input id="filter" placeholder="按窗口标题、进程名、PID 或 hwnd 筛选"><button id="refreshBtn" type="button" disabled>刷新</button></div><div class="hint">按钮是“关闭窗口”，不会 taskkill。explorer.exe 属于 Windows 桌面/任务栏 Shell，已禁止强杀。</div><div id="list" class="apps"><div class="empty">登录后显示任务栏窗口。</div></div></div></section>
</main>
<script>
const $ = (id) => document.getElementById(id);
let loggedIn = false;
let apps = [];
function headers(){return {'Content-Type':'application/json'};}
function setState(text,kind=''){const s=$('state');s.textContent=text;s.className='state '+kind;}
async function api(path,options={}){const res=await fetch(path,Object.assign({credentials:'same-origin'},options)); const body=await res.json().catch(()=>({})); if(!res.ok){const err=new Error(body.error||('HTTP '+res.status));err.status=res.status;err.body=body;throw err;} return body;}
async function login(){ $('loginBtn').disabled=true; try{await api('/login',{method:'POST',headers:headers(),body:JSON.stringify({password:$('password').value})}); $('password').value=''; loggedIn=true; setState('已登录','ok'); $('loginBtn').style.display='none'; $('logoutBtn').style.display=''; $('refreshBtn').disabled=false; await refresh();}catch(err){setState('登录失败：'+(err.body&&err.body.error||err.message),'err');}finally{$('loginBtn').disabled=false;}}
async function logout(){await fetch('/logout',{method:'POST',credentials:'same-origin'}); loggedIn=false; apps=[]; renderList(); setState('已退出',''); $('loginBtn').style.display=''; $('logoutBtn').style.display='none'; $('refreshBtn').disabled=true;}
async function refresh(){if(!loggedIn) return; try{const q=encodeURIComponent($('filter').value.trim()); const data=await api('/processes?limit=80'+(q?'&query='+q:'')); apps=data.processes||data.apps||[]; renderList(); setState('就绪','ok');}catch(err){if(err.status===401){loggedIn=false; setState('请先登录','err'); $('refreshBtn').disabled=true;}else{$('list').innerHTML='<div class="empty">加载失败：'+escapeHtml(err.message)+'</div>'; setState('加载失败','err');}}}
async function checkSession(){try{const data=await api('/processes?limit=80'); loggedIn=true; apps=data.processes||data.apps||[]; $('loginBtn').style.display='none'; $('logoutBtn').style.display=''; $('refreshBtn').disabled=false; renderList(); setState('已登录','ok');}catch(err){loggedIn=false; setState('未登录',''); renderList();}}
function renderList(){const list=$('list'); list.textContent=''; if(!apps.length){list.innerHTML='<div class="empty">'+(loggedIn?'没有找到任务栏可见窗口。':'登录后显示任务栏窗口。')+'</div>'; return;} for(const app of apps){const row=document.createElement('div'); row.className='app'; const info=document.createElement('div'); const title=document.createElement('div'); title.className='app-title'; title.textContent=app.title||app.name||'未命名窗口'; if(!app.killable){const badge=document.createElement('span'); badge.className='badge'; badge.textContent='受保护'; title.appendChild(badge);} const meta=document.createElement('div'); meta.className='app-meta'; meta.textContent=(app.name||'进程')+' | PID '+app.pid+' | '+(app.hwnd||''); info.append(title,meta); const close=document.createElement('button'); close.type='button'; close.className='close'; close.textContent='关闭窗口'; close.disabled=!app.closeable; close.addEventListener('click',()=>closeWindow(app.hwnd,app.title||app.name||'窗口')); row.append(info,close); list.appendChild(row);}}
function escapeHtml(text){return (text||'').replace(/[&<>"']/g,(c)=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
async function closeWindow(hwnd,name){if(!confirm('关闭窗口：'+name+'？')) return; try{const data=await api('/close',{method:'POST',headers:headers(),body:JSON.stringify({hwnd})}); setState(data.ok?'已发送关闭窗口请求':'关闭失败',data.ok?'ok':'err'); setTimeout(refresh,600);}catch(err){setState('关闭失败：'+(err.body&&err.body.error||err.message),'err');}}
$('loginBtn').addEventListener('click',login);
$('logoutBtn').addEventListener('click',logout);
$('refreshBtn').addEventListener('click',refresh);
$('password').addEventListener('keydown',(event)=>{if(event.key==='Enter') login();});
$('filter').addEventListener('input',()=>{if(loggedIn) refresh();});
checkSession();
setInterval(refresh,5000);
</script>
</body>
</html>"""

PAGE = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Windows 控制台 :9091</title>
<style>
:root{color-scheme:light;--bg:#f8fafb;--panel:#fff;--soft:#f3f6f8;--text:#4b5563;--muted:#9ca3af;--line:#e5e7eb;--blue:#3b82f6;--red:#ef4444;--green:#10b981}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:14px/1.5 system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei",sans-serif}
header{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:12px 18px;background:#fff;border-bottom:1px solid var(--line)}.title{font-size:18px;font-weight:750}.sub{color:var(--muted);font-size:12px}.wrap{max-width:1280px;margin:0 auto;padding:14px;display:grid;gap:14px}.workspace{display:grid;grid-template-columns:minmax(0,2fr) minmax(320px,1fr);gap:14px;align-items:start}.panel{background:#fff;border:1px solid var(--line);border-radius:8px;overflow:hidden}.panel h2{margin:0;padding:10px 12px;border-bottom:1px solid var(--line);background:var(--soft);font-size:13px}.body{padding:12px}.row{display:flex;gap:8px;align-items:center;flex-wrap:wrap}input,button{font:inherit}input{flex:1;min-width:220px;border:1px solid #d1d5db;border-radius:6px;padding:9px 10px;outline:none}input:focus{border-color:var(--blue);box-shadow:0 0 0 2px rgba(59,130,246,.14)}button{border:1px solid #d1d5db;border-radius:6px;background:#fff;color:var(--text);padding:8px 12px;font-weight:650;cursor:pointer}button.primary{background:var(--blue);border-color:var(--blue);color:#fff}button.close,button.open{border-color:#bfdbfe;color:#2563eb}button:disabled{opacity:.5;cursor:not-allowed}.hint{margin-top:7px;color:var(--muted);font-size:12px}.state{font-size:12px;color:var(--muted)}.state.ok{color:var(--green)}.state.err{color:var(--red)}.apps,.files{display:grid;gap:7px;overflow:auto}.files{max-height:calc(100vh - 258px)}.apps{max-height:calc(100vh - 258px)}.app,.file{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:10px;align-items:center;border:1px solid var(--line);background:#fff;border-radius:8px;padding:9px 10px}.file{cursor:pointer}.file:hover,.app:hover{background:#f8fafc}.app-title,.file-title{font-weight:700;color:#374151;word-break:break-word}.app-meta,.file-meta{color:var(--muted);font-size:12px;word-break:break-all}.badge{display:inline-block;margin-left:6px;border:1px solid #fde68a;background:#fffbeb;color:#92400e;border-radius:999px;padding:1px 6px;font-size:11px}.empty{color:var(--muted);padding:12px}.pathbar{display:flex;gap:8px;align-items:center;justify-content:space-between;flex-wrap:wrap;margin-bottom:9px}.path{font-weight:700;color:#374151;word-break:break-all}.icon{display:inline-block;min-width:44px;color:#6b7280;font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
@media(max-width:900px){header{align-items:flex-start;flex-direction:column}.workspace{grid-template-columns:1fr}.app,.file{grid-template-columns:1fr}.wrap{padding:10px}.files,.apps{max-height:none}}
</style>
</head>
<body>
<header><div><div class="title">Windows 控制台 :9091</div><div class="sub">左侧此电脑区，右侧任务栏窗口区</div></div><span id="state" class="state">未登录</span></header>
<main class="wrap">
<section class="panel"><h2>登录</h2><div class="body"><div class="row"><input id="password" type="password" autocomplete="current-password" placeholder="网关密码"><button id="loginBtn" class="primary" type="button">登录</button><button id="logoutBtn" type="button" style="display:none">退出</button></div><div class="hint">9090 和 9091 使用同一个登录 Cookie，默认保留 90 天。</div></div></section>
<div class="workspace">
<section class="panel file-panel"><h2>此电脑区</h2><div class="body"><div class="pathbar"><div id="filePath" class="path">桌面</div><div class="row"><button id="desktopBtn" type="button" disabled>桌面</button><button id="upBtn" type="button" disabled>上一级</button><button id="fileRefreshBtn" type="button" disabled>刷新文件</button></div></div><div class="hint">根视图直接读取 Windows Shell 桌面，包含此电脑、公共桌面快捷方式和桌面物品。点文件夹进入下一级，点文件直接在 Windows 中打开。</div><div id="files" class="files"><div class="empty">登录后显示桌面物品。</div></div></div></section>
<section class="panel task-panel"><h2>任务栏窗口</h2><div class="body"><div class="row"><input id="filter" placeholder="按窗口标题、进程名、PID 或 hwnd 筛选"><button id="refreshBtn" type="button" disabled>刷新</button></div><div class="hint">按钮是“关闭窗口”，不会 taskkill。explorer.exe 属于 Windows 桌面/任务栏 Shell，已禁止强杀。</div><div id="list" class="apps"><div class="empty">登录后显示任务栏窗口。</div></div></div></section>
</div>
</main>
<script>
const $ = (id) => document.getElementById(id);
let loggedIn = false;
let apps = [];
let currentPath = '';
let currentParent = '';
let fileEntries = [];
function headers(){return {'Content-Type':'application/json'};}
function setState(text,kind=''){const s=$('state');s.textContent=text;s.className='state '+kind;}
async function api(path,options={}){const res=await fetch(path,Object.assign({credentials:'same-origin'},options)); const body=await res.json().catch(()=>({})); if(!res.ok){const err=new Error(body.error||('HTTP '+res.status));err.status=res.status;err.body=body;throw err;} return body;}
async function login(){ $('loginBtn').disabled=true; try{await api('/login',{method:'POST',headers:headers(),body:JSON.stringify({password:$('password').value})}); $('password').value=''; loggedIn=true; setLoggedInUi(); await Promise.all([refresh(), loadFiles('')]); setState('已登录','ok');}catch(err){setState('登录失败：'+(err.body&&err.body.error||err.message),'err');}finally{$('loginBtn').disabled=false;}}
async function logout(){await fetch('/logout',{method:'POST',credentials:'same-origin'}); loggedIn=false; apps=[]; fileEntries=[]; currentPath=''; currentParent=''; renderList(); renderFiles(); setLoggedOutUi(); setState('已退出','');}
function setLoggedInUi(){$('loginBtn').style.display='none'; $('logoutBtn').style.display=''; $('refreshBtn').disabled=false; $('desktopBtn').disabled=false; $('upBtn').disabled=false; $('fileRefreshBtn').disabled=false;}
function setLoggedOutUi(){$('loginBtn').style.display=''; $('logoutBtn').style.display='none'; $('refreshBtn').disabled=true; $('desktopBtn').disabled=true; $('upBtn').disabled=true; $('fileRefreshBtn').disabled=true;}
async function refresh(){if(!loggedIn) return; try{const q=encodeURIComponent($('filter').value.trim()); const data=await api('/processes?limit=80'+(q?'&query='+q:'')); apps=data.processes||data.apps||[]; renderList(); setState('就绪','ok');}catch(err){if(err.status===401){loggedIn=false; setLoggedOutUi(); setState('请先登录','err');}else{$('list').innerHTML='<div class="empty">加载失败：'+escapeHtml(err.message)+'</div>'; setState('加载失败','err');}}}
async function checkSession(){try{const procData=await api('/processes?limit=80'); const fileData=await api('/files'); loggedIn=true; apps=procData.processes||procData.apps||[]; applyFiles(fileData); setLoggedInUi(); renderList(); renderFiles(); setState('已登录','ok');}catch(err){loggedIn=false; setLoggedOutUi(); renderList(); renderFiles(); setState('未登录','');}}
function renderList(){const list=$('list'); list.textContent=''; if(!apps.length){list.innerHTML='<div class="empty">'+(loggedIn?'没有找到任务栏可见窗口。':'登录后显示任务栏窗口。')+'</div>'; return;} for(const app of apps){const row=document.createElement('div'); row.className='app'; const info=document.createElement('div'); const title=document.createElement('div'); title.className='app-title'; title.textContent=app.title||app.name||'未命名窗口'; if(!app.killable){const badge=document.createElement('span'); badge.className='badge'; badge.textContent='受保护'; title.appendChild(badge);} const meta=document.createElement('div'); meta.className='app-meta'; meta.textContent=(app.name||'进程')+' | PID '+app.pid+' | '+(app.hwnd||''); info.append(title,meta); const close=document.createElement('button'); close.type='button'; close.className='close'; close.textContent='关闭窗口'; close.disabled=!app.closeable; close.addEventListener('click',()=>closeWindow(app.hwnd,app.title||app.name||'窗口')); row.append(info,close); list.appendChild(row);}}
function applyFiles(data){currentPath=data.path||''; currentParent=data.parent||''; fileEntries=data.entries||[]; $('filePath').textContent=data.display_path||'桌面'; $('upBtn').disabled=!loggedIn||!currentPath; $('desktopBtn').disabled=!loggedIn||!currentPath;}
async function loadFiles(path=currentPath){if(!loggedIn) return; try{const data=await api('/files?path='+encodeURIComponent(path||'')); applyFiles(data); renderFiles(); setState('文件就绪','ok');}catch(err){$('files').innerHTML='<div class="empty">文件加载失败：'+escapeHtml(err.message)+'</div>'; setState('文件加载失败','err');}}
function renderFiles(){const box=$('files'); box.textContent=''; if(!loggedIn){box.innerHTML='<div class="empty">登录后显示桌面物品。</div>'; $('filePath').textContent='桌面'; return;} if(!fileEntries.length){box.innerHTML='<div class="empty">这个目录是空的。</div>'; return;} for(const item of fileEntries){const row=document.createElement('div'); row.className='file'; const info=document.createElement('div'); const title=document.createElement('div'); title.className='file-title'; title.innerHTML='<span class="icon">'+iconForItem(item)+'</span>'+escapeHtml(item.name); const meta=document.createElement('div'); meta.className='file-meta'; meta.textContent=describeItem(item); info.append(title,meta); const btn=document.createElement('button'); btn.type='button'; btn.className='open'; btn.textContent=item.is_dir?'进入':'打开'; btn.addEventListener('click',(event)=>{event.stopPropagation(); item.is_dir?loadFiles(item.path):openFile(item.path,item.name);}); row.addEventListener('click',()=>{item.is_dir?loadFiles(item.path):openFile(item.path,item.name);}); row.append(info,btn); box.appendChild(row);}}
function iconForItem(item){if(item.type==='this_pc') return '[PC]'; if(item.type==='drive') return '[DRV]'; return item.is_dir?'[DIR]':'[FILE]';}
function describeItem(item){if(item.type==='this_pc') return 'Windows 此电脑入口，点击进入磁盘列表'; if(item.type==='drive') return '磁盘'; if(item.is_dir) return '文件夹'; return formatSize(item.size)+' | '+formatTime(item.modified);}
async function openFile(path,name){if(!confirm('在 Windows 打开：'+name+'？')) return; try{const data=await api('/files/open',{method:'POST',headers:headers(),body:JSON.stringify({path})}); setState(data.ok?'已发送打开请求':'打开失败',data.ok?'ok':'err');}catch(err){setState('打开失败：'+(err.body&&err.body.error||err.message),'err');}}
async function closeWindow(hwnd,name){if(!confirm('关闭窗口：'+name+'？')) return; try{const data=await api('/close',{method:'POST',headers:headers(),body:JSON.stringify({hwnd})}); setState(data.ok?'已发送关闭窗口请求':'关闭失败',data.ok?'ok':'err'); setTimeout(refresh,600);}catch(err){setState('关闭失败：'+(err.body&&err.body.error||err.message),'err');}}
function formatSize(bytes){const n=Number(bytes||0); if(n<1024) return n+' B'; if(n<1048576) return (n/1024).toFixed(1)+' KB'; if(n<1073741824) return (n/1048576).toFixed(1)+' MB'; return (n/1073741824).toFixed(1)+' GB';}
function formatTime(seconds){if(!seconds) return '-'; return new Date(seconds*1000).toLocaleString();}
function escapeHtml(text){return (text||'').replace(/[&<>"']/g,(c)=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
$('loginBtn').addEventListener('click',login);
$('logoutBtn').addEventListener('click',logout);
$('refreshBtn').addEventListener('click',refresh);
$('fileRefreshBtn').addEventListener('click',()=>loadFiles(currentPath));
$('desktopBtn').addEventListener('click',()=>loadFiles(''));
$('upBtn').addEventListener('click',()=>loadFiles(currentParent));
$('password').addEventListener('keydown',(event)=>{if(event.key==='Enter') login();});
$('filter').addEventListener('input',()=>{if(loggedIn) refresh();});
checkSession();
setInterval(refresh,5000);
</script>
</body>
</html>"""

PAGE = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Windows 控制台 :9091</title>
<style>
:root{color-scheme:light;--bg:#f8fafb;--panel:#fff;--soft:#f3f4f6;--text:#374151;--muted:#8a94a6;--line:#d1d5db;--line2:#9ca3af}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:14px/1.5 system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei",sans-serif}
header{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:12px 18px;background:#fff;border-bottom:1px solid var(--line)}.title{font-size:18px;font-weight:750}.sub{color:var(--muted);font-size:12px}.wrap{max-width:1280px;margin:0 auto;padding:14px;display:grid;gap:14px}.workspace{display:grid;grid-template-columns:minmax(0,2fr) minmax(320px,1fr);gap:14px;align-items:start}.panel{background:#fff;border:1px solid var(--line);overflow:hidden}.panel h2{margin:0;padding:10px 12px;border-bottom:1px solid var(--line);background:var(--soft);font-size:13px}.body{padding:12px}.row{display:flex;gap:8px;align-items:center;flex-wrap:wrap}input,button{font:inherit}input{flex:1;min-width:220px;border:1px solid var(--line);background:#fff;color:var(--text);padding:9px 10px;outline:none}input:focus{border-color:var(--line2);box-shadow:0 0 0 2px rgba(156,163,175,.18)}
button{appearance:none;border:1px solid #cbd5e1;border-radius:7px;background:linear-gradient(#fff,#f8fafc);color:#334155;min-width:72px;height:34px;padding:0 14px;display:inline-flex;align-items:center;justify-content:center;gap:6px;text-align:center;white-space:nowrap;font-weight:650;cursor:pointer;box-shadow:0 1px 1px rgba(15,23,42,.04);transition:background .14s ease,border-color .14s ease,box-shadow .14s ease,transform .08s ease}button:hover{background:#f8fafc;border-color:#94a3b8;box-shadow:0 1px 2px rgba(15,23,42,.08)}button:active{transform:translateY(1px);box-shadow:none}button:disabled{opacity:.48;cursor:not-allowed;transform:none;box-shadow:none}.primary{background:#f8fafc;border-color:#b6c3d1;color:#334155}.primary:hover{background:#f1f5f9;border-color:#94a3b8}.quiet{background:#fff;color:#475569}.danger{background:#fff;color:#9f1239;border-color:#fecdd3}.danger:hover{background:#fff1f2;border-color:#fb7185}.pending,.primary.pending,.danger.pending{background:#111827;border-color:#111827;color:#fff}.hint{margin-top:7px;color:var(--muted);font-size:12px}.state{font-size:12px;color:var(--muted)}.state.ok,.state.err{color:var(--text)}.apps,.files{display:grid;gap:7px;overflow:auto}.files,.apps{max-height:calc(100vh - 286px)}.app,.file{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:10px;align-items:center;border:1px solid var(--line);background:#fff;padding:9px 10px}.file{cursor:pointer}.file:hover,.app:hover{background:#f8fafc}.app-title,.file-title{font-weight:700;color:#374151;word-break:break-word}.app-meta,.file-meta{color:var(--muted);font-size:12px;word-break:break-all}.badge{display:inline-block;margin-left:6px;border:1px solid var(--line);background:#f9fafb;color:#4b5563;padding:1px 6px;font-size:11px}.empty{color:var(--muted);padding:12px}.pathbar{display:flex;gap:8px;align-items:center;justify-content:space-between;flex-wrap:wrap;margin-bottom:9px}.path{font-weight:700;color:#374151;word-break:break-all}.icon{display:inline-block;min-width:44px;color:#6b7280;font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}.toggle{display:inline-flex;align-items:center;gap:7px;color:var(--muted);font-size:12px}.toggle input{width:auto;min-width:0;flex:0 0 auto;padding:0}
@media(max-width:900px){header{align-items:flex-start;flex-direction:column}.workspace{grid-template-columns:1fr}.app,.file{grid-template-columns:1fr}.wrap{padding:10px}.files,.apps{max-height:none}}
</style>
</head>
<body>
<header><div><div class="title">Windows 控制台 :9091</div><div class="sub">左侧此电脑区，右侧任务栏窗口区</div></div><span id="state" class="state">未登录</span></header>
<main class="wrap">
<section class="panel"><h2>登录</h2><div class="body"><div class="row"><input id="password" type="password" autocomplete="current-password" placeholder="网关密码"><button id="loginBtn" class="primary" type="button">登录</button><button id="logoutBtn" class="quiet" type="button" style="display:none">退出</button><label class="toggle"><input id="confirmToggle" type="checkbox" checked>按钮二次确认</label></div><div class="hint">二次确认开启时，打开文件或关闭窗口需要 5 秒内再点一次按钮；关闭后单击执行。</div></div></section>
<div class="workspace">
<section class="panel file-panel"><h2>此电脑区</h2><div class="body"><div class="pathbar"><div id="filePath" class="path">桌面</div><div class="row"><button id="desktopBtn" class="quiet" type="button" disabled>桌面</button><button id="upBtn" class="quiet" type="button" disabled>上一级</button><button id="fileRefreshBtn" class="quiet" type="button" disabled>刷新文件</button></div></div><div class="hint">根视图读取真实桌面图标。点文件夹进入下一级；点文件按钮打开。</div><div id="files" class="files"><div class="empty">登录后显示桌面物品。</div></div></div></section>
<section class="panel task-panel"><h2>任务栏窗口</h2><div class="body"><div class="row"><input id="filter" placeholder="按窗口标题、进程名、PID 或 hwnd 筛选"><button id="refreshBtn" class="quiet" type="button" disabled>刷新</button></div><div class="hint">按钮是“关闭窗口”，不会 taskkill。explorer.exe 属于 Windows 桌面/任务栏 Shell，已禁止强杀。</div><div id="list" class="apps"><div class="empty">登录后显示任务栏窗口。</div></div></div></section>
</div>
</main>
<script>
const $ = (id) => document.getElementById(id);
let loggedIn = false;
let apps = [];
let currentPath = '';
let currentParent = '';
let fileEntries = [];
let pendingKey = '';
let pendingTimer = 0;
let refreshInFlight = false;
let refreshQueued = false;
let refreshAfterPending = false;
let fastPollUntil = 0;
let lastPollAt = 0;
function headers(){return {'Content-Type':'application/json'};}
function setState(text,kind=''){const s=$('state');s.textContent=text;s.className='state '+kind;}
function confirmEnabled(){return $('confirmToggle').checked;}
function saveConfirmSetting(){localStorage.setItem('gatewayConfirmButtons', confirmEnabled() ? '1' : '0'); clearPending();}
function loadConfirmSetting(){const saved=localStorage.getItem('gatewayConfirmButtons'); if(saved!==null) $('confirmToggle').checked=saved!=='0';}
function clearPending(resumeRefresh=true){if(pendingTimer) clearTimeout(pendingTimer); pendingTimer=0; pendingKey=''; document.querySelectorAll('button.pending').forEach((button)=>{button.classList.remove('pending'); if(button.dataset.originalText){button.textContent=button.dataset.originalText; delete button.dataset.originalText;}}); if(resumeRefresh&&refreshAfterPending&&loggedIn){refreshAfterPending=false; setTimeout(refresh,80);}}
function guardedButton(key, button, action){if(!confirmEnabled()){clearPending(); action(); return;} if(pendingKey===key && button.classList.contains('pending')){clearPending(); action(); return;} clearPending(false); pendingKey=key; button.dataset.originalText=button.textContent; button.textContent='确认'; button.classList.add('pending'); pendingTimer=setTimeout(()=>clearPending(true),5000); setState('5 秒内再点一次确认','');}
async function api(path,options={}){const res=await fetch(path,Object.assign({credentials:'same-origin'},options)); const body=await res.json().catch(()=>({})); if(!res.ok){const err=new Error(body.error||('HTTP '+res.status));err.status=res.status;err.body=body;throw err;} return body;}
async function login(){ $('loginBtn').disabled=true; try{await api('/login',{method:'POST',headers:headers(),body:JSON.stringify({password:$('password').value})}); $('password').value=''; loggedIn=true; setLoggedInUi(); await Promise.all([refresh(), loadFiles('')]); setState('已登录','ok');}catch(err){setState('登录失败：'+(err.body&&err.body.error||err.message),'err');}finally{$('loginBtn').disabled=false;}}
async function logout(){await fetch('/logout',{method:'POST',credentials:'same-origin'}); loggedIn=false; apps=[]; fileEntries=[]; currentPath=''; currentParent=''; clearPending(); renderList(); renderFiles(); setLoggedOutUi(); setState('已退出','');}
function setLoggedInUi(){$('loginBtn').style.display='none'; $('logoutBtn').style.display=''; $('refreshBtn').disabled=false; $('desktopBtn').disabled=false; $('upBtn').disabled=false; $('fileRefreshBtn').disabled=false;}
function setLoggedOutUi(){$('loginBtn').style.display=''; $('logoutBtn').style.display='none'; $('refreshBtn').disabled=true; $('desktopBtn').disabled=true; $('upBtn').disabled=true; $('fileRefreshBtn').disabled=true;}
async function refresh(){if(!loggedIn||document.hidden) return; if(pendingKey){refreshAfterPending=true; return;} if(refreshInFlight){refreshQueued=true; return;} refreshInFlight=true; try{const q=encodeURIComponent($('filter').value.trim()); const data=await api('/processes?limit=80'+(q?'&query='+q:'')); if(pendingKey){refreshAfterPending=true; return;} apps=data.processes||data.apps||[]; renderList(); setState('就绪','ok');}catch(err){if(err.status===401){loggedIn=false; setLoggedOutUi(); setState('请先登录','err');}else if(!pendingKey){$('list').innerHTML='<div class="empty">加载失败：'+escapeHtml(err.message)+'</div>'; setState('加载失败','err');}}finally{refreshInFlight=false; if(refreshQueued&&!pendingKey){refreshQueued=false; setTimeout(refresh,80);}}}
function boostTaskbarPolling(ms=7000){fastPollUntil=Date.now()+ms; refresh();}
function pollTaskbar(){if(!loggedIn||document.hidden) return; if(pendingKey){refreshAfterPending=true; return;} const now=Date.now(); const gap=now<fastPollUntil?450:1200; if(now-lastPollAt<gap) return; lastPollAt=now; refresh();}
async function checkSession(){try{const procData=await api('/processes?limit=80'); const fileData=await api('/files'); loggedIn=true; apps=procData.processes||procData.apps||[]; applyFiles(fileData); setLoggedInUi(); renderList(); renderFiles(); setState('已登录','ok');}catch(err){loggedIn=false; setLoggedOutUi(); renderList(); renderFiles(); setState('未登录','');}}
function renderList(){const list=$('list'); list.textContent=''; if(!apps.length){list.innerHTML='<div class="empty">'+(loggedIn?'没有找到任务栏可见窗口。':'登录后显示任务栏窗口。')+'</div>'; return;} for(const app of apps){const row=document.createElement('div'); row.className='app'; const info=document.createElement('div'); const title=document.createElement('div'); title.className='app-title'; title.textContent=app.title||app.name||'未命名窗口'; if(!app.killable){const badge=document.createElement('span'); badge.className='badge'; badge.textContent='受保护'; title.appendChild(badge);} const meta=document.createElement('div'); meta.className='app-meta'; meta.textContent=(app.name||'进程')+' | PID '+app.pid+' | '+(app.hwnd||''); info.append(title,meta); const close=document.createElement('button'); close.type='button'; close.className='danger'; close.textContent='关闭窗口'; close.disabled=!app.closeable; close.addEventListener('click',()=>guardedButton('close:'+app.hwnd,close,()=>closeWindow(app.hwnd))); row.append(info,close); list.appendChild(row);}}
function applyFiles(data){currentPath=data.path||''; currentParent=data.parent||''; fileEntries=data.entries||[]; $('filePath').textContent=data.display_path||'桌面'; $('upBtn').disabled=!loggedIn||!currentPath; $('desktopBtn').disabled=!loggedIn||!currentPath;}
async function loadFiles(path=currentPath){if(!loggedIn) return; clearPending(); try{const data=await api('/files?path='+encodeURIComponent(path||'')); applyFiles(data); renderFiles(); setState('文件就绪','ok');}catch(err){$('files').innerHTML='<div class="empty">文件加载失败：'+escapeHtml(err.message)+'</div>'; setState('文件加载失败','err');}}
function renderFiles(){const box=$('files'); box.textContent=''; if(!loggedIn){box.innerHTML='<div class="empty">登录后显示桌面物品。</div>'; $('filePath').textContent='桌面'; return;} if(!fileEntries.length){box.innerHTML='<div class="empty">这个目录是空的。</div>'; return;} for(const item of fileEntries){const row=document.createElement('div'); row.className='file'; const info=document.createElement('div'); const title=document.createElement('div'); title.className='file-title'; title.innerHTML='<span class="icon">'+iconForItem(item)+'</span>'+escapeHtml(item.name); const meta=document.createElement('div'); meta.className='file-meta'; meta.textContent=describeItem(item); info.append(title,meta); const btn=document.createElement('button'); btn.type='button'; btn.className=item.is_dir?'quiet':'primary'; btn.textContent=item.is_dir?'进入':'打开'; btn.addEventListener('click',(event)=>{event.stopPropagation(); item.is_dir?loadFiles(item.path):guardedButton('open:'+item.path,btn,()=>openFile(item.path));}); row.addEventListener('click',()=>{if(item.is_dir) loadFiles(item.path);}); row.append(info,btn); box.appendChild(row);}}
function iconForItem(item){if(item.type==='this_pc') return '[PC]'; if(item.type==='drive') return '[DRV]'; return item.is_dir?'[DIR]':'[FILE]';}
function describeItem(item){if(item.type==='this_pc') return 'Windows 此电脑入口，点击进入磁盘列表'; if(item.type==='drive') return '磁盘'; if(item.is_dir) return '文件夹'; return formatSize(item.size)+' | '+formatTime(item.modified);}
async function openFile(path){try{const data=await api('/files/open',{method:'POST',headers:headers(),body:JSON.stringify({path})}); setState(data.ok?'已发送打开请求':'打开失败',data.ok?'ok':'err'); boostTaskbarPolling(9000); setTimeout(refresh,500); setTimeout(refresh,1400); setTimeout(refresh,3200);}catch(err){setState('打开失败：'+(err.body&&err.body.error||err.message),'err');}}
async function closeWindow(hwnd){try{const data=await api('/close',{method:'POST',headers:headers(),body:JSON.stringify({hwnd})}); setState(data.ok?'已发送关闭窗口请求':'关闭失败',data.ok?'ok':'err'); boostTaskbarPolling(7000); setTimeout(refresh,350); setTimeout(refresh,1200);}catch(err){setState('关闭失败：'+(err.body&&err.body.error||err.message),'err');}}
function formatSize(bytes){const n=Number(bytes||0); if(n<1024) return n+' B'; if(n<1048576) return (n/1024).toFixed(1)+' KB'; if(n<1073741824) return (n/1048576).toFixed(1)+' MB'; return (n/1073741824).toFixed(1)+' GB';}
function formatTime(seconds){if(!seconds) return '-'; return new Date(seconds*1000).toLocaleString();}
function escapeHtml(text){return (text||'').replace(/[&<>"']/g,(c)=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
$('loginBtn').addEventListener('click',login);
$('logoutBtn').addEventListener('click',logout);
$('refreshBtn').addEventListener('click',refresh);
$('fileRefreshBtn').addEventListener('click',()=>loadFiles(currentPath));
$('desktopBtn').addEventListener('click',()=>loadFiles(''));
$('upBtn').addEventListener('click',()=>loadFiles(currentParent));
$('confirmToggle').addEventListener('change',saveConfirmSetting);
$('password').addEventListener('keydown',(event)=>{if(event.key==='Enter') login();});
$('filter').addEventListener('input',()=>{if(loggedIn) refresh();});
window.addEventListener('focus',()=>{if(loggedIn) refresh();});
document.addEventListener('visibilitychange',()=>{if(loggedIn&&!document.hidden) refresh();});
loadConfirmSetting();
checkSession();
setInterval(()=>{pollTaskbar();},250);
</script>
</body>
</html>"""

# ----------------------------------------------------------------
#  HTTP Handler
# ----------------------------------------------------------------

class KillHandler(BaseHTTPRequestHandler):
    server_version = f"{APP_NAME}/{VERSION}"

    @property
    def config(self):
        return self.server.config

    def _remote(self) -> str:
        return self.client_address[0]

    def _cookies(self) -> dict:
        raw = self.headers.get("Cookie", "")
        out = {}
        for item in raw.replace(" ", "").split(";"):
            if "=" in item:
                k, v = item.split("=", 1)
                out[k] = v
        return out

    def _sign_session_payload(self, payload_text: str) -> str:
        import base64

        digest = hmac.new(
            self.config["session_secret"].encode("utf-8"),
            payload_text.encode("utf-8"),
            hashlib.sha256,
        ).digest()
        return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")

    def _session_ok(self) -> bool:
        secret = self.config.get("session_secret", "")
        if not secret:
            return False
        raw = self._cookies().get(SESSION_COOKIE, "")
        if "." not in raw:
            return False
        payload_text, sig = raw.rsplit(".", 1)
        expected = self._sign_session_payload(payload_text)
        if not hmac.compare_digest(sig, expected):
            return False
        try:
            payload = json.loads(b64url_decode(payload_text).decode("utf-8"))
            return int(payload.get("exp", 0)) >= int(time.time())
        except Exception:
            return False

    def _auth_ok(self) -> tuple:
        remote = self._remote()
        try:
            if ipaddress.ip_address(remote).is_loopback:
                return True, ""
        except ValueError:
            pass
        if self._session_ok():
            return True, ""
        return False, "login required"

    def _require_auth(self) -> bool:
        ok, reason = self._auth_ok()
        if ok:
            return True
        self._send_json({"error": reason}, 401)
        return False

    def _send_json(self, data: dict, status: int = 200) -> None:
        body = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length).decode("utf-8", errors="replace")
        try:
            return json.loads(raw or "{}")
        except json.JSONDecodeError:
            return {}

    # ----- Routes -----

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Authorization, X-Gateway-Token, Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.end_headers()

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        if path == "/":
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            body = PAGE.encode("utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/favicon.ico":
            self.send_response(204)
            self.end_headers()
            return
        if path == "/health":
            self._send_json({"ok": True, "app": APP_NAME, "version": VERSION})
            return
        if not self._require_auth():
            return
        if path in ("/processes", "/taskbar"):
            query = urllib.parse.parse_qs(parsed.query)
            try:
                limit = int(query.get("limit", ["80"])[0])
            except (TypeError, ValueError):
                limit = 80
            apps = list_taskbar_apps(
                query=query.get("query", [""])[0],
                limit=limit,
            )
            self._send_json({"processes": apps, "apps": apps})
            return
        if path == "/files":
            query = urllib.parse.parse_qs(parsed.query)
            try:
                data = list_desktop_items(query.get("path", [""])[0])
            except ValueError as exc:
                self._send_json({"error": str(exc)}, 400)
                return
            except Exception as exc:
                self._send_json({"error": str(exc)}, 500)
                return
            self._send_json(data)
            return
        self._send_json({"error": "not found"}, 404)

    def do_POST(self) -> None:
        path = urllib.parse.urlparse(self.path).path.rstrip("/") or "/"
        if path == "/login":
            pw_hash = self.config.get("password_hash", "")
            if not pw_hash:
                self._send_json({"error": "password login not configured"}, 404)
                return
            body = self._read_json()
            password = body.get("password", "")
            if not verify_password(password, pw_hash):
                self._send_json({"error": "incorrect password"}, 401)
                return
            session_days = int(self.config.get("session_days", 90) or 90)
            exp = int(time.time()) + session_days * 86400
            expires = time.strftime("%a, %d %b %Y %H:%M:%S GMT", time.gmtime(exp))
            payload = json.dumps({"exp": exp, "ts": int(time.time())})
            payload64 = base64_url_encode(payload)
            sig = self._sign_session_payload(payload64)
            cookie = f"{payload64}.{sig}"
            self._send_json(
                {"ok": True, "message": "logged in"},
                extra_headers={
                    "Set-Cookie": (
                        f"{SESSION_COOKIE}={cookie}; "
                        f"Path=/; HttpOnly; SameSite=Lax; Max-Age={session_days * 86400}; Expires={expires}"
                    )
                },
            )
            return
        if path == "/logout":
            self._send_json(
                {"ok": True},
                extra_headers={
                    "Set-Cookie": f"{SESSION_COOKIE}=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0"
                },
            )
            return
        if not self._require_auth():
            return
        if path == "/kill":
            body = self._read_json()
            pid = body.get("pid", 0)
            if not pid:
                self._send_json({"error": "pid is required"}, 400)
                return
            try:
                ok = kill_taskbar_app(int(pid))
            except ValueError as exc:
                self._send_json({"error": str(exc)}, 400)
                return
            self._send_json({"ok": ok, "detail": f"pid={pid}"}, 200 if ok else 500)
            return
        if path == "/close":
            body = self._read_json()
            hwnd = str(body.get("hwnd") or "")
            if not hwnd:
                self._send_json({"error": "hwnd is required"}, 400)
                return
            try:
                ok = close_taskbar_window(hwnd)
            except ValueError as exc:
                self._send_json({"error": str(exc)}, 400)
                return
            self._send_json({"ok": ok, "detail": f"hwnd={hwnd}"}, 200 if ok else 500)
            return
        if path == "/files/open":
            body = self._read_json()
            item_path = str(body.get("path") or "")
            if not item_path:
                self._send_json({"error": "path is required"}, 400)
                return
            try:
                ok = open_desktop_item(item_path)
            except ValueError as exc:
                self._send_json({"error": str(exc)}, 400)
                return
            except Exception as exc:
                self._send_json({"error": str(exc)}, 500)
                return
            self._send_json({"ok": ok, "detail": item_path}, 200 if ok else 500)
            return
        self._send_json({"error": "not found"}, 404)

    def _send_json(self, data: dict, status: int = 200, extra_headers: dict = None) -> None:
        body = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def base64_url_encode(s: str) -> str:
    import base64
    return base64.urlsafe_b64encode(s.encode("utf-8")).decode("ascii").rstrip("=")


def verify_password(password: str, ph: str) -> bool:
    """Supports pbkdf2_sha256 as generated by the main gateway."""
    if not ph or "$" not in ph:
        return False
    parts = ph.split("$", 3)
    if len(parts) != 4:
        return False
    algo, iterations, salt_b64, hash_b64 = parts
    if algo != "pbkdf2_sha256":
        return False
    salt = base64_url_decode_compat(salt_b64)
    try:
        iters = int(iterations)
    except ValueError:
        return False
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iters)
    expected = base64_url_decode_compat(hash_b64)
    return hmac.compare_digest(dk, expected)


def base64_url_decode_compat(s: str) -> bytes:
    """Decode both urlsafe and standard base64."""
    s = s.replace("-", "+").replace("_", "/")
    padding = 4 - len(s) % 4
    if padding != 4:
        s += "=" * padding
    import base64
    return base64.b64decode(s)


# ----------------------------------------------------------------
#  Main
# ----------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Kill-only Gateway")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=9091)
    parser.add_argument("--auth-file", default="")
    args = parser.parse_args()

    # Default auth file
    auth_file = args.auth_file or os.path.join(
        os.environ.get("APPDATA", ""), "ClaudeCodeGateway", "auth.json"
    )
    auth = load_auth_config(auth_file)

    if not auth.get("password_hash") and not auth.get("session_secret"):
        print(f"[KILL GATEWAY] WARNING: No auth config at {auth_file}")
    else:
        print(f"[KILL GATEWAY] Auth loaded from {auth_file}")

    server = ThreadingHTTPServer((args.host, args.port), KillHandler)
    server.config = auth
    print(f"[KILL GATEWAY] Listening on http://{args.host}:{args.port}/")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("[KILL GATEWAY] Stopped")


if __name__ == "__main__":
    main()
