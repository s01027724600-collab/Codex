#!/usr/bin/env python3
import argparse
import base64
import ctypes
import hashlib
import hmac
import ipaddress
import io
import json
import os
import subprocess
import sys
import time
import urllib.parse
import threading
from ctypes import wintypes
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from cursor_highlight import CursorHighlight
from reference_lines import ReferenceLines

try:
    from PIL import Image, ImageGrab
except ImportError:
    Image = ImageGrab = None

APP_NAME = "claude-code-gateway-touchpad"
VERSION = "0.2.7"
SESSION_COOKIE = "gateway_session"

MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_WHEEL = 0x0800
MOUSEEVENTF_VIRTUALDESK = 0x4000
MOUSEEVENTF_ABSOLUTE = 0x8000


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", _MOUSEINPUT)]


class _INPUT(ctypes.Structure):
    _anonymous_ = ("union",)
    _fields_ = [("type", wintypes.DWORD), ("union", _INPUTUNION)]


def load_json(path: str) -> dict:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except Exception:
        return {}


def app_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def resource_path(name: str) -> Path:
    """Locate bundled UI resources in source and PyInstaller builds."""
    candidates = []
    bundle_dir = getattr(sys, "_MEIPASS", "")
    if bundle_dir:
        candidates.append(Path(bundle_dir) / name)
    candidates.append(app_dir() / name)
    candidates.append(Path(__file__).resolve().parent / name)
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return candidates[0]


def b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def b64url_decode(text: str) -> bytes:
    text = text.replace("-", "+").replace("_", "/")
    padding = 4 - len(text) % 4
    if padding != 4:
        text += "=" * padding
    return base64.b64decode(text)


def verify_password(password: str, password_hash: str) -> bool:
    parts = (password_hash or "").split("$", 3)
    if len(parts) != 4:
        return False
    algo, iterations, salt_b64, hash_b64 = parts
    if algo != "pbkdf2_sha256":
        return False
    try:
        salt = b64url_decode(salt_b64)
        expected = b64url_decode(hash_b64)
        rounds = int(iterations)
    except Exception:
        return False
    actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, rounds)
    return hmac.compare_digest(actual, expected)


def run_uia_scan_powershell(script_path: str, max_items: int, timeout_seconds: float) -> dict:
    if os.name != "nt":
        return {"ok": False, "error": "UI scan only supports Windows", "items": []}
    if not Path(script_path).exists():
        return {"ok": False, "error": f"scan script not found: {script_path}", "items": []}
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    completed = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            script_path,
            "-MaxItems",
            str(max(1, int(max_items))),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8-sig",
        errors="replace",
        timeout=max(1.0, float(timeout_seconds)),
        creationflags=creationflags,
    )
    raw = (completed.stdout or "").strip()
    if not raw:
        raw = (completed.stderr or "").strip()
        return {"ok": False, "error": raw or "UI scan returned no output", "items": []}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {"ok": False, "error": raw[:500], "items": []}
    if not isinstance(data, dict):
        return {"ok": False, "error": "UI scan returned invalid data", "items": []}
    return data


