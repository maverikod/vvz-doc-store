#!/usr/bin/env bash
set -euo pipefail

DOC_STORE_USER="${DOC_STORE_USER:-docstoreuesr}"
DOC_STORE_GROUP="${DOC_STORE_GROUP:-docstoregrp}"
DOC_STORE_UID="${DOC_STORE_UID:-1000}"
DOC_STORE_GID="${DOC_STORE_GID:-1000}"

ensure_group() {
    if getent group "$DOC_STORE_GROUP" >/dev/null 2>&1; then
        current_gid="$(getent group "$DOC_STORE_GROUP" | cut -d: -f3)"
        if [[ "$current_gid" != "$DOC_STORE_GID" ]]; then
            groupmod -o -g "$DOC_STORE_GID" "$DOC_STORE_GROUP"
        fi
        return
    fi
    if getent group "$DOC_STORE_GID" >/dev/null 2>&1; then
        groupmod -n "$DOC_STORE_GROUP" "$(getent group "$DOC_STORE_GID" | cut -d: -f1)"
    else
        groupadd -o -g "$DOC_STORE_GID" "$DOC_STORE_GROUP"
    fi
}

ensure_user() {
    if id "$DOC_STORE_USER" >/dev/null 2>&1; then
        current_uid="$(id -u "$DOC_STORE_USER")"
        if [[ "$current_uid" != "$DOC_STORE_UID" ]]; then
            usermod -o -u "$DOC_STORE_UID" "$DOC_STORE_USER"
        fi
        usermod -g "$DOC_STORE_GROUP" -d /var/doc-store "$DOC_STORE_USER"
        return
    fi
    if getent passwd "$DOC_STORE_UID" >/dev/null 2>&1; then
        usermod -l "$DOC_STORE_USER" -d /var/doc-store -g "$DOC_STORE_GROUP" \
            "$(getent passwd "$DOC_STORE_UID" | cut -d: -f1)"
    else
        useradd -o -u "$DOC_STORE_UID" -g "$DOC_STORE_GROUP" \
            -d /var/doc-store -s /usr/sbin/nologin "$DOC_STORE_USER"
    fi
}

ensure_group
ensure_user

install -d -o "$DOC_STORE_USER" -g "$DOC_STORE_GROUP" /var/doc-store /var/log/doc-store /app/logs
install -d -o root -g "$DOC_STORE_GROUP" /etc/doc-store
chmod 0750 /var/doc-store /var/log/doc-store /etc/doc-store
chmod 0775 /app/logs

if [[ "${DOC_STORE_AUTO_MIGRATE:-true}" =~ ^(1|true|TRUE|yes|YES|on|ON)$ ]]; then
    if [[ -n "${DOC_STORE_DATABASE_URL:-${DATABASE_URL:-}}" ]]; then
        echo "INFO: applying doc-store database migrations"
        if ! gosu "$DOC_STORE_USER:$DOC_STORE_GROUP" python -m doc_store_server.db.migrations; then
            echo "WARNING: doc-store database migrations failed; server will still start" >&2
        fi
    else
        echo "INFO: DOC_STORE_DATABASE_URL is not configured; skipping migrations"
    fi
fi

exec gosu "$DOC_STORE_USER:$DOC_STORE_GROUP" "$@"
