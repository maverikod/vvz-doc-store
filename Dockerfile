FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DOC_STORE_HOST=0.0.0.0 \
    DOC_STORE_PORT=8000 \
    DOC_STORE_PROTOCOL=http \
    DOC_STORE_DEBUG=false \
    DOC_STORE_LOG_LEVEL=info \
    DOC_STORE_QUEUE_ENABLED=false

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends gosu \
    && rm -rf /var/lib/apt/lists/* \
    && python -m pip install --no-cache-dir --upgrade pip setuptools wheel

COPY pyproject.toml README.md ./
COPY src ./src
COPY scripts/docker-entrypoint.sh /usr/local/bin/doc-store-entrypoint

RUN python -m pip install --no-cache-dir . \
    && chmod 0755 /usr/local/bin/doc-store-entrypoint \
    && install -d /etc/doc-store /var/doc-store /var/log/doc-store

EXPOSE 8000

ENTRYPOINT ["doc-store-entrypoint"]
CMD ["python", "-m", "doc_store_server.main"]
