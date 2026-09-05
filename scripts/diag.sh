#!/usr/bin/env bash
set -Eeuo pipefail

root=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
container=${AVITO_CONTAINER:-avito-parser}
watchdog=${AVITO_DIAG_DIR:-$root/data/watchdog}
service_log=${AVITO_SERVICE_LOG:-$root/data/service.log}

mkdir -p -- "$watchdog"
exec 9>"$watchdog/diag.lock"
if ! flock -n 9; then
    echo "diag: предыдущая диагностика ещё работает" >&2
    exit 0
fi

stamp=$(date +%Y%m%d-%H%M%S)
snapshot="$watchdog/snap-$stamp.txt"
tmp=$(mktemp "$watchdog/.diag.XXXXXX")
offset_file="$watchdog/offset"

cleanup() {
    rm -f -- "$tmp"
}
trap cleanup EXIT

{
    echo "=== снимок $(date '+%F %T %Z') ==="
    uptime

    if docker inspect "$container" >/dev/null 2>&1; then
        docker inspect "$container" --format \
            'контейнер={{.State.Status}} health={{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}} рестартов={{.RestartCount}} oom={{.State.OOMKilled}} стартовал={{.State.StartedAt}}'
        docker stats --no-stream --format \
            'ресурсы: cpu={{.CPUPerc}} mem={{.MemUsage}} pids={{.PIDs}}' "$container" || true

        echo "--- процессы контейнера (топ по CPU) ---"
        docker exec "$container" ps -eo pid,ppid,pcpu,pmem,stat,etimes,comm,args --sort=-pcpu \
            | sed -n '1,15p' || true
        zombies=$(docker exec "$container" sh -c 'ps -eo stat | grep -c Z || true' 2>/dev/null || true)
        echo "зомби в контейнере: ${zombies:-неизвестно}"
    else
        echo "контейнер $container не найден"
    fi

    echo "--- память и диск ---"
    free -m | sed -n '1,2p'
    df -h "$root/data" 2>/dev/null | sed -n '1,2p' || true
    du -sh "$root/data/session" "$root/data/html" 2>/dev/null || true

    echo "--- новые строки service.log ---"
    if [[ -f $service_log ]]; then
        total=$(wc -l < "$service_log")
        previous=0
        if [[ -f $offset_file ]]; then
            read -r previous < "$offset_file" || previous=0
            [[ $previous =~ ^[0-9]+$ ]] || previous=0
        fi
        if (( total < previous )); then
            previous=0
        fi
        from=$((previous + 1))
        if (( from <= total )); then
            tail -n "+$from" "$service_log" | tail -n 100 | cut -c1-240
        else
            echo "новых строк нет"
        fi
        printf '%s\n' "$total" > "$offset_file"
    else
        echo "service.log ещё не создан"
    fi
} > "$tmp" 2>&1

mv -- "$tmp" "$snapshot"
trap - EXIT

errors=$(grep -cE 'ERROR|WARNING|unhealthy|oom=true' "$snapshot" || true)
printf '%s снимок=%s errors_warnings=%s\n' "$(date '+%F %T')" "$snapshot" "$errors" \
    >> "$watchdog/journal.txt"
echo "$snapshot"
