"""Append-only work-order store.

v4.2's third discipline asked for "所有状态落盘 JSONL，进程重启后 state/ 能恢复
work_order". ``chain.py`` has carried a ``store`` seam for exactly this, but
nothing ever passed one — so nothing was ever persisted, and a run's archive
existed only in the process that produced it. A crash meant it never happened.

Design, and why:

* **Append-only, one JSON object per line.** A work order legitimately moves
  through states, so the file is a log, not a table. Rewriting it on every save
  would make a crash mid-write lose the whole history instead of the last line.
* **Latest record wins on read.** ``load_work_order`` returns the most recent
  record for an id, because the last write is the current state.
* **A malformed line is skipped and counted, not fatal.** A store that refuses
  to read because one line is corrupt hides every other record too. The count is
  reported so the damage is visible rather than silent.
* **Writes are appends at the OS level**, so a concurrent writer cannot
  interleave a partial line with ours.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any, Mapping

DEFAULT_PATH = "state/work_orders.jsonl"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _resolve(path: Any) -> str:
    if path is not None:
        return str(path)
    state_dir = os.environ.get("DSH_STATE_DIR")
    if state_dir:
        return os.path.join(state_dir, "work_orders.jsonl")
    return DEFAULT_PATH


class WorkOrderStore:
    """Callable store matching ``chain.py``'s ``store`` seam."""

    def __init__(self, path: Any = None) -> None:
        self.path = _resolve(path)
        self.skipped_lines = 0
        self.write_error: str | None = None

    # -- writing ------------------------------------------------------------

    def save(self, order_id: Any, data: Any) -> dict:
        """Append one record. Returns the stored record; raises on a bad call."""
        if not isinstance(order_id, str) or not order_id.strip():
            raise ValueError("order_id must be a nonempty string")
        if not isinstance(data, Mapping):
            raise TypeError("data must be a mapping")
        record = dict(data)
        # The envelope always wins: a caller cannot backdate or rename a record.
        record["order_id"] = order_id
        record["saved_at"] = _now()

        directory = os.path.dirname(os.path.abspath(self.path))
        try:
            os.makedirs(directory, exist_ok=True)
            line = json.dumps(record, ensure_ascii=False, default=str) + "\n"
            # O_APPEND keeps a concurrent writer from interleaving with us.
            descriptor = os.open(
                self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600
            )
            try:
                os.write(descriptor, line.encode("utf-8"))
            finally:
                os.close(descriptor)
        except OSError as exc:
            self.write_error = f"{type(exc).__name__}: {exc}"
            raise
        return record

    def __call__(self, archive: Any) -> bool:
        """The ``store(archive)`` protocol the chain calls.

        Returning ``False`` means "not persisted"; the chain records that as a
        degradation rather than pretending the archive survived.
        """
        if not isinstance(archive, Mapping):
            return False
        order_id = archive.get("task_id")
        if not isinstance(order_id, str) or not order_id.strip():
            order_id = "unknown-task"
        try:
            self.save(order_id, dict(archive))
        except (OSError, TypeError, ValueError):
            return False
        return True

    # -- reading ------------------------------------------------------------

    def records(self) -> list[dict]:
        """Every readable record, oldest first."""
        self.skipped_lines = 0
        try:
            with open(self.path, "r", encoding="utf-8") as stream:
                raw_lines = stream.read().splitlines()
        except FileNotFoundError:
            return []
        except OSError:
            return []

        records = []
        for line in raw_lines:
            if not line.strip():
                continue
            try:
                parsed = json.loads(line)
            except ValueError:
                self.skipped_lines += 1
                continue
            if isinstance(parsed, dict):
                records.append(parsed)
            else:
                self.skipped_lines += 1
        return records

    def load(self, order_id: Any) -> dict | None:
        """The most recent record for ``order_id``, or None."""
        if not isinstance(order_id, str):
            return None
        found = None
        for record in self.records():
            if record.get("order_id") == order_id:
                found = record
        return found

    def history(self, order_id: Any) -> list[dict]:
        """Every record for ``order_id``, oldest first.

        ``list``/``load`` deliberately collapse a task to its latest attempt, so
        a re-run replaces what the viewer shows. The earlier attempts are still in
        the file; this is how the viewer can say they exist instead of hiding them.
        """
        if not isinstance(order_id, str):
            return []
        return [
            record for record in self.records()
            if record.get("order_id") == order_id
        ]

    def list(self, limit: int = 50) -> list[dict]:
        """The most recent ``limit`` records, newest first, one per order id.

        "Newest" means most recently *written*, not first seen: an order that was
        updated last belongs at the top even if it was created first.
        """
        if type(limit) is not int or limit <= 0:
            raise ValueError("limit must be a positive integer")
        latest: dict[str, dict] = {}
        last_index: dict[str, int] = {}
        for index, record in enumerate(self.records()):
            key = record.get("order_id")
            if not isinstance(key, str):
                continue
            latest[key] = record
            last_index[key] = index
        ordered = [
            latest[key]
            for key in sorted(last_index, key=last_index.get, reverse=True)
        ]
        return ordered[:limit]

    def get_state(self) -> dict:
        return {
            "path": self.path,
            "orders": len(self.list(limit=10_000)),
            "skipped_lines": self.skipped_lines,
            "write_error": self.write_error,
        }


