#!/bin/sh
set -eu

data_dir=${AVITO_DATA_DIR:-/app/data}
app_uid=${AVITO_UID:-1000}
app_gid=${AVITO_GID:-1000}

if [ ! -d "$data_dir" ]; then
    install -d -m 0750 -o "$app_uid" -g "$app_gid" "$data_dir"
fi

owner=$(stat -c '%u:%g' "$data_dir")
first_entry=$(find "$data_dir" -mindepth 1 -maxdepth 1 -print -quit)

if [ "$owner" = "0:0" ] && [ -z "$first_entry" ]; then
    chown "$app_uid:$app_gid" "$data_dir"
    chmod 0750 "$data_dir"
fi

as_app() {
    setpriv --reuid="$app_uid" --regid="$app_gid" --clear-groups "$@"
}

if ! as_app /usr/bin/test -w "$data_dir"; then
    echo "data-init: $data_dir недоступен для UID:GID $app_uid:$app_gid" >&2
    echo "data-init: каталог с данными не изменён; исправьте владельца после резервной копии" >&2
    exit 1
fi

for path in "$data_dir/avito.db" "$data_dir/service.log" "$data_dir/mail.json" \
            "$data_dir/session" "$data_dir/html"; do
    if [ -e "$path" ] && ! as_app /usr/bin/test -w "$path"; then
        echo "data-init: $path существует, но не доступен для записи UID:GID $app_uid:$app_gid" >&2
        echo "data-init: существующие данные не изменены" >&2
        exit 1
    fi
done

echo "data-init: $data_dir готов для UID:GID $app_uid:$app_gid"
