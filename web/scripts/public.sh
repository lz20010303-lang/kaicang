#!/usr/bin/env bash
# 重启本地服务（默认 MODE=mock）+ Cloudflare 免费临时隧道，并打印公网链接。
# 用法：bash scripts/public.sh          重启服务和隧道
#       bash scripts/public.sh stop     全部停止
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT="$(pwd)"
RUN="$ROOT/data/run"
mkdir -p "$RUN"
PORT="${PORT:-8787}"
CLOUDFLARED="${CLOUDFLARED:-$(command -v cloudflared || echo "$HOME/.local/bin/cloudflared")}"
PY="$ROOT/.venv/bin/python"
[ -x "$PY" ] || PY="python3"

stop_pid() {
  local f="$1"
  if [ -f "$f" ]; then
    local pid; pid="$(cat "$f")"
    if kill -0 "$pid" 2>/dev/null; then kill "$pid" 2>/dev/null || true; sleep 1; kill -9 "$pid" 2>/dev/null || true; fi
    rm -f "$f"
  fi
}

stop_all() {
  stop_pid "$RUN/tunnel.pid"
  stop_pid "$RUN/server.pid"
  # 兜底：清掉占用端口的旧服务
  pkill -f "python -m app.main" 2>/dev/null || true
  pkill -f "cloudflared tunnel --url http://127.0.0.1:$PORT" 2>/dev/null || true
}

if [ "${1:-}" = "stop" ]; then stop_all; echo "已停止服务和隧道"; exit 0; fi

if [ ! -x "$CLOUDFLARED" ]; then
  echo "下载 cloudflared（免费、无需账号）..."
  mkdir -p "$HOME/.local/bin"
  OS="$(uname -s)"; ARCH="$(uname -m)"
  BASE=https://github.com/cloudflare/cloudflared/releases/latest/download
  case "$OS-$ARCH" in
    Linux-x86_64) curl -sSL -o "$HOME/.local/bin/cloudflared" "$BASE/cloudflared-linux-amd64" ;;
    Linux-aarch64|Linux-arm64) curl -sSL -o "$HOME/.local/bin/cloudflared" "$BASE/cloudflared-linux-arm64" ;;
    Darwin-arm64) curl -sSL "$BASE/cloudflared-darwin-arm64.tgz" | tar -xz -C "$HOME/.local/bin" ;;
    Darwin-x86_64) curl -sSL "$BASE/cloudflared-darwin-amd64.tgz" | tar -xz -C "$HOME/.local/bin" ;;
    *) echo "不支持的系统 $OS-$ARCH，请手动安装 cloudflared"; exit 1 ;;
  esac
  chmod +x "$HOME/.local/bin/cloudflared"
  CLOUDFLARED="$HOME/.local/bin/cloudflared"
fi

stop_all

export MODE="${MODE:-mock}"
export PORT
nohup "$PY" -m app.main >"$RUN/server.log" 2>&1 &
echo $! >"$RUN/server.pid"

for _ in $(seq 1 30); do
  curl -fsS -o /dev/null "http://127.0.0.1:$PORT/" && break
  sleep 0.5
done
curl -fsS -o /dev/null "http://127.0.0.1:$PORT/" || { echo "服务没有起来，见 $RUN/server.log"; exit 1; }

: >"$RUN/tunnel.log"
nohup "$CLOUDFLARED" tunnel --no-autoupdate --url "http://127.0.0.1:$PORT" >"$RUN/tunnel.log" 2>&1 &
echo $! >"$RUN/tunnel.pid"

URL=""
for _ in $(seq 1 60); do
  URL="$(grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' "$RUN/tunnel.log" | head -n1 || true)"
  [ -n "$URL" ] && break
  sleep 1
done
if [ -z "$URL" ]; then echo "没拿到隧道链接，见 $RUN/tunnel.log"; exit 1; fi
echo "$URL" >"$RUN/public_url.txt"
# 拿到链接不代表已连通：还要等隧道注册到 Cloudflare 边缘（需要出站 7844 端口）。
OK=""
for _ in $(seq 1 30); do
  if grep -q "Registered tunnel connection" "$RUN/tunnel.log"; then OK=1; break; fi
  sleep 1
done
if [ -z "$OK" ]; then
  echo "警告：隧道没有连上 Cloudflare（常见原因：网络封了出站 7844 端口 UDP/TCP）。"
  echo "链接 $URL 现在会显示 Error 1033。请换一个网络或在自己电脑上运行本脚本。日志：$RUN/tunnel.log"
  exit 2
fi
echo "服务模式：MODE=$MODE（实盘需服务端 ALLOW_LIVE=yes）"
echo "公网链接：$URL"
