"""No credentials, account identifiers, or wallet snapshots belong in access logs."""

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    database_url: str = os.getenv("PPB_DATABASE_URL", "sqlite:///./ppb.db")
    rules_executable: str = os.getenv("PPB_RULES_EXECUTABLE", "")
    rules_backend: str = os.getenv("PPB_RULES_BACKEND", "python")
    rules_data_directory: str = os.getenv("PPB_RULES_DATA_DIRECTORY", "")
    rules_timeout: float = float(os.getenv("PPB_RULES_TIMEOUT", "60"))
    backup_directory: str = os.getenv("PPB_BACKUP_DIRECTORY", ".data/backups")
    backup_keep: int = max(1, int(os.getenv("PPB_BACKUP_KEEP", "14")))
    # A private gateway may set this; it is not a per-user login system.
    gateway_key: str = os.getenv("PPB_GATEWAY_KEY", "")
    trust_cloudflare_proxy: bool = os.getenv("PPB_TRUST_CLOUDFLARE_PROXY", "0") == "1"
    private_diagnostics: bool = os.getenv("PPB_PRIVATE_DIAGNOSTICS", "0") == "1"

    def __post_init__(self):
        if self.rules_backend not in {"python", "swift"}:
            raise ValueError("PPB_RULES_BACKEND must be python or swift")


settings = Settings()
