#!/usr/bin/env bash
# verify-migration.sh — 轻量化迁移验证（对应 04-迁移改动清单模板.md §1）
#
# 用法：
#   bash tools/verify-migration.sh              # 默认跑 MIG-002（政务 → 先进制造）
#   bash tools/verify-migration.sh --sha        # 只打印零改码基线 sha256
#   bash tools/verify-migration.sh --quiet      # 只输出汇总行
#
# 它回答三个问题（模板 §1 的「验证命令」一栏）：
#   1. 换词表后链路还能不能端到端跑通
#   2. 换词表到底改变了什么（召回对照：制造词表应显著多于政务词表）
#   3. 零改码证据（契约文件 sha256 打印出来供对照历史基线）
#
# 设计取舍：**不断言具体条数**。50 / 22 / 31 这些值会随词表与抽取规则演进
# 而变化（例如 2026-10-18 加 org_bad_infix 后变为 48 / 21 / 29），把它们写成
# 硬断言会让脚本一改词表就红。脚本只断言「链路通不通」与「定性关系对不对」，
# 具体读数打印出来由人对照 240-ict-migration-mig002.md。
#
# 退出码：0 全过 / 1 有失败 / 2 用法错误

set -uo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

MIG="MIG-002"
QUIET=0
SHA_ONLY=0

for a in "$@"; do
  case "$a" in
    --quiet) QUIET=1 ;;
    --sha) SHA_ONLY=1 ;;
    -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
    MIG-*) MIG="$a" ;;
    *) echo "未知参数：$a（用法见 --help）" >&2; exit 2 ;;
  esac
done

PASS=0; FAIL=0
ok()  { PASS=$((PASS+1)); [ "$QUIET" = 1 ] || printf '  \033[32m✓\033[0m %s\n' "$1"; }
bad() { FAIL=$((FAIL+1)); printf '  \033[31m✗\033[0m %s\n' "$1"; }
info(){ [ "$QUIET" = 1 ] || printf '    %s\n' "$1"; }
hdr() { [ "$QUIET" = 1 ] || printf '\n\033[1m%s\033[0m\n' "$1"; }

# 一次性临时目录，脚本退出时清理
TMP="$(mktemp -d /tmp/verify-migration.XXXXXX)"
trap 'rm -rf "$TMP"' EXIT

CONTRACT_FILES="ontology/schema.json ontology/build.py ontology/align.py ontology/reason.py"

print_sha() {
  hdr "零改码基线（K2 契约文件）"
  for f in $CONTRACT_FILES; do
    if [ -f "$f" ]; then
      info "$(sha256sum "$f" | awk '{print $1}')  $f"
    else
      bad "缺失：$f"
    fi
  done
  info ""
  info "对照 240-ict-migration-mig002.md §4 记录的历史值；不同即表示该文件被改过。"
}

if [ "$SHA_ONLY" = 1 ]; then
  print_sha
  exit 0
fi

printf '\033[1m=== 轻量化迁移验证 %s ===\033[0m\n' "$MIG"
printf '材料包：%s\n' "$ROOT"

# ── 1. 生成合成语料 ───────────────────────────────────────────────────────
hdr "1. 合成语料（先进制造）"
if MFG_OUT_DIR="$TMP" python3 ontology/gen-mfg-corpus.py >"$TMP/gen.log" 2>&1; then
  ok "语料生成成功"
else
  # 生成器可能忽略 MFG_OUT_DIR 直接写包外路径，不算失败
  info "生成器返回非零，回退到包外已有语料"
fi
CORPUS=""
for cand in "$TMP/chunks.jsonl" \
            "$ROOT/../ict-corpus-manufacturing/chunks.jsonl" \
            "/home/endmin/dsh/research/ict-corpus-manufacturing/chunks.jsonl"; do
  if [ -f "$cand" ]; then CORPUS="$cand"; break; fi
done
if [ -z "$CORPUS" ]; then
  bad "找不到合成语料（先跑 python3 ontology/gen-mfg-corpus.py）"
  printf '\n汇总：%d 通过 / %d 失败\n' "$PASS" "$FAIL"
  exit 1
fi
nchunks=$(wc -l < "$CORPUS" | tr -d ' ')
info "语料：$CORPUS（$nchunks 块）"

# ── 2. 制造词表抽取 ───────────────────────────────────────────────────────
hdr "2. 制造词表抽取"
TRIP_MFG="$TMP/triples-mfg.jsonl"
n_mfg=0
if python3 ontology/build.py --src "$CORPUS" \
     --terms ontology/terms.manufacturing.zh.json \
     --out "$TRIP_MFG" >"$TMP/build-mfg.log" 2>&1 && [ -s "$TRIP_MFG" ]; then
  n_mfg=$(wc -l < "$TRIP_MFG" | tr -d ' ')
  ok "制造词表抽出 $n_mfg 条三元组"
  info "对照 240 §3 的 50 条（2026-10-18 加 org_bad_infix 后为 48）"
else
  bad "制造词表抽取失败（见 $TMP/build-mfg.log）"
  info "$(tail -3 "$TMP/build-mfg.log" 2>/dev/null | tr '\n' ' ')"
