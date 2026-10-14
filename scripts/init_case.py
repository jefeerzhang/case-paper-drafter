"""Create a case workspace without overwriting existing analysis or sources."""
import argparse
import json
import sys
from pathlib import Path
from index_materials import (CORE_NAMES, FACT_CSV, FACT_FIELDS, MATERIAL_CSV,
                             MATERIAL_FIELDS, REGISTRY, SCHEMA_VERSION, TOOLS_VERSION,
                             hash_file, load_json, locator, now, save_csv, save_json)

PLACEHOLDER = "<!-- CASE_TOOLS_PLACEHOLDER -->"


def resolve_core_dir(core_dir=None):
    """Use an explicit directory, or discover a complete supported pack layout."""
    if core_dir is not None:
        candidate = core_dir.resolve()
        missing = [name for name in CORE_NAMES if not (candidate / name).is_file()]
        if missing:
            raise ValueError("指定的五核心目录不完整：" + str(candidate) +
                             "；缺少：" + "、".join(missing))
        return candidate
    pack_root = Path(__file__).resolve().parent.parent
    for candidate in (pack_root / "references", pack_root):
        if all((candidate / name).is_file() for name in CORE_NAMES):
            return candidate
    raise ValueError("未找到完整的五核心目录；已检查 " + str(pack_root / "references") +
                     " 和 " + str(pack_root) +
                     "。请恢复核心文件，或用 --core-dir 指定五核心所在目录。")


def stage_files(organization):
    return {0: ["00-任务范围.md"], 1: ["01-材料与事实表.md"],
            2: ["02-案例时间线.md" if organization == "process" else "02-案例边界与状态表.md"],
            3: ["03-研究问题候选.md"], 4: ["04-理论与分析框架.md"],
            5: ["05-研究设计与材料条件.md"], 6: ["06-证据与解释矩阵.md"],
            7: ["07-论文详细提纲.md"], 8: ["08-分节草稿"],
            9: ["09-复核记录.md", "论文草稿.md"]}


def initialize(case_dir, core_dir, name=None, organization=None, version=None):
    case_dir, core_dir = case_dir.resolve(), core_dir.resolve()
    if case_dir == core_dir:
        raise ValueError("成果目录必须与核心模板目录不同")
    cores = []
    for filename in CORE_NAMES:
        p = core_dir / filename
        digest, _ = hash_file(p)
        cores.append({"name": filename, "path": locator(p, case_dir), "sha256": digest})
    config_path = case_dir / "案例配置.json"
    if config_path.exists():
        config = load_json(config_path)
        if organization and organization != config.get("organization"):
            raise ValueError("组织方式与现有配置不同；保留已有成果并在配置与执行记录中登记变更")
        if version and version != config.get("template_version"):
            raise ValueError("模板版本与已有配置不同；请核查并登记版本迁移")
        if name and name != config.get("case_name"):
            raise ValueError("案例名称与已有配置不同")
        old_hashes = {c["name"]: c["sha256"] for c in config.get("cores", [])}
        if old_hashes != {c["name"]: c["sha256"] for c in cores}:
            raise ValueError("核心文件与已有版本绑定不同；不会自动改写配置或分析成果")
        organization = config["organization"]
    else:
        organization = organization or "state"
        config = {"schema_version": SCHEMA_VERSION, "tools_version": TOOLS_VERSION,
                  "case_name": name or case_dir.name, "template_version": version or "V3",
                  "organization": organization, "created_at_utc": now(), "cores": cores}
    if organization not in ("state", "process"):
        raise ValueError("organization 必须为 state 或 process")
    case_dir.mkdir(parents=True, exist_ok=True)
    created, kept = [], []
    def json_if_missing(filename, obj):
        path = case_dir / filename
        if path.exists():
            kept.append(filename)
        else:
            save_json(path, obj)
            created.append(filename)
    json_if_missing("案例配置.json", config)
    json_if_missing("阶段登记.json", {"schema_version": SCHEMA_VERSION,
        "stages": {str(i): {"status": "not_started", "note": ""} for i in range(10)}})
    json_if_missing(REGISTRY, {"schema_version": SCHEMA_VERSION, "material_root": None,
                              "next_source_number": 1, "sources": [], "duplicate_groups": []})
    for filename, fields in [(FACT_CSV, FACT_FIELDS), (MATERIAL_CSV, MATERIAL_FIELDS)]:
        path = case_dir / filename
        if path.exists():
            kept.append(filename)
        else:
            save_csv(path, fields, [])
            created.append(filename)
    for filenames in stage_files(organization).values():
        for filename in filenames:
            path = case_dir / filename
            if path.exists():
                kept.append(filename)
            elif path.suffix == ".md":
                text = ("# " + path.stem + "\n\n> 尚未完成：本文件为初始化骨架。\n\n"
                        "## 当前成果\n\n" + PLACEHOLDER + "\n\n"
                        "## 缺口与下一步\n\n" + PLACEHOLDER + "\n")
                with path.open("x", encoding="utf-8") as handle:
                    handle.write(text)
                created.append(filename)
            else:
                path.mkdir()
                created.append(filename + "/")
    log = case_dir / "执行记录.md"
    if log.exists():
        kept.append(log.name)
    else:
        with log.open("x", encoding="utf-8") as handle:
            handle.write("# 执行记录\n\n模板版本：" + config["template_version"] +
                         "\n\n案例：" + config["case_name"] +
                         "\n\n当前动作：创建目录与骨架；研究分析未开始。\n\n"
                         "五核心位置及文件哈希见 `案例配置.json`。材料位置及版本见 `材料登记.json`。\n\n"
                         "## 每次执行记录\n\n记录已确定问题、设计、理论任务、成果版本、实际分析、缺口、下一步。\n")
        created.append(log.name)
    return {"status": "initialized", "case_dir": str(case_dir), "created": created,
            "preserved": kept, "research_completed": False}


def main():
    parser = argparse.ArgumentParser(description="创建新案例目录和阶段骨架；现有文件保持原内容。")
    parser.add_argument("--case-dir", type=Path, required=True)
    parser.add_argument("--core-dir", type=Path,
                        help="五核心所在目录；省略时先查 Skill 的 references/，再查独立执行包根目录")
    parser.add_argument("--name", help="案例名称；首次默认采用目录名")
    parser.add_argument("--organization", choices=["state", "process"],
                        help="首次默认 state；过程任务使用 process")
    parser.add_argument("--template-version", help="首次默认 V3；配置声明与文件哈希分别登记")
    args = parser.parse_args()
    try:
        result = initialize(args.case_dir, resolve_core_dir(args.core_dir), args.name,
                            args.organization, args.template_version)
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(json.dumps({"status": "error", "message": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
