# 비공개 S3 외부 백업

서버의 매일 자동 SQLite 백업은 Mac 내부에만 보관됩니다. Mac 디스크 장애에
대비한 외부 사본은 `scripts/backup-offsite.py`로 명시적으로 생성합니다.
이미지 CDN과 분리된 전용 비공개 버킷을 사용합니다.

## 버킷 생성

개인 AWS 계정과 리전을 확인하고 `infra/backups.yaml`을 배포합니다.
이 템플릿은 IAM 사용자·키·역할을 만들지 않습니다.

```bash
aws sts get-caller-identity --profile pokepackbar-bootstrap
aws cloudformation deploy \
  --stack-name pokepackbar-server-backups \
  --template-file infra/backups.yaml \
  --profile pokepackbar-bootstrap \
  --region ap-northeast-2
aws cloudformation describe-stacks \
  --stack-name pokepackbar-server-backups \
  --profile pokepackbar-bootstrap \
  --region ap-northeast-2 \
  --query 'Stacks[0].Outputs'
```

버킷은 공개 접근 차단, ACL 비활성화, AES256 서버 측 암호화, 버전관리를
사용하고 HTTPS만 허용합니다. CloudFormation 스택 삭제나 버킷 교체 시에도
기존 버킷과 백업은 유지합니다. 외부 백업에 자동 삭제 정책은 없습니다.

## 백업 1회 실행

`--directory`에는 일반 일일 백업과 별도 디렉터리를 사용합니다.
`--keep`은 그 디렉터리의 로컬 SQLite 사본 개수만 제한합니다.
S3 사본은 삭제하지 않습니다. 서버 소스 커밋과 규칙 버전은 해당 운영 배포값을
입력합니다. 실행한 스크립트 버전이나 임의의 최신 값으로 대체하지 않습니다.

```bash
aws sso login --profile pokepackbar-bootstrap
uv run python scripts/backup-offsite.py \
  --database-url 'sqlite:////absolute/path/to/ppb.sqlite3' \
  --directory '/absolute/path/to/offsite-backups' \
  --bucket 'CLOUDFORMATION_OUTPUT_BUCKET_NAME' \
  --profile pokepackbar-bootstrap \
  --region ap-northeast-2 \
  --expected-account 'YOUR_12_DIGIT_ACCOUNT_ID' \
  --server-version 'DEPLOYED_SERVER_COMMIT' \
  --rules-version 'DEPLOYED_RULES_VERSION' \
  --rules-source 'DEPLOYED_APP_COMMIT'
```

스크립트는 계정과 버킷 소유자를 먼저 확인한 뒤 기존 `app.backups`의 SQLite
Online Backup API를 사용합니다. 무결성·외래 키·별도 임시 DB 복원 검사를
통과한 사본만 업로드합니다. 업로드에는 SHA-256을 전달하고, S3가 저장한
동일 객체 버전의 크기·SHA-256·암호화를 다시 확인합니다. 기존 키 덮어쓰기는
거부하며, 데이터·서버·규칙 버전을 연결한 JSON manifest도 함께 업로드합니다.

`offsite-status.json`과 stdout에는 성공/실패, 단계, 객체 키·버전, 체크섬만
기록합니다. AWS 원문 오류, 로그인 URL, 비밀값이나 DB 행은 기록하지 않습니다.
실패는 종료 코드 1입니다. 단계가 `identity`인 경우 SSO 만료 여부를 먼저
확인하고 로그인 후 다시 실행합니다. 실패한 로컬 사본은 진단을 위해 남습니다.

## 인증과 자동화 범위

SSO 로그인은 만료되므로 이 프로필만으로 무인 외부 백업이 계속 성공한다고
보장할 수 없습니다. 현재 스크립트는 예약 작업을 설치하지 않습니다.
서버 내부의 일일 백업과 이 수동 외부 백업은 별개입니다.

무인 백업을 추가하려면 장기 Access Key를 앱이나 저장소에 넣지 않고,
갱신 가능한 단기 인증(예: 별도로 승인한 IAM Roles Anywhere 구성)과 해당
버킷·접두어에 제한된 권한, 실패 알림을 함께 준비해야 합니다. 이 템플릿과
스크립트는 그 인증 구성을 자동으로 만들거나 넓은 IAM 권한을 변경하지 않습니다.

## 복구 검증

S3에서 manifest에 기록된 정확한 객체 버전을 **별도 파일**로 내려받고
SHA-256을 비교합니다. 격리된 파일에서 `app.backups.validate`의 무결성,
외래 키, 필수 테이블 및 마이그레이션 버전을 검증한 뒤 복구를 계획합니다.
운영 서버를 중지하고 전체 DB와 manifest에 기록된 서버·규칙 버전을 함께
복구해야 합니다. 거래가 시작된 뒤 개별 계정만 과거 사본으로 덮어쓰지 않습니다.
실행 중인 운영 DB에 자동으로 복원하는 기능은 제공하지 않습니다.
