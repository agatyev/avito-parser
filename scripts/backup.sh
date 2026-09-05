#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

root=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
source_db=${AVITO_DB_PATH:-$root/data/avito.db}
destination=${1:-$root/backups}

if [[ ! -f $source_db ]]; then
    echo "backup: база не найдена: $source_db" >&2
    exit 1
fi

mkdir -p -- "$destination"
destination=$(CDPATH= cd -- "$destination" && pwd)
stamp=$(date -u +%Y%m%dT%H%M%SZ)
filename="avito-$stamp.sqlite3"
target="$destination/$filename"

if [[ -e $target ]]; then
    echo "backup: файл уже существует: $target" >&2
    exit 1
fi

tmp=$(mktemp "$destination/.avito-backup.XXXXXX")
cleanup() {
    rm -f -- "$tmp"
}
trap cleanup EXIT

python3 - "$source_db" "$tmp" <<'PY'
import sqlite3
import sys
from pathlib import Path
from urllib.parse import quote

source_path = Path(sys.argv[1]).resolve()
target_path = Path(sys.argv[2]).resolve()
uri = f"file:{quote(str(source_path), safe='/')}?mode=ro"

with sqlite3.connect(uri, uri=True, timeout=30) as source:
    with sqlite3.connect(target_path) as target:
        source.backup(target, pages=1000, sleep=0.05)
        result = target.execute("PRAGMA integrity_check").fetchone()[0]
        if result != "ok":
            raise RuntimeError(f"integrity_check: {result}")
PY

chmod 0600 "$tmp"
mv -- "$tmp" "$target"
trap - EXIT

(
    cd -- "$destination"
    sha256sum "$filename" > "$filename.sha256"
)

echo "$target"
