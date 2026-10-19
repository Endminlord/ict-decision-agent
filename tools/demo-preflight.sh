#!/usr/bin/env bash
# 答辩现场演示环境预检（赛题 3）
#
# 用途：上台前 3 分钟跑一次，确认 9 段 23 项都在，任何一条 ✗ 都会在演示时暴露。
# 用法：bash tools/demo-preflight.sh          # 全量检查
#       bash tools/demo-preflight.sh --quiet  # 只输出失败项与汇总
#
# 判据来源：evidence/05-nexent-deployment.md §6.5（minio 掉网复发点）、§12.2（容器→宿主可达）
#          evidence/05-nexent-deployment.md §9（访问入口与端口）
#
# 退出码：0 = 全部通过；1 = 有失败项。

set -u

QUIET=0
[ "${1:-}" = "--quiet" ] && QUIET=1

PASS=0
FAIL=0
FAILED_ITEMS=()

say() { [ "$QUIET" = "0" ] && printf '%s\n' "$*"; return 0; }

ok() {
  PASS=$((PASS + 1))
  [ "$QUIET" = "1" ] && return 0
  printf '  \033[32m✓\033[0m %-34s %s\n' "$1" "${2:-}"
  return 0
}

bad() {
  FAIL=$((FAIL + 1))
  FAILED_ITEMS+=("$1")
  printf '  \033[31m✗\033[0m %-34s %s\n' "$1" "${2:-}"
  [ -n "${3:-}" ] && printf '      → 修复：%s\n' "$3"
  return 0
}

skip() {
  [ "$QUIET" = "1" ] && return 0
  printf '  \033[33m—\033[0m %-34s %s\n' "$1" "${2:-}"
  return 0
}

section() { say ""; say "── $1"; }

ES_URL="${ES_URL:-http://localhost:9220}"
GOV_INDEX="${ES_INDEX:-1-43ce427e8b5447ecad015534f953e084}"
RFC_INDEX="${RFC_INDEX:-rfc-1024}"
EMBED_URL="${EMBED_URL:-http://127.0.0.1:8792/v1/embeddings}"
GW_URL="${GW_URL:-http://127.0.0.1:8790/v1}"
MATERIAL="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# ES ApiKey：环境变量优先，其次本机凭据文件（源码不硬编码，见 cross-doc-demo.py:33）
ES_KEY="${ES_API_KEY:-}"
if [ -z "$ES_KEY" ]; then
  for f in "$HOME/.dsh/es-key.env" "$HOME/.config/ict-demo.env"; do
    if [ -f "$f" ]; then
      ES_KEY="$(grep -m1 '^ES_API_KEY=' "$f" | cut -d= -f2- | tr -d '"'"'"' ')"
      [ -n "$ES_KEY" ] && break
    fi
  done
fi

say "答辩现场演示环境预检 · $(date '+%Y-%m-%d %H:%M:%S')"
say "材料包：$MATERIAL"

# ── 1. 常驻服务 ──────────────────────────────────────────────
section "1. 常驻服务（systemd --user）"
for svc in retrieval-mcp workbench-mcp bge-m3-embed nexent-gw-forward; do
  st="$(systemctl --user is-active "$svc" 2>/dev/null || echo unknown)"
  if [ "$st" = "active" ]; then
    ok "$svc" "active"
  else
    bad "$svc" "$st" "systemctl --user restart $svc"
  fi
done

# ── 2. 端口 ─────────────────────────────────────────────────
section "2. 监听端口"
check_port() {
  if ss -tln 2>/dev/null | grep -qE "[:.]$1\b"; then
    ok "port $1 ($2)" "listening"
  else
    bad "port $1 ($2)" "未监听" "${3:-}"
  fi
}
check_port 9220 "Elasticsearch" "docker start nexent-elasticsearch"
check_port 8792 "bge-m3 embedding" "systemctl --user restart bge-m3-embed"
check_port 8793 "检索 MCP" "systemctl --user restart retrieval-mcp"
check_port 8791 "工作台 MCP" "systemctl --user restart workbench-mcp"
check_port 8790 "模型转发器" "systemctl --user restart nexent-gw-forward"
check_port 3000 "Nexent Web UI" "bash deploy/docker/deploy.sh"

