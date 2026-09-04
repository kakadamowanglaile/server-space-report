"""发布包必须可运行，并且不能包含虚拟机、凭据或真实报告。"""
import json
import hashlib
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import shutil
import tempfile
import unittest
from unittest.mock import patch
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "代码"))
from space_report import __version__


class ReleaseTests(unittest.TestCase):
    def builder(self, project):
        spec = importlib.util.spec_from_file_location("release_under_test", project / "工具/构建发布包.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def project_copy(self):
        parent = ROOT / "测试环境/临时"
        parent.mkdir(parents=True, exist_ok=True)
        folder = tempfile.TemporaryDirectory(dir=parent)
        self.addCleanup(folder.cleanup)
        project = Path(folder.name) / "测试工程"
        project.mkdir()
        for name in ("代码", "测试", "工具", "文档"):
            shutil.copytree(ROOT / name, project / name, ignore=shutil.ignore_patterns("__pycache__"))
        for name in ("README.md", "README.zh-CN.md", "CONTRIBUTING.md", "CONTRIBUTING.zh-CN.md",
                     "LICENSE", ".gitignore", "更新记录.md"):
            shutil.copy2(ROOT / name, project / name)
        return project

    def test_runtime_archive_excludes_unlisted_extensionless_private_files(self):
        # 缺少真实文件白名单时，无扩展名的私密文件会进入 pyz。
        project = self.project_copy()
        for name in (".env", "id_rsa", "space_report/秘密"):
            (project / "代码" / name).write_text("人工保密标记-不可发布")
        output = project / "发布"
        result = subprocess.run([sys.executable, "-B", str(project / "工具/构建发布包.py"), "--output", str(output)], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        for path in (*output.glob("*.pyz"), *output.glob("*.zip")):
            with zipfile.ZipFile(path) as archive:
                self.assertFalse(any(b"\xe4\xba\xba\xe5\xb7\xa5\xe4\xbf\x9d\xe5\xaf\x86\xe6\xa0\x87\xe8\xae\xb0" in archive.read(name) for name in archive.namelist()
                                     if not name.endswith("test_release.py")), path.name)
                self.assertFalse(any(Path(name).name in {".env", "id_rsa", "秘密"} for name in archive.namelist()))

    def test_dangling_release_symlink_cannot_create_its_target(self):
        # 即使符号链接目标尚不存在，发布构建也不能沿链接新建文件。
        project = self.project_copy()
        output = project / "发布"
        output.mkdir()
        target = project / "不应创建的文件"
        (output / f"服务器空间去哪了-{__version__}.pyz").symlink_to(target)
        result = subprocess.run([sys.executable, "-B", str(project / "工具/构建发布包.py"), "--output", str(output)], capture_output=True, text=True, timeout=10)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(target.exists())

    def test_distribution_runs_and_source_has_no_private_environment(self):
        script = ROOT / "工具/构建发布包.py"
        self.assertTrue(script.is_file(), "尚未实现可审计的发布包构建")
        parent = ROOT / "测试环境/临时"
        parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=parent) as folder:
            process = subprocess.run([sys.executable, "-B", str(script), "--output", folder],
                                     capture_output=True, text=True, timeout=10)
            self.assertEqual(process.returncode, 0, process.stderr)
            archives = list(Path(folder).glob("*.pyz"))
            self.assertEqual(len(archives), 1)
            result = subprocess.run([sys.executable, "-B", str(archives[0]), "--version"],
                                    capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("服务器空间去哪了", result.stdout)
            invalid = subprocess.run([sys.executable, "-B", str(archives[0]), "--timeout", "0"], capture_output=True, text=True, timeout=5)
            self.assertEqual(invalid.returncode, 2, "单文件入口不能吞掉 main 返回的错误状态")
            with zipfile.ZipFile(next(Path(folder).glob("*源码.zip"))) as archive:
                names = archive.namelist()
                self.assertTrue(any(name.endswith("代码/空间去哪了.py") for name in names))
                self.assertTrue(any(name.endswith("测试/test_cli.py") for name in names))
                self.assertTrue(any(name.endswith("/.gitignore") for name in names))
                self.assertTrue(any(name.endswith("/工具/核对交付包.py") for name in names))
                for document in ("README.md", "README.zh-CN.md", "CONTRIBUTING.md", "CONTRIBUTING.zh-CN.md"):
                    member = f"服务器空间去哪了-{__version__}/{document}"
                    self.assertIn(member, names)
                    self.assertEqual(archive.read(member), (ROOT / document).read_bytes())
                self.assertFalse(any(any(part in name for part in ["测试环境/", "报告/", ".pyc", "__pycache__", "ssh.config", "_config/"]) for name in names))
            manifest = json.loads((Path(folder) / "发布清单.json").read_text())
            self.assertEqual(len(manifest["文件"]), 2)
            self.assertTrue(all(len(item["SHA256"]) == 64 for item in manifest["文件"]))
            for item in manifest["文件"]:
                self.assertEqual(item["SHA256"], hashlib.sha256((Path(folder) / item["名称"]).read_bytes()).hexdigest())

    def test_delivery_check_requires_test_count_before_reading_artifacts(self):
        # 未声明预期测试数量时，应在读取交付包前拒绝核对，不能沿用旧数量。
        parent = ROOT / "测试环境/临时"
        parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=parent) as folder:
            result = subprocess.run(
                [sys.executable, "-B", str(ROOT / "工具/核对交付包.py"),
                 "--release", str(Path(folder) / "release"),
                 "--candidate", str(Path(folder) / "candidate")],
                capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertIn("--test-count", result.stderr)
            self.assertNotIn("Traceback", result.stderr)
            self.assertEqual(list(Path(folder).iterdir()), [])

    def test_missing_license_cannot_create_a_release(self):
        parent = ROOT / "测试环境/临时"
        parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=parent) as folder:
            project = Path(folder) / "缺少文档的工程"
            (project / "工具").mkdir(parents=True)
            shutil.copy2(ROOT / "工具/构建发布包.py", project / "工具/构建发布包.py")
            shutil.copytree(ROOT / "代码", project / "代码")
            output = project / "发布"
            result = subprocess.run([sys.executable, "-B", str(project / "工具/构建发布包.py"), "--output", str(output)], capture_output=True, text=True, timeout=5)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(list(output.glob("*.pyz")))

    def test_replaced_output_parent_never_redirects_archive_writes(self):
        project = self.project_copy()
        output = project / "发布"
        output.mkdir()
        outside = project / "外部资料样本"
        outside.mkdir()
        module = self.builder(project)
        original_init = zipfile.ZipFile.__init__
        replaced = False
        def replace_before_archive(archive, *args, **kwargs):
            nonlocal replaced
            if not replaced:
                output.rename(project / "原发布目录")
                output.symlink_to(outside, target_is_directory=True)
                replaced = True
            return original_init(archive, *args, **kwargs)
        with patch.object(zipfile.ZipFile, "__init__", replace_before_archive):
            try:
                module.build(output)
            except (OSError, ValueError):
                pass
            else:
                self.fail("输出目录被替换后不能声称路径保存成功")
        self.assertEqual(list(outside.iterdir()), [], "不能沿替换的父目录写发布文件")

    def test_source_replaced_after_validation_cannot_pack_external_file(self):
        project = self.project_copy()
        source = project / "代码/__main__.py"
        expected = source.read_bytes()
        private = project / "外部保密样本"
        private.write_bytes(b"PRIVATE_RELEASE_RACE_MARKER")
        module = self.builder(project)
        original_init = zipfile.ZipFile.__init__
        replaced = False
        def replace_before_archive(archive, *args, **kwargs):
            nonlocal replaced
            if not replaced:
                source.rename(project / "代码/原入口副本")
                source.symlink_to(private)
                replaced = True
            return original_init(archive, *args, **kwargs)
        output = project / "发布"
        with patch.object(zipfile.ZipFile, "__init__", replace_before_archive):
            module.build(output)
        for archive_path in (*output.glob("*.pyz"), *output.glob("*.zip")):
            with zipfile.ZipFile(archive_path) as archive:
                entry = next(name for name in archive.namelist() if name.endswith("/__main__.py") or name == "__main__.py")
                self.assertEqual(archive.read(entry), expected, "两个归档只能使用已验证的源码快照")

    def test_opened_output_directory_replacement_keeps_writes_in_original_directory(self):
        project = self.project_copy()
        output = project / "发布"
        output.mkdir()
        outside = project / "外部资料样本"
        outside.mkdir()
        module = self.builder(project)
        original_open = os.open
        replaced = False
        def replace_before_write(name, flags, *args, **kwargs):
            nonlocal replaced
            if str(name).endswith(".pyz") and flags & os.O_CREAT and not replaced:
                output.rename(project / "原发布目录")
                output.symlink_to(outside, target_is_directory=True)
                replaced = True
            return original_open(name, flags, *args, **kwargs)
        with patch.object(os, "open", replace_before_write), self.assertRaises(OSError):
            module.build(output)
        self.assertEqual(list(outside.iterdir()), [])
        self.assertEqual(len(list((project / "原发布目录").iterdir())), 3)

    def test_source_swapped_to_symlink_before_open_is_rejected(self):
        project = self.project_copy()
        source = project / "代码/__main__.py"
        private = project / "外部保密样本"
        private.write_bytes(b"PRIVATE_RELEASE_RACE_MARKER")
        module = self.builder(project)
        original_open = os.open
        replaced = False
        def replace_before_read(name, flags, *args, **kwargs):
            nonlocal replaced
            if str(name) == "__main__.py" and not replaced:
                source.rename(project / "代码/原入口副本")
                source.symlink_to(private)
                replaced = True
            return original_open(name, flags, *args, **kwargs)
        output = project / "发布"
        with patch.object(os, "open", replace_before_read), self.assertRaises((OSError, ValueError)):
            module.build(output)
        self.assertFalse(list(output.glob("*.pyz")))

    def test_source_symlink_ancestor_is_rejected_before_publishing(self):
        project = self.project_copy()
        (project / "文档").rename(project / "实际文档")
        (project / "文档").symlink_to(project / "实际文档", target_is_directory=True)
        output = project / "发布"
        with self.assertRaises((OSError, ValueError)):
            self.builder(project).build(output)
        self.assertFalse(list(output.glob("*.pyz")))

    def test_oversized_source_fails_before_creating_archives(self):
        project = self.project_copy()
        with (project / "代码/过大样本.py").open("wb") as stream:
            stream.truncate(8 * 1024 * 1024 + 1)
        output = project / "发布"
        with self.assertRaises(ValueError):
            self.builder(project).build(output)
        self.assertFalse(list(output.glob("*.pyz")))

    def test_fsync_failure_cannot_report_successful_release(self):
        project = self.project_copy()
        output = project / "发布"
        with patch.object(os, "fsync", side_effect=OSError("测试写入失败")), self.assertRaises(OSError):
            self.builder(project).build(output)
        self.assertFalse((output / "发布清单.json").exists())

    def test_missing_runtime_entry_cannot_create_distributable_files(self):
        project = self.project_copy()
        (project / "代码/__main__.py").unlink()
        output = project / "发布"
        result = subprocess.run([sys.executable, "-B", str(project / "工具/构建发布包.py"),
                                 "--output", str(output)], capture_output=True, text=True, timeout=10)
        self.assertNotEqual(result.returncode, 0, "缺少运行入口不能声称生成可分发包")
        self.assertIn("构建失败", result.stderr)
        self.assertFalse(list(output.glob("*.pyz")))
        self.assertFalse(list(output.glob("*.zip")))
        self.assertFalse((output / "发布清单.json").exists())


if __name__ == "__main__":
    unittest.main()
