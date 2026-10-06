"""Trusted local rules process; no executable/path/seed is accepted from HTTP.

The adapter is intentionally replaceable for a later Linux rules implementation.
Never fall back to a client-supplied draw, price, reward, or inventory snapshot.
"""

import json
import subprocess
import tempfile
from pathlib import Path

from fastapi import HTTPException

from app.config import settings
from app.observability import current_request_id, logger
from app.performance import timed


class SwiftRules:
    supports_context = True
    backend = "swift"

    def __init__(self, executable: str, timeout: float = 60):
        self.executable = executable
        self.timeout = timeout
        self.configured = bool(executable)

    @timed("rules")
    def apply(self, state: dict, command: dict, *, prices=None, protected=None):
        if not self.executable or not Path(self.executable).is_file():
            raise HTTPException(503, "rules_engine_unavailable")
        # Never inherit provider credentials, local account preferences or a
        # production PPB_STATE_DIR. Only the packaged catalogue is required.
        with tempfile.TemporaryDirectory(prefix="ppb-server-engine-") as directory:
            environment = {
                "PATH": "/usr/bin:/bin",
                "TMPDIR": directory,
                "PPB_STATE_DIR": directory,
                "PPB_OFFLINE": "1",
                "LANG": "en_US.UTF-8",
            }
            if prices is not None:
                price_file = Path(directory) / "prices.json"
                price_file.write_text(json.dumps(prices))
                environment["PPB_RULE_PRICES"] = str(price_file)
            try:
                result = subprocess.run(
                    [self.executable, "--server-rules"],
                    input=json.dumps(
                        {"state": state, "command": command, "protected": protected or {}}
                    ).encode(),
                    capture_output=True,
                    timeout=self.timeout,
                    env=environment,
                    check=False,
                )
            except subprocess.TimeoutExpired as error:
                raise HTTPException(503, "rules_engine_timeout") from error
            except OSError as error:
                raise HTTPException(503, "rules_engine_unavailable") from error
        if result.returncode == 2:
            raise HTTPException(409, "game_precondition_failed")
        if result.returncode != 0:
            # Exit 2 is a rejected command; anything else is a crash worth keeping.
            logger.error(
                "rules engine exited %s (request %s): %s",
                result.returncode,
                current_request_id.get(),
                result.stderr.decode(errors="replace")[-400:].strip(),
            )
            raise HTTPException(503, "rules_engine_failed")
        try:
            output = json.loads(result.stdout)
            if not isinstance(output["state"], dict) or not isinstance(output["result"], dict):
                raise ValueError("Invalid rule output")
            return output["state"], output["result"], str(output["rules_version"])
        except (KeyError, ValueError, TypeError) as error:
            raise HTTPException(503, "rules_engine_invalid_output") from error


if settings.rules_backend == "swift":
    # Explicit rollback/oracle only. Python errors never silently switch engines.
    rules = SwiftRules(settings.rules_executable, settings.rules_timeout)
else:
    from app.native_rules import PythonRules

    rules = PythonRules(settings.rules_data_directory or None)
