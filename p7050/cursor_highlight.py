"""Small, non-activating Windows cursor highlight; no screenshots or input injection.

Layered-window hit testing: https://learn.microsoft.com/windows/win32/winmsg/window-features
All window/GDI resources belong to one message-loop thread. Disabled means no timer.
"""
import ctypes
import os
import threading
import time
from ctypes import wintypes


def cursor_bitmap(dpi=96):
    """Four quiet one-DIP strokes; hollow center, no ring, outline or glow."""
    scale = max(96, min(480, int(dpi))) / 96
    size = round(64 * scale)
    center = size // 2
    pixels = bytearray(size * size * 4)
    for y in range(size):
        for x in range(size):
            dx, dy = abs(x - center), abs(y - center)
            along, across = max(dx, dy), min(dx, dy)
            coverage = max(0.0, min(1.0, .5 * scale + .5 - across,
                                     along - 9 * scale + .5, 26 * scale + .5 - along))
            alpha = round(255 * .42 * coverage)
            offset = (y * size + x) * 4
            pixels[offset:offset + 4] = bytes((round(164 * alpha / 255), round(120 * alpha / 255),
                                             round(77 * alpha / 255), alpha))
    return size, bytes(pixels)


class CursorHighlight:
    CLASS_NAME = "P7050CursorHighlight"
    TITLE = "7050 Cursor Highlight"
    WM_APPLY = 0x8001

    def __init__(self):
        self._api_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._ready = threading.Event()
        self._applied = threading.Event()
        self._thread = None
        self._hwnd = None
        self._enabled = False
        self._error = ""
        self._closed = False
        self._dc = self._bitmap = self._original_bitmap = None
        self._dpi = self._size = 0
        self._last_position = None
        self._last_raise = 0

    @property
    def enabled(self):
        with self._state_lock:
            return self._enabled

    def status(self):
        with self._state_lock:
            return {"ok": not bool(self._error), "enabled": self._enabled, "error": self._error}

    def set_enabled(self, enabled):
        if type(enabled) is not bool:
            raise ValueError("enabled must be a boolean")
        with self._api_lock:
            if self._closed:
                raise RuntimeError("cursor highlight is closed")
            if not enabled and not self._thread:
                return self.status()
            if not self._thread or not self._thread.is_alive():
                if os.name != "nt":
                    raise RuntimeError("cursor highlight requires Windows")
                self._ready.clear()
                self._error = ""
                self._thread = threading.Thread(target=self._run, name="7050-cursor-highlight", daemon=True)
                self._thread.start()
            if not self._ready.wait(3) or not self._hwnd:
                raise RuntimeError(self._error or "cursor highlight startup timed out")
            self._applied.clear()
            if not self._user32.PostMessageW(self._hwnd, self.WM_APPLY, int(enabled), 0):
                raise ctypes.WinError(ctypes.get_last_error())
            if not self._applied.wait(3):
                raise RuntimeError("cursor highlight toggle timed out")
            state = self.status()
            if not state["ok"]:
                raise RuntimeError(state["error"])
            return state

    def close(self):
        with self._api_lock:
            self._closed = True
            if self._hwnd:
                self._user32.PostMessageW(self._hwnd, 0x0010, 0, 0)  # WM_CLOSE
            if self._thread and self._thread is not threading.current_thread():
                self._thread.join(3)

    def _bind_api(self):
        self._user32 = u = ctypes.WinDLL("user32", use_last_error=True)
        self._gdi32 = g = ctypes.WinDLL("gdi32", use_last_error=True)
        self._kernel32 = k = ctypes.WinDLL("kernel32", use_last_error=True)
        self._wndproc_type = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT, ctypes.c_size_t, ctypes.c_ssize_t)

        class WNDCLASS(ctypes.Structure):
            _fields_ = [("style", wintypes.UINT), ("lpfnWndProc", self._wndproc_type),
                        ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                        ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
                        ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH),
                        ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR)]

        class BITMAPINFOHEADER(ctypes.Structure):
            _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
                        ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
                        ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                        ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                        ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
                        ("biClrImportant", wintypes.DWORD)]

        class BLENDFUNCTION(ctypes.Structure):
            _fields_ = [("BlendOp", ctypes.c_ubyte), ("BlendFlags", ctypes.c_ubyte),
                        ("SourceConstantAlpha", ctypes.c_ubyte), ("AlphaFormat", ctypes.c_ubyte)]

        self._WNDCLASS, self._BITMAPINFOHEADER, self._BLENDFUNCTION = WNDCLASS, BITMAPINFOHEADER, BLENDFUNCTION
        def bind(dll, name, result, *args):
            fn = getattr(dll, name)
            fn.restype, fn.argtypes = result, list(args)
        ptr = ctypes.c_void_p
        bind(k, "GetModuleHandleW", wintypes.HMODULE, wintypes.LPCWSTR)
        bind(u, "RegisterClassW", wintypes.ATOM, ctypes.POINTER(WNDCLASS))
        bind(u, "UnregisterClassW", wintypes.BOOL, wintypes.LPCWSTR, wintypes.HINSTANCE)
        bind(u, "CreateWindowExW", wintypes.HWND, wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR,
             wintypes.DWORD, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
             wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, ptr)
        bind(u, "DefWindowProcW", ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT, ctypes.c_size_t, ctypes.c_ssize_t)
        bind(u, "PostMessageW", wintypes.BOOL, wintypes.HWND, wintypes.UINT, ctypes.c_size_t, ctypes.c_ssize_t)
        bind(u, "GetMessageW", wintypes.BOOL, ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT)
        bind(u, "TranslateMessage", wintypes.BOOL, ctypes.POINTER(wintypes.MSG))
        bind(u, "DispatchMessageW", ctypes.c_ssize_t, ctypes.POINTER(wintypes.MSG))
        bind(u, "SetTimer", ctypes.c_size_t, wintypes.HWND, ctypes.c_size_t, wintypes.UINT, ptr)
        bind(u, "KillTimer", wintypes.BOOL, wintypes.HWND, ctypes.c_size_t)
        bind(u, "GetCursorPos", wintypes.BOOL, ctypes.POINTER(wintypes.POINT))
        bind(u, "GetDpiForWindow", wintypes.UINT, wintypes.HWND)
        bind(u, "ShowWindow", wintypes.BOOL, wintypes.HWND, ctypes.c_int)
        bind(u, "SetWindowPos", wintypes.BOOL, wintypes.HWND, wintypes.HWND,
             ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT)
        bind(u, "DestroyWindow", wintypes.BOOL, wintypes.HWND)
        bind(u, "PostQuitMessage", None, ctypes.c_int)
        bind(u, "UpdateLayeredWindow", wintypes.BOOL, wintypes.HWND, wintypes.HDC, ctypes.POINTER(wintypes.POINT),
             ctypes.POINTER(wintypes.SIZE), wintypes.HDC, ctypes.POINTER(wintypes.POINT),
             wintypes.COLORREF, ctypes.POINTER(BLENDFUNCTION), wintypes.DWORD)
        bind(g, "CreateCompatibleDC", wintypes.HDC, wintypes.HDC)
        bind(g, "CreateDIBSection", wintypes.HBITMAP, wintypes.HDC, ctypes.POINTER(BITMAPINFOHEADER),
             wintypes.UINT, ctypes.POINTER(ptr), wintypes.HANDLE, wintypes.DWORD)
        bind(g, "SelectObject", wintypes.HANDLE, wintypes.HDC, wintypes.HANDLE)
        bind(g, "DeleteObject", wintypes.BOOL, wintypes.HANDLE)
        bind(g, "DeleteDC", wintypes.BOOL, wintypes.HDC)

    def _render(self, dpi):
        size, data = cursor_bitmap(dpi)
        info = self._BITMAPINFOHEADER()
        info.biSize, info.biWidth, info.biHeight = ctypes.sizeof(info), size, -size
        info.biPlanes, info.biBitCount = 1, 32
        bits = ctypes.c_void_p()
        bitmap = self._gdi32.CreateDIBSection(self._dc, ctypes.byref(info), 0, ctypes.byref(bits), None, 0)
        if not bitmap:
            raise ctypes.WinError(ctypes.get_last_error())
        ctypes.memmove(bits, data, len(data))
        previous = self._gdi32.SelectObject(self._dc, bitmap)
        if not self._original_bitmap:
            self._original_bitmap = previous
        if self._bitmap:
            self._gdi32.DeleteObject(self._bitmap)
        self._bitmap, self._dpi, self._size = bitmap, dpi, size
        self._last_position = None

    def _tick(self):
        point = wintypes.POINT()
        if not self._user32.GetCursorPos(ctypes.byref(point)):
            self._user32.ShowWindow(self._hwnd, 0)
            self._last_position = None
            return
        dpi = self._user32.GetDpiForWindow(self._hwnd) or 96
        redraw = dpi != self._dpi
        if redraw:
            self._render(dpi)
        position = (point.x - self._size // 2, point.y - self._size // 2)
        now = time.monotonic()
        if position != self._last_position or now - self._last_raise >= 1:
            if redraw or self._last_position is None:
                target, origin = wintypes.POINT(*position), wintypes.POINT(0, 0)
                size = wintypes.SIZE(self._size, self._size)
                blend = self._BLENDFUNCTION(0, 0, 255, 1)  # AC_SRC_OVER, AC_SRC_ALPHA
                if not self._user32.UpdateLayeredWindow(self._hwnd, None, ctypes.byref(target), ctypes.byref(size),
                                                       self._dc, ctypes.byref(origin), 0, ctypes.byref(blend), 2):
                    raise ctypes.WinError(ctypes.get_last_error())
            # TOPMOST + NOACTIVATE; only the small locator moves, never the foreground window.
            if not self._user32.SetWindowPos(self._hwnd, wintypes.HWND(-1), *position,
                                            self._size, self._size, 0x0010 | 0x0040):
                raise ctypes.WinError(ctypes.get_last_error())
            self._last_position, self._last_raise = position, now

    def _window_proc(self, hwnd, message, wparam, lparam):
        try:
            if message == self.WM_APPLY:
                if wparam:
                    self._tick()
                    if not self._user32.SetTimer(hwnd, 1, 16, None):
                        raise ctypes.WinError(ctypes.get_last_error())
                else:
                    self._user32.KillTimer(hwnd, 1)
                    self._user32.ShowWindow(hwnd, 0)
                    self._last_position = None
                with self._state_lock:
                    self._enabled, self._error = bool(wparam), ""
                self._applied.set()
                return 0
            if message == 0x0113:  # WM_TIMER
                if self.enabled:
                    self._tick()
                return 0
            if message == 0x0084:  # WM_NCHITTEST, additional no-hit safeguard
                return -1
            if message == 0x0021:  # WM_MOUSEACTIVATE
                return 3
            if message == 0x02E0:  # WM_DPICHANGED: next tick redraws at the new monitor DPI
                return 0
            if message == 0x0010:
                self._user32.KillTimer(hwnd, 1)
                self._user32.DestroyWindow(hwnd)
                return 0
            if message == 0x0002:
                self._user32.PostQuitMessage(0)
                return 0
            return self._user32.DefWindowProcW(hwnd, message, wparam, lparam)
        except Exception as exc:
            with self._state_lock:
                self._enabled, self._error = False, str(exc)
            self._user32.KillTimer(hwnd, 1)
            self._user32.ShowWindow(hwnd, 0)
            self._applied.set()
            return 0

    def _run(self):
        registered = False
        try:
            self._bind_api()
            self._callback = self._wndproc_type(self._window_proc)
            instance = self._kernel32.GetModuleHandleW(None)
            self._class_name = self.CLASS_NAME + str(id(self))
            wc = self._WNDCLASS()
            wc.lpfnWndProc, wc.hInstance, wc.lpszClassName = self._callback, instance, self._class_name
            if not self._user32.RegisterClassW(ctypes.byref(wc)):
                raise ctypes.WinError(ctypes.get_last_error())
            registered = True
            # LAYERED | TRANSPARENT | TOOLWINDOW | NOACTIVATE | TOPMOST
            self._hwnd = self._user32.CreateWindowExW(0x080800A8, self._class_name, self.TITLE,
                                                     0x80000000, 0, 0, 64, 64, None, None, instance, None)
            if not self._hwnd:
                raise ctypes.WinError(ctypes.get_last_error())
            self._dc = self._gdi32.CreateCompatibleDC(None)
            if not self._dc:
                raise ctypes.WinError(ctypes.get_last_error())
            self._ready.set()
            message = wintypes.MSG()
            while True:
                result = self._user32.GetMessageW(ctypes.byref(message), None, 0, 0)
                if result == -1:
                    raise ctypes.WinError(ctypes.get_last_error())
                if not result:
                    break
                self._user32.TranslateMessage(ctypes.byref(message))
                self._user32.DispatchMessageW(ctypes.byref(message))
        except Exception as exc:
            with self._state_lock:
                self._error = str(exc)
        finally:
            if self._hwnd:
                self._user32.DestroyWindow(self._hwnd)
            self._hwnd = None
            if self._dc:
                if self._original_bitmap:
                    self._gdi32.SelectObject(self._dc, self._original_bitmap)
                if self._bitmap:
                    self._gdi32.DeleteObject(self._bitmap)
                self._gdi32.DeleteDC(self._dc)
            self._dc = self._bitmap = self._original_bitmap = None
            self._dpi = 0
            if registered:
                self._user32.UnregisterClassW(self._class_name, instance)
            with self._state_lock:
                self._enabled = False
            self._ready.set()
            self._applied.set()
