import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import availability_monitor


class AvailabilityMonitorTests(unittest.TestCase):
    def settings(self, state_file):
        return availability_monitor.Settings(
            state_file=state_file,
            local_url="http://127.0.0.1:8765/",
            timeout_seconds=1,
            local_failure_limit=2,
            network_restart_limit=3,
            network_reboot_limit=6,
            gateway_port=53,
            route_file=Path("/proc/net/route"),
            dry_run=True,
        )

    def test_healthy_checks_clear_failure_counts(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            settings = self.settings(Path(temporary_directory) / "state.json")
            state = availability_monitor.HealthState(1, 5)

            state, actions = availability_monitor.decide_actions(
                state, settings, local_healthy=True, network_healthy=True
            )

            self.assertEqual(availability_monitor.HealthState(), state)
            self.assertEqual([], actions)

    def test_two_local_failures_restart_backend(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            settings = self.settings(Path(temporary_directory) / "state.json")
            state = availability_monitor.HealthState(local_failures=1)

            state, actions = availability_monitor.decide_actions(
                state, settings, local_healthy=False, network_healthy=True
            )

            self.assertEqual(0, state.local_failures)
            self.assertEqual([("restart", "flightwall.service")], actions)

    def test_three_network_failures_restart_network_manager(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            settings = self.settings(Path(temporary_directory) / "state.json")
            state = availability_monitor.HealthState(network_failures=2)

            state, actions = availability_monitor.decide_actions(
                state, settings, local_healthy=True, network_healthy=False
            )

            self.assertEqual(3, state.network_failures)
            self.assertEqual([("restart-network", None)], actions)

    def test_six_network_failures_reboot_host(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            settings = self.settings(Path(temporary_directory) / "state.json")
            state = availability_monitor.HealthState(network_failures=5)

            state, actions = availability_monitor.decide_actions(
                state, settings, local_healthy=True, network_healthy=False
            )

            self.assertEqual(6, state.network_failures)
            self.assertEqual([("reboot", None)], actions)

    def test_default_gateway_is_read_from_linux_route_table(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            route_file = Path(temporary_directory) / "route"
            route_file.write_text(
                "Iface Destination Gateway Flags RefCnt Use Metric Mask MTU Window IRTT\n"
                "wlan0 00000000 0101A8C0 0003 0 0 600 00000000 0 0 0\n",
                encoding="ascii",
            )

            self.assertEqual("192.168.1.1", availability_monitor.default_gateway(route_file))

    def test_connection_refusal_proves_gateway_is_reachable(self):
        with patch.object(
            availability_monitor, "default_gateway", return_value="192.168.1.1"
        ), patch.object(
            availability_monitor.socket,
            "create_connection",
            side_effect=ConnectionRefusedError,
        ):
            self.assertTrue(
                availability_monitor.probe_gateway(Path("route"), 53, 1)
            )

    def test_state_round_trip_is_atomic_and_tolerates_corruption(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            state_file = Path(temporary_directory) / "state.json"
            expected_state = availability_monitor.HealthState(1, 4)
            availability_monitor.save_state(state_file, expected_state)
            self.assertEqual(expected_state, availability_monitor.load_state(state_file))

            state_file.write_text("not json", encoding="utf-8")
            self.assertEqual(
                availability_monitor.HealthState(),
                availability_monitor.load_state(state_file),
            )

    def test_deployment_enables_availability_and_hardware_watchdogs(self):
        project_root = Path(__file__).resolve().parents[1]
        deploy = (project_root / "deploy.sh").read_text(encoding="utf-8")
        service = (
            project_root / "systemd" / "flightwall-availability-monitor.service"
        ).read_text(encoding="utf-8")
        timer = (
            project_root / "systemd" / "flightwall-availability-monitor.timer"
        ).read_text(encoding="utf-8")
        watchdog = (
            project_root / "systemd" / "flightwall-watchdog.conf"
        ).read_text(encoding="utf-8")

        self.assertIn(
            "ExecStart=/usr/local/sbin/flightwall-availability-monitor", service
        )
        self.assertIn("CacheDirectory=flightwall", service)
        self.assertIn("OnUnitActiveSec=1min", timer)
        self.assertIn("Persistent=true", timer)
        self.assertIn("RuntimeWatchdogSec=30s", watchdog)
        self.assertIn("RebootWatchdogSec=5min", watchdog)
        self.assertIn(
            "systemctl enable --now flightwall-availability-monitor.timer", deploy
        )
        self.assertIn("systemctl daemon-reexec", deploy)
        self.assertIn("RuntimeWatchdogUSec", deploy)


if __name__ == "__main__":
    unittest.main()
