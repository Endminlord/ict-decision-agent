#!/usr/bin/env bash
# 答辩前一键验收：包完整性 + 判据自测 + 读数一致性。
# 与 demo-preflight.sh 分工：那个查"环境能不能跑"，这个查"数字对不对"。
#
#   bash tools/verify-all.sh           # 全跑（含双侧评测，约 15 秒）
#   bash tools/verify-all.sh --fast    # 跳过评测，只查包与自测（约 5 秒）
#
# 退出码：0 全部通过；1 有失败项。

set -uo pipefail
cd "$(dirname "$0")/.." || exit 1

FAST=0
[ "${1:-}" = "--fast" ] && FAST=1

PASS=0; FAIL=0
ok()   { printf '  \033[32m✓\033[0m %s\n' "$1"; PASS=$((PASS+1)); }
bad()  { printf '  \033[31m✗\033[0m %s\n' "$1"; FAIL=$((FAIL+1)); }
head_() { printf '\n\033[1m%s\033[0m\n' "$1"; }

# 从环境或凭证文件取 ES key（与 cross-doc-demo.py 同源）
es_key() {
  [ -n "${ES_API_KEY:-}" ] && { printf '%s' "$ES_API_KEY"; return; }
  local f
  for f in "$HOME/.dsh/es-key.env" "$HOME/.config/ict-demo.env"; do
    [ -f "$f" ] || continue
    local v; v=$(grep -m1 '^ES_API_KEY=' "$f" 2>/dev/null | cut -d= -f2-)
    [ -n "$v" ] && { printf '%s' "$v"; return; }
  done
}

head_ "1. 包完整性（MANIFEST）"
if [ -f MANIFEST.sha256 ]; then
  n=$(wc -l < MANIFEST.sha256 | tr -d ' ')
  if out=$(sha256sum -c MANIFEST.sha256 2>&1); then
    # 清单污染检查：.pyc/.bak/.venv 这三类不该出现在交付清单里。
    # 2026-10-17 冷启动时抓到过一条 ontology/__pycache__/*.pyc —— 在原目录里永远验不出来。
    dirt=$(grep -cE '(__pycache__|\.pyc$|\.bak|\.venv/)' MANIFEST.sha256 2>/dev/null || true)
    if [ "${dirt:-0}" -gt 0 ]; then
      bad "MANIFEST 里有 $dirt 条非交付物（__pycache__/.pyc/.bak/.venv）："
      grep -nE '(__pycache__|\.pyc$|\.bak|\.venv/)' MANIFEST.sha256 | head -5 | sed 's/^/      /'
    else
      ok "清单无污染（无 .pyc / .bak / .venv）"
    fi
    ok "$n 个文件全部匹配"
  else
    bad "$n 个文件中有不匹配："
    printf '%s\n' "$out" | grep -v ': OK$' | head -8 | sed 's/^/      /'
  fi
  # README 里声称的文件数要与实际一致
  claimed=$(grep -oE '[0-9]+ 个文件' README.md | head -1 | grep -oE '[0-9]+')
  if [ -n "$claimed" ]; then
    [ "$claimed" = "$n" ] && ok "README 声称 $claimed 个文件，与实际一致" \
                          || bad "README 声称 $claimed 个文件，实际 $n 个"
  fi
else
  bad "MANIFEST.sha256 不存在"
fi

head_ "2. 判据自测（cross-doc-demo）"
if out=$(python3 tools/cross-doc-demo.py --self-test 2>&1); then
  printf '%s\n' "$out" | grep -qE '全部通过|[0-9]+/[0-9]+' \
    && ok "$(printf '%s' "$out" | tail -1)" || ok "自测通过"
else
  bad "自测失败："; printf '%s\n' "$out" | tail -5 | sed 's/^/      /'
fi

head_ "3. 语料与索引读数（ES）"
KEY=$(es_key)
if [ -z "$KEY" ]; then
  bad "取不到 ES_API_KEY（跳过 ES 检查）"