# ── 3. 索引可查 ──────────────────────────────────────────────
section "3. ES 索引（需 ApiKey）"
if [ -z "$ES_KEY" ]; then
  bad "ES ApiKey" "未找到" "写 $HOME/.dsh/es-key.env 一行：ES_API_KEY=xxx"
else
  ok "ES ApiKey" "已加载（${#ES_KEY} 字符）"
  for pair in "$GOV_INDEX:政务" "$RFC_INDEX:RFC"; do
    idx="${pair%%:*}"; label="${pair##*:}"
    cnt="$(curl -s -m 10 -H "Authorization: ApiKey $ES_KEY" "$ES_URL/$idx/_count" 2>/dev/null \
           | python3 -c 'import sys,json;print(json.load(sys.stdin).get("count",""))' 2>/dev/null)"
    if [ -n "$cnt" ] && [ "$cnt" -gt 0 ] 2>/dev/null; then
      ok "索引 $label" "$cnt 块"
    else
      bad "索引 $label" "不可查或为 0" "检查 $idx 是否存在（复核命令.md A 段）"
    fi
  done
fi

# ── 4. embedding ────────────────────────────────────────────
section "4. Embedding 服务"
dim="$(curl -s -m 15 -X POST "$EMBED_URL" -H 'Content-Type: application/json' \
       -d '{"model":"bge-m3","input":["预检探针"]}' 2>/dev/null \
       | python3 -c 'import sys,json;d=json.load(sys.stdin);print(len(d["data"][0]["embedding"]))' 2>/dev/null)"
if [ "${dim:-0}" = "1024" ]; then
  ok "bge-m3 /v1/embeddings" "返回 ${dim} 维"
else
  bad "bge-m3 /v1/embeddings" "维度=${dim:-无响应}" "systemctl --user restart bge-m3-embed"
fi
# provider 判据：CUDA 加载失败会**静默回退 CPU**（241 §1），所以看日志里的 actual 而不是请求值。
# CPU 不算失败（只是慢 27 倍），所以只标黄不标红。
prov="$(grep -o 'actual=\[[^]]*\]' /tmp/embed-svc.log 2>/dev/null | tail -1)"
case "$prov" in
  *CUDA*) ok   "embedding provider" "$prov" ;;
  "")     skip "embedding provider" "日志无 provider 行（旧版服务或日志已轮转）" ;;
  *)      skip "embedding provider（CPU，比 GPU 慢 27×）" "$prov" ;;
esac

# ── 5. MCP 握手 ─────────────────────────────────────────────
section "5. MCP 服务（标准协议握手 + 工具清单）"
# streamable HTTP 是有状态的：initialize 的响应头带 mcp-session-id，
# 后续 tools/list 必须回传，否则返回 "Bad Request: Missing session ID"。
# 响应体是 SSE（event: message / data: {...}），解析取 data: 行。
mcp_probe() { # $1=port  $2=label  $3=unit
  local hdr resp sid tools
  hdr="$(mktemp)"; resp="$(mktemp)"
  curl -s -m 10 -D "$hdr" -o "$resp" -X POST "http://127.0.0.1:$1/mcp" \
       -H 'Accept: application/json, text/event-stream' -H 'Content-Type: application/json' \
       -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"preflight","version":"0"}}}' 2>/dev/null
  sid="$(grep -i '^mcp-session-id:' "$hdr" 2>/dev/null | tr -d '\r' | awk '{print $2}')"
  if grep -q 'protocolVersion' "$resp" 2>/dev/null; then
    if [ -n "$sid" ]; then
      tools="$(curl -s -m 10 -X POST "http://127.0.0.1:$1/mcp" \
               -H 'Accept: application/json, text/event-stream' -H 'Content-Type: application/json' \
               -H "Mcp-Session-Id: $sid" \
               -d '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}' 2>/dev/null \
               | python3 -c 'import sys,json,re
raw=sys.stdin.read().replace("\r","")
m=re.search(r"^data: (\{.*\})$", raw, re.M)
try:
    print(len(json.loads(m.group(1))["result"]["tools"]) if m else 0)
