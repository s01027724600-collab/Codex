import ctypes
import base64
import hashlib
import io
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from unittest.mock import Mock, patch

import course_monitor as app


class ReleaseTests(unittest.TestCase):
    def test_system_controls_use_only_allowlisted_virtual_keys(self):
        user32 = Mock()
        controls = app.SystemControls(user32)
        result = controls.invoke('presentation_current')
        self.assertEqual(result['label'], '从当前页放映')
        self.assertEqual(
            [call.args for call in user32.keybd_event.call_args_list],
            [(0x10, 0, 0, 0), (0x74, 0, 0, 0),
             (0x74, 0, 2, 0), (0x10, 0, 2, 0)],
        )
        with self.assertRaisesRegex(ValueError, '不支持'):
            controls.invoke('run-arbitrary-command')

    def test_legacy_config_is_upgraded_to_scan_ppt_pptx_and_pdf(self):
        config = app.Config(extensions=['.pptx'])
        self.assertEqual(config.extensions, ['.pptx', '.pdf', '.ppt'])

    def test_scan_finds_legacy_ppt_and_reports_progress(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'usb'
            target = root / 'courses'
            state = app.State(str(root / 'state'))
            source.mkdir()
            (source / 'legacy.PPT').write_bytes(b'legacy presentation')
            monitor = app.Monitor(app.Config(target_root=str(target), state_dir=str(root / 'state')), state)
            with patch.object(app, 'list_removable_drives', return_value=[(str(source), '课堂U盘')]):
                self.assertTrue(monitor.scan_once())
            archived = target / '课堂U盘的课件' / 'legacy.PPT'
            self.assertEqual(archived.read_bytes(), b'legacy presentation')
            status = monitor.scan_status_json()
            self.assertFalse(status['running'])
            self.assertEqual((status['checked'], status['copied']), (1, 1))
            self.assertFalse(status['error'])

    def test_manual_scan_is_queued_when_a_scan_is_busy(self):
        state = Mock()
        monitor = app.Monitor(app.Config(), state)
        entered, release = threading.Event(), threading.Event()
        calls = []

        def scan():
            calls.append(1)
            if len(calls) == 1:
                entered.set()
                self.assertTrue(release.wait(2))

        monitor._perform_scan = scan
        self.assertEqual(monitor.start_scan_async(), {'started': True, 'queued': False})
        self.assertTrue(entered.wait(2))
        self.assertEqual(monitor.start_scan_async(), {'started': False, 'queued': True})
        self.assertTrue(monitor.scan_status_json()['queued'])
        release.set()
        deadline = app.time.monotonic() + 2
        while monitor._scan_lock.locked() and app.time.monotonic() < deadline:
            app.time.sleep(.01)
        self.assertEqual(len(calls), 2)
        self.assertFalse(monitor._scan_lock.locked())
        state.flush_file_index.assert_called_once()

    def test_touchpad_restart_request_is_scoped_to_auth_state_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = app.Config(
                auth_file=str(root / 'auth.json'),
                target_root=str(root / 'courses'),
                context_root=str(root / 'shots'),
                state_dir=str(root / 'state7000'),
            )
            state = app.State(config.state_dir)
            server = app.MonitorServer.__new__(app.MonitorServer)
            server.config, server.state = config, state
            result = server.request_touchpad_restart()
            marker = root / 'restart-7050.request'
            self.assertTrue(result['requested'])
            request = json.loads(marker.read_text(encoding='utf-8'))
            self.assertIn('requested_at', request)
            self.assertEqual(len(request['request_id']), 16)
            self.assertIn('快速重启 7050', state.snapshot_log()[-1]['message'])

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

    def test_projected_image_fit_preserves_aspect_ratio(self):
        self.assertEqual(app.fit_size(4000, 2000, 1920, 1080), (1920, 960))
        self.assertEqual(app.fit_size(1000, 2000, 1920, 1080), (540, 1080))

    def test_tablet_upload_streams_to_transfer_folder_without_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            config = app.Config(
                target_root=str(Path(directory) / 'courses'),
                context_root=str(Path(directory) / 'shots'),
                state_dir=str(Path(directory) / 'state'),
            )
            store = app.FileStore(config)
            first = store.save_upload('../lesson.pdf', io.BytesIO(b'first'), 5)
            second = store.save_upload('lesson.pdf', io.BytesIO(b'second'), 6)
            self.assertEqual(Path(first['path']).read_bytes(), b'first')
            self.assertEqual(Path(second['path']).read_bytes(), b'second')
            self.assertEqual(first['name'], 'lesson.pdf')
            self.assertEqual(second['name'], 'lesson (1).pdf')
            self.assertEqual(Path(first['directory']).name, '平板传输')

    def test_interrupted_tablet_upload_leaves_no_partial_file(self):
        with tempfile.TemporaryDirectory() as directory:
            config = app.Config(
                target_root=str(Path(directory) / 'courses'),
                context_root=str(Path(directory) / 'shots'),
                state_dir=str(Path(directory) / 'state'),
            )
            store = app.FileStore(config)
            with self.assertRaisesRegex(OSError, '提前中断'):
                store.save_upload('broken.bin', io.BytesIO(b'x'), 2)
            upload_dir = Path(directory) / 'courses' / '平板传输'
            self.assertEqual(list(upload_dir.iterdir()), [])

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
        self.assertIn('id="restart-touchpad"', ui)
        self.assertIn("fetchJSON('/api/restart-touchpad'", ui)
        self.assertIn('data-tab="controls"', ui)
        self.assertIn("fetchJSON('/api/control'", ui)
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
        filestore.save_upload.side_effect = lambda name, stream, length: {
            'ok': True, 'name': name, 'path': 'C:/courses/' + name,
            'directory': 'C:/courses', 'size': len(stream.read(length)),
            'size_h': '3B', 'is_image': False,
        }
        monitor = Mock()
        monitor.start_scan_async.return_value = {'started': True, 'queued': False}
        server = app.MonitorServer(
            ('127.0.0.1', 0), config, Mock(), monitor, Mock(), filestore, Mock())
        server.system_controls = Mock()
        server.system_controls.invoke.return_value = {
            'ok': True, 'action': 'volume_up', 'label': '音量提高'}
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
                control = urllib.request.Request(
                    root_url + '/api/control',
                    data=json.dumps({'action': 'volume_up'}).encode(),
                    headers={'Content-Type': 'application/json'}, method='POST')
                with opener.open(control) as response:
                    self.assertEqual(json.load(response)['label'], '音量提高')
                server.system_controls.invoke.assert_called_once_with('volume_up')
                upload = urllib.request.Request(
                    root_url + '/api/upload?name=notes.txt', data=b'abc', method='POST')
                with opener.open(upload) as response:
                    uploaded = json.load(response)
                self.assertEqual((uploaded['name'], uploaded['size']), ('notes.txt', 3))
        finally:
            server.shutdown()
            server.server_close()
            worker.join()


if __name__ == '__main__':
    unittest.main()