class UiaScanWorker:
    def __init__(self, exe_path: str, fallback_script_path: str) -> None:
        self.exe_path = exe_path
        self.fallback_script_path = fallback_script_path
        self.proc = None
        self.last_snapshot = {"ok": True, "engine": "python-empty-snapshot", "items": []}
        self.lock = threading.Lock()

    def _start(self) -> bool:
        if os.name != "nt" or not Path(self.exe_path).exists():
            return False
        if self.proc and self.proc.poll() is None:
            return True
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            self.proc = subprocess.Popen(
                [self.exe_path],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8-sig",
                errors="replace",
                bufsize=1,
                creationflags=creationflags,
            )
            return True
        except Exception:
            self.proc = None
            return False

    def _stop(self) -> None:
        proc = self.proc
        self.proc = None
        if not proc:
            return
        try:
            if proc.poll() is None and proc.stdin:
                proc.stdin.write("quit\n")
                proc.stdin.flush()
        except Exception:
            pass
        try:
            proc.wait(timeout=0.5)
        except Exception:
            try:
                proc.kill()
                proc.wait(timeout=1)
            except Exception:
                pass
        if proc.poll() is not None:
            for stream in (proc.stdin, proc.stdout):
                if stream:
                    stream.close()

    def stop(self) -> None:
        with self.lock:
            self._stop()

    def _read_line(self, out: list) -> None:
        try:
            out.append(self.proc.stdout.readline() if self.proc and self.proc.stdout else "")
        except Exception as exc:
            out.append(json.dumps({"ok": False, "error": str(exc), "items": []}))

    def scan(self, max_items: int, timeout_seconds: float, visit_limit: int = 160) -> dict:
        with self.lock:
            if self._start():
                try:
                    assert self.proc is not None and self.proc.stdin is not None
                    self.proc.stdin.write(f"scan {max(1, int(max_items))} {max(40, int(visit_limit))}\n")
                    self.proc.stdin.flush()
                    lines = []
                    reader = threading.Thread(target=self._read_line, args=(lines,), daemon=True)
                    reader.start()
                    reader.join(max(0.25, float(timeout_seconds)))
                    if not lines:
                        self._stop()
                        return {"ok": False, "error": "C# UI scan worker timed out", "items": []}
                    raw = (lines[0] or "").strip()
                    if not raw:
                        self._stop()
                        return {"ok": False, "error": "C# UI scan worker returned no output", "items": []}
                    data = json.loads(raw)
                    if isinstance(data, dict):
                        return data
                except Exception as exc:
                    self._stop()
                    worker_error = str(exc)
                else:
                    worker_error = "C# UI scan worker returned invalid data"
            else:
                worker_error = "C# UI scan worker is not available"

        fallback = run_uia_scan_powershell(self.fallback_script_path, max_items, timeout_seconds)
        if fallback.get("ok"):
            fallback["engine"] = fallback.get("engine") or "powershell-uia-fallback"
            fallback["workerFallbackReason"] = worker_error
        return fallback

    def snapshot(self, max_items: int, timeout_seconds: float, visit_limit: int = 160, interval_seconds: float = 1.6) -> dict:
        with self.lock:
            if self._start():
                try:
                    assert self.proc is not None and self.proc.stdin is not None
                    interval_ms = max(250, int(float(interval_seconds) * 1000))
                    self.proc.stdin.write(
                        f"snapshot {max(1, int(max_items))} {max(40, int(visit_limit))} {interval_ms}\n"
                    )
                    self.proc.stdin.flush()
                    lines = []
                    reader = threading.Thread(target=self._read_line, args=(lines,), daemon=True)
                    reader.start()
                    reader.join(max(0.15, min(1.2, float(timeout_seconds))))
                    if not lines:
                        self._stop()
                        reader.join(.5)
                        cached = dict(self.last_snapshot)
                        cached["workerPending"] = True
                        return cached
                    raw = (lines[0] or "").strip()
                    if not raw:
                        cached = dict(self.last_snapshot)
                        cached["workerPending"] = True
                        return cached
                    data = json.loads(raw)
                    if isinstance(data, dict):
                        if data.get("ok") and not data.get("pending"):
                            self.last_snapshot = data
                        return data
                except Exception as exc:
                    cached = dict(self.last_snapshot)
                    cached["workerSnapshotError"] = str(exc)
                    return cached
            cached = dict(self.last_snapshot)
            cached["workerSnapshotError"] = "C# UI scan worker is not available"
            return cached

    def request_refresh(self, timeout_seconds: float = 3.0) -> None:
        with self.lock:
            if not self._start():
                return
            try:
                assert self.proc is not None and self.proc.stdin is not None
                self.proc.stdin.write("refresh-foreground\n")
                self.proc.stdin.flush()
                lines = []
                reader = threading.Thread(target=self._read_line, args=(lines,), daemon=True)
                reader.start()
                reader.join(max(0.15, float(timeout_seconds)))
                if not lines:
                    self._stop()
            except Exception:
                self._stop()

    def scan_foreground(self, max_items: int, timeout_seconds: float, visit_limit: int = 160) -> dict:
        with self.lock:
            if not self._start():
                return {"ok": False, "error": "C# UI scan worker is not available", "items": []}
            try:
                assert self.proc is not None and self.proc.stdin is not None
                self.proc.stdin.write(
                    f"scan-foreground {max(1, int(max_items))} {max(40, int(visit_limit))}\n"
                )
                self.proc.stdin.flush()
                lines = []
                reader = threading.Thread(target=self._read_line, args=(lines,), daemon=True)
                reader.start()
                reader.join(max(0.25, float(timeout_seconds)))
                if not lines:
                    self._stop()
                    cached = dict(self.last_snapshot)
                    cached["foregroundScanTimedOut"] = True
                    cached["degraded"] = True
                    return cached
                data = json.loads((lines[0] or "").strip())
                if isinstance(data, dict):
                    if data.get("ok") and not data.get("pending"):
                        self.last_snapshot = data
                    return data
            except Exception as exc:
                self._stop()
                cached = dict(self.last_snapshot)
                cached["foregroundScanError"] = str(exc)
                cached["degraded"] = True
                return cached
        cached = dict(self.last_snapshot)
        cached["foregroundScanInvalid"] = True
        cached["degraded"] = True
        return cached


