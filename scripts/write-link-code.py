#!/usr/bin/env python3
"""Issue an operator link code to a private file without printing the credential."""

import argparse
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--env-file", type=Path, required=True)
parser.add_argument("--account", required=True)
parser.add_argument("--output", type=Path, required=True)
args = parser.parse_args()
if args.output.exists():
    parser.error("Output exists; use a new filename instead of replacing a credential")
load_dotenv(args.env_file, override=True)
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.auth_service import issue_link_code  # noqa: E402
from app.database import SessionLocal  # noqa: E402

args.output.parent.mkdir(parents=True, exist_ok=True)
descriptor = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(descriptor, "w") as output:
    with SessionLocal() as db:
        code = issue_link_code(db, args.account)
    output.write(
        "PokePackBar 계정 연결 (발급 후 10분, 1회 사용)\n\n"
        "서버 주소: https://ppb-api.wonyangs.com\n"
        "설정 → 계정 및 서버 → 새 계정 가입 → 기존 UUID 계정 연결을 선택하세요.\n"
        "원하는 이메일과 비밀번호를 앱에 직접 입력하고 아래 코드를 붙여넣으세요.\n"
        "가입 후 앱을 완전히 종료했다가 다시 실행하세요.\n\n"
        f"연결 코드: {code}\n"
    )
print(f"Private link instructions written to {args.output}; expires in 10 minutes")