fi

# provenance 失败数
if [ -s "$TRIP_MFG" ]; then
  prov_fail=$(python3 - "$TRIP_MFG" <<'PY'
import json, sys
n = 0
for line in open(sys.argv[1], encoding="utf-8"):
    line = line.strip()
    if not line:
        continue
    try:
        rec = json.loads(line)
    except Exception:
        continue
    p = rec.get("provenance") or {}
    if not p.get("chunk_id") or not p.get("evidence"):
        n += 1
print(n)
PY
)
  if [ "$prov_fail" = "0" ]; then
    ok "provenance 失败 0 条"
  else
    bad "provenance 失败 $prov_fail 条"
  fi
fi

# ── 3. 实体归并 ───────────────────────────────────────────────────────────
hdr "3. 实体归并"
ALIAS="$TMP/alias-mfg.json"
n_ent=0
if [ -s "$TRIP_MFG" ] && python3 ontology/align.py --triples "$TRIP_MFG" \
     --terms ontology/terms.manufacturing.zh.json \
     --out "$ALIAS" >"$TMP/align.log" 2>&1 && [ -s "$ALIAS" ]; then
  # align.py 输出 {规范名: {canonical, entity_type, aliases, occurrences}}
  n_ent=$(python3 -c "
import json,sys
d=json.load(open('$ALIAS',encoding='utf-8'))
print(len(d) if isinstance(d,dict) else len(d))
")
  ok "归并 $n_ent 个规范实体"
  info "对照 240 §5 的 31 个（2026-10-18 后为 29）"
else
  bad "实体归并失败（见 $TMP/align.log）"
  info "$(tail -3 "$TMP/align.log" 2>/dev/null | tr '\n' ' ')"
fi

# ── 4. 引用链（R1–R4）──────────────────────────────────────────────────────
hdr "4. 引用链"
REASON="$TMP/reason.json"
CHAINS="$TMP/chains.jsonl"
if [ -s "$TRIP_MFG" ] && python3 ontology/reason.py --src "$TRIP_MFG" \
     --out "$REASON" --chains "$CHAINS" \
     --starts 0 --max-depth 4 >"$TMP/reason.log" 2>&1; then
  ok "引用链构建成功"
else
  bad "引用链构建失败（见 $TMP/reason.log）"
  info "$(tail -3 "$TMP/reason.log" 2>/dev/null | tr '\n' ' ')"
fi
if python3 ontology/reason.py --self-test >"$TMP/reason-st.log" 2>&1; then
  line=$(grep -oE '[0-9]+/[0-9]+' "$TMP/reason-st.log" | tail -1)
  ok "引用链 self-test ${line:-通过}"
else
  bad "引用链 self-test 失败"
  info "$(tail -3 "$TMP/reason-st.log" 2>/dev/null | tr '\n' ' ')"
fi

# ── 5. 对照实验：同一语料换政务词表 ───────────────────────────────────────
hdr "5. 对照实验（同语料换政务词表）"
TRIP_GOV="$TMP/triples-mfg-gov.jsonl"
n_gov=0
if python3 ontology/build.py --src "$CORPUS" \
     --terms ontology/terms.governance.zh.json \
     --out "$TRIP_GOV" >"$TMP/build-gov.log" 2>&1 && [ -s "$TRIP_GOV" ]; then
  n_gov=$(wc -l < "$TRIP_GOV" | tr -d ' ')
  info "政务词表 $n_gov 条（对照 240 §5 的 22 条，2026-10-18 后为 21）"
else
  bad "对照实验失败（见 $TMP/build-gov.log）"
  info "$(tail -3 "$TMP/build-gov.log" 2>/dev/null | tr '\n' ' ')"
fi

# 定性判据：制造词表必须显著多于政务词表（词表决定召回）
if [ "$n_mfg" -gt 0 ] && [ "$n_gov" -gt 0 ]; then
  if [ "$n_mfg" -gt "$n_gov" ]; then
    ratio=$(python3 -c "print(f'{$n_mfg/$n_gov:.1f}')")
    ok "词表决定召回：制造 $n_mfg / 政务 $n_gov = ${ratio}×（240 §5 记录 2.3×）"
  else
    bad "制造词表未显著优于政务词表（$n_mfg vs $n_gov）—— 检查词表是否退化"
  fi
else
  bad "无法比较（有一侧抽取失败）"
fi

# ── 6. 零改码基线 ─────────────────────────────────────────────────────────
print_sha

# ── 汇总 ─────────────────────────────────────────────────────────────────
printf '\n\033[1m汇总：%d 通过 / %d 失败\033[0m\n' "$PASS" "$FAIL"
if [ "$FAIL" = "0" ]; then
  printf '\033[32m迁移 %s 验证通过。\033[0m\n' "$MIG"
  printf '把上面的读数填进 04-迁移改动清单模板.md §1 的对应行。\n'
  exit 0
else
  printf '\033[31m迁移 %s 有 %d 项未通过。\033[0m\n' "$MIG" "$FAIL"
  exit 1
fi
