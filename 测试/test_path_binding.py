"""真实目录变化与重复扫描资源回归，仅使用项目临时目录。"""
import contextlib
import gc
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import tracemalloc
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "代码"))
from space_report import cli, filesystem


@unittest.skipUnless(sys.platform == "linux", "需要真实 Linux 挂载表及目录描述符")
class PathBindingTests(unittest.TestCase):
    def setUp(self):
        parent = ROOT / "测试环境/临时"
        parent.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=parent)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def test_cli_does_not_scan_replacement_after_first_check(self):
        # 若后续采集重新解析用户路径，将读到另一个目录并错误显示完成。
        for use_link in (False, True):
            with self.subTest(symlink=use_link):
                base = self.root / str(use_link)
                selected = base / "selected/target"
                other = base / "other/target"
                selected.mkdir(parents=True)
                other.mkdir(parents=True)
                (selected / "original").write_bytes(b"x" * 4096)
                (other / "unselected").mkdir()
                (other / "unselected/data").write_bytes(b"y" * 32768)

                def exchange(path, timeout):
                    result = filesystem.collect_partition(path, timeout)
                    selected.parent.rename(base / "kept")
                    if use_link:
                        selected.parent.symlink_to(other.parent, target_is_directory=True)
                    else:
                        other.parent.rename(selected.parent)
                    return result

                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    code = cli.main(["--path", str(selected), "--deep", "--output", str(base / "reports")],
                                    collectors=[("分区容量", exchange), ("大目录排行", filesystem.collect_directories)])
                report = json.loads(next((base / "reports").glob("*/报告.json")).read_text())
                result = report["检查结果"][-1]
                self.assertNotEqual(code, 0)
                self.assertNotEqual(result["状态"], "完成")
                self.assertNotIn("unselected", json.dumps(result["数据"]))

    def test_directory_open_cannot_follow_changed_ancestor(self):
        selected = self.root / "selected/target"
        other = self.root / "other/target"
        selected.mkdir(parents=True)
        other.mkdir(parents=True)
        (selected / "original").write_bytes(b"x" * 4096)
        (other / "unselected").mkdir()
        (other / "unselected/data").write_bytes(b"y" * 32768)
        read_mounts = filesystem._read_mountinfo

        def exchange():
            selected.parent.rename(self.root / "kept")
            selected.parent.symlink_to(other.parent, target_is_directory=True)
            return read_mounts()

        with patch.object(filesystem, "_read_mountinfo", side_effect=exchange):
            result = filesystem.collect_directories(str(selected))
        self.assertNotIn("unselected", json.dumps(result["数据"]))

    def test_cli_keeps_original_directory_during_collector(self):
        # 检查开始后的目录更名不能让真正的扫描器改读替换目标。
        selected = self.root / "selected"
        other = self.root / "other"
        selected.mkdir()
        other.mkdir()
        (selected / "original").write_bytes(b"x" * 4096)
        (other / "unselected").mkdir()
        (other / "unselected/data").write_bytes(b"y" * 32768)
        expected = (selected.stat().st_blocks + (selected / "original").stat().st_blocks) * 512

        def exchange_then_scan(path, timeout):
            selected.rename(self.root / "kept")
            other.rename(selected)
            return filesystem.collect_directories(path, timeout)

        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            code = cli.main(["--path", str(selected), "--deep", "--output", str(self.root / "reports")],
                            collectors=[("大目录排行", exchange_then_scan)])
        report = json.loads(next((self.root / "reports").glob("*/报告.json")).read_text())
        result = report["检查结果"][0]
        self.assertEqual(code, 1)
        self.assertEqual(result["状态"], "部分完成")
        self.assertEqual(result["数据"]["已统计字节"], expected)
        self.assertNotIn("unselected", json.dumps(result["数据"]))

    def test_repeated_scans_release_file_index_without_waiting_for_gc(self):
        # 闭包循环不能保留每轮文件索引；仅限制上限会漏掉持续线性增长。
        for index in range(2000):
            (self.root / str(index)).touch()
        gc.collect()
        enabled = gc.isenabled()
        gc.disable()
        tracemalloc.start()
        try:
            first = filesystem.collect_directories(str(self.root))
            self.assertEqual(first["状态"], "完成")
            before = tracemalloc.get_traced_memory()[0]
            for _ in range(12):
                result = filesystem.collect_directories(str(self.root))
                self.assertEqual(result["数据"]["已统计字节"], first["数据"]["已统计字节"])
            growth = tracemalloc.get_traced_memory()[0] - before
            self.assertLess(growth, 1024 * 1024, f"12次扫描仍保留 {growth} 字节")
        finally:
            tracemalloc.stop()
            if enabled:
                gc.enable()
            gc.collect()