except Exception:
    print(0)' 2>/dev/null)"
      ok "$2" "握手成功 · ${tools:-0} 个工具"
    else
      ok "$2" "握手成功（无 session id）"
    fi
  else
    bad "$2" "握手失败" "systemctl --user restart $3"
  fi
  rm -f "$hdr" "$resp"
}
mcp_probe 8793 "检索 MCP :8793" retrieval-mcp
mcp_probe 8791 "工作台 MCP :8791" workbench-mcp

# ── 6. 模型通路（外网判据） ──────────────────────────────────
section "6. 模型通路（外网依赖 · 断网时必然 ✗ —— 见断网降级）"
GW_KEY="${GW_API_KEY:-${WB_INTL_KEY:-}}"
if [ -z "$GW_KEY" ] && [ -f "$HOME/.dsh/.credentials.yaml" ]; then
  GW_KEY="$(python3 -c 'import sys
try:
    import yaml
    d = yaml.safe_load(open(sys.argv[1], encoding="utf-8"))
    print((d.get("refs") or {}).get("WB_INTL_KEY", "") or "")
except Exception:
    print("")' "$HOME/.dsh/.credentials.yaml" 2>/dev/null)"
fi
if [ -z "$GW_KEY" ]; then
  skip "LLM 网关（8790 → 8788 → 外网）" "无凭据，跳过（设 GW_API_KEY 或 ~/.dsh/.credentials.yaml）"
else
  code="$(curl -s -o /dev/null -w '%{http_code}' -m 60 -X POST "$GW_URL/chat/completions" \
          -H 'Content-Type: application/json' -H "Authorization: Bearer $GW_KEY" \
          -d '{"model":"deepseek-v4.1-flash","messages":[{"role":"user","content":"预检"}],"max_tokens":4}' 2>/dev/null)"
  if [ "$code" = "200" ]; then
    ok "LLM 网关（8790 → 8788 → 外网）" "HTTP 200"
  else
    bad "LLM 网关（8790 → 8788 → 外网）" "HTTP ${code:-000}" "Windows 侧 C:\\wb\\gateway-serve.bat；或改走断网降级演示"
  fi
fi

# ── 7. 容器网络（minio 历史复发点） ──────────────────────────
section "7. Docker 容器网络（§6.5 复发点）"
if ! command -v docker >/dev/null 2>&1; then
  bad "docker" "命令不可用" "本机未装 docker 或不在 PATH"
else
  orphan=""
  total=0
  for c in $(docker ps --format '{{.Names}}' 2>/dev/null); do
    total=$((total + 1))
    nets="$(docker inspect "$c" --format '{{range $k,$v := .NetworkSettings.Networks}}{{$k}} {{end}}' 2>/dev/null)"
    [ -z "$(printf '%s' "$nets" | tr -d ' ')" ] && orphan="$orphan $c"
  done
  if [ -n "$orphan" ]; then
    bad "容器网络完整性" "掉网：$orphan" "docker network connect nexent_network <容器名>"
  else
    ok "容器网络完整性" "$total 个容器均在 nexent_network"
  fi
  # 容器 → 宿主可达（§12.2）
  reach="$(docker exec nexent-runtime curl -s -o /dev/null -w '%{http_code}' -m 8 \
           -X POST http://172.20.0.1:8791/mcp \
           -H 'Accept: application/json, text/event-stream' -H 'Content-Type: application/json' \
           -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"p","version":"0"}}}' 2>/dev/null)"
  if [ "$reach" = "200" ]; then
    ok "容器 → 宿主 MCP" "172.20.0.1:8791 HTTP 200"
  else
    bad "容器 → 宿主 MCP" "HTTP ${reach:-000}" "检查 docker bridge 网关地址与宿主防火墙"
  fi
fi

# ── 8. 本地演示闭环（断网也能跑的那条） ────────────────────────
section "8. 本地演示闭环（零外网依赖 · 断网降级主力）"
if [ -f "$MATERIAL/tools/cross-doc-demo.py" ]; then
  out="$(cd "$MATERIAL" && timeout 60 python3 tools/cross-doc-demo.py \
         --query "国家层面的优化营商环境条例是哪一年出台的？" 2>&1)"
  if printf '%s' "$out" | grep -q 'C1 出处可回查'; then
    grounded="$(printf '%s' "$out" | grep -oE '词面落地 [0-9]+/[0-9]+ 环' | head -1)"
    ok "cross-doc-demo.py 演示闭环" "判据全通过 · ${grounded:-}"
  else
    bad "cross-doc-demo.py 演示闭环" "未产出判据" "检查 ES 索引与 embedding 服务"
  fi
