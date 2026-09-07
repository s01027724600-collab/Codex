import threading
import unittest
from unittest.mock import Mock, patch
from touchpad_gateway import PointerController, UiaScanWorker, ScanAssist


class ReleaseTests(unittest.TestCase):
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
        pointer.user32.SetCursorPos.assert_called_once_with(0, 49)
        pointer.user32.SendInput.assert_not_called()

        pointer.user32.SetCursorPos.reset_mock()
        pointer.cursor.side_effect = [{"x": 90, "y": 95}, {"x": 90, "y": 95}]
        pointer.move_relative(20, 20, 1.0)
        pointer.user32.SetCursorPos.assert_called_once_with(99, 99)
        pointer.user32.SendInput.assert_not_called()

    def test_cursor_position_failure_is_reported(self):
        pointer = self.pointer_for_movement()
        pointer.user32.SetCursorPos.return_value = 0
        with self.assertRaisesRegex(OSError, "SetCursorPos failed"):
            pointer.move_absolute(.5, .5)

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