BACKENDS = ("jsonl", "sqlite")


def open_store(state_dir: Any, backend: str = "jsonl"):
    """Open a work-order store for the requested backend.

    Both stores expose save/load/list, so the chain does not care which one it
    gets. JSONL stays the default: the SQLite trigger (a file past ~10MB, or a
    need for field queries) has not been reached.
    """
    choice = (backend or "jsonl").strip().lower()
    if choice not in BACKENDS:
        raise ValueError(f"unknown backend {backend!r}; expected one of {', '.join(BACKENDS)}")
    directory = state_dir or "state"
    if choice == "sqlite":
        from runtime.storage_sqlite import SQLiteWorkOrderStore

        return SQLiteWorkOrderStore(os.path.join(directory, "work_orders.db"))
    return WorkOrderStore(os.path.join(directory, "work_orders.jsonl"))


# --- module-level helpers (the v4.2 Phase 1.2 contract) --------------------

def save_work_order(order_id: str, data: dict, *, path: Any = None) -> dict:
    return WorkOrderStore(path).save(order_id, data)


def load_work_order(order_id: str, *, path: Any = None) -> dict | None:
    return WorkOrderStore(path).load(order_id)


def list_work_orders(limit: int = 50, *, path: Any = None) -> list[dict]:
    return WorkOrderStore(path).list(limit=limit)


# --- 模型检测结果的缓存（4.11 的下拉框标签靠它） ---------------------------

DEFAULT_PROFILE_PATH = "state/model_profiles.jsonl"
DEFAULT_TEMPLATE_PATH = "state/templates.jsonl"
DEFAULT_SIDE_BY_SIDE_PATH = "state/side_by_side.jsonl"


def _resolve_profiles(path: Any) -> str:
    if path is not None:
        return str(path)
    state_dir = os.environ.get("DSH_STATE_DIR")
    if state_dir:
        return os.path.join(state_dir, "model_profiles.jsonl")
    return DEFAULT_PROFILE_PATH


class ModelProfileStore:
    """一次检测一行，按 ``model_id`` 取最新那行。

    为什么是 append-only 的日志而不是一张表：检测结果**会被改写**（同一个模型
    重测一次，分数可能不同），而「上一次是多少」本身是有用的历史 —— 改写就把它
    抹掉了。最新的那行代表现在，旧行还在，谁都能看出一个模型是不是在退化。

    与工单库同样的三条纪律：最新一行说了算；坏行跳过并计数，不因为一行坏了就
    装作整个文件不存在；写入用 O_APPEND，并发写不会互相插进半行。
    """

    def __init__(self, path: Any = None) -> None:
        self.path = _resolve_profiles(path)
        self.skipped_lines = 0
        self.write_error: str | None = None

    def save(self, model_id: Any, report: Any) -> dict:
        """追加一条检测记录。"""
        if not isinstance(model_id, str) or not model_id.strip():
            raise ValueError("model_id must be a nonempty string")
        if not isinstance(report, Mapping):
            raise TypeError("report must be a mapping")
        record = dict(report)
        # 信封永远说了算：调用方不能给记录改名或改时间。
        record["model_id"] = model_id.strip()
        record["saved_at"] = _now()

        directory = os.path.dirname(os.path.abspath(self.path))
        try:
            os.makedirs(directory, exist_ok=True)
            line = json.dumps(record, ensure_ascii=False, default=str) + "\n"
            descriptor = os.open(
                self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600
            )
            try:
                os.write(descriptor, line.encode("utf-8"))
            finally:
                os.close(descriptor)
        except OSError as exc:
            self.write_error = f"{type(exc).__name__}: {exc}"
            raise
        return record

    def records(self) -> list[dict]:
        """能读出来的每一条，老的在前。"""
        self.skipped_lines = 0
        try:
            with open(self.path, "r", encoding="utf-8") as stream:
                raw_lines = stream.read().splitlines()
        except FileNotFoundError:
            return []
        except OSError:
            return []

        records = []
        for line in raw_lines:
            if not line.strip():
                continue
            try:
                parsed = json.loads(line)
            except ValueError:
                self.skipped_lines += 1
                continue
            if isinstance(parsed, Mapping) and str(parsed.get("model_id") or "").strip():
                records.append(dict(parsed))
            else:
                self.skipped_lines += 1
        return records

    def latest(self) -> dict[str, dict]:
        """``model_id`` → 最新一条记录。"""
        newest: dict[str, dict] = {}
        for record in self.records():
            newest[str(record["model_id"])] = record
        return newest

    def get(self, model_id: Any) -> dict | None:
        """这个模型最新一次检测；**没测过就返回 None**，不编一个默认值。"""
        if not isinstance(model_id, str) or not model_id.strip():
            return None
        return self.latest().get(model_id.strip())

    def get_state(self) -> dict:
        return {
            "path": self.path,
            "profiles": len(self.latest()),
            "skipped_lines": self.skipped_lines,
            "write_error": self.write_error,
        }


