#!/usr/bin/env python3
"""
教学机多功能服务 (7000 端口)

功能一：课件自动归档
  - U 盘：检测可移动磁盘，扫描其中的 .pdf/.pptx，按卷标名分类归档，
          例如卷标为 "Lix" 的 U 盘 -> <target>/Lix的课件/开学第一课.pdf
  - 下载：监测下载目录新增的 .pdf/.pptx，归档到 <target>/下载的课件/

功能二：上课时间段定时截屏
  - 每分钟截一张，按小时分文件夹（1 小时一批、0 间隔），存到 <context_root>
  - 目前用计算机时间（全天不停），后续接入真实课表
  - 自动清理超过保留天数的截图

功能三：文件传输（供平板访问）
  - 浏览器访问 7000，浏览截图与课件，手动勾选后下载（单个直接下，多个打 zip）

无第三方依赖，仅用标准库 + Windows ctypes。
"""

import argparse
import base64
import ctypes
import datetime as dt
import hashlib
import hmac
import http.server
import ipaddress
import json
import mimetypes
import os
import platform
import queue
import re
import shutil
import signal
import string
import struct
import sys
import tempfile
import threading
import time
import urllib.parse
import zipfile
import zlib
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO, Dict, Iterable, List, Optional, Tuple

APP_NAME = "course-collector"
VERSION = "0.3.2"
SESSION_COOKIE = "gateway_session"
UI_FILE = Path(__file__).with_name("ui.html")


def b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def b64url_decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def verify_password(password: str, password_hash: str) -> bool:
    parts = (password_hash or "").split("$", 3)
    if len(parts) != 4 or parts[0] != "pbkdf2_sha256":
        return False
    try:
        rounds = int(parts[1])
        salt = b64url_decode(parts[2])
        expected = b64url_decode(parts[3])
    except (ValueError, TypeError):
        return False
    actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, rounds)
    return hmac.compare_digest(actual, expected)

# Windows GetDriveTypeW 返回值
DRIVE_REMOVABLE = 2   # 可移动磁盘（U 盘）
DRIVE_FIXED = 3       # 本地硬盘

# 浏览器下载进行中常见的临时后缀，拷贝前跳过
TEMP_SUFFIXES = {
    ".crdownload", ".partial", ".part", ".tmp", ".temp",
    ".download", ".opdownload",
}

# 文件名非法字符（用于把卷标名清洗成安全的目录名）
INVALID_NAME_CHARS = re.compile(r'[\\/:*?"<>|]')

# 截屏文件夹命名格式：年-月-日_时（每小时一批）
HOUR_DIR_RE = re.compile(r"^\d{4}-\d{2}-\d{2}_\d{2}$")


def is_windows() -> bool:
    return platform.system().lower() == "windows"


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def local_now() -> str:
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def ensure_background_stdio() -> None:
    if sys.stdin is None:
        sys.stdin = open(os.devnull, "r", encoding="utf-8", errors="replace")
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8", errors="replace")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8", errors="replace")


ensure_background_stdio()


def atomic_text(path: Path, text: str) -> None:
    """原子写入文本文件（先写临时文件再替换）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def atomic_bytes(path: Path, data: bytes) -> None:
    """原子写入二进制文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def sanitize_name(name: str, fallback: str) -> str:
    """把卷标名清洗成安全的目录名。空或含非法字符时用兜底名。"""
    name = (name or "").strip()
    name = INVALID_NAME_CHARS.sub("_", name)
    name = name.strip().strip(".")
    if not name:
        return fallback
    return name


def file_sha256(path: Path) -> Optional[str]:
    """计算文件内容 SHA-256（去重指纹）。读取失败返回 None。"""
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            while True:
                chunk = f.read(1 << 20)
                if not chunk:
                    break
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def is_temp_download(path: Path) -> bool:
    return path.suffix.lower() in TEMP_SUFFIXES


IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp"}
PROJECTABLE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".bmp"}
MAX_ZIP_FILES = 500
MAX_POST_BYTES = 1 << 20


def is_image_name(name: str) -> bool:
    return Path(name).suffix.lower() in IMAGE_EXTS


def human_size(num: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if num < 1024 or unit == "TB":
            return f"{num:.1f}{unit}" if unit != "B" else f"{int(num)}B"
        num /= 1024
    return f"{num:.1f}TB"


# ---------------------------------------------------------------------------
# Windows 盘符探测（ctypes，无第三方依赖）
# ---------------------------------------------------------------------------

_k32 = _u32 = _g32 = None

if is_windows():
    _k32 = ctypes.windll.kernel32
    _k32.GetDriveTypeW.argtypes = [ctypes.c_wchar_p]
    _k32.GetDriveTypeW.restype = ctypes.c_uint
    _k32.GetVolumeInformationW.argtypes = [
        ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint,
        ctypes.POINTER(ctypes.c_uint), ctypes.POINTER(ctypes.c_uint),
        ctypes.POINTER(ctypes.c_uint), ctypes.c_wchar_p, ctypes.c_uint,
    ]
    _k32.GetVolumeInformationW.restype = ctypes.c_int


def get_drive_type(root: str) -> int:
    if not is_windows():
        return 0
    return int(_k32.GetDriveTypeW(root))


def get_volume_label(root: str) -> str:
    """返回卷标名；失败返回空字符串。"""
    if not is_windows():
        return ""
    vol = ctypes.create_unicode_buffer(261)
    fs = ctypes.create_unicode_buffer(261)
    serial = ctypes.c_uint(0)
    maxlen = ctypes.c_uint(0)
    flags = ctypes.c_uint(0)
    ok = _k32.GetVolumeInformationW(
        root, vol, 261, ctypes.byref(serial),
        ctypes.byref(maxlen), ctypes.byref(flags), fs, 261,
    )
    if ok:
        return vol.value.strip()
    return ""


def list_removable_drives() -> List[Tuple[str, str]]:
    """返回 [(盘符根路径, 卷标), ...]，仅可移动磁盘。"""
    result: List[Tuple[str, str]] = []
    for letter in string.ascii_uppercase:
        root = f"{letter}:\\"
        if get_drive_type(root) == DRIVE_REMOVABLE:
            label = get_volume_label(root)
            result.append((root, label))
    return result


# ---------------------------------------------------------------------------
# 剪切板（文本，Win32 API）
# ---------------------------------------------------------------------------

CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002


def _clipboard_apis():
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    user32.OpenClipboard.argtypes = [ctypes.c_void_p]
    user32.OpenClipboard.restype = ctypes.c_int
    user32.CloseClipboard.restype = ctypes.c_int
    user32.EmptyClipboard.restype = ctypes.c_int
    user32.GetClipboardData.argtypes = [ctypes.c_uint]
    user32.GetClipboardData.restype = ctypes.c_void_p
    user32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]
    user32.SetClipboardData.restype = ctypes.c_void_p
    kernel32.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
    kernel32.GlobalAlloc.restype = ctypes.c_void_p
    kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalUnlock.restype = ctypes.c_int
    return user32, kernel32


def get_clipboard_text() -> str:
    """读取教学机剪切板文本；失败或非文本返回空字符串。"""
    if not is_windows():
        return ""
    user32, kernel32 = _clipboard_apis()
    if not user32.OpenClipboard(None):
        return ""
    try:
        h = user32.GetClipboardData(CF_UNICODETEXT)
        if not h:
            return ""
        p = kernel32.GlobalLock(h)
        if not p:
            return ""
        try:
            return ctypes.wstring_at(p)
        finally:
            kernel32.GlobalUnlock(h)
    finally:
        user32.CloseClipboard()


def set_clipboard_text(text: str) -> bool:
    """把文本写入教学机剪切板；成功返回 True。"""
    if not is_windows():
        return False
    user32, kernel32 = _clipboard_apis()
    if not user32.OpenClipboard(None):
        return False
    try:
        user32.EmptyClipboard()
        data = text.encode("utf-16-le") + b"\x00\x00"
        h = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
        if not h:
            return False
        p = kernel32.GlobalLock(h)
        if not p:
            return False
        ctypes.memmove(p, data, len(data))
        kernel32.GlobalUnlock(h)
        user32.SetClipboardData(CF_UNICODETEXT, h)
        return True
    finally:
        user32.CloseClipboard()


# ---------------------------------------------------------------------------
# 屏幕截图（GDI 抓屏 + 手写 PNG 编码，无第三方依赖）
# ---------------------------------------------------------------------------

if is_windows():
    _u32 = ctypes.windll.user32
    _g32 = ctypes.windll.gdi32

    # user32 —— 显式声明类型，避免 64 位句柄被默认 c_int 截断
    _u32.SetProcessDPIAware.restype = ctypes.c_int
    _u32.GetSystemMetrics.argtypes = [ctypes.c_int]
    _u32.GetSystemMetrics.restype = ctypes.c_int
    _u32.GetWindowDC.argtypes = [ctypes.c_void_p]
    _u32.GetWindowDC.restype = ctypes.c_void_p
    _u32.ReleaseDC.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    _u32.ReleaseDC.restype = ctypes.c_int

    # gdi32
    _g32.CreateCompatibleDC.argtypes = [ctypes.c_void_p]
    _g32.CreateCompatibleDC.restype = ctypes.c_void_p
    _g32.CreateCompatibleBitmap.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int]
    _g32.CreateCompatibleBitmap.restype = ctypes.c_void_p
    _g32.SelectObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    _g32.SelectObject.restype = ctypes.c_void_p
    _g32.BitBlt.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                            ctypes.c_int, ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_uint]
    _g32.BitBlt.restype = ctypes.c_int
    _g32.GetDIBits.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint, ctypes.c_uint,
                               ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint]
    _g32.GetDIBits.restype = ctypes.c_int
    _g32.DeleteObject.argtypes = [ctypes.c_void_p]
    _g32.DeleteObject.restype = ctypes.c_int
    _g32.DeleteDC.argtypes = [ctypes.c_void_p]
    _g32.DeleteDC.restype = ctypes.c_int


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", ctypes.c_uint32),
        ("biWidth", ctypes.c_int32),
        ("biHeight", ctypes.c_int32),
        ("biPlanes", ctypes.c_uint16),
        ("biBitCount", ctypes.c_uint16),
        ("biCompression", ctypes.c_uint32),
        ("biSizeImage", ctypes.c_uint32),
        ("biXPelsPerMeter", ctypes.c_int32),
        ("biYPelsPerMeter", ctypes.c_int32),
        ("biClrUsed", ctypes.c_uint32),
        ("biClrImportant", ctypes.c_uint32),
    ]


def _png_chunk(tag: bytes, data: bytes) -> bytes:
    return (struct.pack(">I", len(data)) + tag + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))


def _rgb_rows_to_png(width: int, height: int, rows: Iterable[bytes]) -> bytes:
    """rows: 自顶向下，每行 width*3 字节 RGB。"""
    compressor = zlib.compressobj(6)
    compressed: List[bytes] = []
    for row in rows:
        block = compressor.compress(b"\x00" + row)
        if block:
            compressed.append(block)
    compressed.append(compressor.flush())
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n"
            + _png_chunk(b"IHDR", ihdr)
            + _png_chunk(b"IDAT", b"".join(compressed))
            + _png_chunk(b"IEND", b""))


