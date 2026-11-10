FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Asia/Shanghai

WORKDIR /app/backend

# 直接用 uvicorn 对外提供服务（不再安装 nginx）；curl 供健康检查使用
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY backend/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/app ./app
COPY backend/start.sh ./start.sh
COPY frontend /app/static

RUN chmod +x ./start.sh

ENV APPHUB_STATIC=/app/static \
    APPHUB_DATA=/app/data \
    APPHUB_PORT=9080

EXPOSE 9080

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${APPHUB_PORT:-9080}/api/health" || exit 1

CMD ["sh", "./start.sh"]
