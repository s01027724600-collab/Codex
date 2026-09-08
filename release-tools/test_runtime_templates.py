import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RUNTIME = (ROOT / "release-tools" / "template" / "gateway_runtime.ps1").read_text(
    encoding="utf-8-sig"
)


class RuntimeTemplateTests(unittest.TestCase):
    def test_supervisor_has_dual_autostart_and_stop_marker(self):
        self.assertIn("-Command supervise", RUNTIME)
        self.assertIn("Windows\\CurrentVersion\\Run", RUNTIME)
        self.assertIn("TeachingGateway.lnk", RUNTIME)
        self.assertIn("supervisor.lock", RUNTIME)
        self.assertIn("supervisor.stop", RUNTIME)
        self.assertIn("Start-Sleep -Seconds 8", RUNTIME)
        self.assertIn("$failureCounts[$port] -ge 3", RUNTIME)
        self.assertIn("Launch-Supervisor", RUNTIME)

    def test_shell_independent_scheduled_task_and_start_diagnostic(self):
        self.assertIn("Schedule.Service", RUNTIME)
        self.assertIn("TeachingGateway-Logon", RUNTIME)
        self.assertIn("$task.Triggers.Create(9)", RUNTIME)
        self.assertIn("$trigger.Delay = 'PT15S'", RUNTIME)
        self.assertIn("last-supervisor-start.json", RUNTIME)

    def test_supervisor_honors_authenticated_7000_touchpad_restart_request(self):
        self.assertIn("restart-7050.request", RUNTIME)
        self.assertIn("Stop-One 7050", RUNTIME)
        self.assertIn("Start-One 7050", RUNTIME)
        self.assertIn("Remove-Item -LiteralPath $Restart7050Marker", RUNTIME)

    def test_firewall_allows_only_local_subnet_on_every_profile(self):
        self.assertIn("-Profile Any -RemoteAddress LocalSubnet", RUNTIME)
        self.assertIn("Set-NetFirewallAddressFilter -RemoteAddress LocalSubnet", RUNTIME)
        self.assertNotIn("-Profile Domain,Private", RUNTIME)

    def test_operations_and_build_guides_are_tracked(self):
        build = ROOT / "docs" / "BUILD_UPDATE_REPLACE.md"
        operations = ROOT / "docs" / "RUNTIME_OPERATIONS.md"
        handoff = ROOT / "docs" / "LOCAL_CODEX_HANDOFF.md"
        self.assertIn("回滚", build.read_text(encoding="utf-8"))
        self.assertIn("ERR_EMPTY_RESPONSE", operations.read_text(encoding="utf-8"))
        handoff_text = handoff.read_text(encoding="utf-8")
        self.assertIn("p7000 `0.4.0`", handoff_text)
        self.assertIn("p7050 `0.3.2`", handoff_text)
        self.assertIn("可直接给本地 Codex 的任务文本", handoff_text)
        self.assertTrue((ROOT / "release-tools" / "template" / "disable-autostart.cmd").is_file())


if __name__ == "__main__":
    unittest.main()
