import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import claude_gateway_agent as app


class GatewayReviewTests(unittest.TestCase):
    def test_cors_only_allows_explicit_configured_origin(self):
        handler = app.GatewayHandler.__new__(app.GatewayHandler)
        handler.server = SimpleNamespace(
            config=SimpleNamespace(cors_origins=["http://teacher.example"])
        )
        handler.headers = {"Origin": "http://teacher.example"}
        self.assertEqual(
            handler._cors_headers(),
            {
                "Access-Control-Allow-Origin": "http://teacher.example",
                "Access-Control-Allow-Credentials": "true",
                "Vary": "Origin",
            },
        )
        handler.headers = {"Origin": "http://student.example"}
        self.assertEqual(handler._cors_headers(), {})

    def test_failed_taskkill_falls_back_to_process_terminate(self):
        manager = app.JobManager.__new__(app.JobManager)
        process = Mock(pid=1234)
        process.poll.return_value = None
        with patch.object(app, "is_windows", return_value=True), \
                patch.object(app, "hidden_subprocess_kwargs", return_value={}), \
                patch.object(app.subprocess, "run", return_value=SimpleNamespace(returncode=1)):
            manager._terminate_process_tree(process)
        process.terminate.assert_called_once_with()

    def test_password_file_rejects_five_character_password(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "auth.json"
            with patch.object(app.getpass, "getpass", side_effect=["12345", "12345"]):
                with self.assertRaisesRegex(SystemExit, "at least 6"):
                    app.write_password_file(str(target))
            self.assertFalse(target.exists())


if __name__ == "__main__":
    unittest.main()
