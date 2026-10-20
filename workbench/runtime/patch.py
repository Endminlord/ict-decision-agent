"""Context-anchored file patching.

Rewriting a whole file to change three lines is the unreliable pattern: any
difference between the rewrite and the original silently discards the rest of the
file. A patch that names the exact text it replaces fails **loudly** instead of
quietly losing content.

Four properties, all fail-closed:

* **Version check.** The patch carries the sha256 the caller read. If the file
  changed in the meantime the patch is rejected — otherwise the edit lands on
  content nobody looked at.
* **Unique context.** The text to replace must occur exactly once. Zero matches
  means the context is stale; two means the patch is ambiguous. Both are
  rejected rather than guessed at.
* **Atomicity.** Every hunk is validated before anything is written, and the
  write goes to a temporary file that is then renamed. A rejected patch leaves
  the file byte-for-byte untouched.
* **Undo.** When a snapshot directory is given, the original bytes are stored
  **before** the write, and the snapshot fails closed: if it cannot be taken,
  the patch is not applied. A patch that cannot be undone is the same class of
  mistake as a patch without a version stamp — both edit content on the strength
  of a promise nobody can check. The module reports ``undoable`` on every
  result, so a caller that skipped the snapshot can see that it did.

Rollback restores the stored bytes rather than reversing the hunks: a reverse
patch is a second guess, and guessing is the thing this module exists to avoid.

This module never invents content: the caller supplies the replacement text, and
only the matched span is changed.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import sys
import tempfile
import time
from typing import Any, Mapping, Sequence

from runtime.audit import record as audit_record

__all__ = [
    "fingerprint",
    "preview_file",
    "apply_patch",
    "apply_file",
    "resolve_in_root",
    "take_snapshot",
    "rollback",
    "list_snapshots",
    "default_snapshot_dir",
    "main",
]

# 快照放哪。和工单账本同一棵树：都在 state/ 下。
DEFAULT_SNAPSHOT_DIR = os.path.join("state", "snapshots")


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def default_snapshot_dir() -> str:
    return DEFAULT_SNAPSHOT_DIR


def _snapshot_root(snapshot_dir: Any = None) -> str:
    return str(snapshot_dir) if snapshot_dir else default_snapshot_dir()


def resolve_in_root(root: Any, path: Any) -> tuple[str | None, str]:
    """把一个相对路径解析到 ``root`` 里面。出不去就返回原因，不抛。

    内核本身是个工具，写哪儿都写；**边界**是调用方的事。所以这条检查在这里做成
    公共的，好让服务端和命令行用的是同一份判断，而不是各写一份然后慢慢走样。
    """
    if not isinstance(path, str) or not path.strip():
        return None, "path must be a nonempty string"
    root_path = os.path.abspath(str(root))
    if os.path.isabs(path):
        return None, f"absolute path refused: {path}"
    resolved = os.path.abspath(os.path.join(root_path, path))
    if resolved != root_path and not resolved.startswith(root_path + os.sep):
        return None, f"path escapes the workspace: {path}"
    return resolved, ""


def take_snapshot(path: Any, *, snapshot_dir: Any = None, becomes: Any = None) -> dict:
    """把 ``path`` 现在的原文存下来，返回一个能拿来回退的 id。

    文件不存在也算一种状态：记成 ``existed: False``，回退时把它删掉。不这么记的话
    「回退」会在新建的文件上变成一次猜测。

    ``becomes`` 是「这个文件马上要被改成什么」的哈希。记它是因为回退要先判断
    「我有没有盖掉别人后来的改动」——而判据只能是**我们写完时的那一份**。拿快照里
    的哈希去比，刚打完补丁就会永远对不上，于是回退功能看着在、其实永远拒绝。

    快照存的是**文本**，所以读不出来的文件（不是 UTF-8）拒绝存：这样调用方拿到的是
    ``ok: False``，一个字都不写——「存不下来就不改文件」这条规矩对二进制文件同样
    成立，不是往外抛一个异常。
    """
    target = str(path)
    directory = _snapshot_root(snapshot_dir)
    try:
        os.makedirs(directory, exist_ok=True)
        exists = os.path.isfile(target)
        content = _read(target) if exists else ""
        snapshot_id = os.path.basename(tempfile.mkdtemp(prefix=time.strftime("%Y%m%dT%H%M%S-"), dir=directory))
        payload = {
            "snapshot_id": snapshot_id,
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "path": os.path.abspath(target),
            "existed": exists,
            "sha256": _sha256(content) if exists else "",
            "sha256_after": becomes if isinstance(becomes, str) else "",
            "content": content,
        }
        with open(os.path.join(directory, snapshot_id, "snapshot.json"), "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False)
    except (OSError, UnicodeDecodeError) as exc:
        # 存不下来就不许改文件。调用方看到 applied=False，磁盘上什么都没动。
        return {"ok": False, "reason": f"snapshot_failed:{type(exc).__name__}: {exc}"}
    return {
        "ok": True,
        "snapshot_id": snapshot_id,
        "path": target,
        "existed": exists,
        "sha256": payload["sha256"],
        "sha256_after": payload["sha256_after"],
        "dir": os.path.join(directory, snapshot_id),
    }


def _load_snapshot(snapshot_id: Any, *, snapshot_dir: Any = None) -> dict:
    if not isinstance(snapshot_id, str) or not snapshot_id.strip():
        return {"ok": False, "reason": "snapshot_id must be a nonempty string"}
    if os.sep in snapshot_id or "/" in snapshot_id or snapshot_id in (".", ".."):
        return {"ok": False, "reason": f"bad snapshot id: {snapshot_id}"}
    manifest = os.path.join(_snapshot_root(snapshot_dir), snapshot_id, "snapshot.json")
    if not os.path.isfile(manifest):
        return {"ok": False, "reason": f"no such snapshot: {snapshot_id}"}
    try:
        with open(manifest, encoding="utf-8") as stream:
            return {"ok": True, "snapshot": json.load(stream)}
    except (OSError, ValueError) as exc:
        return {"ok": False, "reason": f"snapshot unreadable: {type(exc).__name__}: {exc}"}


def _expected_after(data: Mapping[str, Any]) -> str:
    """回退的判据：**现在磁盘上应该是哪一份**。

    记了 ``becomes`` 就用它——那是我们写完时的那一份，也是唯一能回答「我有没有盖掉
    别人后来的改动」的东西。没记（独立快照，我们什么都没写过）就拿快照自己存的原文比。

    两边都没有，也就是「文件当时不存在、我们也没写过」：那种快照的意思只是「把它删掉」，
    没有可比的一份。拿空文件的哈希去比会让每一次回退都被拒——所以给空串，让调用方
    跳过比较。

    判据只留这一处：列表说「退得回去」而按钮拒绝，比不显示状态更糟。
    """
    after = str(data.get("sha256_after") or "")
    if after:
        return after
    if data.get("existed"):
        return str(data.get("sha256") or "")
    return ""


def _rollback_check(data: Mapping[str, Any]) -> dict:
    """只判断「现在退回去会怎样」，不写、不审计。

    列表要在用户按下去**之前**说清这一行退不退得动，而那个判断必须和 ``rollback``
    一模一样——所以两边共用这一个函数。它不写审计日志：看一眼不算一次尝试。
    """
    target = str(data.get("path") or "")
    if not target:
        return {"ok": False, "reason": "snapshot has no path", "exists_now": False}
    exists = os.path.isfile(target)
    current = ""
    if exists:
        current, error = _read_existing(target)
        if error:
            # 回退要拿磁盘上现在这一份去比哈希。比不了就说比不了——目标变成二进制
            # 不是崩溃的理由，和 apply_file 那边同一条规矩。
            return {"ok": False, "reason": error, "exists_now": True}
    current_hash = _sha256(current) if exists else ""
    expected = _expected_after(data)
    # 比的是「磁盘上这份 == 我们留下的那份」，**不看文件当初存不存在**：新建的文件被
    # 后来改过，退回去就是把它连着那些改动一起删掉，那和覆盖别人写的内容是同一类损失。
    #
    # 只有一种情况不比：快照的意思是「把它删掉」，而它**已经不在了**——那是一次空操作，
    # 不是冲突。拒绝它会让「我已经退过了」看起来像「这份退不动了」。
    mismatch = bool(expected) and current_hash != expected
    if mismatch and (exists or data.get("existed")):
        return {
            "ok": False,
            "reason": "changed_since_snapshot",
            "sha256_now": current_hash,
            "sha256_expected": expected,
            "exists_now": exists,
        }
    return {"ok": True, "sha256_now": current_hash, "exists_now": exists}


def _stamp_from_id(snapshot_id: str) -> str:
    """从快照目录名里把时间抠出来。

    读不出来的那一份也得有个时间：不然它会沉到列表最底下，而「刚存坏的那一份」正是
    用户此刻最该看见的一行。名字是 ``take_snapshot`` 自己按这个格式起的，所以这里
    是读自己的格式，不是猜；抠不出来就给空串。
    """
    head = str(snapshot_id).split("-", 1)[0]
    try:
        parsed = time.strptime(head, "%Y%m%dT%H%M%S")
    except ValueError:
        return ""
    return time.strftime("%Y-%m-%dT%H:%M:%S", parsed)


def list_snapshots(*, snapshot_dir: Any = None) -> list[dict]:
    """按时间倒序列出快照，每一行都带上「现在退得回去吗」。

    读不出来的那几份**照样列出来**（``ok: False`` + 理由），不跳过：一个坏掉的快照
    目录和「根本没有快照」在界面上长得一模一样，而这两种情况该做的事完全不同。

    ``rollback`` 那个判断和这里共用 ``_rollback_check``。它只读：列一遍不写审计日志。
    """
    directory = _snapshot_root(snapshot_dir)
    if not os.path.isdir(directory):
        return []
    out: list[dict] = []
    for name in os.listdir(directory):
        loaded = _load_snapshot(name, snapshot_dir=snapshot_dir)
        if not loaded.get("ok"):
            reason = str(loaded.get("reason") or "")
            out.append({
                "ok": False,
                "snapshot_id": name,
                "created_at": _stamp_from_id(name),
                "path": "",
                "existed": False,
                "sha256": "",
                "reason": reason,
                "rollback": {"ok": False, "reason": reason, "exists_now": False},
            })
            continue
        data = loaded["snapshot"]
        out.append({
            "ok": True,
            "snapshot_id": data.get("snapshot_id") or name,
            "created_at": data.get("created_at", ""),
            "path": data.get("path", ""),
            "existed": bool(data.get("existed")),
            "sha256": data.get("sha256", ""),
            "reason": "",
            "rollback": _rollback_check(data),
        })
    # 同一秒里存下的两份谁在前没有意义，但顺序必须**定得下来**（同一个 id 排两次，
    # 列表跟着抖动会让人以为哪份变了）——所以第二关键字用 id。
    out.sort(key=lambda item: (item.get("created_at", ""), item.get("snapshot_id", "")), reverse=True)
    return out


def rollback(
    snapshot_id: Any,
    *,
    snapshot_dir: Any = None,
    force: bool = False,
) -> dict:
    """按快照把文件退回原样。

    默认会先看磁盘上那份是不是还等于打完补丁的那份：**不等于就拒绝**。回退盖掉的是
    别人后来的改动，而那种损失和「补丁落在没人看过的内容上」是同一类事。确实要盖，
    显式 ``force=True``。

    ``force`` 是把那次判断整个免掉，不是「判断失败也要退」：判断只是拒绝的依据，用户
    已经说了「盖掉它」，那就连读都不用读——目标现在是二进制也照样退得回去。
    """
    loaded = _load_snapshot(snapshot_id, snapshot_dir=snapshot_dir)
    if not loaded.get("ok"):
        return {"restored": False, "reason": loaded.get("reason")}
    data = loaded["snapshot"]
    target = data.get("path") or ""
    if not target:
        return {"restored": False, "reason": "snapshot has no path"}

    if not force:
        check = _rollback_check(data)
        if not check.get("ok"):
            audit_record("rollback_rejected", path=target, reason=check.get("reason"))
            refusal = {key: value for key, value in check.items() if key != "ok"}
            return {"restored": False, "path": target, **refusal}

    try:
        if data.get("existed"):
            os.makedirs(os.path.dirname(os.path.abspath(target)) or ".", exist_ok=True)
            with open(target, "w", encoding="utf-8", newline="") as stream:
                stream.write(data.get("content", ""))
        elif os.path.isfile(target):
            os.unlink(target)
    except OSError as exc:
        audit_record("rollback_rejected", path=target, reason=f"write_failed:{type(exc).__name__}")
        return {"restored": False, "path": target, "reason": f"write_failed:{type(exc).__name__}"}

    audit_record("rollback_applied", path=target, snapshot=snapshot_id)
    return {
        "restored": True,
        "path": target,
        "snapshot_id": snapshot_id,
        "existed": bool(data.get("existed")),
        "sha256_after": data.get("sha256", ""),
    }


def _read(path: Any) -> str:
    with open(path, "r", encoding="utf-8", newline="") as stream:
        return stream.read()


def _read_existing(path: str) -> tuple[str, str]:
    """读出当前内容，读不了就说清是哪一种读不了。

    值得单列的是**不是 UTF-8 的文件**：它正是「没人看过内容的那一份」，而
    ``open`` 为它抛的是 ``UnicodeDecodeError``（``ValueError`` 的子孙）而不是
    ``OSError``——只接 ``OSError`` 会让「拒绝覆盖」变成一次 500。
    """
    try:
        return _read(path), ""
    except (OSError, UnicodeDecodeError) as exc:
        return "", f"read_failed:{type(exc).__name__}"


def fingerprint(path: Any) -> dict:
    """The version stamp a caller must present when patching this file."""
    content = _read(path)
    return {
        "path": str(path),
        "sha256": _sha256(content),
        "bytes": len(content.encode("utf-8")),
        "lines": content.count("\n") + (1 if content and not content.endswith("\n") else 0),
    }


def _validate_hunk(hunk: Any, index: int) -> tuple[str, str]:
    if not isinstance(hunk, Mapping):
        raise ValueError(f"hunk {index} must be a mapping with 'find' and 'replace'")
    find = hunk.get("find")
    replace = hunk.get("replace")
    if not isinstance(find, str) or not find:
        raise ValueError(f"hunk {index}: 'find' must be a nonempty string")
    if not isinstance(replace, str):
        raise ValueError(f"hunk {index}: 'replace' must be a string")
    if find == replace:
        raise ValueError(f"hunk {index}: 'find' and 'replace' are identical")
    return find, replace


def _failure(path: Any, reason: str, **extra: Any) -> dict:
    result = {"applied": False, "path": str(path), "reason": reason}
    result.update(extra)
    return result


def apply_patch(
    path: Any,
    hunks: Sequence[Mapping[str, Any]],
    *,
    expected_sha256: str | None = None,
    dry_run: bool = False,
    snapshot_dir: Any = None,
) -> dict:
    """Apply ``hunks`` to ``path`` atomically, or change nothing.

    ``expected_sha256`` is required unless ``dry_run`` is set: applying a patch
    without a version stamp means editing content the caller never read.

    ``snapshot_dir`` turns on undo. When given, the original bytes are stored
    before the write and the result carries the ``snapshot_id``; a snapshot that
    cannot be taken stops the patch instead of being skipped.
    """
    if not isinstance(hunks, Sequence) or isinstance(hunks, (str, bytes)) or not hunks:
        return _failure(path, "no_hunks")
    if expected_sha256 is not None and not isinstance(expected_sha256, str):
        raise TypeError("expected_sha256 must be a string or None")
    if expected_sha256 is None and not dry_run:
        return _failure(path, "missing_version_stamp")

    try:
        original = _read(path)
    except FileNotFoundError:
        return _failure(path, "path_not_found")
    except OSError as exc:
        return _failure(path, f"read_failed:{type(exc).__name__}")

    before = _sha256(original)
    if expected_sha256 is not None and expected_sha256 != before:
        audit_record("patch_rejected", path=str(path), reason="version_mismatch")
        return _failure(
            path, "version_mismatch", sha256_before=before, expected_sha256=expected_sha256
        )

    # Validate every hunk against the current text before touching anything.
    working = original
    for index, hunk in enumerate(hunks):
        try:
            find, replace = _validate_hunk(hunk, index)
        except ValueError as exc:
            return _failure(path, str(exc))
        occurrences = working.count(find)
        if occurrences == 0:
            audit_record("patch_rejected", path=str(path), reason="context_not_found", hunk=index)
            return _failure(path, "context_not_found", hunk=index, sha256_before=before)
        if occurrences > 1:
            audit_record("patch_rejected", path=str(path), reason="context_ambiguous", hunk=index)
            return _failure(
                path, "context_ambiguous", hunk=index, occurrences=occurrences,
                sha256_before=before,
            )
        working = working.replace(find, replace, 1)

    after = _sha256(working)
    result = {
        "applied": True,
        "path": str(path),
        "dry_run": bool(dry_run),
        "sha256_before": before,
        "sha256_after": after,
        "hunks_applied": len(hunks),
        "bytes_before": len(original.encode("utf-8")),
        "bytes_after": len(working.encode("utf-8")),
        "snapshot_id": "",
        "undoable": False,
    }
    if dry_run or working == original:
        return result

    # 快照在落笔之前。存不下来就一个字都不写——能改不能退的补丁，和没有版本戳的
    # 补丁是同一类东西：都在拿一个没人能核对的承诺去动别人的文件。
    if snapshot_dir is not None:
        snapshot = take_snapshot(path, snapshot_dir=snapshot_dir, becomes=after)
        if not snapshot.get("ok"):
            audit_record("patch_rejected", path=str(path), reason="snapshot_failed")
            return _failure(path, f"snapshot_failed:{snapshot.get('reason')}", sha256_before=before)
        result["snapshot_id"] = snapshot["snapshot_id"]
        result["undoable"] = True

    directory = os.path.dirname(os.path.abspath(str(path))) or "."
    handle = None
    try:
        # Write beside the target so the rename stays on one filesystem.
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", newline="", dir=directory, delete=False
        ) as stream:
            handle = stream.name
            stream.write(working)
        os.replace(handle, str(path))
        handle = None
    except OSError as exc:
        if handle is not None:
            try:
                os.unlink(handle)
            except OSError:
                pass
        audit_record("patch_rejected", path=str(path), reason=f"write_failed:{type(exc).__name__}")
        return _failure(path, f"write_failed:{type(exc).__name__}", sha256_before=before)

    audit_record(
        "patch_applied",
        path=str(path),
        hunks=len(hunks),
        sha256_before=before,
        sha256_after=after,
        snapshot=result["snapshot_id"],
    )
    return result


PREVIEW_MAX_LINES = 400


def preview_file(path: Any, content: Any) -> dict:
    """这份内容落到这个文件，会变成什么样。只读、不写、不审计。

    ``apply_file(dry_run=True)`` 只报字节数是**不够**的：用户点下去之前要知道
    哪些行会没了。整文件重写最危险的地方就是「我没看过的那些行一起被换掉」，
    而「会写 1924 字节」这句话对这件事一个字都没说。

    返回形状和 ``_diff_orders`` 一致（``identical``/``added``/``removed``/
    ``diff``），界面才能用同一段渲染。差异超过 ``PREVIEW_MAX_LINES`` 只截前
    这么多行，但 ``added``/``removed`` 仍是完整统计——截断的是给人看的正文，
    不是数字。
    """
    if not isinstance(content, str):
        return {"ok": False, "path": str(path), "reason": "content_must_be_a_string"}
    target = str(path)
    exists = os.path.isfile(target)
    current = ""
    if exists:
        current, error = _read_existing(target)
        if error:
            return {"ok": False, "path": target, "reason": error}
    before = current.splitlines()
    after = content.splitlines()
    diff = list(difflib.unified_diff(
        before,
        after,
        fromfile=f"a/{os.path.basename(target)}" if exists else "/dev/null",
        tofile=f"b/{os.path.basename(target)}",
        lineterm="",
    ))
    added = sum(1 for line in diff if line.startswith("+") and not line.startswith("+++"))
    removed = sum(1 for line in diff if line.startswith("-") and not line.startswith("---"))
    identical = current == content
    return {
        "ok": True,
        "path": target,
        "exists": exists,
        "created": not exists,
        "identical": identical,
        # 逐行一样、差异为空，但两份内容并不相等。不单说一句的话，界面会显示
        # 「会改 0 行」，看着像坏了。
        "newline_only": (not identical) and before == after,
        # 再分一层：是「只差结尾的换行」，还是「每一行的行尾都不一样」（CRLF 的
        # 文件遇上 LF 的正文）。这两件事写下去的结果不同，说成同一句就是把其中
        # 一件说错了——而差异是空的，用户没有任何别的线索能看出来。
        "trailing_newline_only": (not identical) and current.rstrip("\r\n") == content.rstrip("\r\n"),
        "added": added,
        "removed": removed,
        "diff": diff[:PREVIEW_MAX_LINES],
        "truncated": len(diff) > PREVIEW_MAX_LINES,
        "lines_before": len(before),
        "lines_after": len(after),
        "bytes_before": len(current.encode("utf-8")) if exists else 0,
        "bytes_after": len(content.encode("utf-8")),
        "sha256_before": _sha256(current) if exists else "",
        "sha256_after": _sha256(content),
    }


def apply_file(
    path: Any,
    content: Any,
    *,
    expected_sha256: str | None = None,
    dry_run: bool = False,
    snapshot_dir: Any = None,
) -> dict:
    """把一个完整的文件写过去。给「把生成物落到用户选的文件」用。

    和 ``apply_patch`` 的取舍不同：这里没有「文件其余部分」可言，因为要写的是一整份
    新内容。但有一条同样的规矩——**覆盖一个已经存在、而调用方没看过内容的文件是不
    允许的**。那正是整文件重写最危险的地方：它把「我没看过的那些行」一起换掉了。
    所以文件存在时必须带 ``expected_sha256``。

    新建文件不需要版本戳：没有旧内容可以被弄丢。
    """
    if not isinstance(content, str):
        return {"applied": False, "path": str(path), "reason": "content_must_be_a_string"}
    target = str(path)
    exists = os.path.isfile(target)
    current = ""
    if exists:
        current, error = _read_existing(target)
        if error:
            return {"applied": False, "path": target, "reason": error}
    before = _sha256(current) if exists else ""
    after = _sha256(content)

    if exists and expected_sha256 is None and not dry_run:
        return {
            "applied": False,
            "path": target,
            "reason": "missing_version_stamp",
            "detail": "文件已存在，覆盖它需要带上你读到的 sha256",
        }
    if expected_sha256 is not None and expected_sha256 != before:
        audit_record("patch_rejected", path=target, reason="version_mismatch")
        return {
            "applied": False,
            "path": target,
            "reason": "version_mismatch",
            "sha256_before": before,
            "expected_sha256": expected_sha256,
        }

    result = {
        "applied": True,
        "path": target,
        "dry_run": bool(dry_run),
        "created": not exists,
        "sha256_before": before,
        "sha256_after": after,
        "bytes_after": len(content.encode("utf-8")),
        "snapshot_id": "",
        "undoable": False,
    }
    if dry_run or (exists and current == content):
        return result

    if snapshot_dir is not None:
        snapshot = take_snapshot(target, snapshot_dir=snapshot_dir, becomes=after)
        if not snapshot.get("ok"):
            audit_record("patch_rejected", path=target, reason="snapshot_failed")
            return {
                "applied": False,
                "path": target,
                "reason": f"snapshot_failed:{snapshot.get('reason')}",
                "sha256_before": before,
            }
        result["snapshot_id"] = snapshot["snapshot_id"]
        result["undoable"] = True

    directory = os.path.dirname(os.path.abspath(target)) or "."
    handle = None
    try:
        # 目标可能在一个还不存在的目录里——「把生成物放到 src/utils/x.py」正是常见
        # 用法，而临时文件必须和目标同盘才能 rename。少了这一句，新建目录下的第一次
        # 应用就会以 write_failed:FileNotFoundError 收场。
        os.makedirs(directory, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", newline="", dir=directory, delete=False
        ) as stream:
            handle = stream.name
            stream.write(content)
        os.replace(handle, target)
        handle = None
    except OSError as exc:
        if handle is not None:
            try:
                os.unlink(handle)
            except OSError:
                pass
        audit_record("patch_rejected", path=target, reason=f"write_failed:{type(exc).__name__}")
        return {
            "applied": False,
            "path": target,
            "reason": f"write_failed:{type(exc).__name__}",
            "sha256_before": before,
        }

    audit_record(
        "file_applied",
        path=target,
        created=not exists,
        sha256_before=before,
        sha256_after=after,
        snapshot=result["snapshot_id"],
    )
    return result


def main(argv: list[str] | None = None) -> int:
    """CLI: apply a patch file (``[{"find": ..., "replace": ...}, ...]``)."""
    import argparse

    parser = argparse.ArgumentParser(description="按上下文安全修改文件")
    parser.add_argument("path", nargs="?", help="要修改的文件")
    parser.add_argument("--patch", default=None, help="补丁 JSON：hunk 数组")
    parser.add_argument("--expect-sha256", default=None, help="读取时的 sha256")
    parser.add_argument("--fingerprint", action="store_true", help="只打印版本戳并退出")
    parser.add_argument("--dry-run", action="store_true", help="只校验，不写入")
    parser.add_argument("--audit-log", default=None, help="审计日志路径")
    parser.add_argument("--snapshot-dir", default=None, help="快照目录（给了就能回退）")
    parser.add_argument("--list-snapshots", action="store_true", help="列出快照并退出")
    parser.add_argument("--rollback", default=None, help="按快照 id 退回原样")
    parser.add_argument("--force", action="store_true", help="回退时盖掉后来的改动")
    args = parser.parse_args(argv)

    if args.audit_log:
        from runtime.audit import AuditLog, set_default

        set_default(AuditLog(args.audit_log))

    if args.fingerprint:
        try:
            print(json.dumps(fingerprint(args.path), ensure_ascii=False))
            return 0
        except OSError as exc:
            print(f"无法读取：{exc}", file=sys.stderr)
            return 2

    if args.list_snapshots:
        print(json.dumps(list_snapshots(snapshot_dir=args.snapshot_dir), ensure_ascii=False))
        return 0

    if args.rollback:
        result = rollback(args.rollback, snapshot_dir=args.snapshot_dir, force=args.force)
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result["restored"] else 1

    if not args.patch:
        parser.error("--patch is required unless --fingerprint/--list-snapshots/--rollback is given")

    try:
        hunks = json.loads(open(args.patch, encoding="utf-8").read())
    except (OSError, ValueError) as exc:
        print(f"补丁无法解析：{exc}", file=sys.stderr)
        return 2

    result = apply_patch(
        args.path,
        hunks,
        expected_sha256=args.expect_sha256,
        dry_run=args.dry_run,
        snapshot_dir=args.snapshot_dir,
    )
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["applied"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
