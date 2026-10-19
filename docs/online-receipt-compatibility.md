# Online receipt compatibility after migration 0009

Migration 0009 adds two default-zero money fields to online commands. Existing
receipts hash the complete validated command, so pre-upgrade receipts do not
include these fields. Revalidating an unchanged pending request after upgrade
would otherwise produce a different hash and reject recovery of a committed
operation with `idempotency_key_reused`.

On an existing receipt mismatch, the server accepts the exact pre-0009 canonical
shape only when both money values are zero. The device and every other command
field remain part of the comparison. New receipts retain their current hash;
nonzero money, changed quantities and changed terms remain conflicts.

The match occurs inside the authenticated transaction before any mutation.
Recovery returns the stored result and current snapshot without a second debit,
card movement or journal entry. No stored receipt or account state is rewritten.

`tests/test_online_receipt_compatibility.py` covers old profile and listing
receipts, unchanged replay, resource preservation and changed-payload rejection.
Both cases fail when legacy canonicalization is disabled. Existing tests cover
current-format receipt replay and money-bearing acceptance.

This is a code-only follow-up to PR #15: the schema remains `20261015_0009` and
the Python/Swift rules data is unchanged. Keep a fresh verified backup during
cutover and preserve current data when rolling code back.
