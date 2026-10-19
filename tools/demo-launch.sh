#!/usr/bin/env bash
# 答辩现场一键启动：预检 → 必要时拉起 Nexent → 打开浏览器 → 打印演示问题卡。
#
# 与 demo-preflight.sh 的分工：那个只检查（只读），这个会动手（起容器、开浏览器）。
# 答辩现场给评委看的顺序：先跑这个，绿灯后按屏幕上的问题卡演示。
#
# 用法：
#   bash tools/demo-launch.sh            # 预检 + 必要时启动 + 开浏览器
#   bash tools/demo-launch.sh --check    # 只预检，不动任何东西
#   bash tools/demo-launch.sh --no-open  # 不自动开浏览器（评委机器上没图形界面时用）
set -uo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

# Nexent 部署目录：默认 $HOME/nexent-ict（平台不在本包内，需另装），可用 COMPOSE_DIR 覆盖。
COMPOSE_DIR="${COMPOSE_DIR:-$HOME/nexent-ict/deploy/docker/compose}"
URL="http://localhost:3000/zh/newchat"
CHECK_ONLY=0
OPEN_BROWSER=1

for a in "$@"; do
  case "$a" in
    --check)   CHECK_ONLY=1 ;;
    --no-open) OPEN_BROWSER=0 ;;
    -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
  esac
done

b() { printf '\033[1m%s\033[0m\n' "$*"; }
ok() { printf '  \033[32m✓\033[0m %s\n' "$*"; }
bad() { printf '  \033[31m✗\033[0m %s\n' "$*"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$*"; }

b "1. 环境预检（23 项）"
if bash tools/demo-preflight.sh --quiet; then
  ok "预检全过"
  PREFLIGHT_OK=1
else
  bad "预检有失败项，先跑不带 --quiet 的版本看是哪一条"
  PREFLIGHT_OK=0
fi

if [ "$CHECK_ONLY" = "1" ]; then
  echo
  [ "$PREFLIGHT_OK" = "1" ] && { b "只检查模式：环境可用。"; exit 0; }
  b "只检查模式：环境有缺项。"; exit 1
fi

b "2. Nexent 平台容器"
if docker ps --format '{{.Names}}' 2>/dev/null | grep -qx nexent-web; then
  ok "nexent-web 已在运行"
else
  warn "nexent-web 未运行，尝试拉起（需要 1-2 分钟）"
  if [ -d "$COMPOSE_DIR" ]; then
    ( cd "$COMPOSE_DIR" && docker compose up -d ) 2>&1 | tail -5
    for i in $(seq 1 60); do
      docker ps --format '{{.Names}}' 2>/dev/null | grep -qx nexent-web && break
      sleep 2
    done
    if docker ps --format '{{.Names}}' | grep -qx nexent-web; then
      ok "nexent-web 已启动"
    else
      bad "nexent-web 启动失败，看 docker compose logs"
    fi
  else
    bad "compose 目录不存在：$COMPOSE_DIR"
  fi
fi

# minio 掉网络是历史复发点：容器在跑但不在 nexent_network 上时，
# 所有智能体运行都会失败，而 UI 上仍显示"成功"（假绿）。
b "3. minio 网络（历史复发点）"
if docker inspect nexent-minio --format '{{range $k, $v := .NetworkSettings.Networks}}{{$k}} {{end}}' 2>/dev/null | grep -q nexent_network; then
  ok "nexent-minio 在 nexent_network 上"
else
  warn "nexent-minio 不在 nexent_network 上，正在重连（否则智能体运行会静默失败）"
  docker network connect nexent_network nexent-minio 2>&1 | tail -2 && ok "已重连" || bad "重连失败"
fi

b "4. 打开浏览器"
if [ "$OPEN_BROWSER" = "1" ]; then
  if command -v explorer.exe >/dev/null 2>&1; then
    explorer.exe "$URL" >/dev/null 2>&1 &
    ok "已请求打开 $URL"
  elif command -v xdg-open >/dev/null 2>&1; then
    xdg-open "$URL" >/dev/null 2>&1 &
    ok "已请求打开 $URL"
  else
    warn "没有可用的浏览器启动器，手动打开：$URL"
  fi
else
  warn "跳过（--no-open）：手动打开 $URL"
fi

b "5. 演示问题（照念即可，详见 演示问题卡.md）"
cat <<'CARD'
  Q1（跨层级）  国家层面的优化营商环境条例是哪一年出台的？
                预期：8 环 · 最高分 0.88 · 五条判据全 ✓

  Q2（跨语言）  ES_INDEX=rfc-1024 python3 tools/cross-doc-demo.py \
                  --query "HTTP/2 的流量控制怎么工作？"
                预期：5 环 · 0.82 · 命中 RFC 9110 与 RFC 9000

  Q3（拒答）    量子计算的退相干时间如何测量？
                预期：0 环 · 明确说"证据不足"（不是答不出，是不该答）

  兜底：网络断 / ES 慢 / 平台崩 → 看 演示预检与断网降级.md 三条降级路，
        或直接读 evidence/demo-snapshot/q1.txt（与实时输出逐字节一致）。
CARD

echo
if [ "$PREFLIGHT_OK" = "1" ]; then
  b "就绪。"
  exit 0
fi
b "环境有缺项，但降级路径仍可演示（见上面的兜底行）。"
exit 0
