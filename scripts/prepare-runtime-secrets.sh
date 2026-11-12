#!/bin/sh
set -eu

secret_dir="${YOUTUBE_RUNTIME_SECRET_DIR:-.runtime/secrets}"
umask 077
mkdir -p "$secret_dir"
chmod 700 "$secret_dir"

create_secret() {
    path="$1"
    byte_count="$2"
    mode="$3"
    if [ ! -s "$path" ]; then
        temporary="${path}.tmp.$$"
        openssl rand -hex "$byte_count" >"$temporary"
        chmod 400 "$temporary"
        mv "$temporary" "$path"
    fi

    chmod "$mode" "$path"
}

# Docker Compose implements file-backed secrets as bind mounts and cannot remap
# their uid/gid. Application and PostgreSQL containers use different non-root
# UIDs, so their mounted secrets must be world-readable. Host path access stays
# restricted by the non-searchable 0700 parent directory. Garage runs as root
# and explicitly rejects an RPC secret whose mode is more permissive than 0600.
create_secret "$secret_dir/postgres-password" 24 444
create_secret "$secret_dir/garage-rpc-secret" 32 600
create_secret "$secret_dir/app-secret" 32 444

printf '%s\n' "Runtime secret files are ready."