def scan_fingerprint(data: dict) -> str:
    items = data.get("items") if isinstance(data, dict) else []
    if not isinstance(items, list):
        items = []
    lines = []
    for item in items:
        if not isinstance(item, dict):
            continue
        lines.append(
            "|".join(
                [
                    str(item.get("control", "")),
                    str(item.get("className", "")),
                    str(item.get("label", "")),
                    str(item.get("x", "")),
                    str(item.get("y", "")),
                    str(item.get("width", "")),
                    str(item.get("height", "")),
                ]
            )
        )
    return hashlib.sha1("\n".join(lines).encode("utf-8")).hexdigest()


class ScanAssist:
    def __init__(self, scan_worker: UiaScanWorker, pointer, config: dict) -> None:
        self.scan_worker = scan_worker
        self.pointer = pointer
        self.config = config
        self.enabled = False
        self.scanning = False
        self.scan_seq = 0
        self.revision = 0
        self.fingerprint = ""
        self.latest = {"ok": True, "items": [], "screen": self.pointer.screen()}
        self.last_scan_ms = 0
        self.last_error = ""
        self.last_changed = False
        self.thread = None
        self.stop_event = threading.Event()
        self.lock = threading.Lock()
        self.interaction_refresh_requested = False
        self.interaction_refresh_due = 0.0

    def start(self) -> dict:
        with self.lock:
            self.enabled = True
            self.stop_event.clear()
            if not self.thread or not self.thread.is_alive():
                self.thread = threading.Thread(target=self._loop, daemon=True)
                self.thread.start()
        return self.status(include_data=True)

    def stop(self) -> dict:
        with self.lock:
            self.enabled = False
            self.stop_event.set()
            self.interaction_refresh_requested = False
            self.interaction_refresh_due = 0.0
        if self.thread and self.thread is not threading.current_thread():
            self.thread.join(7)
        self.scan_worker.stop()
        return self.status(include_data=True)

    def notify_interaction(self, delayed_seconds: float = 0.9) -> None:
        with self.lock:
            if not self.enabled:
                return
            self.interaction_refresh_requested = True
            self.interaction_refresh_due = time.monotonic() + max(0.15, float(delayed_seconds))

    def status(self, include_data: bool = True) -> dict:
        with self.lock:
            out = {
                "ok": True,
                "enabled": self.enabled,
                "scanning": self.scanning,
                "scanSeq": self.scan_seq,
                "revision": self.revision,
                "changed": self.last_changed,
                "lastScanMs": self.last_scan_ms,
                "lastError": self.last_error,
            }
            if include_data:
                out["data"] = self.latest
            return out

    def _loop(self) -> None:
        while True:
            with self.lock:
                if not self.enabled:
                    return
                self.scanning = True
                now = time.monotonic()
                request_foreground = self.interaction_refresh_requested
                force_foreground = self.interaction_refresh_due > 0 and now >= self.interaction_refresh_due
                self.interaction_refresh_requested = False
                if force_foreground:
                    self.interaction_refresh_due = 0.0
            started = time.monotonic()
            scan_interval = max(0.6, float(self.config.get("assist_interval_seconds", 1.6) or 1.6))
            poll_interval = max(0.12, float(self.config.get("assist_cache_poll_seconds", 0.18) or 0.18))
            try:
                max_items = int(self.config.get("scan_max_items", 60) or 60)
                timeout_seconds = float(self.config.get("scan_timeout_seconds", 5) or 5)
                visit_limit = int(self.config.get("scan_visit_limit", 160) or 160)
                if force_foreground:
                    data = self.scan_worker.scan_foreground(max_items, timeout_seconds, visit_limit)
                else:
                    if request_foreground:
                        self.scan_worker.request_refresh()
                    data = self.scan_worker.snapshot(max_items, timeout_seconds, visit_limit, scan_interval)
                data["screen"] = self.pointer.screen()
                if data.get("pending"):
                    with self.lock:
                        self.scan_seq += 1
                        self.last_scan_ms = int((time.monotonic() - started) * 1000)
                        self.last_error = ""
                        self.last_changed = False
                        self.scanning = False
                    if self.stop_event.wait(poll_interval):
                        return
                    continue
                fp = scan_fingerprint(data)
                with self.lock:
                    self.scan_seq += 1
                    self.last_scan_ms = int((time.monotonic() - started) * 1000)
                    self.last_error = "" if data.get("ok") else str(data.get("error", "scan failed"))
                    changed = fp != self.fingerprint
                    self.last_changed = changed
                    if data.get("ok"):
                        self.latest = data
                    if changed:
                        self.fingerprint = fp
                        self.revision += 1
                    self.scanning = False
            except Exception as exc:
                with self.lock:
                    self.scan_seq += 1
                    self.last_scan_ms = int((time.monotonic() - started) * 1000)
                    self.last_error = str(exc)
                    self.last_changed = False
                    self.scanning = False

            if self.stop_event.wait(poll_interval):
                with self.lock:
                    self.scanning = False
                return


