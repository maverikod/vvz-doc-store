#!/usr/bin/env bash
exec "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/scripts/release_build.sh" --deb-only "$@"
