#!/bin/sh
set -eu

secret_dir="${YOUTUBE_RUNTIME_SECRET_DIR:-.runtime/secrets}"
umask 077
mkdir -p "$secret_dir"
chmod 700 "$secret_dir"

create_secret() {
    path="$1"
    byte_count="$2"
    if [ ! -s "$path" ]; then
        temporary="${path}.tmp.$$"
        openssl rand -hex "$byte_count" >"$temporary"
        chmod 400 "$temporary"
        mv "$temporary" "$path"
    fi

    # Docker Compose implements file-backed secrets as bind mounts and cannot
    # remap their uid/gid. The consumers run under different non-root UIDs, so
    # the mounted inode must be readable by all of them. Host access remains
    # restricted by the non-searchable 0700 parent directory.
    chmod 444 "$path"
}

create_secret "$secret_dir/postgres-password" 24
create_secret "$secret_dir/garage-rpc-secret" 32
create_secret "$secret_dir/app-secret" 32

printf '%s\n' "Runtime secret files are ready."
