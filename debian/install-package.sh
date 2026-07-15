#!/usr/bin/env bash
set -euo pipefail

CURDIR="${1:?usage: install-package.sh CURDIR}"
ST="${CURDIR}/debian/doc-store-server"
VERSION="$(grep -m1 '^version' "${CURDIR}/pyproject.toml" | sed -E 's/^version[[:space:]]*=[[:space:]]*"([^"]+)".*/\1/')"

install -d "${ST}/lib/systemd/system"
install -m 644 "${CURDIR}/packaging/systemd/doc-store.service" \
    "${ST}/lib/systemd/system/"

install -d "${ST}/usr/lib/doc-store/bin"
install -m 755 "${CURDIR}/packaging/bin/doc-store-init-locales" \
    "${ST}/usr/lib/doc-store/bin/"

install -d "${ST}/etc/default"
install -m 644 "${CURDIR}/packaging/default/doc-store" \
    "${ST}/etc/default/doc-store"

install -d "${ST}/etc/doc-store"
install -m 644 "${CURDIR}/packaging/docker-compose.yml" \
    "${ST}/etc/doc-store/docker-compose.yml"
install -m 644 "${CURDIR}/packaging/config.json" \
    "${ST}/etc/doc-store/config.json"
install -d "${ST}/etc/doc-store/mtls"
install -m 644 "${CURDIR}/mtls-certs/mtls_certificates/server/doc-store.crt" \
    "${ST}/etc/doc-store/mtls/server.crt"
install -m 600 "${CURDIR}/mtls-certs/mtls_certificates/server/doc-store.key" \
    "${ST}/etc/doc-store/mtls/server.key"
install -m 644 "${CURDIR}/mtls-certs/mtls_certificates/ca/ca.crt" \
    "${ST}/etc/doc-store/mtls/ca.crt"
printf 'DOC_STORE_VERSION=%s\n' "$VERSION" > "${ST}/etc/doc-store/.env"
chmod 644 "${ST}/etc/doc-store/.env"

install -d "${ST}/var/doc-store/secrets"
install -m 640 "${CURDIR}/packaging/secrets.env.template" \
    "${ST}/var/doc-store/secrets/.env"

install -d "${ST}/usr/share/doc-store"
if [[ -f "${CURDIR}/debian/doc-store-docker-image" ]]; then
    install -m 644 "${CURDIR}/debian/doc-store-docker-image" \
        "${ST}/usr/share/doc-store/docker-image"
else
    echo "vasilyvz/doc-store:${VERSION}" > "${ST}/usr/share/doc-store/docker-image"
fi

install -d "${ST}/usr/share/doc/doc-store-server"
install -m 644 "${CURDIR}/packaging/secrets.env.template" \
    "${ST}/usr/share/doc/doc-store-server/secrets.env.template"
