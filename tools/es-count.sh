#!/bin/bash
cd /home/endmin/dsh/research/ict-open-source
ESB=$(python3 -c "import importlib.util;spec=importlib.util.spec_from_file_location('c','tools/cross-doc-demo.py');m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);print(m.ES.rstrip('/'))")
ESK=$(python3 -c "import importlib.util;spec=importlib.util.spec_from_file_location('c','tools/cross-doc-demo.py');m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);print(m.ES_KEY)")
curl -sS -m 20 -H "Authorization: ApiKey $ESK" "$ESB/_cat/indices?h=index,docs.count" | awk '{s+=$2} END {print s}'