def _png_thumbnail(data: bytes, max_width: int = 480) -> Optional[bytes]:
    """为本程序生成的 RGB PNG 创建快速等比缩略图。其他 PNG 返回 None。"""
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        return None
    pos = 8
    width = height = 0
    idat: List[bytes] = []
    while pos + 12 <= len(data):
        length = struct.unpack(">I", data[pos:pos + 4])[0]
        tag = data[pos + 4:pos + 8]
        payload = data[pos + 8:pos + 8 + length]
        if len(payload) != length:
            return None
        if tag == b"IHDR":
            width, height, depth, color, comp, filt, interlace = struct.unpack(
                ">IIBBBBB", payload
            )
            if depth != 8 or color != 2 or comp or filt or interlace:
                return None
        elif tag == b"IDAT":
            idat.append(payload)
        elif tag == b"IEND":
            break
        pos += 12 + length
    if width <= max_width or height <= 0 or not idat:
        return data
    try:
        raw = zlib.decompress(b"".join(idat))
    except zlib.error:
        return None
    stride = width * 3
    if len(raw) != (stride + 1) * height:
        return None
    # 截图编码器逐行使用 PNG filter 0；不对其他来源做昂贵的通用解码。
    if any(raw[y * (stride + 1)] != 0 for y in range(height)):
        return None
    step = max(1, (width + max_width - 1) // max_width)
    out_width = (width + step - 1) // step
    out_height = (height + step - 1) // step

    def sampled_rows() -> Iterable[bytes]:
        for source_y in range(0, height, step):
            start = source_y * (stride + 1) + 1
            source = raw[start:start + stride]
            row = bytearray(out_width * 3)
            row[0::3] = source[0::step * 3]
            row[1::3] = source[1::step * 3]
            row[2::3] = source[2::step * 3]
            yield bytes(row)

    return _rgb_rows_to_png(out_width, out_height, sampled_rows())


def capture_screen() -> Optional[bytes]:
    """抓取主屏，返回 PNG 字节；失败返回 None。"""
    if not is_windows():
        return None
    try:
        _u32.SetProcessDPIAware()
    except Exception:
        pass

    width = int(_u32.GetSystemMetrics(0))
    height = int(_u32.GetSystemMetrics(1))
    if width <= 0 or height <= 0:
        return None

    srcdc = _u32.GetWindowDC(0)
    if not srcdc:
        return None
    memdc = _g32.CreateCompatibleDC(srcdc)
    bmp = _g32.CreateCompatibleBitmap(srcdc, width, height)
    if not memdc or not bmp:
        if bmp:
            _g32.DeleteObject(bmp)
        if memdc:
            _g32.DeleteDC(memdc)
        _u32.ReleaseDC(0, srcdc)
        return None
    old_obj = _g32.SelectObject(memdc, bmp)
    try:
        if not _g32.BitBlt(memdc, 0, 0, width, height, srcdc, 0, 0, 0x00CC0020):
            return None
        bmi = _BITMAPINFOHEADER()
        bmi.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
        bmi.biWidth, bmi.biHeight = width, -height
        bmi.biPlanes, bmi.biBitCount = 1, 32
        buf = ctypes.create_string_buffer(width * height * 4)
        if _g32.GetDIBits(memdc, bmp, 0, height, buf, ctypes.byref(bmi), 0) != height:
            return None
        data = memoryview(buf).cast("B")
        def rgb_rows() -> Iterable[bytes]:
            for y in range(height):
                source = data[y * width * 4:(y + 1) * width * 4]
                row = bytearray(width * 3)
                row[0::3], row[1::3], row[2::3] = bytes(source[2::4]), bytes(source[1::4]), bytes(source[0::4])
                yield bytes(row)
        return _rgb_rows_to_png(width, height, rgb_rows())
    finally:
        _g32.SelectObject(memdc, old_obj)
        _g32.DeleteObject(bmp)
        _g32.DeleteDC(memdc)
        _u32.ReleaseDC(0, srcdc)


class _GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_uint32),
        ("Data2", ctypes.c_uint16),
        ("Data3", ctypes.c_uint16),
        ("Data4", ctypes.c_ubyte * 8),
    ]


class _GdiplusStartupInput(ctypes.Structure):
    _fields_ = [
        ("GdiplusVersion", ctypes.c_uint32),
        ("DebugEventCallback", ctypes.c_void_p),
        ("SuppressBackgroundThread", ctypes.c_int),
        ("SuppressExternalCodecs", ctypes.c_int),
    ]


_PNG_ENCODER = _GUID(
    0x557CF406, 0x1A04, 0x11D3,
    (ctypes.c_ubyte * 8)(0x9A, 0x73, 0x00, 0x00, 0xF8, 0x1E, 0xF3, 0x2E),
)
_GDIPLUS_LOCK = threading.Lock()


def _gdiplus_scaled_png(source: Path, destination: Path, max_width: int,
                        max_height: int, allow_upscale: bool = False) -> bool:
    """用 Windows GDI+ 解码并等比缩放常见图片，输出 PNG。"""
    if not is_windows() or max_width <= 0 or max_height <= 0:
        return False
    with _GDIPLUS_LOCK:
        gdip = ctypes.windll.gdiplus
        token = ctypes.c_size_t()
        startup = _GdiplusStartupInput(1, None, 0, 0)
        if gdip.GdiplusStartup(ctypes.byref(token), ctypes.byref(startup), None) != 0:
            return False

        image = ctypes.c_void_p()
        bitmap = ctypes.c_void_p()
        graphics = ctypes.c_void_p()
        try:
            if gdip.GdipLoadImageFromFile(str(source), ctypes.byref(image)) != 0:
                return False
            width = ctypes.c_uint32()
            height = ctypes.c_uint32()
            if (gdip.GdipGetImageWidth(image, ctypes.byref(width)) != 0
                    or gdip.GdipGetImageHeight(image, ctypes.byref(height)) != 0
                    or width.value == 0 or height.value == 0):
                return False

            scale = min(max_width / width.value, max_height / height.value)
            if not allow_upscale:
                scale = min(1.0, scale)
            out_width = max(1, round(width.value * scale))
            out_height = max(1, round(height.value * scale))
            # PixelFormat32bppARGB
            if gdip.GdipCreateBitmapFromScan0(
                    out_width, out_height, 0, 0x26200A, None,
                    ctypes.byref(bitmap)) != 0:
                return False
            if gdip.GdipGetImageGraphicsContext(bitmap, ctypes.byref(graphics)) != 0:
                return False
            gdip.GdipSetInterpolationMode(graphics, 7)  # HighQualityBicubic
            gdip.GdipSetPixelOffsetMode(graphics, 4)    # Half
            gdip.GdipGraphicsClear(graphics, 0x00000000)
            if gdip.GdipDrawImageRectI(
                    graphics, image, 0, 0, out_width, out_height) != 0:
                return False
            gdip.GdipDeleteGraphics(graphics)
            graphics = ctypes.c_void_p()
            return gdip.GdipSaveImageToFile(
                bitmap, str(destination), ctypes.byref(_PNG_ENCODER), None
            ) == 0
        finally:
            if graphics:
                gdip.GdipDeleteGraphics(graphics)
            if bitmap:
                gdip.GdipDisposeImage(bitmap)
            if image:
                gdip.GdipDisposeImage(image)
            gdip.GdiplusShutdown(token)


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

@dataclass
class Config:
    host: str = "0.0.0.0"
    port: int = 7000
    target_root: str = r"D:\所有已知课件"
    context_root: str = r"D:\context"
    download_dirs: List[str] = field(default_factory=list)
    extensions: List[str] = field(default_factory=lambda: [".pdf", ".pptx"])
    scan_interval: int = 5          # 课件扫描间隔（秒）

    # 截屏相关
    screenshot_enabled: bool = True
    screenshot_interval_seconds: int = 60   # 每分钟一张
    screenshot_retention_days: int = 7      # 保留最近 N 天，0 表示不清理

    stable_seconds: int = 3         # 下载文件需静置这么久才算完成
    state_dir: str = ""
    auth_file: str = ""
    password_hash: str = ""
    session_secret: str = ""
    session_days: int = 90

    def __post_init__(self) -> None:
        if not self.download_dirs:
            dl = os.path.join(os.path.expanduser("~"), "Downloads")
            self.download_dirs = [dl]
        self.extensions = [
            e if e.startswith(".") else f".{e}" for e in self.extensions
        ]
        self.extensions = [e.lower() for e in self.extensions]

    @classmethod
    def from_file(cls, path: str) -> "Config":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        kwargs = {k: v for k, v in data.items() if k in known}
        return cls(**kwargs)

    def finalize(self) -> None:
        self.port = min(65535, max(1, int(self.port)))
        self.scan_interval = max(1, int(self.scan_interval))
        self.screenshot_interval_seconds = max(
            1, int(self.screenshot_interval_seconds)
        )
        self.screenshot_retention_days = max(
            0, int(self.screenshot_retention_days)
        )
        self.stable_seconds = max(0, int(self.stable_seconds))
        self.session_days = max(1, int(self.session_days))
        self.download_dirs = [str(path) for path in self.download_dirs]
        if not self.state_dir:
            base = os.environ.get("APPDATA") or tempfile.gettempdir()
            self.state_dir = str(Path(base) / "CourseCollector" / "state")


# ---------------------------------------------------------------------------
# 状态与去重
# ---------------------------------------------------------------------------

