import ctypes
import io
import threading
import unittest
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
            with patch.object(app, '_u32', user), patch.object(app, '_g32', gdi):
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


if __name__ == '__main__':
    unittest.main()
