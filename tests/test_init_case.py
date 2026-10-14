"""Exercise first-run CLI layouts and preserve existing case work."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
CORE_NAMES = ["新案例执行入口.md", "AI执行流程.md", "案例写作手册.md",
              "段落与理论卡片库.md", "管理世界写法专题.md"]


class StartupTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="case-paper-startup-")
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()
        self.pack = self.base / "技能包"
        self.scripts = self.pack / "scripts"
        self.scripts.mkdir(parents=True)
        for name in ("init_case.py", "index_materials.py"):
            shutil.copyfile(ROOT / "scripts" / name, self.scripts / name)
        self.cwd = self.base / "无关工作目录"
        self.cwd.mkdir()
        self.case = self.base / "案例成果"

    def cores(self, directory, label="fixture"):
        directory.mkdir(parents=True, exist_ok=True)
        for name in CORE_NAMES:
            (directory / name).write_text(label + " " + name, encoding="utf-8")
        return directory

    def run_init(self, *extra):
        return subprocess.run(
            [sys.executable, "-B", "-X", "utf8", str(self.scripts / "init_case.py"),
             "--case-dir", str(self.case), *map(str, extra)],
            cwd=self.cwd, capture_output=True, text=True, encoding="utf-8",
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})

    def assert_bound_to(self, directory):
        config = json.loads((self.case / "案例配置.json").read_text(encoding="utf-8"))
        self.assertEqual(len(config["cores"]), 5)
        for core in config["cores"]:
            self.assertEqual((self.case / core["path"]).resolve(),
                             (directory / core["name"]).resolve())
            self.assertEqual(core["sha256"],
                             hashlib.sha256((directory / core["name"]).read_bytes()).hexdigest())

    def snapshot(self):
        return {p.relative_to(self.case).as_posix(): p.read_bytes()
                for p in self.case.rglob("*") if p.is_file()}

    def test_skill_layout_from_unrelated_working_directory(self):
        refs = self.cores(self.pack / "references")
        result = self.run_init()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(json.loads(result.stdout)["research_completed"])
        self.assert_bound_to(refs)

    def test_standalone_pack_root_remains_supported(self):
        self.cores(self.pack)
        result = self.run_init("--organization", "process")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_bound_to(self.pack)
        self.assertTrue((self.case / "02-案例时间线.md").is_file())

    def test_complete_references_precede_complete_root(self):
        refs = self.cores(self.pack / "references", "skill")
        self.cores(self.pack, "legacy")
        result = self.run_init()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_bound_to(refs)

    def test_incomplete_references_can_use_complete_standalone_root(self):
        self.cores(self.pack)
        partial = self.pack / "references"
        partial.mkdir()
        (partial / CORE_NAMES[0]).write_text("incomplete", encoding="utf-8")
        result = self.run_init()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_bound_to(self.pack)

    def test_explicit_relative_directory_overrides_discovery(self):
        self.cores(self.pack / "references")
        explicit = self.cores(self.cwd / "自定义核心", "custom")
        result = self.run_init("--core-dir", "自定义核心")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_bound_to(explicit)

    def test_invalid_explicit_directory_does_not_fall_back(self):
        self.cores(self.pack / "references")
        explicit = self.cores(self.cwd / "不完整核心")
        (explicit / CORE_NAMES[-1]).unlink()
        result = self.run_init("--core-dir", explicit)
        self.assertEqual(result.returncode, 2)
        message = json.loads(result.stderr)["message"]
        self.assertIn(CORE_NAMES[-1], message)
        self.assertIn(str(explicit), message)
        self.assertFalse(self.case.exists())

    def test_missing_default_cores_explain_recovery_without_creating_case(self):
        result = self.run_init()
        self.assertEqual(result.returncode, 2)
        message = json.loads(result.stderr)["message"]
        self.assertIn("--core-dir", message)
        self.assertIn(str(self.pack / "references"), message)
        self.assertIn(str(self.pack), message)
        self.assertFalse(self.case.exists())

    def test_repeat_start_preserves_config_evidence_and_draft(self):
        refs = self.cores(self.pack / "references")
        result = self.run_init("--core-dir", refs)
        self.assertEqual(result.returncode, 0, result.stderr)
        for name in ("01-材料与事实表.md", "论文草稿.md", "执行记录.md", "事实与证据.csv"):
            with (self.case / name).open("a", encoding="utf-8") as handle:
                handle.write("\nexisting user work\n")
        before = self.snapshot()
        result = self.run_init()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["created"], [])
        self.assertEqual(self.snapshot(), before)

    def test_changed_core_hash_rejects_resume_without_overwriting_case(self):
        refs = self.cores(self.pack / "references")
        result = self.run_init("--core-dir", refs)
        self.assertEqual(result.returncode, 0, result.stderr)
        before = self.snapshot()
        (refs / CORE_NAMES[0]).write_text("new template", encoding="utf-8")
        result = self.run_init()
        self.assertEqual(result.returncode, 2)
        self.assertIn("核心文件与已有版本绑定不同", json.loads(result.stderr)["message"])
        self.assertEqual(self.snapshot(), before)


if __name__ == "__main__":
    unittest.main()
