#!/usr/bin/env bash
# 开发环境一键启动/停止脚本（后端 + 前端）
#
# 用法：
#   ./start.sh            # 启动后端 + 前端（含 MySQL 检查与健康等待）
#   ./start.sh stop       # 停止全部
#   ./start.sh status     # 查看运行状态
#   ./start.sh logs       # 实时跟踪后端日志（logs frontend 看前端）
#
# 依赖：MySQL 容器（docker，名 mysql）、backend/.venv、frontend/node_modules
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_DIR="${ROOT}/backend"
FRONTEND_DIR="${ROOT}/frontend"
PYTHON="${ROOT}/.venv/bin/python"

BACKEND_PORT=8100
FRONTEND_PORT=5173
LOG_DIR="${ROOT}/data/logs"
BACKEND_LOG="${LOG_DIR}/backend.log"
FRONTEND_LOG="${LOG_DIR}/frontend.log"
BACKEND_PID="${LOG_DIR}/backend.pid"
FRONTEND_PID="${LOG_DIR}/frontend.pid"

# ---------- 基础工具 ----------

log() { echo -e "\033[32m[OK]\033[0m $*"; }
warn() { echo -e "\033[33m[!!]\033[0m $*"; }
err() { echo -e "\033[31m[!!]\033[0m $*"; }

is_running() { # $1=pid文件 $2=进程名片段
  local pid_file="$1" name="$2"
  if [ -f "${pid_file}" ]; then
    local pid
    pid="$(cat "${pid_file}" 2>/dev/null || echo "")"
    [ -n "${pid}" ] && kill -0 "${pid}" 2>/dev/null && return 0
  fi
  # pid 文件缺失或失效（残留）→ 按进程名兜底
  pgrep -f "${name}" >/dev/null 2>&1 && return 0
  return 1
}

wait_http() { # $1=url $2=标签 $3=秒数
  local url="$1" label="$2" seconds="$3" i
  for i in $(seq 1 "${seconds}"); do
    if curl -sf -m 2 "${url}" >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  warn "${label} 在 ${seconds}s 内未就绪，检查日志：${LOG_DIR}/"
  return 1
}

# ---------- 子命令 ----------

ensure_mysql() {
  if docker ps --format '{{.Names}}' | grep -qx mysql; then
    log "MySQL 容器运行中"
    return 0
  fi
  if docker ps -a --format '{{.Names}}' | grep -qx mysql; then
    warn "MySQL 容器未启动，尝试启动..."
    docker start mysql >/dev/null
    for i in $(seq 1 20); do
      docker exec mysql mysqladmin ping --silent >/dev/null 2>&1 && { log "MySQL 就绪"; return 0; }
      sleep 1
    done
    err "MySQL 启动超时"
    return 1
  fi
  err "未找到名为 mysql 的容器。请先：docker run -d --name mysql -p 3306:3306 -e MYSQL_ROOT_PASSWORD=你的密码 mysql:8"
  return 1
}

start_backend() {
  if is_running "${BACKEND_PID}" "uvicorn app.main:app"; then
    log "后端已在运行（端口 ${BACKEND_PORT}）"
    return 0
  fi
  mkdir -p "${LOG_DIR}"
  if [ ! -x "${PYTHON}" ]; then
    err "未找到 ${PYTHON}，请先在项目根创建 .venv（uv venv）并安装依赖"
    return 1
  fi
  log "启动后端..."
  cd "${BACKEND_DIR}" || return 1
  # 注意：不要用 (cmd &) 子 shell——$! 会拿到子 shell pid 而非 uvicorn pid；
  # nohup 直接 exec 目标进程，$! 即真实 pid，kill 才有效
  nohup "${PYTHON}" -m uvicorn app.main:app --host 0.0.0.0 --port "${BACKEND_PORT}" \
    >> "${BACKEND_LOG}" 2>&1 &
  echo $! > "${BACKEND_PID}"
  wait_http "http://localhost:${BACKEND_PORT}/api/health" "后端" 30
}

start_frontend() {
  if is_running "${FRONTEND_PID}" "vite"; then
    log "前端已在运行（端口 ${FRONTEND_PORT}）"
    return 0
  fi
  mkdir -p "${LOG_DIR}"
  if [ ! -d "${FRONTEND_DIR}/node_modules" ]; then
    warn "frontend/node_modules 不存在，先执行 npm install（走 npmmirror 镜像）..."
    (cd "${FRONTEND_DIR}" && npm install --no-audit --no-fund)
  fi
  log "启动前端..."
  cd "${FRONTEND_DIR}" || return 1
  # 直接跑 vite 可执行文件（而非 npm run dev）：$! 即 vite 进程，kill 有效
  nohup ./node_modules/.bin/vite >> "${FRONTEND_LOG}" 2>&1 &
  echo $! > "${FRONTEND_PID}"
  wait_http "http://localhost:${FRONTEND_PORT}/" "前端" 30
}

cmd_start() {
  ensure_mysql || return 1
  start_backend
  start_frontend
  echo
  log "全部就绪：前端 http://localhost:${FRONTEND_PORT} （后端 ${BACKEND_PORT}，接口文档 /docs）"
}

cmd_stop() {
  for entry in "${BACKEND_PID}:uvicorn app.main:app" "${FRONTEND_PID}:vite"; do
    local pid_file="${entry%%:*}" name="${entry#*:}"
    if [ -f "${pid_file}" ]; then
      local pid
      pid="$(cat "${pid_file}" 2>/dev/null || echo "")"
      if [ -n "${pid}" ] && kill -0 "${pid}" 2>/dev/null; then
        kill "${pid}" 2>/dev/null && log "已停止 ${name}（pid ${pid}）"
      fi
      rm -f "${pid_file}"
    fi
  done
  # 兜底：清理残留进程（精确 kill 后稍等，减少误报）
  sleep 0.5
  pkill -f "uvicorn app.main:app" 2>/dev/null && warn "清理残留后端进程"
  pkill -f "vite" 2>/dev/null && warn "清理残留前端进程"
  log "全部已停止"
}

cmd_status() {
  local backend="未运行" frontend="未运行"
  is_running "${BACKEND_PID}" "uvicorn app.main:app" && backend="运行中（${BACKEND_PORT}）"
  is_running "${FRONTEND_PID}" "vite" && frontend="运行中（${FRONTEND_PORT}）"
  echo "后端：${backend}"
  echo "前端：${frontend}"
  docker ps --format '{{.Names}} {{.Status}}' | grep -q '^mysql ' && echo "MySQL：运行中" || echo "MySQL：未运行"
}

cmd_logs() {
  local target="${1:-backend}"
  mkdir -p "${LOG_DIR}"
  case "${target}" in
    backend)  tail -f "${BACKEND_LOG}" ;;
    frontend) tail -f "${FRONTEND_LOG}" ;;
    *) err "logs 参数：backend 或 frontend" ;;
  esac
}

# ---------- 入口 ----------

case "${1:-start}" in
  start)  cmd_start ;;
  stop)   cmd_stop ;;
  status) cmd_status ;;
  logs)   cmd_logs "${2:-}" ;;
  *) err "用法：./start.sh [start|stop|status|logs [backend|frontend]]" ;;
esac
