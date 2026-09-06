import ctypes
import json
import os
import threading
import time
import unittest
import urllib.error
import urllib.request
from ctypes import wintypes
from http.server import ThreadingHTTPServer
from unittest.mock import patch

from cursor_highlight import CursorHighlight, cursor_bitmap
from touchpad_gateway import Handler


class LocatorBitmapTests(unittest.TestCase):
    def test_transparent_center_and_premultiplied_pixels(self):
        size, pixels = cursor_bitmap()
        self.assertEqual(size, 64)
        self.assertEqual(pixels[(32 * size + 32) * 4:(32 * size + 32) * 4 + 4], bytes(4))
        self.assertEqual(pixels[:4], bytes(4))
        visible = 0
        for offset in range(0, len(pixels), 4):
            b, g, r, a = pixels[offset:offset + 4]
            self.assertLessEqual(max(b, g, r), a)
            self.assertLessEqual(a, 108, "locator should remain translucent")
            visible += a > 0
        self.assertGreater(visible, 60)
        self.assertLess(visible, 100, "four thin strokes, not a thick circle")
        for x, y in ((32, 12), (12, 32), (32, 52), (52, 32)):
            self.assertGreater(pixels[(y * size + x) * 4 + 3], 0)
        for y in range(24, 41):
            for x in range(24, 41):
                self.assertEqual(pixels[(y * size + x) * 4 + 3], 0, "center must stay clear")
        self.assertEqual(pixels[(15 * size + 15) * 4 + 3], 0, "no circular or diagonal outline")
        self.assertEqual(cursor_bitmap(192)[0], 128)

    def test_disabled_is_lazy_and_toggle_type_is_strict(self):
        highlight = CursorHighlight()
        self.assertFalse(highlight.set_enabled(False)["enabled"])
        self.assertIsNone(highlight._thread)
        with self.assertRaises(ValueError):
            highlight.set_enabled("false")
        highlight.close()
        with self.assertRaises(RuntimeError):
            highlight.set_enabled(True)


@unittest.skipUnless(os.name == "nt", "native Windows overlay")
class NativeHighlightTests(unittest.TestCase):
    def test_follows_without_input_focus_or_disabled_polling(self):
        # The test moves the indicator using a mocked position read, never the real mouse.
        user = ctypes.WinDLL("user32", use_last_error=True)
        user.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        user.GetForegroundWindow.restype = wintypes.HWND
        user.IsWindowVisible.argtypes = [wintypes.HWND]
        user.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
        user.GetWindowLongPtrW.restype = ctypes.c_ssize_t
        user.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        user.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, ctypes.c_size_t, ctypes.c_ssize_t]
        user.SendMessageW.restype = ctypes.c_ssize_t
        user.IsWindow.argtypes = [wintypes.HWND]
        foreground = user.GetForegroundWindow()
        highlight = CursorHighlight()
        try:
            self.assertTrue(highlight.set_enabled(True)["enabled"])
            hwnd = highlight._hwnd
            self.assertTrue(user.IsWindowVisible(hwnd))
            style = user.GetWindowLongPtrW(hwnd, -20)
            self.assertEqual(style & 0x080800A8, 0x080800A8)
            self.assertEqual(user.GetForegroundWindow(), foreground, "highlight must not activate")
            self.assertEqual(user.SendMessageW(hwnd, 0x84, 0, 0), -1)
            self.assertEqual(user.SendMessageW(hwnd, 0x21, 0, 0), 3)
            sample = {"x": 420, "y": 300, "ok": True, "reads": 0}

            def cursor_read(out):
                sample["reads"] += 1
                point = ctypes.cast(out, ctypes.POINTER(wintypes.POINT)).contents
                point.x, point.y = sample["x"], sample["y"]
                return sample["ok"]

            def wait_for(predicate):
                until = time.monotonic() + 2
                while time.monotonic() < until:
                    if predicate():
                        return
                    time.sleep(.01)
                self.fail("overlay state did not converge")

            def at_cursor():
                rect = wintypes.RECT()
                user.GetWindowRect(hwnd, ctypes.byref(rect))
                return (rect.left + (rect.right - rect.left) // 2, rect.top + (rect.bottom - rect.top) // 2) == (sample["x"], sample["y"])

            with patch.object(highlight._user32, "GetCursorPos", side_effect=cursor_read):
                wait_for(at_cursor)
                sample["x"], sample["y"] = -30, 80  # signed virtual-desktop coordinates
                wait_for(at_cursor)
                sample["ok"] = False
                wait_for(lambda: not user.IsWindowVisible(hwnd))
                sample["ok"] = True
                wait_for(lambda: user.IsWindowVisible(hwnd))
                self.assertFalse(highlight.set_enabled(False)["enabled"])
                self.assertFalse(user.IsWindowVisible(hwnd))
                reads = sample["reads"]
                time.sleep(.08)
                self.assertEqual(sample["reads"], reads, "disabled highlight must stop polling")
                for _ in range(5):
                    highlight.set_enabled(True)
                    self.assertEqual(highlight._hwnd, hwnd, "reuse the window")
                    highlight.set_enabled(False)
        finally:
            highlight.close()
        self.assertFalse(user.IsWindow(hwnd))
        self.assertFalse(highlight._thread.is_alive())


class HighlightHttpTests(unittest.TestCase):
    def test_shared_state_validation_and_auth(self):
        class FakeHighlight:
            enabled = False
            def status(self):
                return {"ok": True, "enabled": self.enabled, "error": ""}
        class FakePointer:
            crosshair = FakeHighlight()
            guides = FakeHighlight()
            def screen(self):
                return {"x": 0, "y": 0, "width": 1920, "height": 1080}
            def cursor(self):
                return {"x": 500, "y": 400}
            def set_crosshair(self, enabled):
                return self.set_overlay(self.crosshair, enabled)
            def set_guides(self, enabled):
                return self.set_overlay(self.guides, enabled)
            def set_overlay(self, overlay, enabled):
                if type(enabled) is not bool:
                    raise ValueError("enabled must be a boolean")
                overlay.enabled = enabled
                return overlay.status()
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.config, server.pointer = {}, FakePointer()
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        base = f"http://127.0.0.1:{server.server_port}"
        def request(path, enabled=None):
            data = None if enabled is None else json.dumps({"enabled": enabled}).encode()
            req = urllib.request.Request(base + path, data=data, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req) as response:
                return json.load(response)
        try:
            for route, field in [('/crosshair', 'highlight'), ('/guides', 'guides')]:
                self.assertFalse(request(route)["enabled"])
                self.assertTrue(request(route, True)["enabled"])
                self.assertTrue(request('/state')[field]["enabled"])
                other = 'guides' if field == 'highlight' else 'highlight'
                self.assertFalse(request('/state')[other]["enabled"], "overlay controls are independent")
                self.assertFalse(request(route, False)["enabled"])
                with self.assertRaises(urllib.error.HTTPError) as error:
                    request(route, "false")
                self.assertEqual(error.exception.code, 400)
                with patch.object(Handler, "_auth_ok", return_value=False):
                    for enabled in (None, True):
                        with self.assertRaises(urllib.error.HTTPError) as error:
                            request(route, enabled)
                        self.assertEqual(error.exception.code, 401)
        finally:
            server.shutdown()
            server.server_close()
            worker.join()


if __name__ == "__main__":
    unittest.main()
