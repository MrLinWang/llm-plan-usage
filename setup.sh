#!/usr/bin/env bash
# 一键配置虚拟环境并安装依赖 (uv)
# 用法:  ./setup.sh
set -euo pipefail

cd "$(dirname "$0")"

VENV=".venv"

echo "→ 创建虚拟环境 ($VENV) ..."
uv venv "$VENV"

echo "→ 安装依赖 (含 test extra) ..."
uv pip install --python "$VENV/bin/python" -e ".[test]"

echo
echo "✓ 完成。激活方式:"
echo "  source $VENV/bin/activate"
echo
echo "运行测试:  pytest tests/ -v"
echo "启动 CLI:  llm-usage show"