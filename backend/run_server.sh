#!/usr/bin/env bash
# 启动 / 停止后端服务（macOS 与 Linux 通用）。
#
# 用法：
#   ./run_server.sh start [port] [host]   # 默认 port=7013 host=0.0.0.0
#   ./run_server.sh stop
#   ./run_server.sh restart
#   ./run_server.sh status
#   ./run_server.sh logs [n]              # 查看最后 n 行日志，默认 50
#
# 行为：
#   - 自动创建 venv 并安装依赖（首次启动）
#   - 用 nohup 后台运行 uvicorn，断开 SSH 不退出
#   - 不加 --reload（会杀掉正在跑的后台任务线程，破坏断网续跑）
#   - 日志写到 logs/server.out，PID 写到 .server.pid
#   - PID 直接指向 uvicorn 进程，stop 时能精确杀掉（不依赖 setsid）
#
# 可选环境变量：
#   OCR_PORT (默认 7013), OCR_HOST (默认 0.0.0.0)
#   OCR_API_PREFIX (默认 config.py 中的值)
#   OCR_PYTHON (指定 python 解释器路径，默认从 PATH 找 python3)

set -e

# ---- 路径与默认值 ----
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"

PORT="${OCR_PORT:-7013}"
HOST="${OCR_HOST:-0.0.0.0}"
VENV="$DIR/venv"
PID_FILE="$DIR/.server.pid"
LOG_DIR="$DIR/logs"
LOG_FILE="$LOG_DIR/server.out"

mkdir -p "$LOG_DIR"

# ---- 工具函数 ----

# 跨平台检测进程是否存活：kill -0 在两个平台都可用
is_running() {
  local pid="$1"
  [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null
}

# 找一个可用的 python3（>=3.10）
find_python() {
  if [ -n "$OCR_PYTHON" ] && command -v "$OCR_PYTHON" >/dev/null 2>&1; then
    echo "$OCR_PYTHON"
    return
  fi
  for cand in python3 python3.11 python3.10 python3.12 python3.13; do
    if command -v "$cand" >/dev/null 2>&1; then
      local ver
      ver="$("$cand" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")' 2>/dev/null || echo 0.0)"
      local major minor
      major="${ver%%.*}"
      minor="${ver#*.}"
      if [ "$major" -ge 3 ] 2>/dev/null && [ "${minor:-0}" -ge 10 ] 2>/dev/null; then
        echo "$cand"
        return
      fi
    fi
  done
  # 兜底：直接用 python3
  command -v python3 >/dev/null 2>&1 && echo python3 && return
  echo ""
}

# ---- venv 准备 ----
ensure_venv() {
  if [ ! -x "$VENV/bin/python" ]; then
    local py
    py="$(find_python)"
    if [ -z "$py" ]; then
      echo "错误：未找到 python3 (>=3.10)，请安装或通过 OCR_PYTHON 指定路径" >&2
      exit 1
    fi
    echo "创建虚拟环境：$py -m venv $VENV"
    "$py" -m venv "$VENV"
  fi
  # 同步依赖（每次 start 都跑一次，幂等；pip 跳过已装包）
  "$VENV/bin/python" -m pip install --quiet --disable-pip-version-check -r "$DIR/requirements.txt"
}

# ---- 启动 ----
start() {
  if [ -f "$PID_FILE" ] && is_running "$(cat "$PID_FILE")"; then
    echo "后端已在运行，PID=$(cat "$PID_FILE")。如需重启请先 ./run_server.sh stop"
    exit 1
  fi
  rm -f "$PID_FILE"

  ensure_venv

  echo "启动后端：host=$HOST port=$PORT"
  echo "  日志：$LOG_FILE"
  echo "  PID 文件：$PID_FILE"

  # 关键：nohup 直接启动 uvicorn（不经过 setsid），$! 拿到的就是 uvicorn 主进程 PID。
  # Linux 上 nohup + & 已经能让进程在 SSH 断开后继续运行（SIGHUP 被忽略）；
  # macOS 上同理。不依赖 setsid，避免 PID 错位。
  nohup "$VENV/bin/uvicorn" app.main:app \
    --host "$HOST" --port "$PORT" \
    >> "$LOG_FILE" 2>&1 < /dev/null &

  local pid=$!
  echo "$pid" > "$PID_FILE"

  # 等待最多 5 秒确认进程还活着（uvicorn 启动失败会立即退出）
  local ok=0
  for i in 1 2 3 4 5; do
    if ! is_running "$pid"; then
      break
    fi
    # 检查端口是否已监听（uvicorn 启动需要 1-2 秒）
    if curl -s "http://127.0.0.1:$PORT/" >/dev/null 2>&1 \
       || python3 -c "import socket,sys; s=socket.socket(); s.settimeout(0.5); sys.exit(0 if s.connect_ex(('127.0.0.1', $PORT))==0 else 1)" 2>/dev/null; then
      ok=1
      break
    fi
    sleep 1
  done

  if [ "$ok" = "1" ] && is_running "$pid"; then
    echo "已启动，PID=$pid"
  else
    echo "启动失败，查看日志：$LOG_FILE"
    tail -30 "$LOG_FILE" 2>/dev/null || true
    rm -f "$PID_FILE"
    exit 1
  fi
}

# ---- 停止 ----
stop() {
  if [ ! -f "$PID_FILE" ]; then
    echo "未找到 PID 文件，后端可能未运行"
    exit 0
  fi
  local pid
  pid="$(cat "$PID_FILE")"
  if is_running "$pid"; then
    echo "停止后端，PID=$pid"
    # 先发 SIGTERM 优雅退出
    kill "$pid" 2>/dev/null || true
    for i in $(seq 1 10); do
      is_running "$pid" || break
      sleep 1
    done
    # 仍未退出则 SIGKILL
    if is_running "$pid"; then
      echo "未在 10 秒内退出，强制终止"
      kill -9 "$pid" 2>/dev/null || true
    fi
    echo "已停止"
  else
    echo "进程 $pid 已不存在"
  fi
  rm -f "$PID_FILE"
}

# ---- 状态 ----
status() {
  if [ -f "$PID_FILE" ] && is_running "$(cat "$PID_FILE")"; then
    echo "后端运行中，PID=$(cat "$PID_FILE")，端口 $PORT"
  else
    echo "后端未运行"
    rm -f "$PID_FILE" 2>/dev/null || true
    exit 1
  fi
}

# ---- 日志 ----
logs() {
  local n="${1:-50}"
  tail -n "$n" "$LOG_FILE" 2>/dev/null || echo "无日志文件"
}

# ---- 入口 ----
case "${1:-}" in
  start)
    [ -n "$2" ] && PORT="$2"
    [ -n "$3" ] && HOST="$3"
    start
    ;;
  stop)
    stop
    ;;
  restart)
    stop || true
    start
    ;;
  status)
    status
    ;;
  logs)
    logs "$2"
    ;;
  *)
    echo "用法: $0 {start [port] [host]|stop|restart|status|logs [n]}"
    echo "  可选环境变量：OCR_PORT (默认 7013), OCR_HOST (默认 0.0.0.0),"
    echo "                  OCR_API_PREFIX, OCR_PYTHON (指定 python 路径)"
    exit 1
    ;;
esac
