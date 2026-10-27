# 一键复跑入口：make test / eval / ritual / adv / all
.PHONY: test eval retrieval ritual adv full all

test:
	@cd track2-mcp && for t in tests/test_*.py; do echo "== $$t"; python3 $$t || exit 1; done

eval:
	python3 tools/eval-stratified.py

retrieval:
	python3 tools/eval-retrieval.py

ritual:
	python3 tools/verify-ritual.py full

adv:
	python3 tools/verify-ritual.py adv

all: test eval ritual
