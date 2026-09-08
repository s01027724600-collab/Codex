import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from touchpad_gateway import Handler, PointerController, PointerPreview, UiaScanWorker, ScanAssist, clamp_sensitivity


class ReleaseTests(unittest.TestCase):
    def test_touch_sensitivity_is_limited_to_five_levels(self):
        self.assertEqual(clamp_sensitivity(0), 1.0)
        self.assertEqual(clamp_sensitivity(3), 3.0)
        self.assertEqual(clamp_sensitivity(9), 5.0)
        self.assertEqual(clamp_sensitivity('invalid', 2), 2.0)
        ui = Path(__file__).with_name('ui.html').read_text(encoding='utf-8')
        self.assertEqual(ui.count('data-sensitivity="'), 5)
        self.assertIn("Number(localStorage.getItem('touch-sensitivity')) || 3", ui)
        self.assertIn('sensitivity: touchSensitivity', ui)

    def test_preview_uses_fast_active_refresh_and_persistent_http(self):
        preview = PointerPreview(Mock())
        self.assertAlmostEqual(preview._min_interval, 1 / 6)
        self.assertEqual(Handler.protocol_version, 'HTTP/1.1')
        ui = Path(__file__).with_name('ui.html').read_text(encoding='utf-8')
        self.assertIn("/pointer-view?overview=", ui)
        self.assertIn('? 180 : (idleFor < 6000 ? 420 : 1400)', ui)

    @staticmethod
    def pointer_for_movement():
        pointer = PointerController.__new__(PointerController)
        pointer._input_lock = threading.RLock()
        pointer.user32 = Mock()
        pointer.user32.SetCursorPos.return_value = 1
        pointer.screen = Mock(return_value={"x": -100, "y": 0, "width": 200, "height": 100})
        pointer.cursor = Mock(return_value={"x": 12, "y": 34})
        return pointer

    def test_button_lease_expires_and_heartbeat_cannot_resurrect_it(self):
        pointer = PointerController.__new__(PointerController)
        pointer._input_lock = threading.RLock()
        pointer.user32 = Mock()
        pointer.user32.SendInput.side_effect = lambda count, _inputs, _size: count
        pointer._held, pointer._hold_deadline = False, 0
        with patch('touchpad_gateway.time.monotonic', return_value=10):
            pointer.button('down')
            self.assertTrue(pointer.renew_hold()['held'])
        with patch('touchpad_gateway.time.monotonic', return_value=15):
            pointer.expire_buttons()
            self.assertFalse(pointer.renew_hold()['held'])
        event_counts = [call.args[0] for call in pointer.user32.SendInput.call_args_list]
        self.assertEqual(event_counts, [1, 2])

    def test_click_is_injected_as_one_atomic_down_up_batch(self):
        pointer = PointerController.__new__(PointerController)
        pointer._input_lock = threading.RLock()
        pointer._held, pointer._hold_deadline = False, 0
        pointer.user32 = Mock()
        pointer.user32.SendInput.side_effect = lambda count, _inputs, _size: count
        pointer.button('click')
        pointer.user32.SendInput.assert_called_once()
        self.assertEqual(pointer.user32.SendInput.call_args.args[0], 2)

    def test_partial_sendinput_failure_is_reported(self):
        pointer = PointerController.__new__(PointerController)
        pointer.user32 = Mock()
        pointer.user32.SendInput.return_value = 0
        with self.assertRaisesRegex(OSError, 'injected 0 of 1'):
            pointer._send_mouse((1, 3, 4, 0))

    def test_movement_uses_cursor_position_not_synthetic_move_stream(self):
        pointer = self.pointer_for_movement()
        pointer.move_absolute(.5, .5)
        pointer.user32.ClipCursor.assert_called_once_with(None)
        pointer.user32.SetCursorPos.assert_called_once_with(0, 49)
        pointer.user32.SendInput.assert_not_called()

        pointer.user32.SetCursorPos.reset_mock()
        pointer.cursor.side_effect = [{"x": 90, "y": 95}, {"x": 90, "y": 95}]
        pointer.move_relative(20, 20, 1.0)
        self.assertEqual(pointer.user32.ClipCursor.call_count, 2)
        pointer.user32.SetCursorPos.assert_called_once_with(99, 99)
        pointer.user32.SendInput.assert_not_called()

    def test_cursor_position_failure_is_reported(self):
        pointer = self.pointer_for_movement()
        pointer.user32.SetCursorPos.return_value = 0
        with self.assertRaisesRegex(OSError, "SetCursorPos failed"):
            pointer.move_absolute(.5, .5)

    def test_ui_restarts_drain_when_input_arrives_during_finally_window(self):
        ui = (Path(__file__).with_name('ui.html')).read_text(encoding='utf-8')
        self.assertIn('if (logged && inputQueue.length) queueMicrotask(drainInput)', ui)

    def test_snapshot_timeout_stops_worker(self):
        worker = UiaScanWorker('', '')
        worker.proc = Mock()
        worker._start = Mock(return_value=True)
        gate = threading.Event()
        worker._read_line = lambda out: gate.wait(2)
        worker._stop = Mock(side_effect=gate.set)
        self.assertTrue(worker.snapshot(60, .15)['workerPending'])
        worker._stop.assert_called_once()

    def test_disabling_assist_stops_underlying_worker(self):
        worker, pointer = Mock(), Mock()
        pointer.screen.return_value = {}
        assist = ScanAssist(worker, pointer, {})
        self.assertFalse(assist.stop()['enabled'])
        worker.stop.assert_called_once()


if __name__ == '__main__':
    unittest.main()
