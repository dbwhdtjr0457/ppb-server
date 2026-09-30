# MacBook deployment

Public API: `https://ppb-api.wonyangs.com`. A named Cloudflare tunnel forwards
only this hostname to `http://127.0.0.1:8000`. The website on `www.wonyangs.com`
is unrelated. Card artwork stays on the existing S3/CloudFront distribution.

## Installation layout

Use a private service root outside development checkouts, for example
`~/Library/Application Support/PokePackBarServer`:

- `releases/<server-sha>-<app-sha>/`: server source, locked virtual environment,
  rules executable, matching resource bundle and price collectors.
- `server.env`: mode 0600; absolute SQLite/rules/backup paths and deployment settings.
- `ppb.sqlite3`: authoritative account, inventory, transaction and replay state.
- `backups/`: verified SQLite snapshots, mode 0700.
- `logs/`: server rotating logs and launchd diagnostics.
- `migration/`: private incoming local save copies and migration receipts.

Never use `/Applications/PokePackBar.app` as the server's rules executable.
The user updating their menu-bar application must not change server rules.

## Prepare before activating

1. Export the approved app commit's rules with `scripts/export-rules.sh` into a
   new destination. Preserve existing engine snapshots.
2. Place the approved server source in a new release directory. Run
   `uv sync --locked` there before registering any service.
3. Set `PPB_DATABASE_URL`, `PPB_RULES_EXECUTABLE`, `PPB_BACKUP_DIRECTORY`,
   `PPB_RULES_TIMEOUT=60`, `PPB_BACKUP_KEEP=14`, `PPB_PORT=8000`,
   `PPB_REGISTRATION_MODE=link-code-only`, `PPB_TRUST_CLOUDFLARE_PROXY=1`, and
   `PPB_PRIVATE_DIAGNOSTICS=1` in the private `server.env`.
4. On upgrades, stop the API and make a consistent SQLite backup before running
   Alembic. Migrate explicitly once; never migrate during an automatic restart.
5. Check `/health` and `/ready` locally, then generate launch agents using
   `scripts/macos-launchagents.py`. Install the resulting two plists under the
   current user's `~/Library/LaunchAgents` and bootstrap them with launchctl.
6. Configure the tunnel to return 404 for `/ready`, `/docs`, `/redoc`, and
   `/openapi.json`, forward the API hostname, and return 404 for other hosts.
   Keep API responses out of edge caching. Trust the Cloudflare client-IP header
   only through the explicitly enabled local connector path.
7. Verify HTTPS from outside the origin, unauthorized API rejection, and
   link-code-only registration before distributing a client release.

The API runtime is `scripts/run-service.py`; it neither fetches packages nor
changes database schemas. It uses a single Uvicorn worker and rotating server
logs (10 MiB, five backups). launchd restarts failed services. The wrapper holds
an AC sleep assertion while the service runs. Closing the lid, loss of power or
network, logging out, and an un-unlocked FileVault reboot can still stop service.
After reboot, this user must unlock/login before LaunchAgents start.

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
Unattended backup credentials must outlive an interactive SSO session and be
restricted to that bucket; an expired interactive SSO profile is not a working
unattended backup configuration. Monitor the age of the last successful copy.

Restore the whole database with its matching server/rules version, preserving
the current database and WAL/SHM separately first. Do not roll back one account
after trades because that duplicates or loses the counterparty's inventory.
Once writes resume, a rollback to an older backup loses subsequent transactions;
prefer a forward code repair whenever the current database is valid.
