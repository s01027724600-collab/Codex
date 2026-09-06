import ctypes
import base64
import hashlib
import io
import json
import threading
import unittest
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from unittest.mock import Mock, patch

import course_monitor as app


class ReleaseTests(unittest.TestCase):
    def test_capture_runs_and_frees_gdi_on_success_and_failure(self):
        for copied in (True, False):
            user, gdi = Mock(), Mock()
            user.GetSystemMetrics.return_value = 2
            user.GetWindowDC.return_value = 11
            gdi.CreateCompatibleDC.return_value = 22
            gdi.CreateCompatibleBitmap.return_value = 33
            gdi.BitBlt.return_value = copied
            gdi.GetDIBits.return_value = 2
            with patch.object(app, 'is_windows', return_value=True), \
                    patch.object(app, '_u32', user), patch.object(app, '_g32', gdi):
                result = app.capture_screen()
            if copied:
                self.assertTrue(result.startswith(b'\x89PNG'))
            else:
                self.assertIsNone(result)
            gdi.DeleteObject.assert_called_once_with(33)
            gdi.DeleteDC.assert_called_once_with(22)
            user.ReleaseDC.assert_called_once_with(0, 11)

    def test_scan_recovers_after_state_write_failure(self):
        state = Mock()
        state.flush_file_index.side_effect = [OSError('disk full'), None]
        monitor = app.Monitor(app.Config(), state)
        monitor._scan_once_unlocked = Mock()
        with self.assertRaises(OSError):
            monitor.scan_once()
        self.assertFalse(monitor._scan_lock.locked())
        self.assertTrue(monitor.scan_once())

    def test_zip_names_are_unique_even_with_numbered_originals(self):
        store = app.FileStore.__new__(app.FileStore)
        store.safe_resolve = lambda p: Path(p)
        stream = io.BytesIO()
        def write(zf, filename, arcname=None):
            zf.writestr(arcname, str(filename))
        with patch.object(Path, 'is_file', return_value=True), \
                patch.object(app.tempfile, 'SpooledTemporaryFile', return_value=stream), \
                patch.object(zipfile.ZipFile, 'write', write):
            archive, _ = store.build_zip(['C:/a/lesson.pdf', 'C:/b/lesson.pdf',
                                          'C:/c/lesson(1).pdf', 'C:/d/LESSON.pdf'])
            with zipfile.ZipFile(archive) as zf:
                names = zf.namelist()
                self.assertEqual(len(names), len({name.casefold() for name in names}))
                self.assertEqual(len(names), 4)
            archive.close()

    def test_shared_password_hash_is_accepted(self):
        salt = b'0123456789abcdef'
        digest = hashlib.pbkdf2_hmac('sha256', b'classroom-password', salt, 1000)
        encode = lambda value: base64.urlsafe_b64encode(value).decode().rstrip('=')
        encoded = f'pbkdf2_sha256$1000${encode(salt)}${encode(digest)}'
        self.assertTrue(app.verify_password('classroom-password', encoded))
        self.assertFalse(app.verify_password('wrong-password', encoded))

    def test_release_ui_requires_login_and_runtime_passes_auth_file(self):
        root = Path(__file__).resolve().parents[1]
        ui = (root / 'p7000' / 'ui.html').read_text(encoding='utf-8')
        runtime = (root / 'release-tools' / 'template' / 'gateway_runtime.ps1').read_text(
            encoding='utf-8-sig')
        self.assertIn('id="login-form"', ui)
        self.assertIn("fetchJSON('/login'", ui)
        self.assertIn("$arguments += @('--auth-file',$AuthFile)", runtime)
        self.assertNotIn("if ($Port -ne 7000) { $arguments += @('--auth-file'", runtime)
        self.assertIn('$password.Length -ge 6', runtime)

    def test_forwarded_header_is_not_used_as_remote_address(self):
        source = Path(app.__file__).read_text(encoding='utf-8')
        self.assertNotIn('self.headers.get("X-Forwarded-For"', source)

    def test_remote_api_requires_login_and_accepts_session_cookie(self):
        salt = b'0123456789abcdef'
        digest = hashlib.pbkdf2_hmac('sha256', b'classroom-password', salt, 1000)
        encode = lambda value: base64.urlsafe_b64encode(value).decode().rstrip('=')
        config = app.Config(
            password_hash=f'pbkdf2_sha256$1000${encode(salt)}${encode(digest)}',
            session_secret='a-session-secret-long-enough',
        )
        filestore = Mock()
        filestore.roots_json.return_value = {'roots': []}
        monitor = Mock()
        monitor.start_scan_async.return_value = True
        server = app.MonitorServer(
            ('127.0.0.1', 0), config, Mock(), monitor, Mock(), filestore, Mock())
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        root_url = f'http://127.0.0.1:{server.server_address[1]}'
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor())
        try:
            with patch.object(app.MonitorHandler, '_remote', return_value='192.0.2.1'):
                with self.assertRaises(urllib.error.HTTPError) as raised:
                    opener.open(root_url + '/api/roots')
                self.assertEqual(raised.exception.code, 401)
                request = urllib.request.Request(
                    root_url + '/login',
                    data=json.dumps({'password': 'classroom-password'}).encode(),
                    headers={'Content-Type': 'application/json'},
                )
                with opener.open(request) as response:
                    self.assertTrue(json.load(response)['ok'])
                with opener.open(root_url + '/api/roots') as response:
                    self.assertEqual(json.load(response), {'roots': []})
                with self.assertRaises(urllib.error.HTTPError) as raised:
                    opener.open(root_url + '/api/scan')
                self.assertEqual(raised.exception.code, 404)
                scan = urllib.request.Request(root_url + '/api/scan', data=b'', method='POST')
                with opener.open(scan) as response:
                    self.assertTrue(json.load(response)['started'])
        finally:
            server.shutdown()
            server.server_close()
            worker.join()


if __name__ == '__main__':
    unittest.main()
