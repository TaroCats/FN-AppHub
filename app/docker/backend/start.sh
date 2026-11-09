#!/bin/sh
# AppHub 容器启动：nginx 对外做 Host 映射，uvicorn 退到回环内部端口。
# nginx 不可用或配置校验失败时，自动回退到旧模式（uvicorn 直接监听对外端口），
# 保证面板永远能打开。
set -u

PUBLIC_PORT="${APPHUB_PORT:-9080}"
INTERNAL_PORT="${APPHUB_INTERNAL_PORT:-19080}"

BIND_HOST="0.0.0.0"
BIND_PORT="$PUBLIC_PORT"

if [ "${APPHUB_NGINX:-1}" != "0" ] && command -v nginx >/dev/null 2>&1; then
    if python -m app.webconf; then
        if nginx -t >/tmp/apphub-nginx-t.log 2>&1; then
            if nginx; then
                echo "[apphub] nginx 已启动，对外 ${PUBLIC_PORT}，面板内部 127.0.0.1:${INTERNAL_PORT}" >&2
                BIND_HOST="127.0.0.1"
                BIND_PORT="$INTERNAL_PORT"
            else
                echo "[apphub] nginx 启动失败，回退 uvicorn 直连" >&2
            fi
        else
            echo "[apphub] nginx 配置校验失败，回退 uvicorn 直连：" >&2
            cat /tmp/apphub-nginx-t.log >&2 2>/dev/null || true
        fi
    else
        echo "[apphub] nginx 配置生成失败，回退 uvicorn 直连" >&2
    fi
fi

exec uvicorn app.main:app \
    --host "$BIND_HOST" \
    --port "$BIND_PORT" \
    --proxy-headers \
    --forwarded-allow-ips '*'
