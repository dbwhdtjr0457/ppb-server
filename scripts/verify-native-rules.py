#!/usr/bin/env python3
"""Run isolated Python-versus-Swift parity checks without accessing any live API."""

import argparse
import os
import subprocess
import sys
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--swift-executable", required=True, type=Path)
    parser.add_argument("--resources-directory", required=True, type=Path)
    parser.add_argument("--filter", help="Optional pytest -k expression")
    parser.add_argument("--maxfail", type=int, default=0)
    args = parser.parse_args()
    executable = args.swift_executable.resolve()
    resources = args.resources_directory.resolve()
    if not executable.is_file() or not (resources / "native-rules.json").is_file():
        parser.error("The pinned Swift executable and matching resources must exist")
    repo = Path(__file__).resolve().parent.parent
    env = dict(os.environ)
    env["PPB_TEST_RULES_EXECUTABLE"] = str(executable)
    env["PPB_NATIVE_RULES_RESOURCES"] = str(resources)
    # The oracle adapter creates a fresh state/TMP directory for every invocation;
    # this script never opens the production database or inherits app credentials.
    command = [
        sys.executable,
        "-m",
        "pytest",
        "tests/test_native_parity.py",
        "-q",
        f"--maxfail={args.maxfail}",
    ]
    if args.filter:
        command.extend(["-k", args.filter])
    return subprocess.run(command, cwd=repo, env=env, check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
