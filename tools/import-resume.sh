#!/usr/bin/env bash
# 动态断点续跑导入器（systemd 用户单元用；重启后从 ES 现量自动对齐断点）
# 断点公式：offset = floor(ES现量/批)*批 - 10000（宁重叠勿缺口，chunk ID 幂等覆盖）
set -euo pipefail
IDX=1-43ce427e8b5447ecad015534f953e084
SRC=/home/endmin/dsh/research/ict-corpus-chunks/chunks.jsonl
CORE=/home/endmin/dsh/research/ict-open-source/tools/cross-doc-demo.py
exec 9>/tmp/import-corpus.lock
flock -n 9 || exit 0  # 已有实例在跑（手动或上一轮）则直接退出
KEY=$(python3 - <<'PY'
import importlib.util
spec = importlib.util.spec_from_file_location("c", "/home/endmin/dsh/research/ict-open-source/tools/cross-doc-demo.py")
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
print(m.ES_KEY)
PY
)
BASE=$(python3 - <<'PY'
import importlib.util
spec = importlib.util.spec_from_file_location("c", "/home/endmin/dsh/research/ict-open-source/tools/cross-doc-demo.py")
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
print(m.ES.rstrip("/"))
PY
)
COUNT=$(curl -sS -m 30 -H "Authorization: ApiKey $KEY" "$BASE/$IDX/_count" | python3 -c 'import json,sys;print(json.load(sys.stdin)["count"])')
OFFSET=$(( COUNT / 200 * 200 - 10000 ))
if [ "$OFFSET" -lt 0 ]; then OFFSET=0; fi
echo "resume: ES count=$COUNT -> offset=$OFFSET $(date +%F\ %T)"
exec python3 -u /home/endmin/dsh/research/ict-open-source/tools/import-corpus.py --index "$IDX" --src "$SRC" --offset $OFFSET --batch 200 --report /tmp/import-full.json
