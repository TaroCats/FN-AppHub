#!/bin/sh
# AppHub 容器启动：uvicorn 直接监听对外端口，不再经过 nginx 等任何中间层。
set -u

exec uvicorn app.main:app \
    --host 0.0.0.0 \
    --port "${APPHUB_PORT:-9080}" \
    --proxy-headers \
    --forwarded-allow-ips '*'
