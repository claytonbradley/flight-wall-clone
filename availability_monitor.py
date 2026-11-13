#!/usr/bin/env python3
"""Recover Flightwall from backend, network, and operating-system stalls."""

from __future__ import annotations

import errno
import json
import os
import socket
import struct
import subprocess
import tempfile
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    state_file: Path
    local_url: str
    timeout_seconds: float
    local_failure_limit: int
    network_restart_limit: int
    network_reboot_limit: int
    gateway_port: int
    route_file: Path
    dry_run: bool

    @classmethod
    def from_environment(cls) -> "Settings":
        return cls(
            state_file=Path(
                os.environ.get(
                    "FLIGHTWALL_AVAILABILITY_STATE_FILE",
                    "/var/cache/flightwall/availability-monitor.json",
                )
            ),
            local_url=os.environ.get(
                "FLIGHTWALL_AVAILABILITY_LOCAL_URL", "http://127.0.0.1:8765/"
            ),
            timeout_seconds=float(
                os.environ.get("FLIGHTWALL_AVAILABILITY_TIMEOUT_SECONDS", "5")
            ),
            local_failure_limit=int(
                os.environ.get("FLIGHTWALL_AVAILABILITY_LOCAL_FAILURE_LIMIT", "2")
            ),
            network_restart_limit=int(
                os.environ.get("FLIGHTWALL_AVAILABILITY_NETWORK_RESTART_LIMIT", "3")
            ),
            network_reboot_limit=int(
                os.environ.get("FLIGHTWALL_AVAILABILITY_NETWORK_REBOOT_LIMIT", "6")
            ),
            gateway_port=int(
                os.environ.get("FLIGHTWALL_AVAILABILITY_GATEWAY_PORT", "53")
            ),
            route_file=Path(
                os.environ.get(
                    "FLIGHTWALL_AVAILABILITY_ROUTE_FILE", "/proc/net/route"
                )
            ),
            dry_run=os.environ.get("FLIGHTWALL_AVAILABILITY_DRY_RUN", "false")
            == "true",
        )


@dataclass
class HealthState:
    local_failures: int = 0
    network_failures: int = 0


def validate_settings(settings: Settings) -> None:
    if settings.timeout_seconds <= 0:
        raise ValueError("timeout must be positive")
    if settings.local_failure_limit < 1:
        raise ValueError("local failure limit must be at least one")
    if settings.network_restart_limit < 1:
        raise ValueError("network restart limit must be at least one")
    if settings.network_reboot_limit <= settings.network_restart_limit:
        raise ValueError("network reboot limit must exceed restart limit")
    if not 1 <= settings.gateway_port <= 65535:
        raise ValueError("gateway port must be between 1 and 65535")


def load_state(state_file: Path) -> HealthState:
    try:
        payload = json.loads(state_file.read_text(encoding="utf-8"))
        return HealthState(
            local_failures=max(0, int(payload.get("local_failures", 0))),
            network_failures=max(0, int(payload.get("network_failures", 0))),
        )
    except (FileNotFoundError, OSError, ValueError, TypeError, json.JSONDecodeError):
        return HealthState()


def save_state(state_file: Path, state: HealthState) -> None:
    state_file.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=state_file.parent, prefix=f".{state_file.name}.", text=True
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as temporary_file:
            json.dump(asdict(state), temporary_file, sort_keys=True)
            temporary_file.write("\n")
        os.chmod(temporary_name, 0o644)
        os.replace(temporary_name, state_file)
    finally:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass


def probe_local_http(url: str, timeout_seconds: float) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout_seconds) as response:
            response.read(1)
            return 200 <= response.status < 400
    except (OSError, ValueError):
        return False


def default_gateway(route_file: Path) -> str | None:
    try:
        route_lines = route_file.read_text(encoding="ascii").splitlines()[1:]
    except (OSError, UnicodeError):
        return None

    for route_line in route_lines:
        fields = route_line.split()
        if len(fields) < 4:
            continue
        destination, gateway_hex, flags_hex = fields[1:4]
        try:
            route_is_active = int(flags_hex, 16) & 0x1
            gateway_bytes = struct.pack("<L", int(gateway_hex, 16))
        except (ValueError, struct.error):
            continue
        if destination == "00000000" and route_is_active:
            return socket.inet_ntoa(gateway_bytes)
    return None


def probe_gateway(route_file: Path, port: int, timeout_seconds: float) -> bool:
    gateway = default_gateway(route_file)
    if gateway is None:
        return False
    try:
        with socket.create_connection((gateway, port), timeout=timeout_seconds):
            return True
    except ConnectionRefusedError:
        # A TCP rejection still proves that the gateway is reachable.
        return True
    except OSError as error:
        return error.errno == errno.ECONNREFUSED


def decide_actions(
    state: HealthState,
    settings: Settings,
    local_healthy: bool,
    network_healthy: bool,
) -> tuple[HealthState, list[tuple[str, str | None]]]:
    actions: list[tuple[str, str | None]] = []

    if local_healthy:
        state.local_failures = 0
    else:
        state.local_failures += 1
        if state.local_failures >= settings.local_failure_limit:
            actions.append(("restart", "flightwall.service"))
            state.local_failures = 0

    if network_healthy:
        state.network_failures = 0
    else:
        state.network_failures += 1
        if state.network_failures == settings.network_restart_limit:
            actions.append(("restart-network", None))
        elif state.network_failures >= settings.network_reboot_limit:
            actions.append(("reboot", None))

    return state, actions


def active_network_service() -> str | None:
    for service_name in (
        "NetworkManager.service",
        "systemd-networkd.service",
        "dhcpcd.service",
    ):
        result = subprocess.run(
            ["systemctl", "is-active", "--quiet", service_name], check=False
        )
        if result.returncode == 0:
            return service_name
    return None


def perform_action(action: tuple[str, str | None], dry_run: bool) -> None:
    action_name, target = action
    if action_name == "restart-network":
        target = active_network_service()
        if target is None:
            print("status=recovery-skipped action=restart-network reason=no-active-service")
            return
        action_name = "restart"

    if action_name == "restart" and target:
        command = ["systemctl", "restart", target]
    elif action_name == "reboot":
        command = ["systemctl", "reboot"]
    else:
        raise ValueError(f"unsupported recovery action: {action}")

    print(f"status=recovery action={' '.join(command)} dry_run={str(dry_run).lower()}")
    if not dry_run:
        subprocess.run(command, check=True)


def main() -> int:
    settings = Settings.from_environment()
    validate_settings(settings)
    state = load_state(settings.state_file)
    local_healthy = probe_local_http(settings.local_url, settings.timeout_seconds)
    network_healthy = probe_gateway(
        settings.route_file, settings.gateway_port, settings.timeout_seconds
    )
    state, actions = decide_actions(
        state, settings, local_healthy, network_healthy
    )
    save_state(settings.state_file, state)

    print(
        "status=checked "
        f"local_healthy={str(local_healthy).lower()} "
        f"network_healthy={str(network_healthy).lower()} "
        f"local_failures={state.local_failures} "
        f"network_failures={state.network_failures}"
    )
    for action in actions:
        perform_action(action, settings.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
