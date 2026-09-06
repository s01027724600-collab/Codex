import ctypes
import os
import time
import unittest
from ctypes import wintypes
from unittest.mock import patch

from reference_lines import ReferenceLines, guide_geometry, guide_strips


class GuideBitmapTests(unittest.TestCase):
    def test_signed_inclusive_mapping_and_perimeter_memory(self):
        screen = {"x": -1920, "y": -100, "width": 3840, "height": 2160}
        geometry, xs, ys = guide_geometry(screen)
        self.assertEqual(geometry, (-1920, -100, 3840, 2160))
        self.assertEqual(xs, (-640, 639))
        self.assertEqual(ys, (619, 1339))
        strips = guide_strips(screen)
        self.assertEqual([item[:4] for item in strips],
                         [(-641, -100, 3, 2160), (638, -100, 3, 2160),
                          (-1920, 618, 3840, 3), (-1920, 1338, 3840, 3)])
        self.assertEqual(sum(len(item[4]) for item in strips), 2 * 3 * (3840 + 2160) * 4)

    def test_premultiplied_pixels_and_quiet_crossing_accents(self):
        strips = guide_strips({"x": 0, "y": 0, "width": 1920, "height": 1080})
        for x, y, width, height, data in strips:
            self.assertEqual(len(data), width * height * 4)
            for offset in range(0, len(data), 4):
                b, g, r, alpha = data[offset:offset + 4]
                self.assertLessEqual(max(b, g, r), alpha)
                self.assertLessEqual(alpha, 102)
        vertical = strips[0][4]
        self.assertEqual(vertical[7], 61)  # Normal blue core.
        self.assertEqual(vertical[(359 * 3 + 1) * 4 + 3], 102)  # Strengthened crossing.
        with self.assertRaises(ValueError):
            guide_geometry({"x": 0, "y": 0, "width": 0, "height": 1080})

    def test_disabled_laziness_and_strict_switch(self):
        with patch("reference_lines.guide_geometry") as geometry:
            overlay = ReferenceLines(lambda: {})
            self.assertEqual(overlay.set_enabled(False), {"ok": True, "enabled": False, "error": ""})
            self.assertIsNone(overlay._thread)
            geometry.assert_not_called()
            with self.assertRaises(ValueError):
                overlay.set_enabled("false")
            overlay.close()
            with self.assertRaises(RuntimeError):
                overlay.set_enabled(True)


@unittest.skipUnless(os.name == "nt", "native Windows overlay")
class NativeReferenceTests(unittest.TestCase):
    def test_lifecycle_mapping_resize_cache_and_no_focus_or_cursor_reads(self):
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
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.GetCurrentProcess.restype = wintypes.HANDLE
        user.GetGuiResources.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        user.GetGuiResources.restype = wintypes.DWORD
        process = kernel.GetCurrentProcess()
        initial_gdi = user.GetGuiResources(process, 0)
        state = {"screen": {"x": 0, "y": 0, "width": 1920, "height": 1080}, "reads": 0}

        def screen_provider():
            state["reads"] += 1
            return dict(state["screen"])

        foreground = user.GetForegroundWindow()
        overlay = ReferenceLines(screen_provider)
        handles = []
        try:
            self.assertTrue(overlay.set_enabled(True)["enabled"])
            handles = [strip["hwnd"] for strip in overlay._strips]
            self.assertEqual(len(handles), 4)
            self.assertEqual(user.GetForegroundWindow(), foreground)
            for hwnd in handles:
                self.assertTrue(user.IsWindowVisible(hwnd))
                self.assertEqual(user.GetWindowLongPtrW(hwnd, -20) & 0x080800A8, 0x080800A8)
                self.assertEqual(user.SendMessageW(hwnd, 0x84, 0, 0), -1)
                self.assertEqual(user.SendMessageW(hwnd, 0x21, 0, 0), 3)

            def assert_bounds():
                for hwnd, bounds in zip(handles, guide_strips(state["screen"])):
                    rect = wintypes.RECT()
                    self.assertTrue(user.GetWindowRect(hwnd, ctypes.byref(rect)))
                    self.assertEqual((rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top), bounds[:4])

            assert_bounds()
            with patch.object(overlay._user32, "GetCursorPos", side_effect=AssertionError("reference lines must not track mouse")), \
                    patch.object(overlay, "_upload_strip", wraps=overlay._upload_strip) as upload:
                user.SendMessageW(overlay._hwnd, 0x113, 1, 0)
                upload.assert_not_called()
                state["screen"] = {"x": -800, "y": -60, "width": 2400, "height": 1200}
                user.SendMessageW(overlay._hwnd, 0x7E, 0, 0)
                self.assertEqual(upload.call_count, 4)
                assert_bounds()
                self.assertTrue(overlay.status()["ok"])
                for _ in range(5):
                    self.assertFalse(overlay.set_enabled(False)["enabled"])
                    self.assertTrue(all(not user.IsWindowVisible(hwnd) for hwnd in handles))
                    reads = state["reads"]
                    # A stale already-queued timer message also must not read the screen.
                    user.SendMessageW(overlay._hwnd, 0x113, 1, 0)
                    self.assertEqual(state["reads"], reads)
                    self.assertTrue(overlay.set_enabled(True)["enabled"])
                    self.assertEqual([strip["hwnd"] for strip in overlay._strips], handles)
                self.assertEqual(upload.call_count, 4, "toggles reuse the cached four strip bitmaps")
                overlay.set_enabled(False)
                reads = state["reads"]
                time.sleep(1.1)
                self.assertEqual(state["reads"], reads, "disabled mode must have no timer")
        finally:
            overlay.close()
        self.assertTrue(all(not user.IsWindow(hwnd) for hwnd in handles))
        self.assertFalse(overlay._thread.is_alive())
        self.assertEqual(user.GetGuiResources(process, 0), initial_gdi, "release every DC and DIB")

    def test_render_failure_hides_every_strip_and_can_retry(self):
        state = {"width": 1920}
        overlay = ReferenceLines(lambda: {"x": 0, "y": 0, "width": state["width"], "height": 1080})
        try:
            overlay.set_enabled(True)
            user = overlay._user32
            user.IsWindowVisible.argtypes = [wintypes.HWND]
            state["width"] = 0
            with self.assertRaises(RuntimeError):
                overlay.set_enabled(True)
            self.assertFalse(overlay.status()["enabled"])
            self.assertFalse(overlay.status()["ok"])
            self.assertTrue(all(not user.IsWindowVisible(strip["hwnd"]) for strip in overlay._strips))
            state["width"] = 1920
            self.assertTrue(overlay.set_enabled(True)["enabled"])
            self.assertTrue(overlay.status()["ok"])
            state["width"] = 2400
            update = user.UpdateLayeredWindow
            calls = 0

            def partial_upload(*args):
                nonlocal calls
                calls += 1
                if calls == 2:
                    ctypes.set_last_error(8)
                    return False
                return update(*args)

            with patch.object(user, "UpdateLayeredWindow", side_effect=partial_upload):
                with self.assertRaises(RuntimeError):
                    overlay.set_enabled(True)
            self.assertEqual(calls, 2)
            self.assertFalse(overlay.status()["enabled"])
            self.assertTrue(all(not user.IsWindowVisible(strip["hwnd"]) for strip in overlay._strips))
            with patch.object(overlay, "_upload_strip", wraps=overlay._upload_strip) as upload:
                self.assertTrue(overlay.set_enabled(True)["enabled"])
                self.assertEqual(upload.call_count, 4, "retry must rebuild all strips after partial failure")
        finally:
            overlay.close()


if __name__ == "__main__":
    unittest.main()
