"""文件系统采集的行为测试；临时数据仅建立在项目目录中。"""

import os
from contextlib import contextmanager
from pathlib import Path
import tempfile
import unittest
from unittest import mock

try:
    from space_report import filesystem as fs
except ImportError:
    fs = None


PROJECT = Path(__file__).resolve().parents[1]
TEMP_ROOT = PROJECT / "测试环境" / "临时"


class FilesystemTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(fs, "文件系统采集模块尚未实现")
        TEMP_ROOT.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(prefix="文件系统-", dir=TEMP_ROOT)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        identity = mock.patch.object(fs, "_fd_mount_id", return_value=1, create=True)
        identity.start()
        self.addCleanup(identity.stop)

    def mounts(self, extra=None):
        device = self.root.stat().st_dev
        rows = [{"挂载点": "/", "设备号": f"{os.major(device)}:{os.minor(device)}", "文件系统": "ext4",
                 "挂载编号": 1, "挂载选项": "rw", "超级选项": "rw"}]
        return rows + (extra or [])

    def test_partition_reports_actual_available_space_and_inode_counts(self):
        # 错用 f_bfree 代替普通用户可用空间，或漏报文件节点，会使本测试失败。
        expected = os.statvfs(self.root)
        with mock.patch.object(fs, "_read_mountinfo", return_value=self.mounts()):
            report = fs.collect_partition(str(self.root))
        data = report["数据"]
        self.assertEqual(report["状态"], "完成")
        self.assertEqual(data["总字节"], expected.f_blocks * expected.f_frsize)
        self.assertEqual(data["已用字节"], (expected.f_blocks - expected.f_bfree) * expected.f_frsize)
        self.assertGreaterEqual(data["可用字节"], 0)
        self.assertEqual(data["总文件节点"], expected.f_files)

    def test_partition_missing_path_returns_failure_instead_of_raising(self):
        report = fs.collect_partition(str(self.root / "不存在"))
        self.assertEqual(report["状态"], "失败")
        self.assertEqual(report["数据"], {})

    def test_zero_timeout_preserves_timeout_state(self):
        self.assertEqual(fs.collect_partition(str(self.root), timeout=0)["状态"], "超时")
        self.assertEqual(fs.collect_directories(str(self.root), timeout=0)["状态"], "超时")

    def test_mountinfo_decodes_spaces_tabs_newlines_and_backslashes(self):
        # 错用按空白分割解码后的文本，或通用 unicode_escape，会丢失挂载边界。
        raw = "42 1 8:1 / /名字\\040空格\\011制表\\012换行\\134目录 rw,relatime - ext4 /dev/sda1 rw\n"
        rows = fs._parse_mountinfo(raw)
        self.assertEqual(rows, [{"挂载点": "/名字 空格\t制表\n换行\\目录", "设备号": "8:1",
                                 "挂载编号": 42, "文件系统": "ext4", "挂载选项": "rw,relatime", "超级选项": "rw"}])

    def test_missing_mount_table_does_not_claim_safe_complete_scan(self):
        with mock.patch.object(fs, "_read_mountinfo", side_effect=PermissionError("无权限")):
            report = fs.collect_directories(str(self.root))
        self.assertEqual(report["状态"], "权限不足")
        self.assertEqual(report["数据"].get("目录列表", []), [])
        self.assertIsNone(report["数据"]["已统计字节"])

    def test_unopened_target_has_unknown_size_not_zero(self):
        with mock.patch.object(fs, "_read_mountinfo", return_value=self.mounts()), \
                mock.patch.object(fs.os, "open", side_effect=PermissionError("无权限")):
            report = fs.collect_directories(str(self.root))
        self.assertEqual(report["状态"], "权限不足")
        self.assertIsNone(report["数据"]["已统计字节"])
        self.assertIsNone(report["数据"]["根目录普通文件字节"])

    def test_missing_or_unstarted_target_has_unknown_size(self):
        for path, timeout in [(self.root / "不存在", 30), (self.root, 0)]:
            with self.subTest(path=path, timeout=timeout):
                report = fs.collect_directories(str(path), timeout=timeout)
                self.assertIsNone(report["数据"]["已统计字节"])
                self.assertIsNone(report["数据"]["目录总数"])

    def test_regular_files_at_scan_root_are_not_omitted(self):
        top = self.root / "根文件.bin"
        top.write_bytes(b"x" * 8192)
        (self.root / "子目录").mkdir()
        with mock.patch.object(fs, "_read_mountinfo", return_value=self.mounts()):
            data = fs.collect_directories(str(self.root))["数据"]
        self.assertEqual(data["根目录普通文件数量"], 1)
        self.assertEqual(data["根目录普通文件字节"], top.stat().st_blocks * 512)

    def test_sparse_files_use_allocated_blocks_and_hardlinks_count_once(self):
        directory = self.root / "资料"
        directory.mkdir()
        sparse = directory / "稀疏.bin"
        with sparse.open("wb") as handle:
            handle.seek(16 * 1024 * 1024)
            handle.write(b"x")
        os.link(sparse, directory / "同一个文件.bin")
        expected = (directory.stat().st_blocks + sparse.stat().st_blocks) * 512
        with mock.patch.object(fs, "_read_mountinfo", return_value=self.mounts()):
            report = fs.collect_directories(str(self.root))
        self.assertEqual(report["状态"], "完成")
        self.assertEqual(report["数据"]["目录列表"][0]["已分配字节"], expected)
        self.assertLess(expected, sparse.stat().st_size)

    def test_symlinks_and_same_device_bind_mounts_are_not_followed(self):
        child = self.root / "绑定目录"
        child.mkdir()
        (child / "不能统计.bin").write_bytes(b"x" * 16384)
        os.symlink(child, self.root / "链接")
        extra = [{"挂载点": str(child), "设备号": "1:1", "文件系统": "ext4",
                  "挂载选项": "rw", "超级选项": "rw"}]
        with mock.patch.object(fs, "_read_mountinfo", return_value=self.mounts(extra)):
            report = fs.collect_directories(str(self.root))
        self.assertEqual(report["数据"]["目录列表"], [])
        self.assertIn(str(child), report["数据"]["跳过挂载点"])
        self.assertLess(report["数据"]["已统计字节"], 16384)

    def test_top_twenty_sort_by_allocated_size(self):
        for index in range(25):
            directory = self.root / f"目录{index:02d}"
            directory.mkdir()
            (directory / "内容").write_bytes(b"x" * ((index + 1) * 8192))
        with mock.patch.object(fs, "_read_mountinfo", return_value=self.mounts()):
            report = fs.collect_directories(str(self.root))
        rows = report["数据"]["目录列表"]
        self.assertEqual(len(rows), 20)
        self.assertEqual(Path(rows[0]["路径"]).name, "目录24")
        self.assertEqual(Path(rows[-1]["路径"]).name, "目录05")
        self.assertEqual(report["数据"]["目录总数"], 25)

    def test_directory_permission_failure_is_partial_not_zero_success(self):
        (self.root / "可读").mkdir()
        hidden = self.root / "不能读取"
        hidden.mkdir()
        original = os.scandir

        def limited(path):
            identity = os.fstat(path).st_ino if isinstance(path, int) else os.stat(path).st_ino
            if identity == hidden.stat().st_ino:
                raise PermissionError("无权限")
            return original(path)

        with mock.patch.object(fs, "_read_mountinfo", return_value=self.mounts()), \
                mock.patch.object(fs.os, "scandir", side_effect=limited):
            report = fs.collect_directories(str(self.root))
        self.assertEqual(report["状态"], "部分完成")
        self.assertGreater(report["数据"]["未读取条目数"], 0)

    def test_mount_boundary_is_respected_through_symlinked_ancestor(self):
        actual = self.root / "实际"
        scan = actual / "扫描"
        mounted = scan / "子挂载"
        mounted.mkdir(parents=True)
        (mounted / "不能读取").write_bytes(b"x" * 8192)
        os.symlink(actual, self.root / "路径别名")
        extra = [{"挂载点": str(mounted), "设备号": "1:1", "文件系统": "ext4",
                  "挂载选项": "rw", "超级选项": "rw"}]
        with mock.patch.object(fs, "_read_mountinfo", return_value=self.mounts(extra)):
            report = fs.collect_directories(str(self.root / "路径别名" / "扫描"))
        self.assertEqual(report["数据"]["目录列表"], [])
        self.assertIn(str(mounted), report["数据"]["跳过挂载点"])

    def test_timeout_keeps_already_scanned_file_data(self):
        (self.root / "一.bin").write_bytes(b"x" * 8192)
        (self.root / "二.bin").write_bytes(b"x" * 8192)
        clock = [0.0]
        original = os.scandir

        @contextmanager
        def slow_scan(path):
            with original(path) as entries:
                def rows():
                    for entry in entries:
                        yield entry
                        clock[0] = 2.0
                yield rows()

        with mock.patch.object(fs, "_read_mountinfo", return_value=self.mounts()), \
                mock.patch.object(fs.time, "monotonic", side_effect=lambda: clock[0]), \
                mock.patch.object(fs.os, "scandir", side_effect=slow_scan):
            report = fs.collect_directories(str(self.root), timeout=1)
        self.assertEqual(report["状态"], "超时")
        self.assertEqual(report["数据"]["根目录普通文件数量"], 1)
        self.assertGreater(report["数据"]["根目录普通文件字节"], 0)

    def test_malformed_mountinfo_is_rejected_instead_of_ignoring_boundaries(self):
        with self.assertRaises(ValueError):
            fs._parse_mountinfo("无效的挂载记录\n")

    def test_scanning_a_symbolic_link_root_is_rejected(self):
        actual = self.root / "实际"
        actual.mkdir()
        os.symlink(actual, self.root / "链接")
        report = fs.collect_directories(str(self.root / "链接"))
        self.assertEqual(report["状态"], "失败")

    def test_stacked_mount_uses_actual_device_instead_of_hidden_lower_mount(self):
        visible_device = self.mounts()[0]["设备号"]
        rows = self.mounts([
            {"挂载点": str(self.root), "设备号": "888:1", "挂载编号": 9,
             "文件系统": "ext4", "挂载选项": "rw", "超级选项": "rw"},
            {"挂载点": str(self.root), "设备号": visible_device, "挂载编号": 10,
             "文件系统": "tmpfs", "挂载选项": "rw", "超级选项": "rw"},
        ])
        with mock.patch.object(fs, "_read_mountinfo", return_value=rows):
            report = fs.collect_partition(str(self.root))
        self.assertEqual(report["数据"]["文件系统"], "tmpfs")

    def test_stacked_same_device_uses_last_visible_layer_or_reports_unknown(self):
        device = self.mounts()[0]["设备号"]
        rows = self.mounts([
            {"挂载点": str(self.root), "设备号": device, "挂载编号": 9,
             "文件系统": "ext4", "挂载选项": "rw", "超级选项": "rw"},
            {"挂载点": str(self.root), "设备号": device, "挂载编号": 10,
             "文件系统": "xfs", "挂载选项": "rw", "超级选项": "rw"},
        ])
        with mock.patch.object(fs, "_read_mountinfo", return_value=rows):
            report = fs.collect_partition(str(self.root))
        if report["状态"] == "完成":
            self.assertEqual(report["数据"]["文件系统"], "xfs")
        else:
            self.assertNotIn("文件系统", report["数据"])

    def test_directory_replaced_between_stat_and_open_is_not_scanned(self):
        child = self.root / "目录"
        child.mkdir()
        old = self.root / "旧目录"
        original = os.open
        replaced = [False]

        def racing_open(path, flags, *args, **kwargs):
            if path == "目录" and kwargs.get("dir_fd") is not None and not replaced[0]:
                replaced[0] = True
                child.rename(old)
                child.mkdir()
                (child / "不应扫描.bin").write_bytes(b"x" * 8192)
            return original(path, flags, *args, **kwargs)

        with mock.patch.object(fs, "_read_mountinfo", return_value=self.mounts()), \
                mock.patch.object(fs.os, "open", side_effect=racing_open):
            report = fs.collect_directories(str(self.root))
        self.assertEqual(report["状态"], "部分完成")
        selected = next(row for row in report["数据"]["目录列表"] if row["路径"] == str(child))
        self.assertIsNone(selected["已分配字节"])

    def test_new_same_device_mount_id_is_not_traversed(self):
        child = self.root / "动态挂载"
        child.mkdir()
        (child / "不应扫描.bin").write_bytes(b"x" * 8192)
        child_inode = child.stat().st_ino

        def mount_id(descriptor):
            return 2 if os.fstat(descriptor).st_ino == child_inode else 1

        with mock.patch.object(fs, "_read_mountinfo", return_value=self.mounts()), \
                mock.patch.object(fs, "_fd_mount_id", side_effect=mount_id):
            report = fs.collect_directories(str(self.root))
        self.assertEqual(report["状态"], "部分完成")
        self.assertIn(str(child), report["数据"]["跳过挂载点"])
        selected = next(row for row in report["数据"]["目录列表"] if row["路径"] == str(child))
        self.assertIsNone(selected["已分配字节"])


if __name__ == "__main__":
    unittest.main()