class State:
    """持有已归档文件的指纹（去重）与日志，持久化到 state 目录。"""

    def __init__(self, state_dir: str):
        self.dir = Path(state_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.seen_path = self.dir / "seen.json"
        self.file_index_path = self.dir / "file-index.json"
        self.log_path = self.dir / "log.jsonl"
        self.lock = threading.Lock()

        self.seen: Dict[str, dict] = {}
        self._load_seen()
        self.file_index: Dict[str, dict] = {}
        self._file_index_dirty = False
        self._load_file_index()

        self.log: deque = deque(maxlen=800)
        self._load_recent_log()

    def _load_seen(self) -> None:
        if self.seen_path.exists():
            try:
                self.seen = json.loads(self.seen_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                self.seen = {}

    def _save_seen(self) -> None:
        atomic_text(self.seen_path, json.dumps(self.seen, ensure_ascii=False, indent=2))

    def _load_file_index(self) -> None:
        if not self.file_index_path.exists():
            return
        try:
            data = json.loads(self.file_index_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                self.file_index = {
                    str(key): value for key, value in data.items()
                    if isinstance(value, dict)
                }
        except (OSError, json.JSONDecodeError):
            self.file_index = {}

    @staticmethod
    def _source_key(path: Path) -> str:
        return os.path.normcase(os.path.realpath(str(path)))

    def cached_digest(self, path: Path, stat: os.stat_result) -> Optional[str]:
        key = self._source_key(path)
        with self.lock:
            entry = self.file_index.get(key)
            if not entry:
                return None
            if (entry.get("size") != stat.st_size
                    or entry.get("mtime_ns") != stat.st_mtime_ns
                    or entry.get("ctime_ns") != stat.st_ctime_ns):
                return None
            now = int(time.time())
            if now - int(entry.get("checked", 0)) >= 86400:
                entry["checked"] = now
                self._file_index_dirty = True
            digest = entry.get("digest")
            return digest if isinstance(digest, str) else None

    def cache_digest(self, path: Path, stat: os.stat_result, digest: str) -> None:
        key = self._source_key(path)
        with self.lock:
            self.file_index[key] = {
                "size": stat.st_size,
                "mtime_ns": stat.st_mtime_ns,
                "ctime_ns": stat.st_ctime_ns,
                "digest": digest,
                "checked": int(time.time()),
            }
            self._file_index_dirty = True

    def flush_file_index(self) -> None:
        """一次扫描只落盘一次，并顺带清理长期消失的来源记录。"""
        with self.lock:
            if not self._file_index_dirty:
                return
            cutoff = int(time.time()) - 30 * 86400
            self.file_index = {
                key: value for key, value in self.file_index.items()
                if int(value.get("checked", 0)) >= cutoff
            }
            atomic_text(
                self.file_index_path,
                json.dumps(self.file_index, ensure_ascii=False, separators=(",", ":")),
            )
            self._file_index_dirty = False

    def _load_recent_log(self) -> None:
        if not self.log_path.exists():
            return
        try:
            lines = self.log_path.read_text(encoding="utf-8").splitlines()
            for line in lines[-800:]:
                if line.strip():
                    self.log.append(json.loads(line))
        except (OSError, json.JSONDecodeError):
            pass

    def is_seen(self, digest: str, target_root: Optional[Path] = None) -> bool:
        with self.lock:
            entry = self.seen.get(digest)
            if not entry:
                return False
            destination = entry.get("dst")
        if not destination:
            return False
        try:
            dst = Path(destination)
            if not dst.is_file():
                return False
            if target_root is not None:
                dst_norm = os.path.normcase(os.path.realpath(str(dst)))
                root_norm = os.path.normcase(os.path.realpath(str(target_root)))
                return os.path.commonpath((dst_norm, root_norm)) == root_norm
            return True
        except (OSError, ValueError):
            return False

    def mark_seen(self, digest: str, src: str, dst: str) -> None:
        with self.lock:
            self.seen[digest] = {"src": src, "dst": dst, "ts": utc_now()}
            self._save_seen()

    def add_log(self, level: str, message: str, **extra: object) -> None:
        entry = {"ts": local_now(), "level": level, "message": message}
        entry.update(extra)
        with self.lock:
            self.log.append(entry)
            try:
                with open(self.log_path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            except OSError:
                pass

    def snapshot_log(self, limit: int = 200) -> List[dict]:
        with self.lock:
            return list(self.log)[-limit:]


# ---------------------------------------------------------------------------
# 课件归档监测
# ---------------------------------------------------------------------------

class Monitor:
    """后台线程：周期扫描 U 盘与下载目录，归档新课件。"""

    def __init__(self, config: Config, state: State):
        self.config = config
        self.state = state
        self.stop_event = threading.Event()
        self.info_lock = threading.Lock()
        self._scan_lock = threading.Lock()
        self.current_drives: List[dict] = []
        self.last_scan_ts: str = ""

    def log(self, level: str, message: str, **extra: object) -> None:
        self.state.add_log(level, message, **extra)

    def _iter_candidates(self, root: str, recursive: bool):
        root_path = Path(root)
        if not root_path.is_dir():
            return
        it = root_path.rglob("*") if recursive else root_path.glob("*")
        for p in it:
            try:
                if not p.is_file():
                    continue
            except OSError:
                continue
            if p.suffix.lower() not in self.config.extensions:
                continue
            if is_temp_download(p):
                continue
            yield p

    def _copy_one(self, src: Path, category_dir: Path, source_label: str) -> Optional[str]:
        try:
            before = src.stat()
        except OSError:
            return None
        digest = self.state.cached_digest(src, before)
        if digest is None:
            digest = file_sha256(src)
            if digest is None:
                return None
            try:
                after_hash = src.stat()
            except OSError:
                return None
            if (after_hash.st_size != before.st_size
                    or after_hash.st_mtime_ns != before.st_mtime_ns):
                return None
            before = after_hash
            self.state.cache_digest(src, before, digest)
        if self.state.is_seen(digest, Path(self.config.target_root)):
            return None

        category_dir.mkdir(parents=True, exist_ok=True)
        dst = self._unique_dst(category_dir, src.name)
        temp_handle = tempfile.NamedTemporaryFile(
            prefix=".7000-", suffix=".partial", dir=category_dir, delete=False
        )
        temp_path = Path(temp_handle.name)
        temp_handle.close()
        try:
            shutil.copy2(src, temp_path)
            after_copy = src.stat()
            if (after_copy.st_size != before.st_size
                    or after_copy.st_mtime_ns != before.st_mtime_ns):
                return None
            os.replace(temp_path, dst)
        finally:
            temp_path.unlink(missing_ok=True)
        self.state.mark_seen(digest, str(src), str(dst))
        return str(dst)

    @staticmethod
    def _unique_dst(category_dir: Path, filename: str) -> Path:
        p = category_dir / filename
        if not p.exists():
            return p
        stem, suffix = p.stem, p.suffix
        i = 2
        while True:
            q = category_dir / f"{stem}({i}){suffix}"
            if not q.exists():
                return q
            i += 1

    def scan_once(self) -> bool:
        if not self._scan_lock.acquire(blocking=False):
            return False
        try:
            self._scan_once_unlocked()
            return True
        finally:
            try:
                self.state.flush_file_index()
            finally:
                self._scan_lock.release()

    def start_scan_async(self) -> bool:
        """仅在当前没有扫描时启动一个后台扫描。"""
        if not self._scan_lock.acquire(blocking=False):
            return False

        def work() -> None:
            try:
                self._scan_once_unlocked()
            except Exception as error:  # noqa: BLE001
                self.log("error", f"扫描异常: {error}", detail=repr(error))
            finally:
                try:
                    self.state.flush_file_index()
                except OSError as error:
                    self.log("error", f"保存扫描状态失败: {error}")
                finally:
                    self._scan_lock.release()

        try:
            threading.Thread(target=work, daemon=True).start()
        except Exception:
            self._scan_lock.release()
            raise
        return True

    def _scan_once_unlocked(self) -> None:
        target_root = Path(self.config.target_root)

        # 1) U 盘
        drives = list_removable_drives()
        drive_info: List[dict] = []
        for root, label in drives:
            if label and label not in ("", "可移动磁盘"):
                category_name = f"{label}的课件"
            else:
                category_name = f"U盘-{root[0]}"
            category_dir = target_root / category_name
            drive_info.append({"drive": root, "label": label, "category": category_name})

            count = 0
            for src in self._iter_candidates(root, recursive=True):
                try:
                    dst = self._copy_one(src, category_dir, label or root)
                except (OSError, shutil.Error) as e:
                    self.log("warn", f"拷贝失败（可能文件正被打开）: {src.name}",
                             source=label or root, category=category_name, detail=str(e))
                    continue
                if dst:
                    count += 1
                    self.log("ok", f"归档 U 盘课件: {src.name}",
                             source=label or root, category=category_name, file=src.name, dst=dst)
            if count:
                self.log("info", f"U 盘「{label or root}」本次归档 {count} 个课件",
                         source=label or root, category=category_name)

        with self.info_lock:
            self.current_drives = drive_info

        # 2) 下载目录
        for dl in self.config.download_dirs:
            if not os.path.isdir(dl):
                continue
            category_dir = target_root / "下载的课件"
            for src in self._iter_candidates(dl, recursive=False):
                try:
                    age = time.time() - src.stat().st_mtime
                except OSError:
                    continue
                if age < self.config.stable_seconds:
                    continue
                try:
                    dst = self._copy_one(src, category_dir, "下载")
                except (OSError, shutil.Error) as e:
                    self.log("warn", f"拷贝失败: {src.name}",
                             source="下载", category="下载的课件", detail=str(e))
                    continue
                if dst:
                    self.log("ok", f"归档下载课件: {src.name}",
                             source="下载", category="下载的课件", file=src.name, dst=dst)

        self.last_scan_ts = local_now()

    def run(self) -> None:
        self.log("info", "课件监测线程已启动")
        while not self.stop_event.is_set():
            try:
                self.scan_once()
            except Exception as e:  # noqa: BLE001
                self.log("error", f"扫描异常: {e}", detail=repr(e))
            self.stop_event.wait(self.config.scan_interval)


# ---------------------------------------------------------------------------
# 定时截屏
# ---------------------------------------------------------------------------

class ScreenCapturer:
    """后台线程：上课时间段每分钟截屏，按小时分文件夹，自动清理过期截图。"""

    def __init__(self, config: Config, state: State):
        self.config = config
        self.state = state
        self.stop_event = threading.Event()
        self.info_lock = threading.Lock()
        self._capture_lock = threading.Lock()
        self.info = {"last_shot": "", "current_dir": "", "shots": 0}
        self._last_cleanup = 0.0

    def _is_class_time(self, now: dt.datetime) -> bool:
        # 目前：全天（计算机时间）。后续接入真实课表后改写此处。
        return True

    def _wait_for_next_capture(self) -> None:
        interval = max(1, int(self.config.screenshot_interval_seconds))
        self.stop_event.wait(interval)

    def _capture(self) -> Optional[bytes]:
        # GDI 抓屏会暂时分配一块全屏像素缓冲，串行执行可避免请求叠加内存。
        with self._capture_lock:
            return capture_screen()

    def cleanup_old(self) -> None:
        days = self.config.screenshot_retention_days
        if days <= 0:
            return
        root = Path(self.config.context_root)
        if not root.is_dir():
            return
        cutoff = dt.datetime.now() - dt.timedelta(days=days)
        for d in root.iterdir():
            if not d.is_dir():
                continue
            if not HOUR_DIR_RE.match(d.name):
                continue
            try:
                t = dt.datetime.strptime(d.name, "%Y-%m-%d_%H")
            except ValueError:
                continue
            if t < cutoff:
                shutil.rmtree(d, ignore_errors=True)
                self.state.add_log("info", f"清理过期截图: {d.name}", source="截图")

    def run(self) -> None:
        if not self.config.screenshot_enabled:
            return
        self.state.add_log("info", "截屏线程已启动", source="截图")
        while not self.stop_event.is_set():
            now = dt.datetime.now()
            try:
                if not self._is_class_time(now):
                    self._wait_for_next_capture()
                    continue

                # 每小时顺带清理一次过期截图
                if time.time() - self._last_cleanup > 3600:
                    self.cleanup_old()
                    self._last_cleanup = time.time()

                hour_dir = Path(self.config.context_root) / now.strftime("%Y-%m-%d_%H")
                hour_dir.mkdir(parents=True, exist_ok=True)
                png = self._capture()
                if png is None:
                    self.state.add_log("warn", "截屏失败（可能无桌面/锁屏）", source="截图")
                else:
                    path = hour_dir / now.strftime("%H%M%S.png")
                    if not path.exists():
                        atomic_bytes(path, png)
                        with self.info_lock:
                            self.info["last_shot"] = now.strftime("%H:%M:%S")
                            self.info["current_dir"] = hour_dir.name
                            self.info["shots"] += 1
            except Exception as e:  # noqa: BLE001
                self.state.add_log("error", f"截屏异常: {e}", source="截图", detail=repr(e))
            self._wait_for_next_capture()

    def shoot_now(self) -> Optional[dict]:
        """立即截一张屏，保存到当前小时文件夹，返回路径信息。"""
        now = dt.datetime.now()
        hour_dir = Path(self.config.context_root) / now.strftime("%Y-%m-%d_%H")
        hour_dir.mkdir(parents=True, exist_ok=True)
        png = self._capture()
        if png is None:
            self.state.add_log("warn", "手动截图失败", source="截图")
            return None
        name = now.strftime("%H%M%S") + f"_{now.microsecond // 1000:03d}.png"
        path = hour_dir / name
        atomic_bytes(path, png)
        with self.info_lock:
            self.info["last_shot"] = now.strftime("%H:%M:%S")
            self.info["current_dir"] = hour_dir.name
            self.info["shots"] += 1
        self.state.add_log("ok", f"手动截图: {name}", source="截图")
        return {
            "path": str(path),
            "name": name,
            "dir": hour_dir.name,
            "url": "/api/file?path=" + urllib.parse.quote(str(path)),
        }

    def status(self) -> dict:
        with self.info_lock:
            info = dict(self.info)
        info["enabled"] = self.config.screenshot_enabled
        info["context_root"] = self.config.context_root
        info["retention_days"] = self.config.screenshot_retention_days
        info["interval_seconds"] = self.config.screenshot_interval_seconds
        return info


# ---------------------------------------------------------------------------
# 文件浏览与传输
# ---------------------------------------------------------------------------

class DisplayManager:
    """教学机全屏投放图片（tkinter 全屏窗口，独立线程，通过队列通信）。"""

    def __init__(self):
        self._queue: "queue.Queue" = queue.Queue()
        self._ready = threading.Event()
        self._root = None
        self._label = None
        self._img = None
        self._visible = False
        self._visible_lock = threading.Lock()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        self._ready.wait(timeout=5)

    def _run(self) -> None:
        import tkinter as tk
        try:
            self._root = tk.Tk()
        except Exception:
            self._ready.set()
            return
        self._root.overrideredirect(True)
        try:
            self._root.attributes("-fullscreen", True)
        except Exception:
            pass
        self._root.configure(bg="black")
        self._label = tk.Label(self._root, bg="black")
        self._label.pack(fill="both", expand=True)
        self._root.bind("<Escape>", lambda e: self._set_hidden())
        # 不再用单击退出：课堂触屏很容易误触。投放可通过 Esc 或网页端结束。
        self._root.withdraw()
        self._ready.set()
        self._root.after(100, self._poll)
        self._root.mainloop()

    def _poll(self) -> None:
        try:
            while True:
                item = self._queue.get_nowait()
                if item is None:
                    self._set_hidden()
                elif isinstance(item, tuple):
                    path, done, result = item
                    try:
                        result["ok"] = self._set_visible(path)
                    finally:
                        done.set()
                else:
                    self._set_visible(item)
        except queue.Empty:
            pass
        if self._root is not None:
            self._root.after(100, self._poll)

    def _force_topmost(self) -> None:
        if self._root is None:
            return
        try:
            self._root.attributes("-topmost", True)
            self._root.lift()
            self._root.focus_force()
            if is_windows():
                hwnd = self._root.winfo_id()
                hwnd_topmost = -1
                swp_nomove = 0x0002
                swp_nosize = 0x0001
                swp_showwindow = 0x0040
                ctypes.windll.user32.SetWindowPos(
                    hwnd, hwnd_topmost, 0, 0, 0, 0,
                    swp_nomove | swp_nosize | swp_showwindow,
                )
                ctypes.windll.user32.SetForegroundWindow(hwnd)
        except Exception:
            pass

    def _set_visible(self, path: str) -> bool:
        import tkinter as tk
        if self._root is None:
            return False
        screen_width = max(1, self._root.winfo_screenwidth())
        screen_height = max(1, self._root.winfo_screenheight())
        temp_path: Optional[Path] = None
        try:
            img = tk.PhotoImage(file=path)
        except Exception:
            img = None
        try:
            if img is None:
                if Path(path).suffix.lower() not in PROJECTABLE_EXTS:
                    return False
                fd, temp_name = tempfile.mkstemp(prefix="7000-display-", suffix=".png")
                os.close(fd)
                temp_path = Path(temp_name)
                temp_path.unlink(missing_ok=True)
                if not _gdiplus_scaled_png(
                        Path(path), temp_path, screen_width, screen_height,
                        allow_upscale=True):
                    return False
                img = tk.PhotoImage(file=str(temp_path))
            else:
                scale = min(screen_width / img.width(), screen_height / img.height())
                target_width = max(1, round(img.width() * scale))
                target_height = max(1, round(img.height() * scale))
                if target_width != img.width() or target_height != img.height():
                    fd, temp_name = tempfile.mkstemp(prefix="7000-display-", suffix=".png")
                    os.close(fd)
                    temp_path = Path(temp_name)
                    temp_path.unlink(missing_ok=True)
                    if _gdiplus_scaled_png(
                            Path(path), temp_path, screen_width, screen_height,
                            allow_upscale=True):
                        img = tk.PhotoImage(file=str(temp_path))
        except Exception:
            return False
        finally:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)
        self._img = img
        self._label.config(image=img)
        self._root.deiconify()
        try:
            self._root.attributes("-fullscreen", True)
        except Exception:
            pass
        self._force_topmost()
        with self._visible_lock:
            self._visible = True
        # 某些全屏应用会在窗口出现的同一帧抢回层级，稍后再强化一次。
        self._root.after(120, self._force_topmost)
        return True

    def _set_hidden(self) -> None:
        if self._root is None:
            return
        self._label.config(image="")
        self._img = None
        try:
            self._root.attributes("-topmost", False)
        except Exception:
            pass
        self._root.withdraw()
        with self._visible_lock:
            self._visible = False

    def show(self, path: str) -> bool:
        done = threading.Event()
        result = {"ok": False}
        self._queue.put((path, done, result))
        # 大尺寸 JPEG 首次需要解码和缩放，给 GUI 线程足够时间返回真实结果。
        done.wait(timeout=15)
        return bool(result["ok"])

    def hide(self) -> None:
        self._queue.put(None)

    def is_visible(self) -> bool:
        with self._visible_lock:
            return self._visible


class FileStore:
    """浏览、下载截图与课件文件。所有访问限制在允许的根目录内。"""

    def __init__(self, config: Config):
        self._config = config
        self.roots: List[dict] = [
            {"key": "screenshots", "label": "屏幕截图", "path": config.context_root},
            {"key": "courses", "label": "课件", "path": config.target_root},
        ]
        self.root_keys = {r["key"]: r for r in self.roots}
        self._thumb_dir = Path(config.state_dir) / "thumbnails"
        self._thumb_dir.mkdir(parents=True, exist_ok=True)
        self._thumb_lock = threading.Lock()
        self._last_thumb_prune = 0.0
        self._image_cache_lock = threading.Lock()
        self._image_cache: Dict[str, Tuple[float, List[dict]]] = {}
        self._prune_thumbnails()

    def _prune_thumbnails(self) -> None:
        """删除超过保留期的缩略图缓存，避免缓存无限增长。"""
        cutoff = time.time() - max(2, self._config.screenshot_retention_days + 1) * 86400
        try:
            for item in self._thumb_dir.iterdir():
                if item.is_file() and item.stat().st_mtime < cutoff:
                    item.unlink(missing_ok=True)
        except OSError:
            pass
        self._last_thumb_prune = time.time()

    def _norm(self, p: str) -> str:
        return os.path.normcase(os.path.realpath(p))

    def _root_paths(self) -> List[str]:
        return [self._norm(r["path"]) for r in self.roots]

    def safe_resolve(self, raw_path: str) -> Optional[Path]:
        """把请求的路径解析为某个根目录内的绝对路径；越界返回 None。"""
        try:
            cand = self._norm(raw_path)
        except (OSError, ValueError):
            return None
        for root in self._root_paths():
            if cand == root or cand.startswith(root + os.sep):
                return Path(cand)
        return None

    def roots_json(self) -> dict:
        out = []
        for r in self.roots:
            p = Path(r["path"])
            out.append({
                "key": r["key"], "label": r["label"],
                "path": r["path"],
                "exists": p.is_dir(),
            })
        return {"roots": out}

    def list_dir(self, raw_path: str) -> Optional[dict]:
        resolved = self.safe_resolve(raw_path)
        if resolved is None:
            return None
        if not resolved.is_dir():
            return {"path": str(resolved), "dirs": [], "files": [], "error": "不是目录"}

        dirs: List[dict] = []
        files: List[dict] = []
        try:
            for child in sorted(resolved.iterdir(), key=lambda c: c.name.lower()):
                try:
                    st = child.stat()
                except OSError:
                    continue
                if child.is_dir():
                    dirs.append({
                        "name": child.name, "path": str(child),
                        "type": "dir", "mtime": int(st.st_mtime),
                    })
                else:
                    files.append({
                        "name": child.name, "path": str(child),
                        "type": "file", "size": st.st_size,
                        "size_h": human_size(st.st_size),
                        "mtime": int(st.st_mtime),
                    })
        except OSError as e:
            return {"path": str(resolved), "dirs": [], "files": [], "error": str(e)}

        # 相对根目录的上级路径（用于"返回上级"）
        parent = str(resolved.parent)
        return {"path": str(resolved), "parent": parent, "dirs": dirs, "files": files}

    def file(self, raw_path: str) -> Optional[Path]:
        resolved = self.safe_resolve(raw_path)
        if resolved is None or not resolved.is_file():
            return None
        return resolved

    def thumbnail(self, raw_path: str) -> Optional[bytes]:
        """返回缓存的小尺寸 PNG；常见非 PNG 格式由 GDI+ 解码。"""
        source = self.file(raw_path)
        if source is None:
            return None
        try:
            st = source.stat()
            signature = f"thumb-v2|{source}|{st.st_mtime_ns}|{st.st_size}"
            key = hashlib.sha256(signature.encode("utf-8")).hexdigest()
            cached = self._thumb_dir / f"{key}.png"
            if cached.is_file():
                return cached.read_bytes()
            with self._thumb_lock:
                if time.time() - self._last_thumb_prune >= 3600:
                    self._prune_thumbnails()
                if cached.is_file():
                    return cached.read_bytes()
                result = None
                if source.suffix.lower() == ".png":
                    result = _png_thumbnail(source.read_bytes())
                if result is None and source.suffix.lower() in PROJECTABLE_EXTS:
                    work = self._thumb_dir / f".{key}.work.png"
                    work.unlink(missing_ok=True)
                    try:
                        if _gdiplus_scaled_png(source, work, 480, 480):
                            os.replace(work, cached)
                            return cached.read_bytes()
                    finally:
                        work.unlink(missing_ok=True)
                if result is None:
                    return None
                atomic_bytes(cached, result)
                return result
        except OSError:
            return None

    def build_zip(self, paths: List[str]) -> Optional[Tuple[BinaryIO, int]]:
        files: List[Path] = []
        unique_paths = set()
        for p in paths[:MAX_ZIP_FILES]:
            f = self.safe_resolve(p)
            if f and f.is_file() and str(f) not in unique_paths:
                files.append(f)
                unique_paths.add(str(f))
        if not files:
            return None
        archive = tempfile.SpooledTemporaryFile(max_size=8 << 20, mode="w+b")
        try:
            with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
                # 用文件名（必要时加序号）避免重名冲突。
                used = set()
                for f in files:
                    arc = f.name
                    index = 1
                    while arc.casefold() in used:
                        arc = f"{f.stem}({index}){f.suffix}"
                        index += 1
                    used.add(arc.casefold())
                    zf.write(f, arcname=arc)
            size = archive.tell()
            archive.seek(0)
            return archive, size
        except Exception:
            archive.close()
            raise

    def invalidate_image_cache(self, root_key: Optional[str] = None) -> None:
        with self._image_cache_lock:
            if root_key is None:
                self._image_cache.clear()
            else:
                self._image_cache.pop(root_key, None)

    def list_all_images(self, root_key: str, offset: int = 0,
                        limit: int = 24) -> Optional[dict]:
        """分页列出图片；短时缓存目录索引，避免每次翻页都递归磁盘。"""
        root = self.root_keys.get(root_key)
        if not root:
            return None
        base = Path(root["path"])
        if not base.is_dir():
            return {"shots": [], "total": 0, "offset": 0, "limit": limit}
        now = time.monotonic()
        with self._image_cache_lock:
            cached = self._image_cache.get(root_key)
            shots = cached[1] if cached and cached[0] > now else None
        if shots is None:
            shots = []
            for p in base.rglob("*"):
                try:
                    if not p.is_file() or not is_image_name(p.name):
                        continue
                    st = p.stat()
                except OSError:
                    continue
                shots.append({
                    "path": str(p),
                    "name": p.name,
                    "dir": p.parent.name,
                    "size": st.st_size,
                    "size_h": human_size(st.st_size),
                    "mtime": int(st.st_mtime),
                })
            shots.sort(key=lambda item: (item["mtime"], item["path"]), reverse=True)
            with self._image_cache_lock:
                self._image_cache[root_key] = (now + 3.0, shots)
        offset = max(0, offset)
        limit = max(1, min(100, limit))
        return {
            "shots": shots[offset:offset + limit],
            "total": len(shots),
            "offset": offset,
            "limit": limit,
        }


# ---------------------------------------------------------------------------
# HTTP 服务
# ---------------------------------------------------------------------------

class MonitorHandler(http.server.BaseHTTPRequestHandler):
    server_version = f"{APP_NAME}/{VERSION}"

    def log_message(self, format: str, *args: object) -> None:
        pass

    @property
    def app(self) -> "MonitorServer":
        return self.server  # type: ignore[return-value]

    def _send_json(self, obj: object, status: int = 200,
                   extra_headers: Optional[Dict[str, str]] = None) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _remote(self) -> str:
        # These gateways are not deployed behind a trusted reverse proxy.  Never
        # trust a client-supplied forwarding header for authentication decisions.
        return self.client_address[0]

    def _cookies(self) -> Dict[str, str]:
        cookies: Dict[str, str] = {}
        for item in self.headers.get("Cookie", "").replace(" ", "").split(";"):
            if "=" in item:
                key, value = item.split("=", 1)
                cookies[key] = value
        return cookies

    def _sign(self, payload: str) -> str:
        digest = hmac.new(self.app.config.session_secret.encode("utf-8"),
                          payload.encode("utf-8"), hashlib.sha256).digest()
        return b64url_encode(digest)

    def _session_ok(self) -> bool:
        if not self.app.config.session_secret:
            return False
        raw = self._cookies().get(SESSION_COOKIE, "")
        if "." not in raw:
            return False
        payload, signature = raw.rsplit(".", 1)
        if not hmac.compare_digest(self._sign(payload), signature):
            return False
        try:
            data = json.loads(b64url_decode(payload).decode("utf-8"))
            return int(data.get("exp", 0)) >= int(time.time())
        except (ValueError, TypeError, json.JSONDecodeError):
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

    def _send_html(self, html: str, status: int = 200) -> None:
        body = html.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path: Path) -> None:
        ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        size = path.stat().st_size
        fname = urllib.parse.quote(path.name)
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(size))
        self.send_header("Content-Disposition",
                         f"attachment; filename*=UTF-8''{fname}")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        with open(path, "rb") as f:
            shutil.copyfileobj(f, self.wfile, length=1 << 16)

    def _send_inline_file(self, path: Path, cache_seconds: int = 0) -> None:
        ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        size = path.stat().st_size
        fname = urllib.parse.quote(path.name)
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(size))
        self.send_header("Content-Disposition", f"inline; filename*=UTF-8''{fname}")
        self.send_header(
            "Cache-Control",
            f"private, max-age={cache_seconds}" if cache_seconds else "no-store",
        )
        self.end_headers()
        with open(path, "rb") as f:
            shutil.copyfileobj(f, self.wfile, length=1 << 16)

    def _send_thumbnail(self, body: bytes) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "private, max-age=86400")
        self.end_headers()
        self.wfile.write(body)

    def _read_request_body(self) -> Optional[bytes]:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self._send_json({"error": "无效请求长度"}, status=400)
            return None
        if length < 0 or length > MAX_POST_BYTES:
            self._send_json({"error": "请求内容过大"}, status=413)
            return None
        return self.rfile.read(length) if length else b""

    def do_GET(self) -> None:
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path
        q = urllib.parse.parse_qs(parsed.query)

        if path in ("/", "/index.html"):
            try:
                html = UI_FILE.read_text(encoding="utf-8")
            except OSError:
                self._send_json({"error": "ui.html 不存在"}, status=500)
                return
            self._send_html(html)
            return
        if path == "/health":
            self._send_json({"ok": True, "app": APP_NAME, "version": VERSION})
            return
        if not self._require_auth():
            return
        if path == "/api/status":
            include_log = q.get("log", ["1"])[0] != "0"
            self._send_json(self.app.status_json(include_log=include_log))
            return
        if path == "/api/clipboard":
            self._send_json({"text": get_clipboard_text()})
            return
        if path == "/api/roots":
            self._send_json(self.app.filestore.roots_json())
            return
        if path == "/api/shots":
            try:
                offset = int(q.get("offset", ["0"])[0])
                limit = int(q.get("limit", ["24"])[0])
            except ValueError:
                self._send_json({"error": "无效分页参数"}, status=400)
                return
            result = self.app.filestore.list_all_images("screenshots", offset, limit)
            self._send_json(result or {"shots": [], "total": 0})
            return
        if path == "/api/list":
            raw = q.get("path", [""])[0]
            result = self.app.filestore.list_dir(raw)
            if result is None:
                self._send_json({"error": "路径越界"}, status=403)
            else:
                self._send_json(result)
            return
        if path == "/api/file":
            raw = q.get("path", [""])[0]
            f = self.app.filestore.file(raw)
            if f is None:
                self._send_json({"error": "文件不存在"}, status=404)
            else:
                self._send_file(f)
            return
        if path == "/api/preview":
            raw = q.get("path", [""])[0]
            f = self.app.filestore.file(raw)
            if f is None or not is_image_name(f.name):
                self._send_json({"error": "文件不存在"}, status=404)
            else:
                self._send_inline_file(f, cache_seconds=3600)
            return
        if path == "/api/thumbnail":
            raw = q.get("path", [""])[0]
            f = self.app.filestore.file(raw)
            if f is None or not is_image_name(f.name):
                self._send_json({"error": "文件不存在"}, status=404)
            else:
                body = self.app.filestore.thumbnail(raw)
                if body is not None:
                    self._send_thumbnail(body)
                else:
                    self._send_inline_file(f, cache_seconds=3600)
            return
        self._send_json({"error": "not found"}, status=404)

    def do_POST(self) -> None:
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.path == "/login":
            raw_body = self._read_request_body()
            try:
                request = json.loads((raw_body or b"{}").decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                request = {}
            if not verify_password(str(request.get("password", "")),
                                   self.app.config.password_hash):
                self._send_json({"error": "incorrect password"}, 401)
                return
            days = self.app.config.session_days
            expires_at = int(time.time()) + days * 86400
            payload = b64url_encode(json.dumps({"exp": expires_at}).encode("utf-8"))
            cookie = f"{payload}.{self._sign(payload)}"
            expires = time.strftime("%a, %d %b %Y %H:%M:%S GMT",
                                    time.gmtime(expires_at))
            self._send_json({"ok": True}, extra_headers={"Set-Cookie":
                f"{SESSION_COOKIE}={cookie}; Path=/; HttpOnly; SameSite=Lax; "
                f"Max-Age={days * 86400}; Expires={expires}"})
            return
        if parsed.path == "/logout":
            self._send_json({"ok": True}, extra_headers={"Set-Cookie":
                f"{SESSION_COOKIE}=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0"})
            return
        if not self._require_auth():
            return
        if parsed.path == "/api/scan":
            started = self.app.trigger_scan()
            self._send_json({"ok": True, "started": started})
            return
        if parsed.path == "/api/shoot":
            shot = self.app.capturer.shoot_now()
            if shot is None:
                self._send_json({"error": "截图失败"}, status=500)
            else:
                self.app.filestore.invalidate_image_cache("screenshots")
                self._send_json(shot)
            return
        if parsed.path == "/api/show":
            raw = urllib.parse.parse_qs(parsed.query).get("path", [""])[0]
            f = self.app.filestore.file(raw)
            if f is None or not is_image_name(f.name):
                self._send_json({"error": "无效图片"}, status=404)
            elif f.suffix.lower() not in PROJECTABLE_EXTS:
                self._send_json(
                    {"error": "该格式暂不支持投放，请使用 PNG、JPG、GIF 或 BMP"},
                    status=415,
                )
            elif self.app.display.show(str(f)):
                self._send_json({"ok": True, "path": str(f)})
            else:
                self._send_json({"error": "投放窗口启动失败"}, status=500)
            return
        if parsed.path == "/api/hide":
            self.app.display.hide()
            self._send_json({"ok": True})
            return
        if parsed.path == "/api/zip":
            raw_body = self._read_request_body()
            if raw_body is None:
                return
            try:
                content_type = self.headers.get("Content-Type", "")
                if content_type.startswith("application/json"):
                    req = json.loads(raw_body.decode("utf-8"))
                    paths = req.get("paths", [])
                else:
                    form = urllib.parse.parse_qs(raw_body.decode("utf-8"))
                    paths = form.get("path", [])
                if (not isinstance(paths, list)
                        or not all(isinstance(path, str) for path in paths)):
                    raise ValueError
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
                self._send_json({"error": "bad request"}, status=400)
                return
            if len(paths) > MAX_ZIP_FILES:
                self._send_json(
                    {"error": f"一次最多下载 {MAX_ZIP_FILES} 个文件"}, status=400
                )
                return
            result = self.app.filestore.build_zip(paths)
            if result is None:
                self._send_json({"error": "无可下载文件"}, status=404)
                return
            archive, size = result
            try:
                self.send_response(200)
                self.send_header("Content-Type", "application/zip")
                self.send_header("Content-Length", str(size))
                self.send_header("Content-Disposition",
                                 "attachment; filename=7000-selected.zip")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                shutil.copyfileobj(archive, self.wfile, length=1 << 16)
            finally:
                archive.close()
            return
        if parsed.path == "/api/clipboard":
            raw_body = self._read_request_body()
            if raw_body is None:
                return
            try:
                req = json.loads(raw_body.decode("utf-8"))
                text = req.get("text", "")
                if not isinstance(text, str):
                    raise ValueError
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
                self._send_json({"error": "bad request"}, status=400)
                return
            ok = set_clipboard_text(text)
            self._send_json({"ok": ok})
            return
        self._send_json({"error": "not found"}, status=404)


class MonitorServer(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: Tuple[str, int], config: Config, state: State,
                 monitor: Monitor, capturer: ScreenCapturer, filestore: FileStore,
                 display: DisplayManager):
        self.config = config
        self.state = state
        self.monitor = monitor
        self.capturer = capturer
        self.filestore = filestore
        self.display = display
        super().__init__(address, MonitorHandler)

    def trigger_scan(self) -> bool:
        return self.monitor.start_scan_async()

    def status_json(self, include_log: bool = True) -> dict:
        with self.monitor.info_lock:
            drives = list(self.monitor.current_drives)
        return {
            "app": APP_NAME,
            "version": VERSION,
            "time": local_now(),
            "last_scan": self.monitor.last_scan_ts,
            "target_root": self.config.target_root,
            "context_root": self.config.context_root,
            "scan_interval": self.config.scan_interval,
            "extensions": self.config.extensions,
            "download_dirs": self.config.download_dirs,
            "drives": drives,
            "seen_count": len(self.state.seen),
            "screenshot": self.capturer.status(),
            "display": {"visible": self.display.is_visible()},
            "log": self.state.snapshot_log(200) if include_log else [],
        }


# ---------------------------------------------------------------------------
# 旧版 UI 源码快照（不再加载或提供，仅供迁移期对照）
# ---------------------------------------------------------------------------

r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>教学机服务</title>
<style>
  :root {
    color-scheme: light dark;
    --blue: #007aff; --green: #34c759; --red: #ff3b30;
    --orange: #ff9500; --yellow: #ffcc00;
    --r: 12px; --r-lg: 16px; --r-pill: 999px;
    --bg: #f2f2f7; --elev: #ffffff; --elev-2: #f8f8fa;
    --text: #1c1c1e; --text-2: #3a3a3c; --text-3: #6e6e73; --text-4: #aeaeb2;
    --sep: rgba(60,60,67,0.14); --sep-2: rgba(60,60,67,0.28);
    --fill: rgba(120,120,128,0.12); --fill-2: rgba(120,120,128,0.08);
    --nav: rgba(248,248,250,0.78);
    --shadow: 0 1px 2px rgba(0,0,0,0.04), 0 8px 28px rgba(0,0,0,0.06);
    --ease: cubic-bezier(.4,0,.2,1);
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #000; --elev: #1c1c1e; --elev-2: #2c2c2e;
      --text: #f5f5f7; --text-2: #e5e5ea; --text-3: #98989f; --text-4: #636366;
      --sep: rgba(84,84,88,0.36); --sep-2: rgba(84,84,88,0.6);
      --fill: rgba(120,120,128,0.24); --fill-2: rgba(120,120,128,0.16);
      --nav: rgba(22,22,24,0.78);
      --shadow: 0 1px 2px rgba(0,0,0,0.4), 0 8px 28px rgba(0,0,0,0.35);
    }
  }
  * { box-sizing: border-box; -webkit-tap-highlight-color: transparent; }
  html { -webkit-font-smoothing: antialiased; -webkit-text-size-adjust: 100%; }
  body {
    margin: 0; background: var(--bg); color: var(--text);
    font-family: -apple-system, BlinkMacSystemFont, "SF Pro Text", "Segoe UI",
      "PingFang SC", "Microsoft YaHei", system-ui, sans-serif;
    font-size: 15px; line-height: 1.5; letter-spacing: -0.01em;
    min-height: 100vh;
  }

  /* ---- 顶部导航：毛玻璃 + 大标题 + 分段控件 ---- */
  header {
    position: sticky; top: 0; z-index: 40;
    background: var(--nav);
    -webkit-backdrop-filter: saturate(180%) blur(20px);
    backdrop-filter: saturate(180%) blur(20px);
    border-bottom: 0.5px solid var(--sep);
    padding: 12px 20px 10px;
  }
  .title-row { display: flex; align-items: flex-end; justify-content: space-between; gap: 12px; margin-bottom: 12px; }
  h1 { margin: 0; font-size: 28px; font-weight: 700; letter-spacing: -0.02em; line-height: 1.1; }
  .sub { color: var(--text-3); font-size: 13px; margin-top: 3px; }
  .conn { display: inline-flex; align-items: center; gap: 6px; font-size: 12px; color: var(--text-3);
          background: var(--fill-2); border-radius: var(--r-pill); padding: 5px 11px; white-space: nowrap; }
  .conn .dot { width: 8px; height: 8px; border-radius: 50%; background: var(--green);
               box-shadow: 0 0 0 3px rgba(52,199,89,0.2); transition: background .3s var(--ease); }
  .conn.off .dot { background: var(--red); box-shadow: 0 0 0 3px rgba(255,59,48,0.2); }
  .segmented { display: inline-flex; background: var(--fill-2); border-radius: 10px; padding: 2px; gap: 2px; width: 100%; }
  .segmented button {
    appearance: none; border: none; background: transparent; color: var(--text-3);
    font-size: 14px; font-weight: 500; font-family: inherit; padding: 7px 0; flex: 1;
    border-radius: 8px; cursor: pointer;
    transition: color .2s var(--ease), background .2s var(--ease), box-shadow .2s var(--ease);
  }
  .segmented button.active { background: var(--elev); color: var(--text); box-shadow: 0 1px 3px rgba(0,0,0,0.10); }

  /* ---- 内容区 ---- */
  .wrap { max-width: 760px; margin: 0 auto; padding: 20px 20px 120px; }
  section.pane { animation: fadeup .3s var(--ease); }
  @keyframes fadeup { from { opacity: 0; transform: translateY(6px); } to { opacity: 1; transform: none; } }

  /* ---- 分组卡片（Inset Grouped） ---- */
  .group { background: var(--elev); border-radius: var(--r-lg); overflow: hidden;
           box-shadow: var(--shadow); margin-bottom: 16px; }
  .group-head { padding: 14px 18px 8px; font-size: 13px; color: var(--text-3); font-weight: 600; }
  .row { display: flex; align-items: center; gap: 14px; padding: 12px 18px; border-top: 0.5px solid var(--sep); }
  .row:first-of-type { border-top: none; }
  .row .k { color: var(--text-3); font-size: 14px; flex: none; }
  .row .v { margin-left: auto; font-size: 14px; color: var(--text); text-align: right; word-break: break-all; }
  .badge { display: inline-flex; align-items: center; gap: 5px; padding: 3px 10px; border-radius: var(--r-pill);
           font-size: 12px; font-weight: 600; background: rgba(52,199,89,0.14); color: var(--green); }

  /* 统计卡片 */
  .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(200px, 1fr)); gap: 12px; margin-bottom: 16px; }
  .card { background: var(--elev); border-radius: var(--r); padding: 16px; box-shadow: var(--shadow); }
  .card .k { color: var(--text-3); font-size: 12px; font-weight: 500; }
  .card .v { font-size: 17px; font-weight: 600; margin-top: 4px; word-break: break-all; }

  .section-title { font-size: 13px; color: var(--text-3); font-weight: 600; margin: 20px 4px 8px; }

  /* ---- 归档记录表 ---- */
  table { width: 100%; border-collapse: collapse; }
  th, td { text-align: left; padding: 11px 18px; border-bottom: 0.5px solid var(--sep); font-size: 14px; vertical-align: top; }
  th { color: var(--text-3); font-weight: 500; font-size: 13px; }
  tr:last-child td { border-bottom: none; }
  .lvl-ok { color: var(--green); font-weight: 600; }
  .lvl-warn { color: var(--orange); font-weight: 600; }
  .lvl-error { color: var(--red); font-weight: 600; }
  .lvl-info { color: var(--text-3); }
  .src { color: var(--blue); }

  .empty { color: var(--text-4); padding: 48px 24px; text-align: center; font-size: 14px; }
  .empty .spinner { margin: 0 auto 12px; }
  .scroll { max-height: 60vh; overflow-y: auto; background: var(--elev); border-radius: var(--r-lg); box-shadow: var(--shadow); }
  .hidden { display: none !important; }

  /* ---- 文件传输 ---- */
  .crumbs { color: var(--text-3); font-size: 13px; padding: 14px 18px 10px; word-break: break-all; }
  .crumb { color: var(--blue); cursor: pointer; transition: opacity .15s var(--ease); }
  .crumb:hover { opacity: 0.7; }
  .file-row { display: flex; align-items: center; gap: 12px; padding: 11px 18px; border-bottom: 0.5px solid var(--sep);
              transition: background .15s var(--ease); }
  .file-row:last-child { border-bottom: none; }
  .file-row:active { background: var(--fill-2); }
  .file-row input[type="checkbox"] { width: 22px; height: 22px; flex: none; accent-color: var(--blue); cursor: pointer; }
  .file-row .fname { flex: 1; word-break: break-all; cursor: pointer; overflow: hidden; text-overflow: ellipsis; }
  .file-row .fname.dir { color: var(--blue); font-weight: 500; }
  .file-row .fmeta { color: var(--text-3); font-size: 13px; flex: none; white-space: nowrap; }

  /* ---- 底部悬浮工具栏 ---- */
  .toolbar {
    position: fixed; left: 50%; transform: translateX(-50%); bottom: 20px; z-index: 30;
    display: flex; gap: 8px; align-items: center;
    background: var(--nav); -webkit-backdrop-filter: saturate(180%) blur(20px); backdrop-filter: saturate(180%) blur(20px);
    border: 0.5px solid var(--sep); border-radius: var(--r-lg);
    padding: 8px; box-shadow: 0 10px 34px rgba(0,0,0,0.16);
    max-width: calc(100vw - 32px); overflow-x: auto;
  }
  .btn { appearance: none; border: none; background: var(--blue); color: #fff; padding: 10px 18px;
         border-radius: 10px; font-size: 15px; font-weight: 600; font-family: inherit; cursor: pointer;
         transition: transform .12s var(--ease), opacity .2s var(--ease), filter .15s var(--ease);
         white-space: nowrap; }
  .btn:hover { filter: brightness(1.06); }
  .btn:active { transform: scale(0.96); }
  .btn:disabled { opacity: 0.35; cursor: not-allowed; }
  .btn.ghost { background: var(--fill); color: var(--text); }
  .sel { color: var(--text-3); font-size: 13px; }

  /* ---- 缩略图网格 ---- */
  .thumbgrid { display: grid; grid-template-columns: repeat(auto-fill, minmax(140px, 1fr)); gap: 12px; padding: 6px 18px 18px; }
  .thumb-item { position: relative; border-radius: var(--r); overflow: hidden; background: var(--elev-2);
                box-shadow: var(--shadow); transition: transform .18s var(--ease), box-shadow .18s var(--ease); }
  .thumb-item:active { transform: scale(0.97); }
  .thumb-item img { width: 100%; height: 108px; object-fit: cover; display: block; cursor: zoom-in; background: #000; }
  .thumb-bar { display: flex; align-items: center; gap: 8px; padding: 7px 9px; }
  .thumb-bar input[type="checkbox"] { width: 18px; height: 18px; flex: none; accent-color: var(--blue); cursor: pointer; }
  .tname { font-size: 11px; color: var(--text-3); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .mini { background: var(--blue); color: #fff; border: none; border-radius: 6px; padding: 3px 9px; font-size: 11px; font-weight: 600; cursor: pointer; flex: none; }

  /* ---- 模态查看器 ---- */
  .modal { position: fixed; inset: 0; background: rgba(0,0,0,0.88); display: flex;
           align-items: center; justify-content: center; z-index: 50; padding: 16px;
           opacity: 0; transition: opacity .22s var(--ease); }
  .modal:not(.hidden) { opacity: 1; }
  .modal.hidden { display: none; }
  .modal-box { max-width: 95vw; max-height: 95vh; text-align: center; transform: scale(0.96); transition: transform .22s var(--ease); }
  .modal:not(.hidden) .modal-box { transform: scale(1); }
  .modal-name { margin-bottom: 10px; font-size: 13px; color: rgba(255,255,255,0.7); word-break: break-all; }
  .modal-box img { max-width: 95vw; max-height: 72vh; border-radius: 10px; }
  .modal-hint { color: rgba(255,255,255,0.55); font-size: 13px; margin: 12px 0 16px; }

  /* ---- 表单元素 ---- */
  textarea { width: 100%; background: var(--elev-2); color: var(--text);
             border: 0.5px solid var(--sep-2); border-radius: var(--r); padding: 12px 14px;
             font-size: 15px; font-family: inherit; resize: vertical; line-height: 1.5; }
  textarea:focus { outline: none; border-color: var(--blue); box-shadow: 0 0 0 3px rgba(0,122,255,0.2); }
  select { padding: 9px 12px; background: var(--elev); color: var(--text);
           border: 0.5px solid var(--sep-2); border-radius: 10px; font-size: 14px; font-family: inherit; }
  .btn-row { display: flex; gap: 10px; flex-wrap: wrap; }

  /* ---- Toast ---- */
  #toast { position: fixed; left: 50%; bottom: 92px; transform: translateX(-50%) translateY(8px); z-index: 60;
           background: rgba(28,28,30,0.92); color: #fff; padding: 10px 18px; border-radius: var(--r-pill);
           font-size: 14px; opacity: 0; pointer-events: none; transition: opacity .25s var(--ease), transform .25s var(--ease);
           -webkit-backdrop-filter: blur(10px); backdrop-filter: blur(10px); }
  #toast.show { opacity: 1; transform: translateX(-50%) translateY(0); }

  /* ---- Spinner ---- */
  .spinner { width: 22px; height: 22px; border-radius: 50%; border: 2.5px solid var(--fill);
             border-top-color: var(--blue); animation: spin .7s linear infinite; }
  @keyframes spin { to { transform: rotate(360deg); } }

  @media (max-width: 560px) {
    h1 { font-size: 24px; }
    .grid { grid-template-columns: repeat(auto-fill, minmax(150px, 1fr)); }
  }
</style>
</head>
<body>
<header>
  <div class="title-row">
    <div>
      <h1>教学机</h1>
      <div class="sub">课件归档 · 定时截屏 · 文件传输</div>
    </div>
    <div class="conn" id="conn"><span class="dot"></span><span id="conn-text">在线</span></div>
  </div>
  <nav class="segmented">
    <button id="tab-files" class="active">文件</button>
    <button id="tab-shots">截图</button>
    <button id="tab-courses">归档</button>
    <button id="tab-clipboard">剪切板</button>
  </nav>
</header>

<div class="wrap">

  <section id="pane-files" class="pane">
    <div class="group">
      <div class="row"><span class="k">来源</span>
        <select id="roots" class="v"></select>
      </div>
      <div class="row"><span class="k">位置</span><span class="v crumbs" id="crumbs">-</span></div>
      <div class="row"><span class="k">已选择</span><span class="v sel" id="selcount">0 项</span></div>
    </div>
    <div class="scroll">
      <div id="filelist"><div class="empty"><div class="spinner"></div>加载中…</div></div>
    </div>
    <div class="toolbar">
      <button class="btn" id="shoot">立即截图</button>
      <button class="btn ghost" id="up">返回上级</button>
      <button class="btn" id="download" disabled>下载所选</button>
      <button class="btn ghost" id="hide-display" disabled>关闭投放</button>
      <span class="sel" id="hint"></span>
    </div>
  </section>

  <section id="pane-shots" class="pane hidden">
    <div class="grid">
      <div class="card"><div class="k">截屏状态</div><div class="v"><span class="badge" id="shot_enabled">-</span></div></div>
      <div class="card"><div class="k">最近一张</div><div class="v" id="shot_last">-</div></div>
      <div class="card"><div class="k">当前批次</div><div class="v" id="shot_dir">-</div></div>
      <div class="card"><div class="k">本次运行已截</div><div class="v" id="shot_count">-</div></div>
      <div class="card"><div class="k">截图根目录</div><div class="v" id="shot_root">-</div></div>
      <div class="card"><div class="k">保留天数</div><div class="v" id="shot_ret">-</div></div>
    </div>
  </section>

  <section id="pane-courses" class="pane hidden">
    <div class="group">
      <div class="row"><span class="k">运行状态</span><span class="v"><span class="badge">运行中</span></span></div>
      <div class="row"><span class="k">课件根目录</span><span class="v" id="target">-</span></div>
      <div class="row"><span class="k">已归档文件数</span><span class="v" id="seen">-</span></div>
      <div class="row"><span class="k">已连接 U 盘</span><span class="v" id="drives">-</span></div>
    </div>
    <div class="section-title">归档记录</div>
    <div class="scroll">
      <table>
        <thead><tr><th style="width:140px">时间</th><th>来源</th><th>事件</th></tr></thead>
        <tbody id="rows"><tr><td colspan="3" class="empty">暂无记录</td></tr></tbody>
      </table>
    </div>
  </section>

  <section id="pane-clipboard" class="pane hidden">
    <div class="section-title">教学机剪切板 → 平板</div>
    <div class="group">
      <div class="row" style="display:block; padding:14px 18px;">
        <textarea id="pc-text" rows="4" placeholder="点「读取」获取教学机剪切板内容"></textarea>
        <div class="btn-row" style="margin-top:12px;">
          <button class="btn" id="clip-read">读取</button>
          <button class="btn ghost" id="clip-copy">复制到平板</button>
        </div>
      </div>
    </div>
    <div class="section-title">平板 → 教学机剪切板</div>
    <div class="group">
      <div class="row" style="display:block; padding:14px 18px;">
        <textarea id="pad-text" rows="4" placeholder="在此粘贴内容，或点「读取平板剪切板」"></textarea>
        <div class="btn-row" style="margin-top:12px;">
          <button class="btn" id="clip-send">发送到教学机</button>
          <button class="btn ghost" id="clip-read-pad">读取平板剪切板</button>
        </div>
      </div>
    </div>
    <div class="sel" id="clip-msg"></div>
  </section>

</div>

<div id="toast"></div>

<div id="modal" class="modal hidden">
  <div class="modal-box">
    <div class="modal-name" id="modal-name"></div>
    <img id="modal-img" alt="">
    <div class="modal-hint">长按图片即可存储到相册</div>
    <div class="btn-row" style="justify-content:center;">
      <button class="btn" onclick="downloadOriginal(modalCurrentPath)">下载原图</button>
      <button class="btn" onclick="showOnScreen(modalCurrentPath)">投放到大屏</button>
      <button class="btn ghost" onclick="closeModal()">关闭</button>
    </div>
  </div>
</div>

<script>
  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g,
      c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
  }
  function $(id) { return document.getElementById(id); }

  // ---- Toast 轻提示 ----
  let toastTimer = null;
  function toast(msg) {
    const t = $('toast');
    if (!t) return;
    t.textContent = msg;
    t.classList.add('show');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => t.classList.remove('show'), 1800);
  }

  // ---- tab 切换（带过渡动画） ----
  const tabs = [['files','pane-files'],['shots','pane-shots'],['courses','pane-courses'],['clipboard','pane-clipboard']];
  function activateTab(b) {
    tabs.forEach(([bb,pp]) => {
      $('tab-'+bb).classList.toggle('active', bb===b);
      const pane = $('pane-'+pp);
      const show = bb===b;
      pane.classList.toggle('hidden', !show);
      if (show) { pane.style.animation = 'none'; void pane.offsetWidth; pane.style.animation = ''; }
    });
  }
  tabs.forEach(([b]) => $('tab-'+b).addEventListener('click', () => activateTab(b)));

  // ---- 文件传输 ----
  let currentPath = '';
  let currentRootKey = '';
  let modalCurrentPath = '';
  let roots = [];
  const loadingHtml = '<div class="empty"><div class="spinner"></div>加载中…</div>';
  async function loadRoots() {
    const r = await (await fetch('/api/roots')).json();
    roots = r.roots;
    const sel = $('roots');
    sel.innerHTML = roots.map(x => '<option value="' + esc(x.key) + '">' + esc(x.label) + '</option>').join('');
    sel.onchange = () => { openRoot(sel.value); };
    if (roots.length) openRoot(roots[0].key);
  }
  function openRoot(key) {
    currentRootKey = key;
    if (key === 'screenshots') { loadShots(); return; }
    const root = roots.find(x => x.key === key);
    if (root) loadDir(root.path);
  }
  async function loadShots() {
    currentPath = 'shots';
    $('crumbs').innerHTML = '屏幕截图（全部）';
    $('filelist').innerHTML = loadingHtml;
    const r = await (await fetch('/api/shots')).json();
    let html = '';
    if (r.shots && r.shots.length) {
      html += '<div class="thumbgrid">';
      r.shots.forEach(f => {
        html += '<div class="thumb-item">'
          + '<img src="/api/thumbnail?path=' + encodeURIComponent(f.path) + '" loading="lazy" onclick="openModal(\'' + f.path.replace(/\\/g,'\\\\').replace(/'/g,"\\'") + '\', \'' + esc(f.dir + ' / ' + f.name) + '\')">'
          + '<div class="thumb-bar"><input type="checkbox" data-path="' + esc(f.path) + '" data-dir="0">'
          + '<span class="tname">' + esc(f.dir + ' ' + f.name) + '</span></div></div>';
      });
      html += '</div>';
    } else {
      html = '<div class="empty">暂无截图</div>';
    }
    $('filelist').innerHTML = html;
    $('filelist').querySelectorAll('input').forEach(cb => cb.addEventListener('change', updateSel));
    updateSel();
  }
  function isImage(name) {
    return /\.(png|jpe?g|gif|bmp|webp)$/i.test(name);
  }
  async function loadDir(path) {
    currentPath = path;
    $('crumbs').innerHTML = '&hellip;';
    $('filelist').innerHTML = loadingHtml;
    const r = await (await fetch('/api/list?path=' + encodeURIComponent(path))).json();
    if (r.error) { $('filelist').innerHTML = '<div class="empty">' + esc(r.error) + '</div>'; return; }
    const crumbs = path.split(/[\\/]/).filter(Boolean);
    $('crumbs').innerHTML = crumbs.map((c,i) => {
      const p = path.split(/[\\/]/).slice(0, i+1).join('\\');
      return '<span class="crumb" onclick="loadDir(\'' + p.replace(/\\/g,'\\\\').replace(/'/g,"\\'") + '\')">' + esc(c) + '</span>';
    }).join(' &gt; ');
    let html = '';
    r.dirs.forEach(d => {
      html += '<div class="file-row"><span class="fname dir" onclick="loadDir(\'' + d.path.replace(/\\/g,'\\\\').replace(/'/g,"\\'") + '\')">' + esc(d.name) + '</span>'
        + '<span class="fmeta">文件夹</span></div>';
    });
    const images = r.files.filter(f => isImage(f.name));
    const others = r.files.filter(f => !isImage(f.name));
    if (images.length) {
      html += '<div class="thumbgrid">';
      images.forEach(f => {
        html += '<div class="thumb-item">'
          + '<img src="/api/thumbnail?path=' + encodeURIComponent(f.path) + '" loading="lazy" onclick="openModal(\'' + f.path.replace(/\\/g,'\\\\').replace(/'/g,"\\'") + '\', \'' + esc(f.name) + '\')">'
          + '<div class="thumb-bar"><input type="checkbox" data-path="' + esc(f.path) + '" data-dir="0">'
          + '<span class="tname">' + esc(f.name) + '</span></div></div>';
      });
      html += '</div>';
    }
    others.forEach(f => {
      html += '<div class="file-row"><input type="checkbox" data-path="' + esc(f.path) + '" data-dir="0">'
        + '<span class="fname">' + esc(f.name) + '</span>'
        + '<span class="fmeta">' + esc(f.size_h) + '</span></div>';
    });
    if (!html) html = '<div class="empty">空文件夹</div>';
    $('filelist').innerHTML = html;
    $('filelist').querySelectorAll('input').forEach(cb => cb.addEventListener('change', updateSel));
    updateSel();
  }
  function openModal(path, name) {
    modalCurrentPath = path;
    $('modal-img').src = '/api/file?path=' + encodeURIComponent(path);
    $('modal-name').textContent = name;
    $('modal').classList.remove('hidden');
  }
  function showOnScreen(path) {
    fetch('/api/show?path=' + encodeURIComponent(path)).then(r => r.json()).then(d => {
      if (d.ok) { $('hide-display').disabled = false; toast('已投放到大屏'); }
    }).catch(() => {});
  }
  function downloadOriginal(path) {
    window.location.href = '/api/file?path=' + encodeURIComponent(path);
  }
  function closeModal() {
    $('modal').classList.add('hidden');
    $('modal-img').src = '';
  }
  function selectedFiles() {
    return Array.from($('filelist').querySelectorAll('input:checked'))
      .filter(cb => cb.dataset.dir === '0').map(cb => cb.dataset.path);
  }
  function updateSel() {
    const n = selectedFiles().length;
    $('selcount').textContent = n + ' 项';
    $('download').disabled = n === 0;
  }
  $('up').addEventListener('click', () => {
    const parts = currentPath.split(/[\\/]/).filter(Boolean);
    if (parts.length <= 1) return;
    const parent = currentPath.split(/[\\/]/).slice(0, -1).join('\\');
    loadDir(parent);
  });
  $('download').addEventListener('click', () => {
    const files = selectedFiles();
    if (files.length === 0) return;
    if (files.length === 1) {
      window.location.href = '/api/file?path=' + encodeURIComponent(files[0]);
    } else {
      toast('正在打包…');
      fetch('/api/zip', {method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({paths: files})})
        .then(r => r.blob())
        .then(b => {
          const a = document.createElement('a');
          a.href = URL.createObjectURL(b);
          a.download = 'selected.zip';
          a.click();
          URL.revokeObjectURL(a.href);
        }).catch(() => toast('打包失败'));
    }
  });

  $('shoot').addEventListener('click', async () => {
    try {
      const r = await (await fetch('/api/shoot')).json();
      if (r.path) {
        $('roots').value = 'screenshots';
        openRoot('screenshots');
        openModal(r.path, r.dir + ' / ' + r.name);
        toast('已截屏');
      } else {
        toast('截图失败');
      }
    } catch (e) { toast('截图失败'); }
  });
  $('hide-display').addEventListener('click', () => {
    fetch('/api/hide').then(() => { $('hide-display').disabled = true; toast('已关闭投放'); }).catch(() => {});
  });

  // ---- 剪切板互传 ----
  $('clip-read').addEventListener('click', async () => {
    try {
      const r = await (await fetch('/api/clipboard')).json();
      $('pc-text').value = r.text || '';
      toast(r.text ? '已读取教学机剪切板' : '教学机剪切板为空');
    } catch (e) { toast('读取失败'); }
  });
  $('clip-copy').addEventListener('click', async () => {
    const t = $('pc-text').value;
    if (!t) { toast('没有可复制的内容'); return; }
    if (navigator.clipboard && navigator.clipboard.writeText) {
      try {
        await navigator.clipboard.writeText(t);
        toast('已复制到平板剪切板');
        return;
      } catch (e) {}
    }
    $('pc-text').select();
    toast('已选中文字，请长按复制');
  });
  $('clip-send').addEventListener('click', async () => {
    const t = $('pad-text').value;
    if (!t) { toast('请先输入或粘贴内容'); return; }
    try {
      const r = await (await fetch('/api/clipboard', {method:'POST',
        headers:{'Content-Type':'application/json'}, body: JSON.stringify({text: t})})).json();
      toast(r.ok ? '已发送到教学机剪切板' : '发送失败（剪切板被占用）');
    } catch (e) { toast('发送失败'); }
  });
  $('clip-read-pad').addEventListener('click', async () => {
    if (navigator.clipboard && navigator.clipboard.readText) {
      try {
        const t = await navigator.clipboard.readText();
        $('pad-text').value = t;
        toast(t ? '已读取平板剪切板' : '平板剪切板为空');
        return;
      } catch (e) {}
    }
    toast('浏览器不允许读剪切板，请长按粘贴');
  });

  // ---- 状态轮询 ----
  async function refresh() {
    try {
      const d = await (await fetch('/api/status', {cache:'no-store'})).json();
      $('conn').classList.remove('off');
      $('conn-text').textContent = '在线';
      // 课件归档
      $('target').textContent = d.target_root;
      $('seen').textContent = d.seen_count;
      $('drives').textContent = d.drives.length
        ? d.drives.map(x => (x.label || x.drive) + ' (' + x.drive + ')').join('、')
        : '未检测到';
      const rows = $('rows');
      if (!d.log.length) {
        rows.innerHTML = '<tr><td colspan="3" class="empty">暂无记录</td></tr>';
      } else {
        rows.innerHTML = d.log.map(e =>
          '<tr><td>' + esc(e.ts) + '</td><td class="src">' + esc(e.source || '') + '</td>'
          + '<td class="lvl-' + esc(e.level) + '">' + esc(e.message) + '</td></tr>'
        ).join('');
      }
      // 截屏
      const s = d.screenshot;
      $('shot_enabled').textContent = s.enabled ? '运行中' : '已停用';
      $('shot_last').textContent = s.last_shot || '-';
      $('shot_dir').textContent = s.current_dir || '-';
      $('shot_count').textContent = s.shots;
      $('shot_root').textContent = s.context_root;
      $('shot_ret').textContent = s.retention_days > 0 ? s.retention_days + ' 天' : '不清理';
    } catch (e) {
      $('conn').classList.add('off');
      $('conn-text').textContent = '离线';
    }
  }

  loadRoots();
  refresh();
  setInterval(refresh, 2000);
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# 命令行入口
# ---------------------------------------------------------------------------

def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="教学机多功能服务")
    parser.add_argument("--config", help="JSON 配置文件路径")
    parser.add_argument("--host")
    parser.add_argument("--port", type=int)
    parser.add_argument("--target-root", help="课件归档根目录，例如 D:\\所有已知课件")
    parser.add_argument("--context-root", help="截图根目录，例如 D:\\context")
    parser.add_argument("--state-dir", help="状态/日志目录")
    parser.add_argument("--auth-file", help="包含网页登录密码哈希和会话密钥的 JSON 文件")
    parser.add_argument("--scan-interval", type=int)
    parser.add_argument("--scan-once", action="store_true",
                        help="只跑一次课件扫描然后退出")
    parser.add_argument("--shot-once", help="截一张屏存到指定路径后退出")
    return parser


def load_config(args: argparse.Namespace) -> Config:
    cfg = Config.from_file(args.config) if args.config else Config()
    if args.host:
        cfg.host = args.host
    if args.port:
        cfg.port = args.port
    if args.target_root:
        cfg.target_root = args.target_root
    if args.context_root:
        cfg.context_root = args.context_root
    if args.state_dir:
        cfg.state_dir = args.state_dir
    if args.auth_file:
        cfg.auth_file = args.auth_file
    if cfg.auth_file:
        auth = json.loads(Path(cfg.auth_file).read_text(encoding="utf-8-sig"))
        cfg.password_hash = str(auth.get("password_hash", ""))
        cfg.session_secret = str(auth.get("session_secret", ""))
        cfg.session_days = int(auth.get("session_days", cfg.session_days))
    if args.scan_interval:
        cfg.scan_interval = args.scan_interval
    cfg.finalize()
    return cfg


def main() -> int:
    args = build_arg_parser().parse_args()
    config = load_config(args)
    state = State(config.state_dir)

    if args.shot_once:
        png = capture_screen()
        if png is None:
            print("截屏失败")
            return 1
        out = Path(args.shot_once)
        out.write_bytes(png)
        print(f"已保存: {out} ({len(png)} 字节)")
        return 0

    monitor = Monitor(config, state)
    if args.scan_once:
        monitor.scan_once()
        for e in state.snapshot_log(50):
            print(f"[{e['level']}] {e['ts']} {e.get('source','')} {e['message']}")
        return 0

    if not config.password_hash or len(config.session_secret) < 16:
        raise SystemExit("有效的 --auth-file 是启动 7000 服务的必需项")

    capturer = ScreenCapturer(config, state)
    filestore = FileStore(config)
    display = DisplayManager()
    server = MonitorServer((config.host, config.port), config, state,
                           monitor, capturer, filestore, display)

    threading.Thread(target=monitor.run, daemon=True).start()
    if config.screenshot_enabled:
        threading.Thread(target=capturer.run, daemon=True).start()

    url = f"http://{config.host}:{config.port}/"
    print(f"{APP_NAME} {VERSION}")
    print(f"URL: {url}")
    print(f"课件根目录: {config.target_root}")
    print(f"截图根目录: {config.context_root}")
    print(f"截屏: {'开启' if config.screenshot_enabled else '关闭'}"
          f"（每 {config.screenshot_interval_seconds} 秒一张，"
          f"保留 {config.screenshot_retention_days} 天）")
    print(f"下载目录: {', '.join(config.download_dirs)}")
    print(f"状态目录: {config.state_dir}")
    print("按 Ctrl+C 停止。")

    def shutdown(signum: int, frame: object) -> None:
        print("\n正在停止...")
        monitor.stop_event.set()
        capturer.stop_event.set()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, shutdown)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, shutdown)

    try:
        server.serve_forever()
    finally:
        server.server_close()
        monitor.stop_event.set()
        capturer.stop_event.set()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
