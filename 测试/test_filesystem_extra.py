"""扩大验证：预期来自实际临时文件及明确的系统边界案例。"""

import errno
import os
from contextlib import contextmanager
from pathlib import Path
import tempfile
import sys
import unittest
from unittest import mock
from types import SimpleNamespace

from space_report import filesystem as fs


TEMP_ROOT = Path(__file__).resolve().parents[1] / "测试环境" / "临时"


class FilesystemExtraTests(unittest.TestCase):
    def setUp(self):
        TEMP_ROOT.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(prefix="目录扩展-", dir=TEMP_ROOT)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        device = self.root.stat().st_dev
        self.mounts = [{"挂载点": "/", "设备号": f"{os.major(device)}:{os.minor(device)}",
                        "挂载编号": 1, "文件系统": "ext4", "挂载选项": "rw", "超级选项": "rw"}]
        patcher = mock.patch.object(fs, "_fd_mount_id", return_value=1)
        self.mount_identity_patch = patcher
        patcher.start()
        self.addCleanup(patcher.stop)

    def collect(self, **kwargs):
        with mock.patch.object(fs, "_read_mountinfo", return_value=self.mounts):
            return fs.collect_directories(str(self.root), **kwargs)

    def test_mount_table_preserves_non_newline_control_characters(self):
        # Linux 只以 LF 分隔记录；splitlines 会把合法名字中的 FF/CR/NEL 误当新行。
        for character in ["\v", "\f", "\r", "\x85", "\u2028", "\u2029"]:
            with self.subTest(character=repr(character)):
                point = "/挂载" + character + "目录"
                raw = f"9 1 8:1 / {point} rw - ext4 /dev/example rw\n"
                try:
                    parsed = fs._parse_mountinfo(raw)
                except ValueError:
                    parsed = []
                self.assertEqual([row["挂载点"] for row in parsed], [point])

    def test_real_mount_reader_does_not_normalize_carriage_return_in_path(self):
        # 文本模式的通用换行转换也会改写合法挂载路径中的 CR。
        table = self.root / "挂载表"
        table.write_bytes(b"9 1 8:1 / /a\rb rw - ext4 /dev/example rw\n")
        original = open

        def controlled(path, *args, **kwargs):
            return original(table if path == "/proc/self/mountinfo" else path, *args, **kwargs)

        with mock.patch("builtins.open", side_effect=controlled):
            try:
                parsed = fs._read_mountinfo()
            except ValueError:
                parsed = []
        self.assertEqual([row["挂载点"] for row in parsed], ["/a\rb"])

    def test_hardlink_with_decreasing_link_count_is_still_counted_once(self):
        first = self.root / "甲"
        first.write_bytes(b"x" * 8192)
        second = self.root / "乙"
        os.link(first, second)
        expected = first.stat().st_blocks * 512
        original = os.scandir

        @contextmanager
        def unlink_after_first(fd):
            with original(fd) as entries:
                ordered = sorted(entries, key=lambda entry: entry.name, reverse=True)
                def changed():
                    for index, entry in enumerate(ordered):
                        yield entry
                        if index == 0:
                            (self.root / entry.name).unlink()
                yield changed()

        with mock.patch.object(fs.os, "scandir", side_effect=unlink_after_first):
            report = self.collect()
        self.assertEqual(report["数据"]["根目录普通文件字节"], expected)

    def test_hardlinks_across_directories_do_not_duplicate_total(self):
        left, right = self.root / "甲", self.root / "乙"
        left.mkdir()
        right.mkdir()
        first = left / "内容"
        first.write_bytes(b"x" * 8192)
        os.link(first, right / "同内容")
        expected = sum(path.stat().st_blocks for path in [self.root, left, right, first]) * 512
        report = self.collect()
        self.assertEqual(report["数据"]["已统计字节"], expected)
        self.assertEqual(sum(row["已分配字节"] for row in report["数据"]["目录列表"]),
                         expected - self.root.stat().st_blocks * 512)

    def test_raw_byte_filename_is_preserved_without_losing_its_size(self):
        raw = os.fsencode(self.root) + b"/name-\xff-\xfe"
        try:
            os.mkdir(raw)
        except OSError as error:
            if error.errno in {errno.EILSEQ, errno.EINVAL}:
                self.skipTest("当前文件系统不支持该原始字节文件名")
            raise
        path = os.path.join(raw, b"data")
        with open(path, "wb") as handle:
            handle.write(b"x" * 8192)
        expected = (os.stat(raw).st_blocks + os.stat(path).st_blocks) * 512
        report = self.collect()
        row = report["数据"]["目录列表"][0]
        self.assertEqual(os.fsencode(row["路径"]), raw)
        self.assertEqual(row["已分配字节"], expected)

    def test_nested_mount_is_excluded_but_sibling_files_remain(self):
        parent = self.root / "父目录"
        mounted = parent / "挂载目录"
        mounted.mkdir(parents=True)
        regular = parent / "应该统计"
        regular.write_bytes(b"x" * 8192)
        (mounted / "不应统计").write_bytes(b"x" * 32768)
        self.mounts.append({**self.mounts[0], "挂载点": str(mounted), "挂载编号": 2})
        report = self.collect()
        expected = (parent.stat().st_blocks + regular.stat().st_blocks) * 512
        self.assertEqual(report["数据"]["目录列表"][0]["已分配字节"], expected)
        self.assertEqual(report["数据"]["跳过挂载点"], [str(mounted)])

    def test_directory_replaced_with_symlink_is_not_traversed(self):
        child = self.root / "目录"
        child.mkdir()
        secret = self.root / "被排除挂载"
        secret.mkdir()
        (secret / "不可统计").write_bytes(b"x" * 32768)
        self.mounts.append({**self.mounts[0], "挂载点": str(secret), "挂载编号": 2})
        original = os.open
        changed = [False]

        def swapping(path, flags, *args, **kwargs):
            if path == child.name and not changed[0]:
                changed[0] = True
                child.rmdir()
                child.symlink_to(secret, target_is_directory=True)
            return original(path, flags, *args, **kwargs)

        with mock.patch.object(fs.os, "open", side_effect=swapping):
            report = self.collect()
        self.assertEqual(report["状态"], "部分完成")
        self.assertLess(report["数据"]["已统计字节"], 32768)

    def test_deleted_directory_entry_does_not_discard_readable_sibling(self):
        doomed = self.root / "即将消失"
        doomed.write_bytes(b"x" * 8192)
        survivor = self.root / "保留"
        survivor.write_bytes(b"x" * 16384)
        original = os.scandir

        @contextmanager
        def disappearing(fd):
            with original(fd) as entries:
                rows = list(entries)
                doomed.unlink()
                yield iter(rows)

        with mock.patch.object(fs.os, "scandir", side_effect=disappearing):
            report = self.collect()
        self.assertEqual(report["状态"], "部分完成")
        self.assertEqual(report["数据"]["根目录普通文件数量"], 1)
        self.assertEqual(report["数据"]["根目录普通文件字节"], survivor.stat().st_blocks * 512)

    def test_last_file_metadata_overruns_deadline_but_keeps_measured_size(self):
        item = self.root / "最后一个"
        item.write_bytes(b"x" * 8192)
        original = os.scandir
        clock = [0.0]

        @contextmanager
        def slow_last(fd):
            with original(fd) as entries:
                def delayed():
                    for entry in entries:
                        yield entry
                        clock[0] = 2.0
                yield delayed()

        with mock.patch.object(fs.os, "scandir", side_effect=slow_last), \
                mock.patch.object(fs.time, "monotonic", side_effect=lambda: clock[0]):
            report = self.collect(timeout=1)
        self.assertEqual(report["状态"], "超时")
        self.assertEqual(report["数据"]["根目录普通文件字节"], item.stat().st_blocks * 512)

    def test_unknown_inode_capacity_is_not_reported_as_zero_capacity(self):
        real = os.statvfs(self.root)
        values = {name: getattr(real, name) for name in dir(real) if name.startswith("f_")}
        values.update(f_files=0, f_ffree=0, f_favail=0)
        with mock.patch.object(fs, "_read_mountinfo", return_value=self.mounts), \
                mock.patch.object(fs.os, "statvfs", return_value=SimpleNamespace(**values)):
            report = fs.collect_partition(str(self.root))
        self.assertIsNone(report["数据"]["总文件节点"])
        self.assertIsNone(report["数据"]["已用文件节点"])
        self.assertIsNone(report["数据"]["可用文件节点"])

    def test_mount_parser_preserves_raw_non_utf8_path_bytes(self):
        raw = b"9 1 8:1 / /raw-\xff-\xfe rw - ext4 /dev/example rw\n"
        rows = fs._parse_mountinfo(raw.decode("utf-8", errors="surrogateescape"))
        self.assertEqual(os.fsencode(rows[0]["挂载点"]), b"/raw-\xff-\xfe")

    def test_deep_tree_reports_partial_before_recursion_limit(self):
        current = self.root
        for _ in range(135):
            current = current / "d"
            current.mkdir()
        (current / "超过检查深度").write_bytes(b"x" * 8192)
        report = self.collect()
        self.assertEqual(report["状态"], "部分完成")
        self.assertGreater(report["数据"]["未读取条目数"], 0)

    def test_mount_id_not_matching_visible_device_never_uses_hidden_layer(self):
        self.mounts.append({**self.mounts[0], "挂载点": str(self.root), "挂载编号": 2})
        report = self.collect()
        self.assertNotEqual(report["状态"], "完成")
        self.assertIsNone(report["数据"]["已统计字节"])

    def test_missing_root_mount_id_stops_scan_instead_of_crossing_unknown_mounts(self):
        (self.root / "不应读取").write_bytes(b"x" * 8192)
        with mock.patch.object(fs, "_fd_mount_id", return_value=None):
            report = self.collect()
        self.assertEqual(report["状态"], "部分完成")
        self.assertIsNone(report["数据"]["根目录普通文件字节"])

    def test_missing_child_mount_id_does_not_scan_that_branch(self):
        child = self.root / "无法识别挂载"
        child.mkdir()
        (child / "不应统计").write_bytes(b"x" * 8192)
        inode = child.stat().st_ino

        def unknown(fd):
            return None if os.fstat(fd).st_ino == inode else 1

        with mock.patch.object(fs, "_fd_mount_id", side_effect=unknown):
            report = self.collect()
        self.assertEqual(report["状态"], "部分完成")
        self.assertIsNone(report["数据"]["目录列表"][0]["已分配字节"])

    def test_new_same_device_file_bind_is_excluded_by_mount_identity(self):
        bound = self.root / "动态文件挂载"
        bound.write_bytes(b"x" * 8192)
        inode = bound.stat().st_ino

        def changed(fd):
            return 2 if os.fstat(fd).st_ino == inode else 1

        # macOS 无 O_PATH；此受控普通文件用 O_RDONLY 验证同一元数据分支。
        with mock.patch.object(fs.os, "O_PATH", getattr(os, "O_PATH", 0), create=True), \
                mock.patch.object(fs, "_fd_mount_id", side_effect=changed):
            report = self.collect()
        self.assertEqual(report["状态"], "部分完成")
        self.assertEqual(report["数据"]["根目录普通文件字节"], 0)
        self.assertIn(str(bound), report["数据"]["跳过挂载点"])

    @unittest.skipUnless(sys.platform == "linux", "真实 O_PATH 和挂载编号须在 Linux 验证")
    def test_linux_real_metadata_scan_does_not_need_file_read_permission(self):
        item = self.root / "不能读取内容"
        item.write_bytes(b"x" * 8192)
        item.chmod(0)
        expected = item.stat().st_blocks * 512
        self.mount_identity_patch.stop()
        report = fs.collect_directories(str(self.root))
        self.assertEqual(report["状态"], "完成")
        self.assertEqual(report["数据"]["根目录普通文件字节"], expected)


if __name__ == "__main__":
    unittest.main()
