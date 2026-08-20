#!/bin/sh
set -e

# 首次启动生成配置模板；之后可在 Web 端「供应商配置」页修改凭证，
# 或挂载自己的 config.toml 到 $LLM_USAGE_CONFIG。
if [ ! -f "$LLM_USAGE_CONFIG" ]; then
    llm-usage config --init
fi

exec llm-usage web --host 0.0.0.0 --port "${PORT:-8765}" "$@"