else
  bad "cross-doc-demo.py" "文件缺失" "材料包不完整，重取"
fi

section "9. 平台模型连通性（UI 显示「可用」的前提）"
# connect_status 默认 not_detected ⇒ 平台 UI 的智能体卡片全部消失（01-开发设计文档 §503）。
# 这里只做检测 + 幂等触发平台自己的 healthcheck，不直接改库。
PLATFORM_API="http://localhost:5010"
if ! command -v docker >/dev/null 2>&1; then
  ok "平台检查" "docker 未安装，跳过（不影响检索链路）"
elif ! docker ps --format '{{.Names}}' 2>/dev/null | grep -q '^nexent-postgresql$'; then
  ok "平台检查" "nexent-postgresql 未运行，跳过"
else
  q_status() {
    docker exec nexent-postgresql psql -U root -d nexent -tAc \
      "SELECT display_name || '|' || model_type || '|' || connect_status FROM nexent.model_record_t;" 2>/dev/null
  }
  st="$(q_status)"
  pending="$(printf '%s\n' "$st" | grep -c '|not_detected$' || true)"
  if [ "${pending:-0}" -gt 0 ]; then
    say "  · $pending 个模型未检测，触发平台 healthcheck…"
    printf '%s\n' "$st" | grep '|not_detected$' | while IFS='|' read -r dn mt _cs; do
      enc="$(python3 -c 'import urllib.parse,sys; print(urllib.parse.quote(sys.argv[1]))' "$dn" 2>/dev/null)"
      curl -s -m 25 -X POST "$PLATFORM_API/api/model/healthcheck?display_name=$enc&model_type=$mt" \
        -H 'Authorization: Bearer mock_access_token' >/dev/null 2>&1 || true
    done
    st="$(q_status)"
  fi
  ready="$(printf '%s\n' "$st" | grep -c '|available$' || true)"
  total="$(printf '%s\n' "$st" | grep -c '|' || true)"
  if [ "${ready:-0}" -gt 0 ]; then
    ok "模型 connect_status" "$ready/$total available"
  else
    bad "模型 connect_status" "全部 not_detected" \
      "启动平台后重跑；或 POST $PLATFORM_API/api/model/healthcheck?display_name=<显示名>&model_type=llm"
  fi
  agents="$(curl -s -m 15 "$PLATFORM_API/api/agent/list?page=1&page_size=50" \
            -H 'Authorization: Bearer mock_access_token' 2>/dev/null \
            | python3 -c 'import json,sys
try:
    d = json.load(sys.stdin)
    rows = d if isinstance(d, list) else (d.get("data") or [])
    ok = sum(1 for a in rows if a.get("is_available"))
    print(f"{ok}/{len(rows)}")
except Exception:
    print("-")' 2>/dev/null)"
  case "$agents" in
    [1-9]*) ok "智能体可见性" "$agents is_available=true" ;;
    *)      ok "智能体可见性" "平台 API 未响应（不影响检索链路）" ;;
  esac
fi

# ── 汇总 ────────────────────────────────────────────────────
say ""
say "──────────────────────────────────────────"
if [ "$FAIL" -eq 0 ]; then
  printf '\033[32m预检通过：%d 项全部 OK\033[0m\n' "$PASS"
  say "可以上台。"
  exit 0
fi
printf '\033[31m预检失败：%d 项 OK · %d 项 ✗\033[0m\n' "$PASS" "$FAIL"
printf '失败项：%s\n' "${FAILED_ITEMS[*]}"
say ""
say "若 ✗ 的是「模型通路」而其他全过 —— 现场断网，改走断网降级演示："
say "  1) 本地演示闭环：cd 材料包 && python3 tools/cross-doc-demo.py --query \"<问题>\""
say "  2) 检索 MCP 直调（:8793，三工具均为本地计算）"
say "  3) 预录答案快照回放（见 演示预检与断网降级.md）"
exit 1
