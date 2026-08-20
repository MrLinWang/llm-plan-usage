FROM python:3.12-slim

# tzdata：让 -e TZ=Asia/Shanghai 生效（Web/终端显示按容器本地时区转换 UTC 时间戳）
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --uid 1000 app \
    && mkdir -p /data \
    && chown app:app /data

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir ".[web]"

COPY --chmod=0755 docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh

ENV LLM_USAGE_CONFIG=/data/config.toml \
    LLM_USAGE_DB=/data/history.db \
    PORT=8765

EXPOSE 8765
USER app

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c 'import os,urllib.request;urllib.request.urlopen("http://127.0.0.1:"+os.environ.get("PORT","8765")+"/login")'

ENTRYPOINT ["docker-entrypoint.sh"]