def _resolve_side_by_side(path: Any) -> str:
    if path is not None:
        return str(path)
    state_dir = os.environ.get("DSH_STATE_DIR")
    if state_dir:
        return os.path.join(state_dir, "side_by_side.jsonl")
    return DEFAULT_SIDE_BY_SIDE_PATH


class SideBySideStore:
    """一次并排对比一行，**只追加**。

    为什么不做成「每个模型一行最新状态」：这里比的不是模型的当前状态，是**同一句
    话在不同模型上各自答了什么**。同一句话跑第二遍，第二遍之所以值得看，恰恰是
    因为它跟第一遍不一样（模型会变、温度会变）——按模型合并就把这件事抹掉了。

    与工单库同样的三条纪律：坏行跳过并计数；写入用 O_APPEND，并发写不会互相插进
    半行；「还没跑过」和「读不出来」分得开（前者不是故障，后者要能看见）。
    """

    def __init__(self, path: Any = None) -> None:
        self.path = _resolve_side_by_side(path)
        self.skipped_lines = 0
        self.write_error: str | None = None
        self.read_error: str | None = None

    def save(self, run_id: Any, report: Any) -> dict:
        """追加一次对比。信封（run_id / saved_at）永远说了算。"""
        if not isinstance(run_id, str) or not run_id.strip():
            raise ValueError("run_id must be a nonempty string")
        if not isinstance(report, Mapping):
            raise TypeError("report must be a mapping")
        record = dict(report)
        record["run_id"] = run_id.strip()
        record["saved_at"] = _now()

        directory = os.path.dirname(os.path.abspath(self.path))
        try:
            os.makedirs(directory, exist_ok=True)
            line = json.dumps(record, ensure_ascii=False, default=str) + "\n"
            descriptor = os.open(
                self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600
            )
            try:
                os.write(descriptor, line.encode("utf-8"))
            finally:
                os.close(descriptor)
        except OSError as exc:
            self.write_error = f"{type(exc).__name__}: {exc}"
            raise
        return record

    def records(self) -> list[dict]:
        """能读出来的每一条，老的在前。"""
        self.skipped_lines = 0
        self.read_error = None
        try:
            with open(self.path, "r", encoding="utf-8") as stream:
                raw_lines = stream.read().splitlines()
        except FileNotFoundError:
            # 还没跑过。这不是故障——第一次打开界面不该看到一条不存在的错误。
            return []
        except OSError as exc:
            self.read_error = f"{type(exc).__name__}: {exc}"
            return []

        records = []
        for line in raw_lines:
            if not line.strip():
                continue
            try:
                parsed = json.loads(line)
            except ValueError:
                self.skipped_lines += 1
                continue
            if isinstance(parsed, Mapping) and str(parsed.get("run_id") or "").strip():
                records.append(dict(parsed))
            else:
                self.skipped_lines += 1
        return records

    def recent(self, limit: int = 5) -> list[dict]:
        """最近几次，新的在前。"""
        try:
            count = max(1, int(limit))
        except (TypeError, ValueError):
            count = 5
        return list(reversed(self.records()))[:count]

    def get_state(self) -> dict:
        return {
            "path": self.path,
            "runs": len(self.records()),
            "skipped_lines": self.skipped_lines,
            "read_error": self.read_error,
            "write_error": self.write_error,
        }


def _resolve_templates(path: Any) -> str:
    if path is not None:
        return str(path)
    state_dir = os.environ.get("DSH_STATE_DIR")
    if state_dir:
        return os.path.join(state_dir, "templates.jsonl")
    return DEFAULT_TEMPLATE_PATH


