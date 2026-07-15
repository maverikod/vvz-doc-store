#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

error() { echo "ERROR: $*" >&2; exit 1; }
info() { echo "INFO: $*"; }

VERSION=""
DO_DOCKER=1
DO_DEB=1
SKIP_DOCKER_PUSH=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --deb-only) DO_DOCKER=0 ;;
        --docker-only) DO_DEB=0 ;;
        --skip-docker-push) SKIP_DOCKER_PUSH=1 ;;
        -h|--help)
            cat <<'EOF'
Usage: scripts/release_build.sh [VERSION] [--deb-only|--docker-only] [--skip-docker-push]

Builds/pushes vasilyvz/doc-store:<version> and builds doc-store-server .deb.
Docker Hub credentials are read from .env: DOCKERHUB_USER and DOCKERHUB_PAT.
EOF
            exit 0
            ;;
        -*)
            error "Unknown option: $1"
            ;;
        *)
            [[ -z "$VERSION" ]] || error "Unexpected argument: $1"
            VERSION="$1"
            ;;
    esac
    shift
done

if [[ -z "$VERSION" ]]; then
    VERSION="$(python3 - <<'PY'
import tomllib
from pathlib import Path
print(tomllib.loads(Path("pyproject.toml").read_text())["project"]["version"])
PY
)"
fi

[[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+([.-][0-9A-Za-z.+~]+)?$ ]] \
    || error "VERSION must look like semver, got: $VERSION"

REGISTRY="${DOC_STORE_DOCKER_REGISTRY:-vasilyvz}"
IMAGE_NAME="${DOC_STORE_DOCKER_IMAGE_NAME:-doc-store}"
FULL_IMAGE="${REGISTRY}/${IMAGE_NAME}:${VERSION}"
LATEST_IMAGE="${REGISTRY}/${IMAGE_NAME}:latest"
DEB_VERSION="${VERSION}-1"

if [[ -f .env ]]; then
    set -a
    # shellcheck disable=SC1091
    source .env
    set +a
fi

docker_login_if_configured() {
    if [[ -n "${DOCKERHUB_USER:-}" && -n "${DOCKERHUB_PAT:-}" ]]; then
        info "Logging in to Docker Hub as ${DOCKERHUB_USER}"
        printf '%s' "$DOCKERHUB_PAT" | docker login -u "$DOCKERHUB_USER" --password-stdin
    fi
}

image_on_hub() {
    local ref="$1"
    docker manifest inspect "$ref" >/dev/null 2>&1
}

build_push_image() {
    docker_login_if_configured
    info "Building Docker image ${FULL_IMAGE}"
    docker build -f Dockerfile -t "$FULL_IMAGE" -t "$LATEST_IMAGE" .
    if (( SKIP_DOCKER_PUSH )); then
        info "Skipping Docker push by request"
        return
    fi
    info "Pushing ${FULL_IMAGE}"
    docker push "$FULL_IMAGE"
    info "Pushing ${LATEST_IMAGE}"
    docker push "$LATEST_IMAGE"
    image_on_hub "$FULL_IMAGE" || error "Docker Hub verification failed: $FULL_IMAGE"
}

ensure_image_published() {
    if (( SKIP_DOCKER_PUSH )); then
        return
    fi
    if image_on_hub "$FULL_IMAGE"; then
        info "Docker Hub already has ${FULL_IMAGE}"
        return
    fi
    build_push_image
}

if (( DO_DOCKER )); then
    build_push_image
elif (( DO_DEB )); then
    ensure_image_published
fi

echo "$FULL_IMAGE" > debian/doc-store-docker-image

cat > debian/changelog <<EOF
doc-store-server (${DEB_VERSION}) unstable; urgency=medium

  * Release ${VERSION} - Docker image ${FULL_IMAGE}

 -- Vasiliy Zdanovskiy <vasilyvz@gmail.com>  $(date -R)

EOF

if (( DO_DEB )); then
    command -v dpkg-buildpackage >/dev/null 2>&1 || error "dpkg-buildpackage is required"
    rm -rf debian/doc-store-server debian/files ../doc-store-server_*.deb \
        ../doc-store-server_*.changes ../doc-store-server_*.buildinfo 2>/dev/null || true
    info "Building Debian package doc-store-server_${DEB_VERSION}_all.deb"
    dpkg-buildpackage -us -uc -b
    ls -t ../doc-store-server_*.deb | head -1
fi
