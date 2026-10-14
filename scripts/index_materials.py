"""Register source files and preserve IDs across additions, edits and unique renames.

Standard library only. Shared format/IO helpers also serve the other two CLIs.
Importing this module does not read or write a case.
"""
import argparse
import copy
import csv
import hashlib
import json
import os
import re
import sys
import tempfile
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = 1
TOOLS_VERSION = "1.0.0"
CORE_NAMES = ["新案例执行入口.md", "AI执行流程.md", "案例写作手册.md",
              "段落与理论卡片库.md", "管理世界写法专题.md"]
REGISTRY = "材料登记.json"
MATERIAL_CSV = "材料登记.csv"
FACT_CSV = "事实与证据.csv"
FACT_FIELDS = ["evidence_id", "source_id", "source_version", "source_sha256",
               "locator", "statement", "material_status", "analysis_status"]
META_FIELDS = ["source_actor", "material_type", "material_formed_at", "event_period",
               "original_language", "translation_version"]
MATERIAL_FIELDS = ["source_id", "relative_path", "status", "version", "sha256",
                   "size_bytes", "filesystem_modified_at_utc"] + META_FIELDS


def now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def within(path, root):
    return path == root or root in path.parents


def locator(path, case_dir):
    try:
        return Path(os.path.relpath(path, case_dir)).as_posix()
    except ValueError:  # Different Windows drives require an absolute locator.
        return path.as_posix()


def resolve_locator(value, base):
    p = Path(value)
    return (p if p.is_absolute() else base / p).resolve()


def load_json(path):
    obj = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(obj, dict) or obj.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("不支持的登记格式：" + str(path))
    return obj