class PointerController:
    def __init__(self, config: dict = None) -> None:
        if os.name != "nt":
            raise RuntimeError("7050 touchpad control only supports Windows")
        self.user32 = ctypes.windll.user32
        self.user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(_INPUT), ctypes.c_int]
        self.user32.SendInput.restype = wintypes.UINT
        try:
            if not self.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
                self.user32.SetProcessDPIAware()
        except Exception:
            self.user32.SetProcessDPIAware()
        self.crosshair = CursorHighlight()
        self.guides = ReferenceLines(self.screen)
        self._input_lock = threading.RLock()
        self._held = False
        self._hold_deadline = 0.0
        self._watchdog_stop = threading.Event()
        self._watchdog = threading.Thread(target=self._watch_buttons, daemon=True,
                                          name="7050-button-watchdog")
        self._watchdog.start()

    def _watch_buttons(self) -> None:
        while not self._watchdog_stop.wait(.25):
            self.expire_buttons()

    def expire_buttons(self) -> None:
        with self._input_lock:
            if self._held and time.monotonic() >= self._hold_deadline:
                self.release_buttons()

    def renew_hold(self) -> dict:
        with self._input_lock:
            self.expire_buttons()
            if self._held:
                self._hold_deadline = time.monotonic() + 4.0
            return {"ok": True, "held": self._held}

    def close(self) -> None:
        self._watchdog_stop.set()
        self._watchdog.join(1)
        self.release_buttons()

    def screen(self) -> dict:
        sm_xvirtualscreen = 76
        sm_yvirtualscreen = 77
        sm_cxvirtualscreen = 78
        sm_cyvirtualscreen = 79
        x = int(self.user32.GetSystemMetrics(sm_xvirtualscreen))
        y = int(self.user32.GetSystemMetrics(sm_yvirtualscreen))
        width = int(self.user32.GetSystemMetrics(sm_cxvirtualscreen))
        height = int(self.user32.GetSystemMetrics(sm_cyvirtualscreen))
        if width <= 0 or height <= 0:
            x = 0
            y = 0
            width = int(self.user32.GetSystemMetrics(0))
            height = int(self.user32.GetSystemMetrics(1))
        return {"x": x, "y": y, "width": width, "height": height}

    def cursor(self) -> dict:
        point = wintypes.POINT()
        self.user32.GetCursorPos(ctypes.byref(point))
        return {"x": int(point.x), "y": int(point.y)}

    def _send_mouse(self, *events: tuple) -> None:
        inputs = (_INPUT * len(events))()
        for index, event in enumerate(events):
            flags, dx, dy, data = event
            inputs[index].type = 0
            inputs[index].mi = _MOUSEINPUT(
                int(dx), int(dy), int(data) & 0xFFFFFFFF, int(flags), 0, 0)
        sent = int(self.user32.SendInput(len(inputs), inputs, ctypes.sizeof(_INPUT)))
        if sent != len(inputs):
            raise OSError(f"SendInput injected {sent} of {len(inputs)} mouse events")

    def absolute_point(self, nx: float, ny: float) -> tuple:
        screen = self.screen()
        nx = max(0.0, min(1.0, float(nx)))
        ny = max(0.0, min(1.0, float(ny)))
        x = int(screen["x"] + nx * max(1, screen["width"] - 1))
        y = int(screen["y"] + ny * max(1, screen["height"] - 1))
        return x, y

    def move_absolute(self, nx: float, ny: float) -> dict:
        with self._input_lock:
            nx = max(0.0, min(1.0, float(nx)))
            ny = max(0.0, min(1.0, float(ny)))
            self._send_mouse((MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE |
                              MOUSEEVENTF_VIRTUALDESK,
                              round(nx * 65535), round(ny * 65535), 0))
            cursor = self.cursor()
        return {"ok": True, **cursor}

    def move_relative(self, dx: float, dy: float, sensitivity: float) -> dict:
        with self._input_lock:
            cursor = self.cursor()
            screen = self.screen()
            x = max(screen["x"], min(screen["x"] + screen["width"] - 1,
                                     round(cursor["x"] + float(dx) * float(sensitivity))))
            y = max(screen["y"], min(screen["y"] + screen["height"] - 1,
                                     round(cursor["y"] + float(dy) * float(sensitivity))))
            nx = (x - screen["x"]) / max(1, screen["width"] - 1)
            ny = (y - screen["y"]) / max(1, screen["height"] - 1)
            self._send_mouse((MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE |
                              MOUSEEVENTF_VIRTUALDESK,
                              round(nx * 65535), round(ny * 65535), 0))
            cursor = self.cursor()
        return {"ok": True, **cursor}

    def set_crosshair(self, enabled: bool) -> dict:
        return self.crosshair.set_enabled(enabled)

    def set_guides(self, enabled: bool) -> dict:
        return self.guides.set_enabled(enabled)

    def button(self, action: str) -> dict:
        with self._input_lock:
            if action == "down":
                self._send_mouse((MOUSEEVENTF_LEFTDOWN, 0, 0, 0))
                self._held = True
                self._hold_deadline = time.monotonic() + 4.0
            elif action == "up":
                self._send_mouse((MOUSEEVENTF_LEFTUP, 0, 0, 0))
                self._held = False
            elif action == "click":
                self._send_mouse(
                    (MOUSEEVENTF_LEFTDOWN, 0, 0, 0),
                    (MOUSEEVENTF_LEFTUP, 0, 0, 0),
                )
            elif action == "dblclick":
                for _ in range(2):
                    self._send_mouse(
                        (MOUSEEVENTF_LEFTDOWN, 0, 0, 0),
                        (MOUSEEVENTF_LEFTUP, 0, 0, 0),
                    )
                    time.sleep(0.04)
            elif action == "rightclick":
                self._send_mouse(
                    (MOUSEEVENTF_RIGHTDOWN, 0, 0, 0),
                    (MOUSEEVENTF_RIGHTUP, 0, 0, 0),
                )
            else:
                raise ValueError("unknown button action")
        return {"ok": True, "action": action}

    def release_buttons(self) -> dict:
        with self._input_lock:
            self._send_mouse(
                (MOUSEEVENTF_LEFTUP, 0, 0, 0),
                (MOUSEEVENTF_RIGHTUP, 0, 0, 0),
            )
            self._held = False
            self._hold_deadline = 0.0
        return {"ok": True}

    def wheel(self, delta: int) -> dict:
        with self._input_lock:
            self._send_mouse((MOUSEEVENTF_WHEEL, 0, 0, int(delta)))
        return {"ok": True, "delta": int(delta)}


