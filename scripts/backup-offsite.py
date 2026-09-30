#!/usr/bin/env python3
"""One explicit, verified offsite backup; SSO sessions are not unattended credentials."""

import argparse
import base64
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.backups import verified_backup  # noqa: E402


def aws_json(args, *arguments):
    environment = os.environ.copy()
    environment["AWS_PAGER"] = ""
    environment["AWS_CLI_AUTO_PROMPT"] = "off"
    # Only the explicitly named profile supplies credentials and endpoints.
    for key in list(environment):
        if key in {"AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"} or (
            key.startswith("AWS_ENDPOINT_URL")
        ):
            environment.pop(key)
    result = subprocess.run(
        [
            "aws",
            "--profile",
            args.profile,
            "--region",
            args.region,
            "--output",
            "json",
            "--no-cli-pager",
            *arguments,
        ],
        env=environment,
        capture_output=True,
        text=True,
        timeout=600,
        check=True,
    )
    return json.loads(result.stdout or "{}")


def write_metadata(path, metadata):
    """Never include exception text, credentials, database rows or local paths."""
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as handle:
        temporary = Path(handle.name)
        try:
            json.dump(metadata, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


def upload_verified(args, path, key, content_type):
    with path.open("rb") as handle:
        digest = hashlib.file_digest(handle, "sha256")
    checksum = base64.b64encode(digest.digest()).decode("ascii")
    size = path.stat().st_size
    uploaded = aws_json(
        args,
        "s3api",
        "put-object",
        "--bucket",
        args.bucket,
        "--key",
        key,
        "--body",
        str(path),
        "--content-type",
        content_type,
        "--server-side-encryption",
        "AES256",
        "--checksum-algorithm",
        "SHA256",
        "--checksum-sha256",
        checksum,
        "--metadata",
        f"sha256={digest.hexdigest()}",
        "--expected-bucket-owner",
        args.expected_account,
        "--if-none-match",
        "*",
    )
    version = uploaded.get("VersionId")
    if not version or version == "null":
        raise ValueError("A versioned backup bucket is required")
    remote = aws_json(
        args,
        "s3api",
        "head-object",
        "--bucket",
        args.bucket,
        "--key",
        key,
        "--version-id",
        version,
        "--checksum-mode",
        "ENABLED",
        "--expected-bucket-owner",
        args.expected_account,
    )
    if (
        remote.get("ContentLength") != size
        or remote.get("ChecksumSHA256") != checksum
        or remote.get("ServerSideEncryption") != "AES256"
    ):
        raise ValueError("Uploaded backup verification failed")
    return {"key": key, "version_id": version, "size_bytes": size, "sha256": digest.hexdigest()}


def run(args):
    os.umask(0o077)
    if args.directory.is_symlink():
        raise ValueError("Backup directory must not be a symlink")
    args.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    status = {
        "started_at": datetime.now(UTC).isoformat(),
        "bucket": args.bucket,
        "region": args.region,
        "status": "failed",
        "stage": "identity",
    }
    try:
        identity = aws_json(args, "sts", "get-caller-identity")
        if identity.get("Account") != args.expected_account:
            raise ValueError("Unexpected AWS account")
        status["stage"] = "bucket"
        aws_json(
            args,
            "s3api",
            "head-bucket",
            "--bucket",
            args.bucket,
            "--expected-bucket-owner",
            args.expected_account,
        )
        versioning = aws_json(
            args,
            "s3api",
            "get-bucket-versioning",
            "--bucket",
            args.bucket,
            "--expected-bucket-owner",
            args.expected_account,
        )
        if versioning.get("Status") != "Enabled":
            raise ValueError("A versioned backup bucket is required")
        status["stage"] = "snapshot"
        snapshot = verified_backup(args.database_url, args.directory, args.keep)
        key = f"{args.prefix}/{datetime.now(UTC):%Y/%m/%d}/{snapshot.name}"
        status["stage"] = "upload"
        status["database"] = upload_verified(args, snapshot, key, "application/vnd.sqlite3")
        status["stage"] = "manifest"
        manifest = snapshot.with_suffix(".json")
        write_metadata(
            manifest,
            {
                "created_at": status["started_at"],
                "bucket": args.bucket,
                "region": args.region,
                "database": status["database"],
                "server_version": args.server_version,
                "rules_version": args.rules_version,
                "rules_source": args.rules_source,
                "local_integrity_and_restore_verified": True,
            },
        )
        status["manifest"] = upload_verified(args, manifest, key + ".json", "application/json")
        status.update(status="success", stage="complete")
    except Exception as exc:
        # AWS errors can contain login URLs, private paths or credential details.
        status["error_type"] = type(exc).__name__
    status["finished_at"] = datetime.now(UTC).isoformat()
    write_metadata(args.directory / "offsite-status.json", status)
    print(json.dumps(status, sort_keys=True))
    return 0 if status["status"] == "success" else 1


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--expected-account", required=True)
    parser.add_argument("--server-version", required=True)
    parser.add_argument("--rules-version", required=True)
    parser.add_argument("--rules-source", required=True)
    parser.add_argument("--prefix", default="sqlite")
    parser.add_argument("--keep", type=int, default=14)
    args = parser.parse_args()
    if not re.fullmatch(r"\d{12}", args.expected_account):
        parser.error("--expected-account must be a 12-digit AWS account ID")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9/_-]*", args.prefix):
        parser.error("--prefix must contain only letters, digits, /, _ and -")
    if args.keep < 1:
        parser.error("--keep must be positive")
    args.prefix = args.prefix.rstrip("/")
    return args


if __name__ == "__main__":
    raise SystemExit(run(parse_args()))
