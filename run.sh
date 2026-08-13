#!/usr/bin/env bash
# 一键启动：创建虚拟环境 -> 装依赖 -> 起服务
set -e
cd "$(dirname "$0")"

if [ ! -d .venv ]; then
  echo "▶ 创建虚拟环境 .venv"
  python3 -m venv .venv
fi
source .venv/bin/activate

if [ ! -f .venv/.deps_ok ]; then
  echo "▶ 安装依赖"
  pip install -q -U pip
  pip install -q -r requirements.txt
  touch .venv/.deps_ok
fi

[ -f .env ] || { cp .env.example .env; echo "▶ 已生成 .env，请填写 API Key"; }

HOST=${HOST:-127.0.0.1}
PORT=${PORT:-8000}
echo "▶ 服务启动: http://${HOST}:${PORT}"
echo "▶ 运行日志: ${AIHUB_LOG_DIR:-$(pwd)/data/logs}/aihub.log"
# 日志统一由 app/logging_setup.py 接管（控制台 + 滚动文件），access log 我们自己打
exec python -m uvicorn app.main:app --host "$HOST" --port "$PORT" --no-access-log
