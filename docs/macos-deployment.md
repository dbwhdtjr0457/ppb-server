# MacBook deployment

Public API: `https://ppb-api.wonyangs.com`. A named Cloudflare tunnel forwards
only this hostname to `http://127.0.0.1:8000`. The website on `www.wonyangs.com`
is unrelated. Card artwork stays on the existing S3/CloudFront distribution.

## Installation layout

Use a private service root outside development checkouts, for example
`~/Library/Application Support/PokePackBarServer`:

- `releases/<server-sha>/`: server source, locked virtual environment, immutable
  `data/` catalogue, price snapshots and Python price collectors.
- `server.env`: mode 0600; absolute SQLite/data/backup paths and deployment settings.
- `ppb.sqlite3`: authoritative account, inventory, transaction and replay state.
- `backups/`: verified SQLite snapshots, mode 0700.
- `logs/`: server rotating logs and launchd diagnostics.
- `migration/`: private incoming local save copies and migration receipts.

The production Python backend never invokes `/Applications/PokePackBar.app` or
any Swift rules executable. Preserve older releases only for explicit rollback.
Updating the user's menu-bar app must not change the server's immutable catalogue.

## Prepare before activating

1. Use reviewed `data/` from the approved server commit. For a rules-data update,
   use `scripts/export-native-data.py` and run `scripts/verify-native-rules.py`
   against the matching Swift build before publishing. This is build-time only.
2. Place the approved server source in a new release directory. Run
   `uv sync --locked` there before registering any service.
3. Set `PPB_DATABASE_URL`, `PPB_RULES_BACKEND=python`,
   `PPB_RULES_DATA_DIRECTORY=<release>/data`, `PPB_BACKUP_DIRECTORY`,
   `PPB_BACKUP_KEEP=14`, `PPB_PORT=8000`, `PPB_TRUST_CLOUDFLARE_PROXY=1`, and
   `PPB_PRIVATE_DIAGNOSTICS=1` in the private `server.env`.
4. On upgrades, stop the API and make a consistent SQLite backup before running
   Alembic. Migrate explicitly once; never migrate during an automatic restart.
5. Check `/health` and `/ready` locally, then generate launch agents using
   `scripts/macos-launchagents.py`. Install the resulting two plists under the
   current user's `~/Library/LaunchAgents` and bootstrap them with launchctl.
   Both request-serving processes use `ProcessType=Interactive`: background
   scheduling reproduced a roughly fivefold rules slowdown on this Mac.
   Warm rules/catalogue and the active price version before readiness.
6. Configure the tunnel to return 404 for `/ready`, `/docs`, `/redoc`, and
   `/openapi.json`, forward the API hostname, and return 404 for other hosts.
   Keep API responses out of edge caching. Trust the Cloudflare client-IP header
   only through the explicitly enabled local connector path.
7. Verify HTTPS from outside the origin, unauthorized API rejection, and
   email/password registration before distributing a client release.

The API runtime is `scripts/run-service.py`; it neither fetches packages nor
changes database schemas. It uses a single Uvicorn worker and rotating server
logs (10 MiB, five backups). launchd restarts failed services. The wrapper holds
an AC sleep assertion while the service runs. Closing the lid, loss of power or
network, logging out, and an un-unlocked FileVault reboot can still stop service.
After reboot, this user must unlock/login before LaunchAgents start.

The Python cutover retains schema `20260930_0006`, all existing sessions and
`ppb-server-v2/d77433ccd09256e18d32a057b9f4fc28a0c0ba257e22ece02d181f924e5a5866`.
Do not rewrite account state merely to switch rule implementations. Validate
copied states first and take a verified backup immediately before activation.
Code rollback uses the preserved release/env/plist and the current database;
do not restore an old database after new writes unless data loss is approved.

## Legacy data cutover

Before signup, stop the user's app and copy its raw `game-state.json`; record
its checksum, app version and capture time. Choose one authoritative save per
person. Test imports on a disposable database first. Import only with the
operator tool, reconcile projections, compare wallet/card/printing/pack/coupon/
pity/reward state, then issue a short-lived link code for the user to register.
Do not collect the user's password or provider credentials. Preserve the source
save and reject repeated imports. A user who already registered needs a separate
reviewed linking procedure; do not overwrite their account.

Verify the same already-paid bonus window cannot grant again after raw-to-hash
conversion. Verify each device establishes a new token collection baseline and
that restoring device preferences cannot replay an earlier device's lifetime
total. Never infer missing historical statistics from current inventory.

## Backups and rollback

Automatic backups are verified SQLite snapshots on the same disk. Replicate
snapshots to a separate, private backup bucket, never the artwork CDN bucket.
The verified upload/download/restore command is documented in
[offsite-backups.md](offsite-backups.md).
Unattended backup credentials must outlive an interactive SSO session and be
restricted to that bucket; an expired interactive SSO profile is not a working
unattended backup configuration. Monitor the age of the last successful copy.

Restore the whole database with its matching server/rules version, preserving
the current database and WAL/SHM separately first. Do not roll back one account
after trades because that duplicates or loses the counterparty's inventory.
Once writes resume, a rollback to an older backup loses subsequent transactions;
prefer a forward code repair whenever the current database is valid.
