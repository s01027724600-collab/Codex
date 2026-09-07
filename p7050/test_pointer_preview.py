import base64
import io
import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from unittest.mock import patch

from PIL import Image

from touchpad_gateway import Handler, PointerPreview


class FakePointer:
    def __init__(self):
        self.geometry = {"x": -1200, "y": -100, "width": 2400, "height": 1200}
        self.position = {"x": -1199, "y": -99}

    def screen(self):
        return self.geometry.copy()

    def cursor(self):
        return self.position.copy()


class PointerPreviewTests(unittest.TestCase):
    def test_negative_monitor_origin_and_shared_capture(self):
        preview = PointerPreview(FakePointer())
        with patch("touchpad_gateway.ImageGrab.grab", side_effect=lambda **_: Image.new("RGB", (2400, 1200))) as grab:
            first = preview.snapshot()
            second = preview.snapshot()
        self.assertIs(first, second)
        self.assertEqual(grab.call_count, 1)
        self.assertEqual(first["crop"], {"x": -1200, "y": -100, "width": 960, "height": 540})
        for key, expected in (("overview", (512, 256)), ("detail", (960, 540))):
            encoded = first[key].split(",", 1)[1]
            with Image.open(io.BytesIO(base64.b64decode(encoded))) as frame:
                self.assertEqual(frame.size, expected)

    def test_detail_stays_steady_then_follows_only_the_exiting_axis(self):
        pointer = FakePointer()
        pointer.position = {"x": 0, "y": 500}
        preview = PointerPreview(pointer)
        with patch("touchpad_gateway.ImageGrab.grab", side_effect=lambda **_: Image.new("RGB", (2400, 1200))):
            first = preview.snapshot()
            pointer.position = {"x": 50, "y": 470}
            preview._last_attempt = 0
            small_move = preview.snapshot()
            self.assertEqual(small_move["crop"], first["crop"])
            pointer.position = {"x": 300, "y": 470}
            preview._last_attempt = 0
            followed = preview.snapshot()
            self.assertEqual(followed["crop"]["x"], -180)
            self.assertEqual(followed["crop"]["y"], first["crop"]["y"])
            pointer.position = {"x": 280, "y": 480}
            preview._last_attempt = 0
            self.assertEqual(preview.snapshot()["crop"], followed["crop"])

    def test_detail_clamps_at_edges_and_resets_for_new_geometry(self):
        pointer = FakePointer()
        pointer.position = {"x": 1199, "y": 1099}
        preview = PointerPreview(pointer)
        def desktop(**_):
            return Image.new("RGB", (pointer.geometry["width"], pointer.geometry["height"]))
        with patch("touchpad_gateway.ImageGrab.grab", side_effect=desktop):
            self.assertEqual(preview.snapshot()["crop"], {"x": 240, "y": 560, "width": 960, "height": 540})
            pointer.position = {"x": 1180, "y": 1080}
            preview._last_attempt = 0
            self.assertEqual(preview.snapshot()["crop"], {"x": 240, "y": 560, "width": 960, "height": 540})
            pointer.geometry = {"x": -1280, "y": 0, "width": 1280, "height": 720}
            pointer.position = {"x": -300, "y": 400}
            preview._last_attempt = 0
            self.assertEqual(preview.snapshot()["crop"], {"x": -960, "y": 130, "width": 960, "height": 540})
            pointer.geometry = {"x": 0, "y": 0, "width": 640, "height": 360}
            pointer.position = {"x": 1, "y": 1}
            preview._last_attempt = 0
            self.assertEqual(preview.snapshot()["crop"], {"x": 0, "y": 0, "width": 640, "height": 360})

    def test_active_detail_can_omit_the_slow_overview_frame(self):
        preview = PointerPreview(FakePointer())
        with patch("touchpad_gateway.ImageGrab.grab", return_value=Image.new("RGB", (2400, 1200))) as grab:
            full = preview.snapshot()
            detail_only = preview.snapshot(include_overview=False)
        self.assertIn("overview", full)
        self.assertNotIn("overview", detail_only)
        self.assertIn("detail", detail_only)
        self.assertEqual(grab.call_count, 1)

    def test_capture_failure_is_throttled_and_recovers(self):
        preview = PointerPreview(FakePointer())
        with patch("touchpad_gateway.ImageGrab.grab", side_effect=OSError("desktop unavailable")) as grab:
            for _ in range(2):
                with self.assertRaisesRegex(RuntimeError, "desktop unavailable"):
                    preview.snapshot()
            self.assertEqual(grab.call_count, 1)
        preview._last_attempt = 0
        with patch("touchpad_gateway.ImageGrab.grab", return_value=Image.new("RGB", (2400, 1200))):
            self.assertTrue(preview.snapshot()["ok"])

    def test_mismatched_display_dimensions_are_not_returned(self):
        preview = PointerPreview(FakePointer())
        with patch("touchpad_gateway.ImageGrab.grab", return_value=Image.new("RGB", (1200, 600))):
            with self.assertRaisesRegex(RuntimeError, "desktop size changed"):
                preview.snapshot()

    def test_http_success_auth_and_capture_failure(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.config = {}
        server.pointer_preview = PointerPreview(FakePointer())
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        url = f"http://127.0.0.1:{server.server_address[1]}/pointer-view"
        try:
            with patch.object(server.pointer_preview, "snapshot", return_value={"ok": True}):
                with urllib.request.urlopen(url) as response:
                    self.assertEqual(json.load(response), {"ok": True})
                    self.assertEqual(response.headers["Cache-Control"], "no-store")
                with patch.object(Handler, "_remote", return_value="192.0.2.1"):
                    with self.assertRaises(urllib.error.HTTPError) as raised:
                        urllib.request.urlopen(url)
                self.assertEqual(raised.exception.code, 401)
            with patch.object(server.pointer_preview, "snapshot", side_effect=RuntimeError("desktop unavailable")):
                with self.assertRaises(urllib.error.HTTPError) as raised:
                    urllib.request.urlopen(url)
                self.assertEqual(raised.exception.code, 503)
                self.assertEqual(json.load(raised.exception)["error"], "desktop unavailable")
        finally:
            server.shutdown()
            server.server_close()
            worker.join()


if __name__ == "__main__":
    unittest.main()
