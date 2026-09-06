import threading
import unittest
from unittest.mock import Mock, patch
from touchpad_gateway import PointerController, UiaScanWorker, ScanAssist


class ReleaseTests(unittest.TestCase):
    def test_button_lease_expires_and_heartbeat_cannot_resurrect_it(self):
        pointer = PointerController.__new__(PointerController)
        pointer._input_lock = threading.RLock()
        pointer.user32 = Mock()
        pointer._held, pointer._hold_deadline = False, 0
        with patch('touchpad_gateway.time.monotonic', return_value=10):
            pointer.button('down')
            self.assertTrue(pointer.renew_hold()['held'])
        with patch('touchpad_gateway.time.monotonic', return_value=15):
            pointer.expire_buttons()
            self.assertFalse(pointer.renew_hold()['held'])
        events = [call.args[0] for call in pointer.user32.mouse_event.call_args_list]
        self.assertEqual(events, [2, 4, 16])

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
