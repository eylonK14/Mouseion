# Backups and restore

Mouseion has three authoritative data classes: `library.db`, content-addressed `pdfs/`, and PageIndex-compatible `trees/`. Open WebUI state is useful but separate; back up its named volume independently if preserving chat history and Function configuration matters.

## What the backup command does

`python -m mouseion.maintenance backup`:

1. checkpoints SQLite's WAL;
2. runs `VACUUM INTO` to create a transactionally consistent database file;
3. copies `pdfs/` and `trees/` into the same temporary snapshot;
4. writes a count manifest and atomically publishes `backups/YYYYMMDDTHHMMSSZ/`;
5. removes completed snapshots older than `BACKUP_RETENTION_DAYS` (30 by default).

An interrupted backup leaves only a hidden `.partial` directory and never replaces an older completed snapshot. The command never rewrites the live data directory.

## Run and schedule

Development/bind-mounted stack:

```bash
make backup-dry-run
make backup
make restore-drill
```

Production named-volume stack:

```bash
/bin/sh scripts/backup.sh --dry-run
/bin/sh scripts/backup.sh
docker compose -f docker-compose.prod.yml --profile maintenance run --rm backup \
  python -m mouseion.maintenance restore-drill
```

Install a nightly host cron at 02:15. Use an absolute repository path and a log location monitored by the host:

```cron
15 2 * * * cd /opt/mouseion && /bin/sh scripts/backup.sh >> /var/log/mouseion-backup.log 2>&1
```

`BACKUP_HOST_DIR` is a host bind mount. Put it on a different physical disk when possible. A local snapshot is not a complete backup until it exists on another device or provider.

### Off-site restic or rsync

If `RESTIC_REPOSITORY` and the normal restic credential environment are set for the cron job, `scripts/backup.sh` uploads the snapshot directory and applies the same daily retention count:

```bash
export RESTIC_REPOSITORY=s3:s3.example.net/mouseion
export RESTIC_PASSWORD_FILE=/root/.config/restic/mouseion-password
/bin/sh scripts/backup.sh
```

Alternatively, copy only completed dated directories to another host:

```bash
rsync -a --delete /opt/mouseion/backups/ backup-host:/srv/backups/mouseion/
```

Do not rsync a live `library.db`; use the `VACUUM INTO` snapshot.

## Restore drill

`make restore-drill` first creates a current snapshot, restores the latest snapshot into a scratch directory, opens the copied database with the real SQLite/vec stack, and runs DB-writable, `quick_check`, FTS-count, embedding-load, PageIndex, and disk checks. OpenRouter is skipped because restoring local data must not depend on an external service. Scratch data is removed automatically; live data is untouched.

Run it after initial setup, monthly, and after dependency or migration upgrades.

## Restore into production

These commands intentionally replace the active library. Confirm `SNAPSHOT` resolves inside the configured backup directory and create one final pre-restore snapshot first.

```bash
cd /opt/mouseion
/bin/sh scripts/backup.sh
SNAPSHOT="$(find "${BACKUP_HOST_DIR:-./backups}" -mindepth 1 -maxdepth 1 -type d -name '20??????T??????Z' | sort | tail -1)"
test -f "$SNAPSHOT/library.db"
printf 'restoring %s\n' "$SNAPSHOT"

docker compose -f docker-compose.prod.yml stop api worker
docker compose -f docker-compose.prod.yml run --rm --no-deps \
  -v "$SNAPSHOT:/restore:ro" api sh -eu -c '
    stamp=$(date -u +%Y%m%dT%H%M%SZ)
    test ! -e /app/data/library.db || mv /app/data/library.db "/app/data/library.db.pre-restore-$stamp"
    test ! -e /app/data/pdfs || mv /app/data/pdfs "/app/data/pdfs.pre-restore-$stamp"
    test ! -e /app/data/trees || mv /app/data/trees "/app/data/trees.pre-restore-$stamp"
    cp /restore/library.db /app/data/library.db
    cp -a /restore/pdfs /app/data/pdfs
    cp -a /restore/trees /app/data/trees
  '

docker compose -f docker-compose.prod.yml up -d migrate api worker
docker compose -f docker-compose.prod.yml exec -T api python -m mouseion.maintenance health-check
```

Once the restored system is verified, the timestamped `*.pre-restore-*` items inside the named data volume may be removed manually. They are deliberately not deleted by the procedure.

To restore from restic, first restore one completed dated snapshot into `BACKUP_HOST_DIR`, then use the same steps. Open WebUI state is separate: restoring Mouseion papers, QA logs, tests, and notes does not depend on that volume.
