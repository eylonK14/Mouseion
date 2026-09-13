#!/bin/sh
set -eu

repo_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$repo_dir"

docker compose -f docker-compose.prod.yml --profile maintenance run --rm backup \
  python -m mouseion.maintenance backup "$@"

# Optional encrypted/off-site second copy. The local snapshot and retention
# happen inside Mouseion; when RESTIC_REPOSITORY is set, restic reads the host
# backup directory without ever entering the application container.
if [ -n "${RESTIC_REPOSITORY:-}" ]; then
  command -v restic >/dev/null 2>&1 || {
    echo "RESTIC_REPOSITORY is set but restic is not installed" >&2
    exit 1
  }
  restic backup "${BACKUP_HOST_DIR:-./backups}"
  restic forget --keep-daily "${BACKUP_RETENTION_DAYS:-30}" --prune
fi
