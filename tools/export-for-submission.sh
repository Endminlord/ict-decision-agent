#!/usr/bin/env bash
# 提交导出：把材料包打成可直接上传的 tar.gz，并在打包前强制校验完整性。
# 对应 `待补充清单.md` #18（参赛版 / 自用版分离）—— 把「上传什么」从记忆变成一条命令。
#
# 用法：
#   bash tools/export-for-submission.sh              # 校验 → 打包到上级目录
#   bash tools/export-for-submission.sh --out /tmp   # 指定输出目录
#   bash tools/export-for-submission.sh --no-verify  # 跳过校验（仅调试用，不推荐）
set -euo pipefail

PKG="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT_DIR="$(dirname "$PKG")"
VERIFY=1

while [ $# -gt 0 ]; do
  case "$1" in
    --out) OUT_DIR="$2"; shift 2 ;;
    --no-verify) VERIFY=0; shift ;;
    -h|--help) sed -n '2,10p' "$0"; exit 0 ;;
    *) echo "未知参数：$1" >&2; exit 2 ;;
  esac
done

cd "$PKG"

if [ "$VERIFY" = 1 ]; then
  echo "[1/3] 校验完整性"
  if ! sha256sum -c MANIFEST.sha256 >/tmp/export-verify.log 2>&1; then
    echo "✗ MANIFEST 校验失败，拒绝导出：" >&2
    grep -v ': OK' /tmp/export-verify.log | head -10 >&2
    exit 1
  fi
  OK=$(grep -c ': OK' /tmp/export-verify.log)
  echo "    ✓ ${OK} 个文件全部匹配"
else
  echo "[1/3] 跳过校验（--no-verify）"
fi

STAMP=$(date +%Y%m%d-%H%M)
NAME="ict-material-${STAMP}"
TARBALL="${OUT_DIR}/${NAME}.tar.gz"

echo "[2/3] 打包（排除 .venv / __pycache__ / *.pyc / *.bak-* / 候选层）"
tar czf "$TARBALL" \
  --exclude='.venv' \
  --exclude='__pycache__' \
  --exclude='*.pyc' \
  --exclude='*.bak-*' \
  --exclude='ontology/triples-governance-loose.jsonl' \
  -C "$(dirname "$PKG")" "$(basename "$PKG")"

echo "[3/3] 核对产物"
SIZE=$(du -h "$TARBALL" | cut -f1)
NFILE=$(tar tzf "$TARBALL" | grep -vc '/$' || true)
echo "    $TARBALL"
echo "    ${SIZE} · ${NFILE} 个文件"

echo
echo "候选层（ontology/triples-governance-loose.jsonl，约 200 MB）按设计不随包，"
echo "需要时用 \`python3 ontology/build.py --loose-out <路径>\` 重建。"