class PointerPreview:
    """One on-demand, shared frame; never retain full-resolution screenshots."""

    def __init__(self, pointer: PointerController) -> None:
        self.pointer = pointer
        self._lock = threading.Lock()
        self._last_attempt = 0.0
        self._cached = None
        self._last_error = ""
        self._min_interval = 1.0 / 3.0
        self._crop = None
        self._crop_screen = None

    @staticmethod
    def _jpeg(image) -> str:
        with io.BytesIO() as buffer:
            image.save(buffer, format="JPEG", quality=65)
            return "data:image/jpeg;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")

    def _detail_crop(self, screen: dict, cursor: dict) -> dict:
        width = min(720, screen["width"])
        height = min(420, screen["height"])
        previous = self._crop if self._crop_screen == screen else None
        left = cursor["x"] - width // 2
        top = cursor["y"] - height // 2
        if previous is not None:
            # Follow only after leaving the central 60%, keeping small movements steady.
            if previous["x"] + width * .2 <= cursor["x"] <= previous["x"] + width * .8:
                left = previous["x"]
            if previous["y"] + height * .2 <= cursor["y"] <= previous["y"] + height * .8:
                top = previous["y"]
        return {
            "x": max(screen["x"], min(left, screen["x"] + screen["width"] - width)),
            "y": max(screen["y"], min(top, screen["y"] + screen["height"] - height)),
            "width": width,
            "height": height,
        }

    def snapshot(self) -> dict:
        if ImageGrab is None:
            raise RuntimeError("pointer preview requires Pillow")
        with self._lock:
            if time.monotonic() - self._last_attempt < self._min_interval:
                if self._last_error:
                    raise RuntimeError(self._last_error)
                if self._cached is not None:
                    return self._cached
            self._last_attempt = time.monotonic()
            try:
                screen = self.pointer.screen()
                with ImageGrab.grab(all_screens=True) as desktop:
                    if desktop.size != (screen["width"], screen["height"]):
                        raise RuntimeError("desktop size changed; retry preview")
                    cursor = self.pointer.cursor()
                    crop = self._detail_crop(screen, cursor)
                    left = crop["x"] - screen["x"]
                    top = crop["y"] - screen["y"]
                    with desktop.crop((left, top, left + crop["width"], top + crop["height"])) as detail:
                        detail_data = self._jpeg(detail)
                    desktop.thumbnail((480, 300), Image.Resampling.BILINEAR)
                    overview_data = self._jpeg(desktop)
                result = {
                    "ok": True,
                    "screen": screen,
                    "cursor": cursor,
                    "overview": overview_data,
                    "detail": detail_data,
                    "crop": crop,
                    "capturedAt": int(time.time() * 1000),
                }
                self._cached = result
                self._crop = crop
                self._crop_screen = screen.copy()
                self._last_error = ""
                return result
            except Exception as exc:
                self._cached = None
                self._last_error = str(exc)
                raise RuntimeError(self._last_error) from exc


