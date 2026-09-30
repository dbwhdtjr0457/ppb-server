#!/usr/bin/env python3
"""Generate PPB launchd agents for an already prepared release and tunnel.

This does not change power settings, start a service, or migrate a database.
Agents run after this macOS user logs in; FileVault still requires unlocking
after reboot. caffeinate prevents system sleep on AC while each service runs.
"""

import argparse
import os
import plistlib
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--service-root", type=Path, required=True)
    parser.add_argument("--release", type=Path, required=True)
    parser.add_argument("--cloudflared", type=Path, required=True)
    parser.add_argument("--tunnel-config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root, release = args.service_root.resolve(), args.release.resolve()
    python = release / ".venv/bin/python"
    runner = release / "scripts/run-service.py"
    for required in (python, runner, args.cloudflared, args.tunnel_config, root / "server.env"):
        if not required.is_file():
            parser.error(f"Required deployment input is missing: {required}")
    args.output.mkdir(parents=True, exist_ok=True)
    logs = root / "logs"
    logs.mkdir(parents=True, exist_ok=True, mode=0o700)
    plans = {
        "dev.wonyangs.ppb-server": [str(python), str(runner)],
        "dev.wonyangs.ppb-tunnel": [
            str(args.cloudflared), "--no-autoupdate", "tunnel", "--config",
            str(args.tunnel_config.resolve()), "run",
        ],
    }
    for label, command in plans.items():
        payload = {
            "Label": label,
            "ProgramArguments": ["/usr/bin/caffeinate", "-s", *command],
            "WorkingDirectory": str(release),
            "EnvironmentVariables": {"PPB_SERVICE_ROOT": str(root), "PYTHONUNBUFFERED": "1"},
            "RunAtLoad": True,
            "KeepAlive": True,
            "ThrottleInterval": 15,
            "ExitTimeOut": 75,
            "ProcessType": "Background",
            "Umask": 0o077,
            "StandardOutPath": str(logs / f"{label}.out.log"),
            "StandardErrorPath": str(logs / f"{label}.err.log"),
        }
        destination = args.output / f"{label}.plist"
        with destination.open("wb") as output:
            plistlib.dump(payload, output)
        destination.chmod(0o600)
        print(destination)
    print(f"Prepared for gui/{os.getuid()}; activate after readiness checks")


if __name__ == "__main__":
    main()
