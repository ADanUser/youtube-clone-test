#!/usr/bin/env bash
set -Eeuo pipefail

readonly script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
readonly project_dir="$(cd -- "${script_dir}/.." && pwd)"
readonly runtime_dir="${project_dir}/.runtime"
readonly deploy_env="${runtime_dir}/deploy.env"

log() {
    printf '[deploy] %s\n' "$*"
}

fail() {
    printf '[deploy] ERROR: %s\n' "$*" >&2
    exit 1
}

require_root() {
    [[ "${EUID}" -eq 0 ]] || fail "run as root: sudo bash scripts/deploy.sh"
}

check_platform() {
    [[ -r /etc/os-release ]] || fail "cannot identify the operating system"
    # shellcheck disable=SC1091
    . /etc/os-release
    [[ "${ID:-}" == "ubuntu" && "${VERSION_ID:-}" == "24.04" ]] || \
        fail "supported platform is Ubuntu 24.04 LTS (found ${PRETTY_NAME:-unknown})"
    [[ "$(uname -m)" == "x86_64" ]] || fail "supported architecture is x86_64"

    local memory_kib available_kib
    memory_kib="$(awk '/^MemTotal:/ {print $2}' /proc/meminfo)"
    available_kib="$(df -Pk "${project_dir}" | awk 'NR == 2 {print $4}')"
    (( memory_kib >= 3800000 )) || fail "at least 4 GiB RAM is required"
    (( available_kib >= 20 * 1024 * 1024 )) || fail "at least 20 GiB free disk is required"
}

install_dependencies() {
    log "Installing operating-system dependencies"
    export DEBIAN_FRONTEND=noninteractive
    apt-get update
    apt-get install -y --no-install-recommends ca-certificates curl openssl nginx certbot python3-certbot-nginx

    if ! command -v docker >/dev/null 2>&1 || ! docker compose version >/dev/null 2>&1; then
        install -m 0755 -d /etc/apt/keyrings
        curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
            -o /etc/apt/keyrings/docker.asc
        chmod a+r /etc/apt/keyrings/docker.asc
        local architecture codename
        architecture="$(dpkg --print-architecture)"
        codename="$(. /etc/os-release && printf '%s' "${VERSION_CODENAME}")"
        printf 'deb [arch=%s signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu %s stable\n' \
            "${architecture}" "${codename}" > /etc/apt/sources.list.d/docker.list
        apt-get update
        apt-get install -y --no-install-recommends docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
    fi
    systemctl enable --now docker
}

write_deploy_environment() {
    local origin bind
    umask 077
    mkdir -p "${runtime_dir}"

    if [[ -n "${YOUTUBE_DOMAIN:-}" ]]; then
        [[ "${YOUTUBE_DOMAIN}" =~ ^[A-Za-z0-9.-]+$ ]] || fail "YOUTUBE_DOMAIN is invalid"
        [[ -n "${YOUTUBE_TLS_EMAIL:-}" ]] || fail "YOUTUBE_TLS_EMAIL is required with YOUTUBE_DOMAIN"
        origin="https://${YOUTUBE_DOMAIN}"
        bind="127.0.0.1:8080"
    else
        origin="http://${YOUTUBE_PUBLIC_IP:-$(hostname -I | awk '{print $1}')}:8080"
        [[ "${origin}" != "http://:8080" ]] || fail "set YOUTUBE_PUBLIC_IP for HTTP deployment"
        bind="8080"
    fi

    local temporary="${deploy_env}.tmp.$$"
    {
        printf 'YOUTUBE_PUBLIC_ORIGIN=%s\n' "${origin}"
        printf 'YOUTUBE_COOKIE_SECURE=%s\n' "$([[ -n "${YOUTUBE_DOMAIN:-}" ]] && printf true || printf false)"
        printf 'YOUTUBE_HTTP_BIND=%s\n' "${bind}"
    } > "${temporary}"
    chmod 600 "${temporary}"
    mv "${temporary}" "${deploy_env}"
}

compose() {
    docker compose --project-directory "${project_dir}" --env-file "${deploy_env}" "$@"
}

configure_https() {
    [[ -n "${YOUTUBE_DOMAIN:-}" ]] || return 0
    log "Configuring HTTPS for ${YOUTUBE_DOMAIN}"
    local nginx_site="/etc/nginx/sites-available/youtube-copy"
    local temporary="${nginx_site}.tmp.$$"
    sed -e "s/__DOMAIN__/${YOUTUBE_DOMAIN}/g" \
        "${project_dir}/infrastructure/nginx/host-tls.conf" > "${temporary}"
    install -m 0644 "${temporary}" "${nginx_site}"
    rm -f "${temporary}"
    ln -sfn "${nginx_site}" /etc/nginx/sites-enabled/youtube-copy
    rm -f /etc/nginx/sites-enabled/default
    nginx -t
    systemctl enable --now nginx
    certbot --nginx --non-interactive --agree-tos --redirect \
        --email "${YOUTUBE_TLS_EMAIL}" -d "${YOUTUBE_DOMAIN}"
    nginx -t
    systemctl reload nginx
}

wait_for_http() {
    local url="$1" attempt
    for attempt in {1..60}; do
        if curl --fail --silent --show-error --max-time 5 "${url}/healthz" >/dev/null; then
            return 0
        fi
        sleep 2
    done
    return 1
}

deploy() {
    cd "${project_dir}"
    write_deploy_environment
    "${project_dir}/scripts/prepare-runtime-secrets.sh"
    compose config --quiet

    log "Building images and starting PostgreSQL and Garage"
    compose build web garage-init
    compose up -d postgres garage
    compose up --no-log-prefix garage-init
    compose up --no-log-prefix migrate
    compose up -d web worker nginx

    wait_for_http "http://127.0.0.1:8080" || {
        compose ps
        compose logs --tail=100 web worker nginx >&2
        fail "application did not become healthy"
    }

    log "Creating or verifying demo data"
    compose run --rm seed
    configure_https

    local public_origin
    public_origin="$(awk -F= '$1 == "YOUTUBE_PUBLIC_ORIGIN" {print $2}' "${deploy_env}")"
    wait_for_http "${public_origin}" || fail "public endpoint failed its health check"
    log "Deployment is ready: ${public_origin}"
}

require_root
check_platform
install_dependencies
deploy
