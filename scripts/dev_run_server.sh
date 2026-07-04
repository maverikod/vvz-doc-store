#!/usr/bin/env bash
set -euo pipefail
exec uvicorn doc_store_server.main:app --reload --host 127.0.0.1 --port 8000