class Handler(BaseHTTPRequestHandler):
    server_version = f"{APP_NAME}/{VERSION}"

    def log_message(self, format: str, *args) -> None:
        return

    @property
    def config(self) -> dict:
        return self.server.config

    @property
    def pointer(self) -> PointerController:
        return self.server.pointer

    def _remote(self) -> str:
        return self.client_address[0]

    def _cookies(self) -> dict:
        out = {}
        for item in self.headers.get("Cookie", "").replace(" ", "").split(";"):
            if "=" in item:
                key, value = item.split("=", 1)
                out[key] = value
        return out

    def _sign(self, payload_text: str) -> str:
        digest = hmac.new(
            self.config.get("session_secret", "").encode("utf-8"),
            payload_text.encode("utf-8"),
            hashlib.sha256,
        ).digest()
        return b64url_encode(digest)

    def _session_ok(self) -> bool:
        secret = self.config.get("session_secret", "")
        if not secret:
            return False
        raw = self._cookies().get(SESSION_COOKIE, "")
        if "." not in raw:
            return False
        payload_text, sig = raw.rsplit(".", 1)
        if not hmac.compare_digest(self._sign(payload_text), sig):
            return False
        try:
            payload = json.loads(b64url_decode(payload_text).decode("utf-8"))
            return int(payload.get("exp", 0)) >= int(time.time())
        except Exception:
            return False

    def _auth_ok(self) -> bool:
        try:
            if ipaddress.ip_address(self._remote()).is_loopback:
                return True
        except ValueError:
            pass
        return self._session_ok()

    def _require_auth(self) -> bool:
        if self._auth_ok():
            return True
        self._send_json({"error": "login required"}, 401)
        return False

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(length).decode("utf-8", errors="replace")
        try:
            return json.loads(raw or "{}")
        except json.JSONDecodeError:
            return {}

    def _send_json(self, data: dict, status: int = 200, extra_headers: dict = None) -> None:
        body = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        path = urllib.parse.urlparse(self.path).path.rstrip("/") or "/"
        if path == "/":
            try:
                body = resource_path("ui.html").read_bytes()
            except OSError:
                self._send_json({"error": "ui resource not found"}, 500)
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
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
        if path == "/state":
            self._send_json({"ok": True, "screen": self.pointer.screen(), "cursor": self.pointer.cursor(),
                             "highlight": self.pointer.crosshair.status(), "guides": self.pointer.guides.status()})
            return
        if path == "/guides":
            self._send_json(self.pointer.guides.status())
            return
        if path == "/crosshair":
            self._send_json(self.pointer.crosshair.status())
            return
        if path == "/pointer-view":
            try:
                self._send_json(self.server.pointer_preview.snapshot())
            except Exception as exc:
                self._send_json({"ok": False, "error": str(exc)}, 503)
            return
        if path == "/assist":
            self._send_json(self.server.assist.status(include_data=True))
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
            if not verify_password(str(body.get("password", "")), pw_hash):
                self._send_json({"error": "incorrect password"}, 401)
                return
            session_days = int(self.config.get("session_days", 90) or 90)
            exp = int(time.time()) + session_days * 86400
            payload = b64url_encode(json.dumps({"exp": exp, "ts": int(time.time())}).encode("utf-8"))
            cookie = f"{payload}.{self._sign(payload)}"
            expires = time.strftime("%a, %d %b %Y %H:%M:%S GMT", time.gmtime(exp))
            self._send_json(
                {"ok": True},
                extra_headers={
                    "Set-Cookie": (
                        f"{SESSION_COOKIE}={cookie}; Path=/; HttpOnly; SameSite=Lax; "
                        f"Max-Age={session_days * 86400}; Expires={expires}"
                    )
                },
            )
            return
        if path == "/logout":
            self._send_json(
                {"ok": True},
                extra_headers={"Set-Cookie": f"{SESSION_COOKIE}=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0"},
            )
            return
        if not self._require_auth():
            return
        if path == "/assist":
            body = self._read_json()
            enabled = bool(body.get("enabled"))
            if enabled:
                self._send_json(self.server.assist.start())
            else:
                self._send_json(self.server.assist.stop())
            return
        if path == "/scan":
            self._read_json()
            data = self.server.scan_worker.scan(
                int(self.config.get("scan_max_items", 60) or 60),
                float(self.config.get("scan_timeout_seconds", 5) or 5),
                int(self.config.get("scan_visit_limit", 160) or 160),
            )
            data["screen"] = self.pointer.screen()
            self._send_json(data, 200 if data.get("ok") else 400)
            return
        body = self._read_json()
        try:
            if path == "/move":
                if body.get("mode") == "relative":
                    result = self.pointer.move_relative(
                        float(body.get("dx", 0) or 0),
                        float(body.get("dy", 0) or 0),
                        float(self.config.get("sensitivity", 1.35) or 1.35),
                    )
                else:
                    result = self.pointer.move_absolute(
                        float(body.get("nx", 0) or 0),
                        float(body.get("ny", 0) or 0),
                    )
                self._send_json(result)
                return
            if path == "/button":
                action = str(body.get("action", ""))
                result = self.pointer.button(action)
                if action in {"up", "click", "dblclick", "rightclick"}:
                    self.server.assist.notify_interaction()
                self._send_json(result)
                return
            if path == "/release":
                result = self.pointer.release_buttons()
                self.server.assist.notify_interaction()
                self._send_json(result)
                return
            if path == "/hold":
                self._send_json(self.pointer.renew_hold())
                return
            if path == "/wheel":
                result = self.pointer.wheel(int(body.get("delta", 0) or 0))
                self.server.assist.notify_interaction(0.65)
                self._send_json(result)
                return
            if path == "/crosshair":
                self._send_json(self.pointer.set_crosshair(body.get("enabled")))
                return
            if path == "/guides":
                self._send_json(self.pointer.set_guides(body.get("enabled")))
                return
        except Exception as exc:
            self._send_json({"error": str(exc)}, 400)
            return
        self._send_json({"error": "not found"}, 404)