def atomic_text(path, writer, encoding="utf-8"):
    """Replace a generated file only after its temporary file is fully written."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding=encoding, newline="",
                                         dir=path.parent, prefix=".case-tools-",
                                         suffix=".tmp", delete=False) as handle:
            temp = Path(handle.name)
            writer(handle)
        os.replace(temp, path)
    finally:
        if temp is not None and temp.exists():
            temp.unlink()


def save_json(path, obj):
    atomic_text(path, lambda h: h.write(json.dumps(obj, ensure_ascii=False, indent=2) + "\n"))


def save_csv(path, fields, rows):
    def write(handle):
        out = csv.DictWriter(handle, fieldnames=fields)
        out.writeheader()
        out.writerows(rows)
    atomic_text(path, write, encoding="utf-8-sig")


def hash_file(path):
    h = hashlib.sha256()
    with path.open("rb") as handle:
        before = os.fstat(handle.fileno())
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(block)
        after = os.fstat(handle.fileno())
    current = path.stat()
    signature = lambda s: (s.st_size, s.st_mtime_ns, s.st_ino)
    if signature(before) != signature(after) or signature(after) != signature(current):
        raise ValueError("登记期间文件发生变化，请重新运行：" + str(path))
    return h.hexdigest(), current


def source_errors(sources):
    if not isinstance(sources, list):
        return ["sources 必须是列表"]
    errors, ids, paths = [], [], []
    for item in sources:
        if not isinstance(item, dict):
            errors.append("材料记录必须是对象")
            continue
        sid = item.get("source_id", "")
        if not isinstance(sid, str) or not re.fullmatch(r"S\d{4,}", sid):
            errors.append("无效来源编号：" + str(sid))
        ids.append(str(sid))
        rel = item.get("relative_path")
        if not isinstance(rel, str) or not rel or Path(rel).is_absolute() or ".." in Path(rel).parts:
            errors.append("来源路径必须位于材料目录内：" + str(rel))
        else:
            paths.append(os.path.normcase(rel))
        if not re.fullmatch(r"[0-9a-f]{64}", str(item.get("sha256", ""))):
            errors.append("无效文件哈希：" + str(sid))
        if type(item.get("version")) is not int or item["version"] < 1:
            errors.append("无效材料版本：" + str(sid))
        if item.get("status") not in ("present", "missing"):
            errors.append("无效材料状态：" + str(sid))
        if not isinstance(item.get("metadata", {}), dict) or not isinstance(item.get("history", []), list):
            errors.append("无效材料元数据或历史：" + str(sid))
    if any(n > 1 for n in Counter(ids).values()):
        errors.append("来源编号重复")
    if any(n > 1 for n in Counter(paths).values()):
        errors.append("当前来源路径重复")
    return errors


def material_rows(sources):
    return [{**{k: s.get(k, "") for k in MATERIAL_FIELDS if k not in META_FIELDS},
             **{k: s.get("metadata", {}).get(k, "") for k in META_FIELDS}} for s in sources]


def scan_files(root, case_dir):
    files, skipped = [], []
    exclude_case = within(case_dir, root)
    def fail(error):
        raise error
    for folder, dirs, names in os.walk(root, followlinks=False, onerror=fail):
        parent = Path(folder)
        keep = []
        for name in sorted(dirs):
            child = parent / name
            if child.is_symlink() or not within(child.resolve(), root) or name in (".git", "__pycache__") or (
                    exclude_case and within(child.resolve(), case_dir)):
                skipped.append(child.relative_to(root).as_posix())
            else:
                keep.append(name)
        dirs[:] = keep
        for name in sorted(names):
            p = parent / name
            if p.is_symlink() or not within(p.resolve(), root) or name in (".DS_Store", "Thumbs.db"):
                skipped.append(p.relative_to(root).as_posix())
            elif p.is_file():
                files.append(p)
    return sorted(files, key=lambda p: p.relative_to(root).as_posix()), skipped


def index_case(case_dir, materials_dir=None, relocate=False):
    case_dir = case_dir.resolve()
    load_json(case_dir / "案例配置.json")
    path = case_dir / REGISTRY
    registry = load_json(path)
    errors = source_errors(registry.get("sources"))
    if errors:
        raise ValueError("；".join(errors))
    old_root = registry.get("material_root")
    root = (materials_dir.resolve() if materials_dir is not None else
            resolve_locator(old_root, case_dir) if old_root else None)
    if root is None or not root.is_dir():
        raise ValueError("首次登记必须提供存在的 --materials-dir 目录")
    if root == case_dir:
        raise ValueError("材料目录与成果目录必须不同")
    if old_root and resolve_locator(old_root, case_dir) != root and not relocate:
        raise ValueError("材料目录发生迁移；确认目录身份后使用 --relocate")
    files, skipped = scan_files(root, case_dir)
    scanned = {}
    for p in files:  # Finish every hash before modifying any registry output.
        digest, stat = hash_file(p)
        scanned[p.relative_to(root).as_posix()] = {
            "sha256": digest, "size_bytes": stat.st_size,
            "filesystem_modified_at_utc": datetime.fromtimestamp(
                stat.st_mtime, timezone.utc).isoformat().replace("+00:00", "Z")}
    sources = copy.deepcopy(registry["sources"])
    by_path = {s["relative_path"]: s for s in sources}
    unmatched_old, unmatched_new = defaultdict(list), defaultdict(list)
    for rel, s in by_path.items():
        if rel not in scanned:
            unmatched_old[s["sha256"]].append(s)
    for rel, data in scanned.items():
        if rel not in by_path:
            unmatched_new[data["sha256"]].append(rel)
    stamp, renamed, changed = now(), [], []
    for digest, rels in unmatched_new.items():
        old = unmatched_old[digest]
        if len(rels) == len(old) == 1:
            s, new_rel = old[0], rels[0]
            before = s["relative_path"]
            by_path.pop(before)
            s.setdefault("path_history", []).append({"from": before, "to": new_rel,
                "at_utc": stamp, "reason": "unique_equal_sha256"})
            s["relative_path"] = new_rel
            by_path[new_rel] = s
            renamed.append(s["source_id"])
    next_number = max([int(s["source_id"][1:]) for s in sources] + [0]) + 1
    recorded_next = registry.get("next_source_number", next_number)
    if type(recorded_next) is not int or recorded_next < next_number:
        raise ValueError("next_source_number 无效或会重复使用旧编号")
    next_number = max(next_number, recorded_next)
    for rel, data in scanned.items():
        s = by_path.get(rel)
        if s is None:
            s = {"source_id": f"S{next_number:04d}", "relative_path": rel,
                 "version": 1, "registered_at_utc": stamp, "history": [],
                 "metadata": {k: "" for k in META_FIELDS}}
            sources.append(s)
            next_number += 1
        elif s["sha256"] != data["sha256"]:
            s.setdefault("history", []).append({k: s.get(k) for k in (
                "version", "sha256", "relative_path", "size_bytes", "last_seen_at_utc")})
            s["version"] += 1
            changed.append(s["source_id"])
        s.update(data)
        s.update(status="present", last_seen_at_utc=stamp)
    for s in sources:
        if s["relative_path"] not in scanned:
            s["status"] = "missing"  # IDs and previous hashes are retained.
    sources.sort(key=lambda s: int(s["source_id"][1:]))
    groups = defaultdict(list)
    for s in sources:
        if s["status"] == "present":
            groups[s["sha256"]].append(s["source_id"])
    if old_root and resolve_locator(old_root, case_dir) != root:
        registry.setdefault("root_history", []).append({"old_locator": old_root, "at_utc": stamp})
    registry.update(material_root=locator(root, case_dir), sources=sources,
                    next_source_number=next_number, indexed_at_utc=stamp, skipped_paths=skipped,
                    duplicate_groups=[{"sha256": h, "source_ids": ids}
                                      for h, ids in groups.items() if len(ids) > 1])
    save_json(path, registry)
    save_csv(case_dir / MATERIAL_CSV, MATERIAL_FIELDS, material_rows(sources))
    return {"status": "indexed", "sources_present": sum(s["status"] == "present" for s in sources),
            "sources_missing": sum(s["status"] == "missing" for s in sources),
            "duplicate_groups": len(registry["duplicate_groups"]), "renamed_ids": renamed,
            "changed_version_ids": changed, "skipped_paths": skipped,
            "registry": str(path), "facts_modified": False}


def main():
    parser = argparse.ArgumentParser(description="登记材料文件；保留来源编号、材料版本和人工元数据。")
    parser.add_argument("--case-dir", type=Path, required=True, help="已初始化的成果目录")
    parser.add_argument("--materials-dir", type=Path, help="首次必填；后续默认读取登记中的位置")
    parser.add_argument("--relocate", action="store_true", help="明确登记材料目录迁移")
    args = parser.parse_args()
    try:
        result = index_case(args.case_dir, args.materials_dir, args.relocate)
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(json.dumps({"status": "error", "message": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
