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

    def test_firewall_allows_only_local_subnet_on_every_profile(self):
        self.assertIn("-Profile Any -RemoteAddress LocalSubnet", RUNTIME)
        self.assertIn("Set-NetFirewallAddressFilter -RemoteAddress LocalSubnet", RUNTIME)
        self.assertNotIn("-Profile Domain,Private", RUNTIME)

    def test_operations_and_build_guides_are_tracked(self):
        build = ROOT / "docs" / "BUILD_UPDATE_REPLACE.md"
        operations = ROOT / "docs" / "RUNTIME_OPERATIONS.md"
        self.assertIn("回滚", build.read_text(encoding="utf-8"))
        self.assertIn("ERR_EMPTY_RESPONSE", operations.read_text(encoding="utf-8"))
        self.assertTrue((ROOT / "release-tools" / "template" / "disable-autostart.cmd").is_file())


if __name__ == "__main__":
    unittest.main()
