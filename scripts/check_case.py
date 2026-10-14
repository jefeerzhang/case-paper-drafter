"""Check file bindings, IDs and declared stages; never certify research claims."""
import argparse
import csv
import json
import re
import sys
from pathlib import Path
from urllib.parse import unquote
from init_case import PLACEHOLDER, stage_files
from index_materials import (CORE_NAMES, FACT_CSV, FACT_FIELDS, REGISTRY, hash_file,
                             load_json, now, resolve_locator, save_json, scan_files,
                             source_errors, within)

MATERIAL_STATES = {"目标", "设计", "观察", "陈述", "规范", "协商参数", "条件预测", "建议", "待确认"}
ANALYSIS_STATES = {"支持", "待检验", "材料不足"}
STAGE_STATES = {"not_started", "in_progress", "complete", "blocked"}
IDS = re.compile(r"(?<![A-Za-z0-9_])([SE]\d{4,})(?![A-Za-z0-9_])")


def check(case_dir, stage=None, core_dir=None):
    case_dir = case_dir.resolve()
    errors, warnings = [], []
    def issue(code, message, location="", warning=False):
        (warnings if warning else errors).append({"code": code, "message": message, "location": location})
    config = load_json(case_dir / "案例配置.json")
    registry = load_json(case_dir / REGISTRY)
    stages = load_json(case_dir / "阶段登记.json").get("stages", {})
    organization = config.get("organization")
    if organization not in ("state", "process"):
        raise ValueError("案例组织方式无效")
    if not isinstance(stages, dict):
        raise ValueError("阶段登记的 stages 必须是对象")
    for code in source_errors(registry.get("sources")):
        issue("registry_invalid", code, REGISTRY)
    sources = {s["source_id"]: s for s in registry.get("sources", []) if isinstance(s, dict)
               and isinstance(s.get("source_id"), str)}
    cores = config.get("cores", [])
    if not isinstance(cores, list) or any(not isinstance(c, dict) for c in cores):
        raise ValueError("五核心登记必须是对象列表")
    if len(cores) != 5 or {c.get("name") for c in cores} != set(CORE_NAMES):
        issue("cores_incomplete", "五核心文件登记不完整", "案例配置.json")
    checked_files = 0
    for c in cores:
        try:
            if c.get("name") not in CORE_NAMES:
                continue
            p = (core_dir.resolve() / c["name"] if core_dir else
                 resolve_locator(c["path"], case_dir))
            digest, _ = hash_file(p)
            checked_files += 1
            if digest != c.get("sha256"):
                issue("core_changed", "核心文件与已登记版本不同", str(p))
        except (OSError, ValueError, KeyError, TypeError) as error:
            issue("core_unreadable", str(error), str(c.get("name")))
    root_value = registry.get("material_root")
    root = resolve_locator(root_value, case_dir) if root_value else None
    source_paths = {}
    if sources and root is None:
        issue("material_root_missing", "已有来源但没有材料目录", REGISTRY)
    if root is not None:
        try:
            if not root.is_dir() or root == case_dir:
                raise ValueError("材料目录不存在或与成果目录相同")
            actual, skipped = scan_files(root, case_dir)
            known = {s.get("relative_path") for s in sources.values() if s.get("status") == "present"}
            new_paths = sorted(p.relative_to(root).as_posix() for p in actual
                               if p.relative_to(root).as_posix() not in known)
            if new_paths:
                issue("materials_not_indexed", "材料尚未登记，请重新运行登记脚本：" + ", ".join(new_paths), REGISTRY)
            if skipped:
                issue("material_paths_skipped", "扫描按规则跳过：" + ", ".join(skipped), warning=True)
        except (OSError, ValueError, TypeError) as error:
            issue("material_scan_unreadable", str(error), REGISTRY)
    for sid, source in sources.items():
        try:
            rel = source.get("relative_path", "")
            if root is None or not rel or Path(rel).is_absolute() or ".." in Path(rel).parts:
                continue  # Already reported by registry validation.
            raw = root / rel
            p = raw.resolve()
            if not within(p, root) or raw.is_symlink():
                issue("source_path_unsafe", "来源路径离开材料目录或成为符号链接", sid)
                continue
            source_paths[sid] = p
            if source.get("status") == "missing":
                issue("source_marked_missing", "登记保留此旧编号，当前材料缺失", sid, warning=True)
                if p.exists():
                    issue("source_reappeared", "材料重新出现，请重新登记", sid)
                continue
            digest, _ = hash_file(p)
            checked_files += 1
            if digest != source.get("sha256"):
                issue("source_changed", "材料字节变化；重新登记并复核旧证据", sid)
        except (OSError, ValueError, TypeError) as error:
            issue("source_unreadable", str(error), sid)
    if not sources:
        issue("no_sources", "尚无材料登记；此检查不表示材料充分", warning=True)
    for group in registry.get("duplicate_groups", []):
        issue("same_bytes", "字节重复不等于来源独立性判断：" + ", ".join(group["source_ids"]), warning=True)
    fact_path = case_dir / FACT_CSV
    with fact_path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames != FACT_FIELDS:
            issue("fact_header", "证据表列名或顺序与格式不同", FACT_CSV)
        rows = []
        previous_line = reader.line_num
        for row in reader:
            first_line = previous_line + 1
            previous_line = reader.line_num
            if any(isinstance(v, str) and v.strip() for v in row.values()):
                rows.append((first_line, row))
    evidence = {}
    for line, row in rows:
        loc = FACT_CSV + ":" + str(line)
        if None in row or any(not isinstance(row.get(k), str) or not row[k].strip() for k in FACT_FIELDS):
            issue("evidence_required_fields", "存在缺项或额外字段", loc)
        eid = (row.get("evidence_id") or "").strip()
        sid = (row.get("source_id") or "").strip()
        if not re.fullmatch(r"E\d{4,}", eid):
            issue("evidence_id_invalid", "证据编号格式为 E 加至少四位数字", loc)
        if eid in evidence:
            issue("evidence_id_duplicate", "证据编号重复：" + eid, loc)
        evidence[eid] = row
        source = sources.get(sid)
        if source is None:
            issue("evidence_source_unknown", "来源编号未登记：" + sid, loc)
        else:
            if source.get("status") != "present":
                issue("evidence_source_missing", "证据引用当前缺失的材料：" + sid, loc)
            if row.get("source_version") != str(source.get("version")) or row.get("source_sha256") != source.get("sha256"):
                issue("evidence_version_stale", "证据绑定的材料版本或哈希不同：" + sid, loc)
            position = (row.get("locator") or "").strip()
            match = re.fullmatch(r"L(\d+)(?:[-–—]L?(\d+))?", position)
            p = source_paths.get(sid)
            if match and p is not None and p.exists() and source.get("status") == "present":
                if p.suffix.lower() in (".txt", ".md", ".csv", ".json", ".jsonl"):
                    try:
                        length = len(p.read_text(encoding="utf-8-sig").splitlines())
                        start, end = int(match[1]), int(match[2] or match[1])
                        if not 1 <= start <= end <= length:
                            issue("locator_line_bounds", "行定位超出材料实际范围", loc)
                    except (OSError, UnicodeError) as error:
                        issue("locator_unresolved", "无法核对文本行范围：" + str(error), loc, warning=True)
                else:
                    issue("locator_unresolved", "此格式不支持自动文本行核查", loc, warning=True)
            elif position:
                issue("locator_unresolved", "仅检查定位非空；页码/段落/时间戳由 Agent 回读核查", loc, warning=True)
        if row.get("material_status") not in MATERIAL_STATES or row.get("analysis_status") not in ANALYSIS_STATES:
            issue("evidence_state_invalid", "材料身份或分析状态不在约定值内", loc)
        if row.get("material_status") == "待确认" or row.get("analysis_status") != "支持":
            issue("evidence_pending", "本条证据仍保留待确认或待检验范围", loc, warning=True)
    if not rows:
        issue("no_evidence", "尚无结构化证据行", FACT_CSV, warning=True)
    definitions = stage_files(organization)
    texts, refs_by_stage, completed = {}, {}, []
    for i, filenames in definitions.items():
        entry = stages.get(str(i))
        if not isinstance(entry, dict) or entry.get("status") not in STAGE_STATES:
            issue("stage_status_invalid", "阶段缺失或状态无效", str(i))
            status = None
        else:
            status = entry["status"]
        if status == "complete":
            completed.append(i)
        required = status == "complete" or (stage is not None and i <= stage)
        if stage is not None and i <= stage and status != "complete":
            issue("stage_not_completed", "要求核查的阶段尚未登记完成", str(i))
        paths = []
        for filename in filenames:
            p = case_dir / filename
            if i == 8:
                if not p.is_dir():
                    issue("stage_directory_missing", "分节目录不存在", filename)
                else:
                    paths.extend(sorted(p.rglob("*.md")))
                    if required and not paths:
                        issue("stage_drafts_empty", "分节目录没有 Markdown 草稿", filename)
            elif not p.is_file():
                issue("stage_file_missing", "阶段文件不存在", filename)
            else:
                paths.append(p)
        refs_by_stage[i] = set()
        for p in paths:
            text = p.read_text(encoding="utf-8-sig")
            texts[p] = text
            refs_by_stage[i].update(IDS.findall(text))
            body = re.sub(r"<!--.*?-->", "", text, flags=re.S)
            body = "\n".join(l for l in body.splitlines() if l.strip() and not l.lstrip().startswith("#"))
            if required and (PLACEHOLDER in text or "尚未完成：本文件为初始化骨架" in text or not body.strip()):
                issue("stage_placeholder", "阶段仍是空骨架或保留初始化占位符", p.name)
        if required and i == 1 and (not rows or not any(x in sources for x in refs_by_stage[i])):
            issue("stage_facts_unbound", "材料事实表必须有结构化证据及已登记来源编号", str(i))
        if required and i in (6, 7, 8, 9) and not any(x in evidence for x in refs_by_stage[i]):
            issue("stage_evidence_unbound", "此阶段必须引用已登记证据编号", str(i))
    if set(stages) != {str(i) for i in range(10)}:
        issue("stage_keys", "阶段登记必须包含且仅包含 0—9", "阶段登记.json")
    log = case_dir / "执行记录.md"
    if log.is_file():
        texts[log] = log.read_text(encoding="utf-8-sig")
    else:
        issue("execution_log_missing", "执行记录不存在", log.name)
    for p, text in texts.items():
        for identifier in sorted(set(IDS.findall(text))):
            if identifier.startswith("S"):
                if identifier not in sources:
                    issue("source_reference_unknown", "正文引用未登记来源：" + identifier, p.name)
                elif sources[identifier].get("status") != "present":
                    issue("source_reference_missing", "正文引用缺失来源：" + identifier, p.name)
            elif identifier not in evidence:
                issue("evidence_reference_unknown", "正文引用未登记证据：" + identifier, p.name)
        for target in re.findall(r"\[[^\]\n]+\]\((<[^>\n]+>|[^)\n]+)\)", text):
            target = target.strip().strip("<>")
            if target.startswith("#") or re.match(r"^(https?://|mailto:)", target, re.I):
                continue
            if re.match(r"^[a-z][a-z0-9+.-]*://", target, re.I):
                issue("link_unresolved", "此链接协议未自动核查", p.name, warning=True)
                continue
            target = unquote(re.sub(r":\d+$", "", target.split("#")[0]))
            if target and not resolve_locator(target, p.parent).exists():
                issue("local_link_missing", "本地链接目标不存在：" + target, p.name)
    return {"checked_at_utc": now(), "status": "passed" if not errors else "failed",
            "scope": "阶段文件、声明状态、文件字节、编号、必填字段与有限定位核查",
            "semantic_verification": "not_performed", "requested_stage": stage,
            "declared_complete_stages": completed,
            "counts": {"registered_sources": len(sources), "evidence_rows": len(rows),
                       "files_hashed": checked_files, "markdown_files": len(texts)},
            "errors": errors, "warnings": warnings}


def main():
    parser = argparse.ArgumentParser(description="检查结构与来源版本；不判断理论、因果或研究质量。")
    parser.add_argument("--case-dir", required=True, type=Path)
    parser.add_argument("--stage", type=int, choices=range(10), help="要求第 0—N 步登记完成并核查文件")
    parser.add_argument("--core-dir", type=Path, help="迁移后以此目录定位同一版本五核心")
    parser.add_argument("--write-report", action="store_true", help="另存脚本核查报告.json；不改写人工复核记录")
    args = parser.parse_args()
    try:
        result = check(args.case_dir, args.stage, args.core_dir)
        if args.write_report:
            save_json(args.case_dir.resolve() / "脚本核查报告.json", result)
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(json.dumps({"status": "error", "message": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    sys.exit(main())