class TemplateStore:
    """一段可复用的输入，一行一个版本，按 ``name`` 取最新那行。

    为什么还是 append-only：模板会被改（同一个名字存第二次就是一次改写），而
    「上一次是怎么写的」在把一次跑坏的运行追回到输入的时候有用——重放一个模板
    跑出来的东西不对，你得能看见当时存的到底是什么。

    **退休不是删除**：``retire()`` 追加一条带 ``retired`` 的记录，而不是把行抹掉。
    删除是一次事件，值得留一行；把行抹掉之后，「这个模板以前存在过」就无从查证了。
    退休的模板 ``get()`` 照样取得到（带 ``retired: True``），``list_active()`` 不列。
    """

    def __init__(self, path: Any = None) -> None:
        self.path = _resolve_templates(path)
        self.skipped_lines = 0
        self.write_error: str | None = None
        # 读不出来和「不存在」是两件事。前者要是被当成后者，界面会说「没这个模板」，
        # 而真相是这个文件根本打不开——用户会去翻自己到底存没存过，而不是去查权限。
        # 和「没测过 ≠ 测出来不行」是同一条纪律。
        self.read_error: str | None = None

    def save(self, name: Any, user_input: Any, *, domain: str = "", note: str = "") -> dict:
        """追加一条模板（同名就是新版本）。"""
        if not isinstance(name, str) or not name.strip():
            raise ValueError("name must be a nonempty string")
        if not isinstance(user_input, str) or not user_input.strip():
            raise ValueError("user_input must be a nonempty string")
        record = {
            "name": name.strip(),
            "user_input": user_input.strip(),
            "domain": str(domain or "").strip(),
            "note": str(note or "").strip(),
            "retired": False,
            "saved_at": _now(),
        }
        return self._append(record)

    def retire(self, name: Any) -> dict:
        """让一个模板退休：追加一条退休记录，不抹掉任何历史。"""
        current = self.get(name)
        if current is None:
            # 「这个文件打不开」和「没这个模板」会通向两个完全不同的排查方向，
            # 所以不能都报成「没这个模板」。
            if self.read_error:
                raise OSError(self.read_error)
            raise KeyError(str(name))
        record = dict(current)
        record["retired"] = True
        record["saved_at"] = _now()
        return self._append(record)

    def _append(self, record: dict) -> dict:
        directory = os.path.dirname(os.path.abspath(self.path))
        try:
            os.makedirs(directory, exist_ok=True)
            line = json.dumps(record, ensure_ascii=False, default=str) + "\n"
            descriptor = os.open(
                self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600
            )
            try:
                os.write(descriptor, line.encode("utf-8"))
            finally:
                os.close(descriptor)
        except OSError as exc:
            self.write_error = f"{type(exc).__name__}: {exc}"
            raise
        return record

    def records(self) -> list[dict]:
        """能读出来的每一条，老的在前。"""
        self.skipped_lines = 0
        self.read_error = None
        try:
            with open(self.path, "r", encoding="utf-8") as stream:
                raw_lines = stream.read().splitlines()
        except FileNotFoundError:
            # 还没存过。这不是错误，read_error 保持 None——否则第一次打开界面就会
            # 看到一条不存在的故障。
            return []
        except OSError as exc:
            self.read_error = f"{type(exc).__name__}: {exc}"
            return []

        records = []
        for line in raw_lines:
            if not line.strip():
                continue
            try:
                parsed = json.loads(line)
            except ValueError:
                self.skipped_lines += 1
                continue
            # 名字和输入都要有：一个没有输入的模板填进输入框就是一片空白，而列表里
            # 出现一个按下去没反应的条目，比少一个条目更难解释。读的时候严一点，
            # 是为了让**列出来的模板一定是能用的**。
            if (
                isinstance(parsed, Mapping)
                and str(parsed.get("name") or "").strip()
                and str(parsed.get("user_input") or "").strip()
            ):
                records.append(dict(parsed))
            else:
                self.skipped_lines += 1
        return records

    def latest(self) -> dict[str, dict]:
        """``name`` → 最新一条记录（退休的也在里面）。"""
        newest: dict[str, dict] = {}
        for record in self.records():
            newest[str(record["name"])] = record
        return newest

    def get(self, name: Any) -> dict | None:
        """这个模板最新的一条；**没有就返回 None**，不编一个默认值。

        退休的模板照样返回（带 ``retired: True``）：退休和不存在是两件事，
        调用方自己决定怎么处理。
        """
        if not isinstance(name, str) or not name.strip():
            return None
        return self.latest().get(name.strip())

    def list_active(self) -> list[dict]:
        """没退休的模板，按名字排序（界面下拉直接用这个顺序）。"""
        return [
            record for _, record in sorted(self.latest().items())
            if not record.get("retired")
        ]

    def get_state(self) -> dict:
        return {
            "path": self.path,
            "templates": len(self.list_active()),
            "skipped_lines": self.skipped_lines,
            "write_error": self.write_error,
            "read_error": self.read_error,
        }
