import base64
import hashlib
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

spec = importlib.util.spec_from_file_location(
    "backup_offsite", Path(__file__).resolve().parents[1] / "scripts" / "backup-offsite.py"
)
offsite = importlib.util.module_from_spec(spec)
spec.loader.exec_module(offsite)


@pytest.fixture
def args(tmp_path):
    return SimpleNamespace(
        database_url="sqlite:///private.sqlite3",
        directory=tmp_path,
        keep=14,
        bucket="private-backup",
        profile="explicit-profile",
        region="ap-northeast-2",
        expected_account="123456789012",
        prefix="sqlite",
        server_version="source-commit",
        rules_version="rules-version",
        rules_source="rules-commit",
    )


def test_offsite_requires_expected_account_before_backup(args, monkeypatch, capsys):
    monkeypatch.setattr(offsite, "aws_json", lambda *_: {"Account": "000000000000"})
    monkeypatch.setattr(offsite, "verified_backup", lambda *_: pytest.fail("must not read DB"))
    assert offsite.run(args) == 1
    status = json.loads(capsys.readouterr().out)
    assert status["stage"] == "identity"
    assert status["error_type"] == "ValueError"
    assert json.loads((args.directory / "offsite-status.json").read_text()) == status


def test_offsite_verifies_uploaded_checksum_and_sanitizes_failure(args, monkeypatch, capsys):
    snapshot = args.directory / "snapshot.sqlite3"
    snapshot.write_bytes(b"private database rows")
    monkeypatch.setattr(offsite, "verified_backup", lambda *_: snapshot)

    def aws(_args, service, operation, *arguments):
        if operation == "get-caller-identity":
            return {"Account": args.expected_account}
        if operation == "get-bucket-versioning":
            return {"Status": "Enabled"}
        if operation == "put-object":
            assert "--if-none-match" in arguments
            assert "--expected-bucket-owner" in arguments
            return {"VersionId": "v1"}
        if operation == "head-object":
            return {
                "ContentLength": snapshot.stat().st_size,
                "ChecksumSHA256": "incorrect-checksum",
                "ServerSideEncryption": "AES256",
            }
        return {}

    monkeypatch.setattr(offsite, "aws_json", aws)
    assert offsite.run(args) == 1
    output = capsys.readouterr().out
    assert "private database rows" not in output
    assert "private.sqlite3" not in output
    status = json.loads(output)
    assert status["stage"] == "upload" and status["error_type"] == "ValueError"
    assert snapshot.exists()
    assert not snapshot.with_suffix(".json").exists()


def test_offsite_success_records_versions_without_database_contents(args, monkeypatch, capsys):
    snapshot = args.directory / "snapshot.sqlite3"
    snapshot.write_bytes(b"private database rows")
    monkeypatch.setattr(offsite, "verified_backup", lambda *_: snapshot)
    uploaded = {}

    def aws(_args, service, operation, *arguments):
        if operation == "get-caller-identity":
            return {"Account": args.expected_account}
        if operation == "get-bucket-versioning":
            return {"Status": "Enabled"}
        if operation == "put-object":
            key = arguments[arguments.index("--key") + 1]
            path = Path(arguments[arguments.index("--body") + 1])
            uploaded[key] = path.read_bytes()
            return {"VersionId": "verified-version"}
        if operation == "head-object":
            key = arguments[arguments.index("--key") + 1]
            data = uploaded[key]
            return {
                "ContentLength": len(data),
                "ChecksumSHA256": base64.b64encode(hashlib.sha256(data).digest()).decode(),
                "ServerSideEncryption": "AES256",
            }
        return {}

    monkeypatch.setattr(offsite, "aws_json", aws)
    assert offsite.run(args) == 0
    output = capsys.readouterr().out
    status = json.loads(output)
    assert status["status"] == "success" and status["stage"] == "complete"
    assert status["database"]["version_id"] == "verified-version"
    manifest = json.loads(snapshot.with_suffix(".json").read_text())
    assert manifest["rules_version"] == args.rules_version
    assert manifest["local_integrity_and_restore_verified"]
    assert "private database rows" not in output
