#!/usr/bin/env python3
"""校验 schema.json 与 standard-mapping.json 的对应关系。

检查四件事：
1. schema.json 的每个 entity_type 在映射里都有条目
2. schema.json 的每个 relation_type 在映射里都有条目
3. 映射里没有 schema.json 不认识的 id（防止拼写错或残留）
4. summary 里声明的数字与实际统计一致

用法：
  python3 tools/check-standard-mapping.py            # 校验包内文件
  python3 tools/check-standard-mapping.py --self-test # 用畸形输入验证能抓错
"""
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCHEMA = ROOT / "ontology" / "schema.json"
MAPPING = ROOT / "ontology" / "standard-mapping.json"


def load(schema_path: Path, mapping_path: Path):
    with open(schema_path, encoding="utf-8") as f:
        schema = json.load(f)
    with open(mapping_path, encoding="utf-8") as f:
        mapping = json.load(f)
    return schema, mapping


def check(schema: dict, mapping: dict) -> list[str]:
    """返回问题列表；空列表表示全部通过。"""
    problems: list[str] = []

    schema_entities = {e["id"] for e in schema.get("entity_types") or []}
    schema_relations = {r["id"] for r in schema.get("relation_types") or []}

    mapped_entities = {e["id"] for e in mapping.get("entity_types") or []}
    mapped_relations = {r["id"] for r in mapping.get("relation_types") or []}

    # 1/2. 覆盖：schema 有的，映射必须有
    for missing in sorted(schema_entities - mapped_entities):
        problems.append(f"实体类型未映射: {missing}")
    for missing in sorted(schema_relations - mapped_relations):
        problems.append(f"关系类型未映射: {missing}")

    # 3. 多余：映射有的，schema 必须有
    for extra in sorted(mapped_entities - schema_entities):
        problems.append(f"映射里有 schema 不认识的实体类型: {extra}")
    for extra in sorted(mapped_relations - schema_relations):
        problems.append(f"映射里有 schema 不认识的关系类型: {extra}")

    # 每条映射必须至少指向一个词汇表，否则等于没映射
    for group, label in (
        (mapping.get("entity_types") or [], "实体"),
        (mapping.get("relation_types") or [], "关系"),
    ):
        for item in group:
            has_schema_org = bool(item.get("schema_org"))
            has_prov = bool(item.get("prov_o"))
            if not (has_schema_org or has_prov):
                problems.append(f"{label} {item.get('id')} 未指向任何词汇表")
            if item.get("coverage") not in ("exact", "partial"):
                problems.append(
                    f"{label} {item.get('id')} 的 coverage 必须是 exact 或 partial，"
                    f"实际是 {item.get('coverage')!r}"
                )

    # 4. summary 自洽
    summary = mapping.get("summary") or {}
    expected = {
        "entity_types_total": len(mapped_entities),
        "entity_types_exact": sum(
            1 for e in mapping.get("entity_types") or [] if e.get("coverage") == "exact"
        ),
        "relation_types_total": len(mapped_relations),
        "relation_types_exact": sum(
            1 for r in mapping.get("relation_types") or [] if r.get("coverage") == "exact"
        ),
    }
    for key, value in expected.items():
        if summary.get(key) != value:
            problems.append(
                f"summary.{key} 声明 {summary.get(key)}，实际 {value}"
            )

    return problems


def self_test() -> int:
    """用畸形输入验证校验器能抓错。"""
    schema, mapping = load(SCHEMA, MAPPING)
    cases: list[tuple[str, dict, dict, str]] = []

    # 基线：原样应通过
    cases.append(("基线（应通过）", schema, mapping, ""))

    # 删一个实体映射
    m = json.loads(json.dumps(mapping))
    m["entity_types"] = [e for e in m["entity_types"] if e["id"] != "Region"]
    cases.append(("删掉 Region 映射", schema, m, "实体类型未映射: Region"))

    # 删一个关系映射
    m = json.loads(json.dumps(mapping))
    m["relation_types"] = [r for r in m["relation_types"] if r["id"] != "repeals"]
    cases.append(("删掉 repeals 映射", schema, m, "关系类型未映射: repeals"))

    # 加一个 schema 没有的 id
    m = json.loads(json.dumps(mapping))
    m["relation_types"].append(
        {"id": "citesX", "schema_org": ["citation"], "prov_o": [], "coverage": "exact"}
    )
    cases.append(("映射里有拼错的 id", schema, m, "schema 不认识的关系类型: citesX"))

    # 词汇表全空
    m = json.loads(json.dumps(mapping))
    for r in m["relation_types"]:
        if r["id"] == "mentions":
            r["schema_org"] = []
            r["prov_o"] = []
    cases.append(("mentions 未指向任何词汇表", schema, m, "mentions 未指向任何词汇表"))

    # summary 数字不一致
    m = json.loads(json.dumps(mapping))
    m["summary"]["entity_types_exact"] = 99
    cases.append(("summary 数字不一致", schema, m, "summary.entity_types_exact"))

    # coverage 取值非法
    m = json.loads(json.dumps(mapping))
    for e in m["entity_types"]:
        if e["id"] == "Document":
            e["coverage"] = "perfect"
    cases.append(("coverage 非法取值", schema, m, "coverage 必须是 exact 或 partial"))

    failures = 0
    for name, sc, mp, expect in cases:
        problems = check(sc, mp)
        if expect:
            hit = any(expect in p for p in problems)
            if hit:
                print(f"  ✓ {name} → 抓到「{expect}」")
            else:
                print(f"  ✗ {name} → 未抓到「{expect}」，实际 {problems}")
                failures += 1
        else:
            if not problems:
                print(f"  ✓ {name}")
            else:
                print(f"  ✗ {name} → 不该报错却报了 {problems}")
                failures += 1

    total = len(cases)
    print(f"\n自测：{total - failures}/{total} 通过")
    return 0 if failures == 0 else 1


def main() -> int:
    if "--self-test" in sys.argv:
        return self_test()

    if not SCHEMA.exists() or not MAPPING.exists():
        print(f"找不到文件：{SCHEMA} 或 {MAPPING}", file=sys.stderr)
        return 2

    schema, mapping = load(SCHEMA, MAPPING)
    problems = check(schema, mapping)

    if problems:
        print(f"标准映射校验：{len(problems)} 个问题")
        for p in problems:
            print(f"  ✗ {p}")
        return 1

    n_ent = len(mapping.get("entity_types") or [])
    n_rel = len(mapping.get("relation_types") or [])
    exact = sum(
        1
        for item in (mapping.get("entity_types") or []) + (mapping.get("relation_types") or [])
        if item.get("coverage") == "exact"
    )
    print(f"标准映射校验：通过（{n_ent} 实体 + {n_rel} 关系，其中 {exact} 项 exact）")
    print(f"  词汇表：{', '.join(mapping.get('vocabularies', {}))}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
