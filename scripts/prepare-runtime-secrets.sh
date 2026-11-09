#!/bin/sh
set -eu

secret_dir="${YOUTUBE_RUNTIME_SECRET_DIR:-.runtime/secrets}"
umask 077
mkdir -p "$secret_dir"

create_secret() {
    path="$1"
    byte_count="$2"
    if [ ! -s "$path" ]; then
        temporary="${path}.tmp.$$"
        openssl rand -hex "$byte_count" >"$temporary"
        chmod 600 "$temporary"
        mv "$temporary" "$path"
    fi
}

create_secret "$secret_dir/postgres-password" 24
create_secret "$secret_dir/garage-rpc-secret" 32
create_secret "$secret_dir/app-secret" 32

printf '%s\n' "Runtime secret files are ready."
