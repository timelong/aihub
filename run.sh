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
# 监听 0.0.0.0 时浏览器要访问的是本机地址
BROWSE_HOST=$HOST
[ "$HOST" = "0.0.0.0" ] && BROWSE_HOST=127.0.0.1
URL="http://${BROWSE_HOST}:${PORT}"

echo "▶ 服务启动: ${URL}"
echo "▶ 运行日志: ${AIHUB_LOG_DIR:-$(pwd)/data/logs}/aihub.log"

# 等服务真的起来再开浏览器（NO_OPEN=1 可跳过）
if [ -z "$NO_OPEN" ]; then
  case "$(uname -s)" in
    Darwin) OPENER="open" ;;
    Linux)  OPENER="xdg-open" ;;
    *)      OPENER="" ;;
  esac
  if [ -n "$OPENER" ] && command -v "$OPENER" >/dev/null 2>&1; then
    (
      for _ in $(seq 1 60); do            # 最多等 30 秒
        if curl -sf -o /dev/null "${URL}/api/models" 2>/dev/null; then
          echo "▶ 已在浏览器打开 ${URL}"
          "$OPENER" "$URL" >/dev/null 2>&1
          exit 0
        fi
        sleep 0.5
      done
      echo "⚠ 服务启动超时，未自动打开浏览器，请手动访问 ${URL}"
    ) &
  fi
fi
# 日志统一由 app/logging_setup.py 接管（控制台 + 滚动文件），access log 我们自己打
python -m uvicorn app.main:app --host "$HOST" --port "$PORT" --no-access-log &
SERVER_PID=$!
echo "▶ 服务进程 PID: ${SERVER_PID}（停止：Ctrl-C，或 kill ${SERVER_PID}）"
trap 'kill $SERVER_PID 2>/dev/null' INT TERM
CODE=0
wait $SERVER_PID || CODE=$?   # 不能直接 wait，set -e 会让脚本在这里就退出
# 143 = 128+15(SIGTERM)，130 = 128+2(SIGINT/Ctrl-C)
case $CODE in
  0|130) echo "▶ 服务已停止" ;;
  143)   echo "⚠ 服务被 SIGTERM 终止（kill / pkill / 关终端），不是程序崩溃；详见日志" ;;
  *)     echo "⚠ 服务异常退出，退出码 ${CODE}，详见 ${AIHUB_LOG_DIR:-$(pwd)/data/logs}/aihub.log" ;;
esac
exit $CODE
