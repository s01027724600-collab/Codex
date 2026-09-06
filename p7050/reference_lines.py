"""Fixed thirds shared with the blind touchpad, not a cursor-following overlay.

Four thin, cached layered windows keep memory proportional to screen perimeter.
The one-second timer only checks screen geometry and restores topmost ordering;
unchanged geometry never uploads pixels. Disabled means no timer or screen reads.
"""
import ctypes
import os
import threading
from ctypes import wintypes

from cursor_highlight import CursorHighlight


GUIDE_COLOR = (77, 120, 164)


def guide_geometry(screen):
    """Use the same inclusive coordinate mapping as absolute_point()."""
    x, y, width, height = (int(screen[key]) for key in ("x", "y", "width", "height"))
    if width < 1 or height < 1:
        raise ValueError("screen dimensions must be positive")
    # Truncate after adding the signed origin, exactly as absolute_point does.
    # Adding an already-truncated offset is one pixel off at negative origins.
    xs = tuple(int(x + fraction * max(1, width - 1)) for fraction in (1 / 3, 2 / 3))
    ys = tuple(int(y + fraction * max(1, height - 1)) for fraction in (1 / 3, 2 / 3))
    return (x, y, width, height), xs, ys


def _pixel(color, opacity):
    alpha = round(255 * opacity)
    r, g, b = color
    return bytes((round(b * alpha / 255), round(g * alpha / 255), round(r * alpha / 255), alpha))


def guide_strips(screen):
    """Return four (x, y, width, height, premultiplied BGRA) strips.

    Each core is one physical pixel with a quiet light/dark one-pixel edge.
    Stronger 19-pixel cross arms clarify the four intersections without icons.
    """
    (x, y, width, height), xs, ys = guide_geometry(screen)
    normal = (_pixel((255, 255, 255), .13), _pixel(GUIDE_COLOR, .24), _pixel((15, 34, 59), .09))
    accent = (_pixel((255, 255, 255), .20), _pixel(GUIDE_COLOR, .40), _pixel((15, 34, 59), .14))
    result = []
    for line_x in xs:
        pixels = bytearray(b"".join(normal) * height)
        for line_y in ys:
            start, end = max(0, line_y - y - 9), min(height, line_y - y + 10)
            pixels[start * 12:end * 12] = b"".join(accent) * (end - start)
        result.append((line_x - 1, y, 3, height, bytes(pixels)))
    for line_y in ys:
        pixels = bytearray(b"".join(pixel * width for pixel in normal))
        for row in range(3):
            for line_x in xs:
                start, end = max(0, line_x - x - 9), min(width, line_x - x + 10)
                offset = (row * width + start) * 4
                pixels[offset:offset + (end - start) * 4] = accent[row] * (end - start)
        result.append((x, line_y - 1, width, 3, bytes(pixels)))
    return result