else
  for pair in "政务:1-43ce427e8b5447ecad015534f953e084:15494" \
              "RFC:rfc-1024:1739"; do
    name=${pair%%:*}; rest=${pair#*:}
    idx=${rest%%:*}; want=${rest##*:}
    got=$(curl -s -H "Authorization: ApiKey $KEY" \
            "http://localhost:9220/${idx}/_count" 2>/dev/null \
          | python3 -c 'import sys,json;print(json.load(sys.stdin).get("count",""))' 2>/dev/null)
    if [ "$got" = "$want" ]; then ok "$name 索引 ${idx} = $got 块"
    else bad "$name 索引 ${idx} = ${got:-取不到}（文档声称 $want）"; fi
  done
fi

head_ "4. track2 MCP 判据"
T2=track2-mcp
if [ -d "$T2" ] && [ -d "$T2/src" ]; then
  # 数据文件在包内但不入 MANIFEST（11.7 MB，可由 build_data.py 逐字节重建）；
  # 重建一次保证与当前 build_data.py 的输出一致。
  if [ ! -f "$T2/src/ict_track2_mcp/data/chunks.jsonl.gz" ]; then
    printf '  … 数据文件不存在，先跑 scripts/build_data.py 重建（约 20 秒）\n'
    if ( cd "$T2" && python3 scripts/build_data.py >/dev/null 2>&1 ); then
      ok "已重建 data/chunks.jsonl.gz"
    else
      bad "build_data.py 失败，后两组判据会跳过"
    fi
  fi
  # 第 0 层：数据生成自测（6 条）。BUILD.md 的四层合计 80 条里，这 6 条在这里。
  if [ -f "$T2/scripts/build_data.py" ]; then
    if out=$(cd "$T2" && python3 scripts/build_data.py --self-test 2>&1); then
      line=$(printf '%s\n' "$out" | grep -oE '[0-9]+/[0-9]+' | tail -1)
      ok "build_data --self-test ${line:-(通过)}"
    else
      bad "build_data --self-test 失败"; printf '%s\n' "$out" | tail -3 | sed 's/^/      /'
    fi
  fi

  total=0
  for t in test_lookup test_mcp_protocol test_domains; do
    [ -f "$T2/tests/$t.py" ] || continue
    if out=$(cd "$T2" && PYTHONPATH=src python3 "tests/$t.py" 2>&1); then
      line=$(printf '%s\n' "$out" | grep -oE '[0-9]+/[0-9]+' | tail -1)
      ok "$t ${line:-(通过)}"; total=$((total+1))
    else
      bad "$t 失败"; printf '%s\n' "$out" | tail -3 | sed 's/^/      /'
    fi
  done
  [ "$total" -eq 0 ] && bad "没找到可跑的测试文件"

  # 对抗测试：28 条攻击用例里已知 9 条绕过（记录在案），判据是「绕过数不超过 9」
  if [ -f "$T2/demo/adversarial-round2.py" ]; then
    if out=$(cd "$T2" && PYTHONPATH=src python3 demo/adversarial-round2.py 2>&1); then
      n=$(printf '%s\n' "$out" | grep -oE '攻击用例 [0-9]+ 条：绕过 [0-9]+' | grep -oE '[0-9]+$')
      if [ "$n" -le 9 ]; then
        ok "对抗测试 绕过 ${n:-?} / 28（≤9，其余强拦住）"
      else
        bad "对抗测试 绕过 $n / 28（>9，有回归）"
      fi
    else
      bad "adversarial-round2.py 失败"
    fi
  fi
else
  bad "$T2/src 不存在"
fi

head_ "5. 演示链路（四条命令）"
demo() {
  local label="$1" q="$2" idx="${3:-}"
  local out
  if [ -n "$idx" ]; then
    out=$(ES_INDEX="$idx" python3 tools/cross-doc-demo.py --query "$q" 2>&1)
  else
    out=$(python3 tools/cross-doc-demo.py --query "$q" 2>&1)
  fi
  if printf '%s\n' "$out" | grep -qE '证据不足|拒答|abstain'; then
    ok "$label → 拒答"
  elif printf '%s\n' "$out" | grep -qE '最高分 [0-9]'; then
    ok "$label → 答出（$(printf '%s\n' "$out" | grep -oE '最高分 [0-9.]+' | head -1)）"
  else
    bad "$label → 输出异常"; printf '%s\n' "$out" | tail -3 | sed 's/^/      /'
  fi
}
demo "Q1 跨层级" "国家层面的优化营商环境条例是哪一年出台的？"
demo "Q2 跨语言" "HTTP/2 的流量控制怎么工作？" "rfc-1024"
demo "Q3 领域外拒答" "量子计算的退相干时间如何测量？"
demo "Q4 纯符号拒答" "？？？"

if [ "$FAST" -eq 0 ]; then
  head_ "6. 双侧评测读数（约 10 秒）"
  run_eval() {
    local label="$1" idx="$2" domain="$3"
    local out
    if [ -n "$idx" ]; then
      out=$(ES_INDEX="$idx" python3 tools/eval-retrieval.py --domain "$domain" 2>&1)
    else
      out=$(python3 tools/eval-retrieval.py --domain "$domain" 2>&1)
    fi
    local a b c d
    a=$(printf '%s\n' "$out" | grep -oE '"hit@1": *[0-9.]+' | grep -oE '[0-9.]+$')
    b=$(printf '%s\n' "$out" | grep -oE 'grounding_rate[^0-9]*([0-9.]+)' | grep -oE '[0-9.]+$')
    c=$(printf '%s\n' "$out" | grep -oE 'abstain_recall[^0-9]*([0-9.]+)' | grep -oE '[0-9.]+$')
    d=$(printf '%s\n' "$out" | grep -oE 'answer_rate[^0-9]*([0-9.]+)' | grep -oE '[0-9.]+$')
    if [ -n "$a" ] || [ -n "$c" ]; then
      ok "$label hit@1=${a:-?} grounding=${b:-?} abstain=${c:-?} answer=${d:-?}"
    else
      bad "$label 评测无输出"; printf '%s\n' "$out" | tail -5 | sed 's/^/      /'
    fi
  }
  run_eval "政务" "" "gov"
  run_eval "RFC " "rfc-1024" "rfc"
fi

# ── 迁移验证（MIG-002：换词表零改码 + 词表决定召回）────────────────────
echo
echo "[迁移验证] MIG-002 政务 → 先进制造"
if [ -f tools/verify-migration.sh ]; then
  mout=$(bash tools/verify-migration.sh --quiet 2>&1)
  mrc=$?
  msum=$(printf '%s\n' "$mout" | grep -E '汇总：' | tail -1)
  if [ "$mrc" -eq 0 ]; then
    ok "MIG-002 ${msum:-通过}"
  else
    bad "MIG-002 ${msum:-有失败}"
    printf '%s\n' "$mout" | grep -E '✗|失败' | head -5 | sed 's/^/      /'
  fi
else
  bad "缺少 tools/verify-migration.sh"
fi

echo
echo "[数字自检] 文档里写的数 vs 代码实测的数"
if [ -f tools/check-numbers.py ]; then
  nout=$(python3 tools/check-numbers.py --quiet 2>&1)
  nsum=$(printf '%s\n' "$nout" | grep -E '^汇总' | tail -1)
  if [ $? -eq 0 ] || printf '%s' "$nsum" | grep -q '不一致 0'; then
    ok "关键数字 ${nsum#汇总  }"
  else
    bad "关键数字不一致"
    printf '%s\n' "$nout" | grep -E '^  ✗' | head -8 | sed 's/^/      /'
  fi
else
  bad "缺少 tools/check-numbers.py"
fi

printf '\n\033[1m汇总\033[0m  通过 %d · 失败 %d\n' "$PASS" "$FAIL"
if [ "$FAIL" -gt 0 ]; then
  printf '失败项请先看上方 ✗ 行。答辩前至少跑一次不带 --fast 的完整验收。\n'
  exit 1
fi
printf '全部通过。\n'