def main() -> int:
    parser = argparse.ArgumentParser(description="Blind tablet touchpad gateway")
    parser.add_argument("--config", default="")
    parser.add_argument("--host", default="")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--auth-file", default="")
    args = parser.parse_args()

    config = load_json(args.config) if args.config else {}
    host = args.host or config.get("host") or "0.0.0.0"
    port = int(args.port or config.get("port") or 7050)
    auth_file = args.auth_file or config.get("auth_file") or os.path.join(
        os.environ.get("APPDATA", ""), "ClaudeCodeGateway", "auth.json"
    )
    config.update(load_json(auth_file))
    config["auth_file"] = auth_file
    if not config.get("password_hash"):
        print(f"[7050] WARNING: no password hash loaded from {auth_file}")

    server = ThreadingHTTPServer((host, port), Handler)
    server.config = config
    base_dir = app_dir()
    server.scan_worker = UiaScanWorker(
        str(base_dir / "uia_scan_worker.exe"),
        str(base_dir / "scan_uia.ps1"),
    )
    server.pointer = PointerController(config)
    server.pointer_preview = PointerPreview(server.pointer)
    server.assist = ScanAssist(server.scan_worker, server.pointer, config)
    print(f"[7050] Listening on http://{host}:{port}/")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.pointer.close()
        server.pointer.crosshair.close()
        server.pointer.guides.close()
        server.assist.stop()
        server.scan_worker._stop()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
