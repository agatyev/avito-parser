#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

root=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
source_backup=${1:-}
target_db=${AVITO_DB_PATH:-$root/data/avito.db}
rollback_dir=${AVITO_BACKUP_DIR:-$root/backups}

if [[ -z $source_backup ]]; then
    echo "usage: $0 /path/to/avito-YYYYmmddTHHMMSSZ.sqlite3" >&2
    exit 2
fi
if [[ ! -f $source_backup ]]; then
    echo "restore: резервная копия не найдена: $source_backup" >&2
    exit 1
fi

if docker compose -f "$root/docker-compose.yml" ps --services --status running 2>/dev/null \
        | grep -qx avito; then
    echo "restore: сначала остановите сервис: docker compose stop avito" >&2
    exit 1
fi

if [[ -f $source_backup.sha256 ]]; then
    expected=$(awk 'NR == 1 { print $1 }' "$source_backup.sha256")
    actual=$(sha256sum "$source_backup" | awk '{ print $1 }')
    if [[ -z $expected || $expected != "$actual" ]]; then
        echo "restore: checksum не совпал" >&2
        exit 1
    fi
fi

python3 - "$source_backup" <<'PY'
import sqlite3
import sys
from pathlib import Path
from urllib.parse import quote

source_path = Path(sys.argv[1]).resolve()
uri = f"file:{quote(str(source_path), safe='/')}?mode=ro"
with sqlite3.connect(uri, uri=True) as database:
    result = database.execute("PRAGMA integrity_check").fetchone()[0]
    if result != "ok":
        raise RuntimeError(f"integrity_check: {result}")
PY

mkdir -p -- "$(dirname -- "$target_db")"
rollback=""
if [[ -f $target_db ]]; then
    rollback=$("$root/scripts/backup.sh" "$rollback_dir")
fi

tmp=$(mktemp "${target_db}.restore.XXXXXX")
cleanup() {
    rm -f -- "$tmp"
}
trap cleanup EXIT
cp -- "$source_backup" "$tmp"
chmod 0640 "$tmp"
if [[ -e $target_db ]]; then
    chown --reference="$target_db" "$tmp"
fi

rm -f -- "${target_db}-wal" "${target_db}-shm"
mv -- "$tmp" "$target_db"
trap - EXIT

echo "restore: восстановлено $target_db"
if [[ -n $rollback ]]; then
    echo "restore: предыдущая база сохранена в $rollback"
fi
