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
install -m 755 "${CURDIR}/packaging/bin/doc-store-init-tsearch" \
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
# /etc/doc-store/.env is deliberately NOT shipped. Everything in it --
# DOC_STORE_VERSION, DOC_STORE_UID, DOC_STORE_GID -- is derived on the machine
# it is installed on, and postinst writes all three. Shipping it made dpkg treat
# it as a conffile (everything under /etc is auto-marked), while postinst
# rewrote it on every install, so each upgrade raised a conffile prompt that a
# non-interactive install cannot answer -- after the stack had already been
# stopped. A generated file must not be packaged as configuration.

# /var/doc-store/secrets/.env is not shipped for the same reason, and its
# consequence was worse: because the package placed the template there, the file
# always existed, so postinst's "generate secrets on first install" branch never
# ran and the shipped placeholder password stayed live. The template is still
# installed under /usr/share/doc, which is where postinst reads it from.
install -d "${ST}/var/doc-store/secrets"

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