class ReferenceLines(CursorHighlight):
    """Owns native windows on one UI thread; accepts a virtual-screen provider."""
    CLASS_NAME = "P7050ReferenceLines"
    TITLE = "7050 Reference Lines"
    CHECK_INTERVAL_MS = 1000

    def __init__(self, screen_provider):
        super().__init__()
        self._screen_provider = screen_provider
        self._strips = []
        self._geometry = None

    def set_enabled(self, enabled):
        if type(enabled) is not bool:
            raise ValueError("enabled must be a boolean")
        with self._api_lock:
            if self._closed:
                raise RuntimeError("reference lines are closed")
            if not enabled and not self._thread:
                return self.status()
            if not self._thread or not self._thread.is_alive():
                if os.name != "nt":
                    raise RuntimeError("reference lines require Windows")
                self._ready.clear()
                self._error = ""
                self._thread = threading.Thread(target=self._run, name="7050-reference-lines", daemon=True)
                self._thread.start()
            if not self._ready.wait(3) or not self._hwnd:
                raise RuntimeError(self._error or "reference lines startup timed out")
            self._applied.clear()
            if not self._user32.PostMessageW(self._hwnd, self.WM_APPLY, int(enabled), 0):
                raise ctypes.WinError(ctypes.get_last_error())
            if not self._applied.wait(3):
                raise RuntimeError("reference lines toggle timed out")
            state = self.status()
            if not state["ok"]:
                raise RuntimeError(state["error"])
            return state

    def _upload_strip(self, strip, bounds):
        x, y, width, height, data = bounds
        info = self._BITMAPINFOHEADER()
        info.biSize, info.biWidth, info.biHeight = ctypes.sizeof(info), width, -height
        info.biPlanes, info.biBitCount = 1, 32
        bits = ctypes.c_void_p()
        bitmap = self._gdi32.CreateDIBSection(strip["dc"], ctypes.byref(info), 0, ctypes.byref(bits), None, 0)
        if not bitmap:
            raise ctypes.WinError(ctypes.get_last_error())
        ctypes.memmove(bits, data, len(data))
        previous = self._gdi32.SelectObject(strip["dc"], bitmap)
        if not previous or previous == ctypes.c_void_p(-1).value:
            self._gdi32.DeleteObject(bitmap)
            raise ctypes.WinError(ctypes.get_last_error())
        if not strip["original"]:
            strip["original"] = previous
        if strip["bitmap"]:
            self._gdi32.DeleteObject(strip["bitmap"])
        strip["bitmap"] = bitmap
        target, origin = wintypes.POINT(x, y), wintypes.POINT(0, 0)
        size = wintypes.SIZE(width, height)
        blend = self._BLENDFUNCTION(0, 0, 255, 1)
        if not self._user32.UpdateLayeredWindow(strip["hwnd"], None, ctypes.byref(target), ctypes.byref(size),
                                               strip["dc"], ctypes.byref(origin), 0, ctypes.byref(blend), 2):
            raise ctypes.WinError(ctypes.get_last_error())

    def _tick(self):
        screen = self._screen_provider()
        geometry, _, _ = guide_geometry(screen)
        if geometry != self._geometry:
            for strip, bounds in zip(self._strips, guide_strips(screen)):
                self._upload_strip(strip, bounds)
            self._geometry = geometry
        for strip in self._strips:
            # TOPMOST, NOMOVE, NOSIZE, NOACTIVATE, SHOWWINDOW. No input injection.
            if not self._user32.SetWindowPos(strip["hwnd"], wintypes.HWND(-1), 0, 0, 0, 0, 0x0053):
                raise ctypes.WinError(ctypes.get_last_error())

    def _hide(self):
        if self._hwnd:
            self._user32.KillTimer(self._hwnd, 1)
        for strip in self._strips:
            self._user32.ShowWindow(strip["hwnd"], 0)

    def _window_proc(self, hwnd, message, wparam, lparam):
        try:
            if message == self.WM_APPLY:
                if wparam:
                    self._tick()
                    if not self._user32.SetTimer(self._hwnd, 1, self.CHECK_INTERVAL_MS, None):
                        raise ctypes.WinError(ctypes.get_last_error())
                else:
                    self._hide()
                with self._state_lock:
                    self._enabled, self._error = bool(wparam), ""
                self._applied.set()
                return 0
            if message in (0x0113, 0x007E):  # WM_TIMER or WM_DISPLAYCHANGE
                if self.enabled:
                    self._tick()
                return 0
            if message == 0x0084:  # WM_NCHITTEST
                return -1  # HTTRANSPARENT
            if message == 0x0021:  # WM_MOUSEACTIVATE
                return 3  # MA_NOACTIVATE
            if message == 0x02E0:  # Physical screen coordinates; ignore suggested DPI rectangle.
                return 0
            if message == 0x0010:  # WM_CLOSE
                self._hide()
                for strip in reversed(self._strips):
                    self._user32.DestroyWindow(strip["hwnd"])
                return 0
            if message == 0x0002 and hwnd == self._hwnd:
                self._user32.PostQuitMessage(0)
                return 0
            return self._user32.DefWindowProcW(hwnd, message, wparam, lparam)
        except Exception as exc:
            with self._state_lock:
                self._enabled, self._error = False, str(exc)
            self._hide()
            # Force all strips to be repainted on retry after a partial upload failure.
            self._geometry = None
            self._applied.set()
            return 0

    def _run(self):
        registered = False
        self._strips = []
        self._geometry = None
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
            for index in range(4):
                hwnd = self._user32.CreateWindowExW(0x080800A8, self._class_name, f"{self.TITLE} {index + 1}",
                                                    0x80000000, 0, 0, 1, 1, None, None, instance, None)
                if not hwnd:
                    raise ctypes.WinError(ctypes.get_last_error())
                strip = {"hwnd": hwnd, "dc": None, "bitmap": None, "original": None}
                self._strips.append(strip)
                if index == 0:
                    self._hwnd = hwnd
                strip["dc"] = self._gdi32.CreateCompatibleDC(None)
                if not strip["dc"]:
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
            for strip in reversed(self._strips):
                self._user32.DestroyWindow(strip["hwnd"])
                if strip["dc"]:
                    if strip["original"]:
                        self._gdi32.SelectObject(strip["dc"], strip["original"])
                    if strip["bitmap"]:
                        self._gdi32.DeleteObject(strip["bitmap"])
                    self._gdi32.DeleteDC(strip["dc"])
            self._strips = []
            self._hwnd = None
            self._geometry = None
            if registered:
                self._user32.UnregisterClassW(self._class_name, instance)
            with self._state_lock:
                self._enabled = False
            self._ready.set()
            self._applied.set()
