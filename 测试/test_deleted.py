"""已删除但仍持有文件：真实文件描述符加受控进程目录。"""

import os
import builtins
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock
from types import SimpleNamespace

try:
    from space_report import deleted
except ImportError:
    deleted = None


PROJECT = Path(__file__).resolve().parents[1]
TEMP_ROOT = PROJECT / "测试环境" / "临时"


class DeletedTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(deleted, "已删除占用采集模块尚未实现")
        TEMP_ROOT.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(prefix="已删除-", dir=TEMP_ROOT)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.proc = self.root / "受控进程表"
        self.proc.mkdir()
        device = self.root.stat().st_dev
        self.mounts = [{"挂载点": "/", "设备号": f"{os.major(device)}:{os.minor(device)}", "文件系统": "ext4",
                        "挂载选项": "rw", "超级选项": "rw"}]

    def hold_file(self, name="待删除.bin", unlink=True):
        path = self.root / name
        handle = path.open("w+b")
        self.addCleanup(handle.close)
        handle.write(b"x" * 8192)
        handle.flush()
        if unlink:
            path.unlink()
        return handle

    def add_process(self, pid, handles):
        process = self.proc / str(pid)
        (process / "fd").mkdir(parents=True)
        (process / "comm").write_text("示例进程\n", encoding="utf-8")
        for index, handle in enumerate(handles):
            os.symlink(f"/dev/fd/{handle.fileno()}", process / "fd" / str(index))
        return process

    def collect(self, fd_snapshot=None, fd_error=None, **kwargs):
        # macOS 的 /dev/fd 链接 stat 不等同 Linux procfs；仅替换这一系统边界。
        # 返回值来自实际已打开文件的 fstat，没有人工构造大小或 inode。
        original_stat = os.stat

        def proc_stat(path, *args, **options):
            text = os.fspath(path) if not isinstance(path, int) else ""
            if fd_error is not None and text.startswith(str(self.proc) + os.sep):
                raise fd_error
            if sys.platform != "linux" and text.startswith(str(self.proc) + os.sep):
                link = os.readlink(path)
                if link.startswith("/dev/fd/"):
                    return (fd_snapshot(path) if callable(fd_snapshot) else fd_snapshot) or os.fstat(int(link.rsplit("/", 1)[1]))
            if fd_snapshot is not None and text.startswith(str(self.proc) + os.sep):
                return fd_snapshot(path) if callable(fd_snapshot) else fd_snapshot
            return original_stat(path, *args, **options)

        with mock.patch.object(deleted, "PROC_ROOT", str(self.proc)), \
                mock.patch.object(deleted, "_read_mountinfo", return_value=self.mounts), \
                mock.patch.object(deleted.os, "stat", side_effect=proc_stat), \
                mock.patch.object(deleted.os, "geteuid", return_value=0):
            return deleted.collect_deleted(str(self.root), **kwargs)

    def test_open_unlinked_file_is_counted_once_across_processes_and_descriptors(self):
        # 错误地逐 fd 累加会把同一份实际占用重复统计三次。
        handle = self.hold_file()
        expected = os.fstat(handle.fileno()).st_blocks * 512
        self.add_process(101, [handle, handle])
        self.add_process(102, [handle])
        report = self.collect()
        self.assertEqual(report["状态"], "完成")
        self.assertEqual(report["数据"]["文件数"], 1)
        self.assertEqual(report["数据"]["已分配字节"], expected)
        holders = report["数据"]["文件列表"][0]["持有进程"]
        self.assertEqual({item["进程号"] for item in holders}, {101, 102})
        self.assertTrue(all(item["进程名"] == "示例进程" for item in holders))

    def test_existing_linked_file_is_not_reported_as_deleted(self):
        self.add_process(101, [self.hold_file(unlink=False)])
        report = self.collect()
        self.assertEqual(report["数据"]["文件数"], 0)
        self.assertEqual(report["数据"]["已分配字节"], 0)

    def test_sparse_deleted_file_reports_allocated_not_logical_size(self):
        handle = self.hold_file()
        handle.seek(32 * 1024 * 1024)
        handle.write(b"x")
        handle.flush()
        self.add_process(101, [handle])
        report = self.collect()
        data = report["数据"]
        self.assertEqual(data["已分配字节"], os.fstat(handle.fileno()).st_blocks * 512)
        self.assertLess(data["已分配字节"], data["文件列表"][0]["逻辑字节"])

    def test_vanished_file_descriptor_does_not_crash_or_report_phantom_usage(self):
        process = self.add_process(101, [])
        os.symlink(self.root / "已经不存在", process / "fd" / "7")
        report = self.collect()
        self.assertEqual(report["状态"], "完成")
        self.assertEqual(report["数据"]["文件数"], 0)

    def test_permission_denied_keeps_visible_results_but_marks_partial(self):
        self.add_process(101, [self.hold_file()])
        inaccessible = self.add_process(102, []) / "fd"
        original = os.scandir

        def limited(path):
            if os.fspath(path) == str(inaccessible):
                raise PermissionError("无权限")
            return original(path)

        with mock.patch.object(deleted.os, "scandir", side_effect=limited):
            report = self.collect()
        self.assertEqual(report["状态"], "部分完成")
        self.assertEqual(report["数据"]["文件数"], 1)
        self.assertEqual(report["数据"]["不可访问进程数"], 1)

    def test_unprivileged_visibility_cannot_be_claimed_complete(self):
        self.add_process(101, [])
        with mock.patch.object(deleted, "PROC_ROOT", str(self.proc)), \
                mock.patch.object(deleted, "_read_mountinfo", return_value=self.mounts), \
                mock.patch.object(deleted.os, "geteuid", return_value=1000):
            report = deleted.collect_deleted(str(self.root))
        self.assertEqual(report["状态"], "部分完成")

    def test_memory_filesystem_is_not_reported_as_disk_space(self):
        self.mounts[0]["文件系统"] = "tmpfs"
        self.add_process(101, [self.hold_file()])
        report = self.collect()
        self.assertNotEqual(report["状态"], "完成")
        self.assertIsNone(report["数据"]["文件数"])
        self.assertIsNone(report["数据"]["已分配字节"])
        self.assertIsNone(report["数据"]["逻辑字节"])

    def test_zero_timeout_returns_timeout(self):
        report = self.collect(timeout=0)
        self.assertEqual(report["状态"], "超时")
        self.assertIsNone(report["数据"]["已分配字节"])

    def test_missing_target_path_returns_failure(self):
        report = deleted.collect_deleted(str(self.root / "不存在"))
        self.assertEqual(report["状态"], "失败")
        self.assertIsNone(report["数据"]["已分配字节"])

    def test_no_readable_process_has_unknown_usage_not_zero(self):
        process = self.add_process(101, [])
        original = os.scandir

        def limited(path):
            if os.fspath(path) == str(process / "fd"):
                raise PermissionError("无权限")
            return original(path)

        with mock.patch.object(deleted.os, "scandir", side_effect=limited):
            report = self.collect()
        self.assertNotEqual(report["状态"], "完成")
        self.assertIsNone(report["数据"]["已分配字节"])
        self.assertIsNone(report["数据"]["文件数"])

    def test_no_readable_descriptor_has_unknown_usage_not_zero(self):
        self.add_process(101, [self.hold_file()])
        report = self.collect(fd_error=PermissionError("无权限"))
        self.assertNotEqual(report["状态"], "完成")
        self.assertIsNone(report["数据"]["已分配字节"])

    def test_scanned_empty_visible_range_can_report_zero_with_scope_note(self):
        self.add_process(101, [])
        report = self.collect()
        self.assertEqual(report["数据"]["已分配字节"], 0)
        self.assertTrue(any("可读取范围" in note for note in report["说明"]))

    def test_hidden_process_mount_cannot_be_claimed_complete(self):
        self.mounts.append({"挂载点": str(self.proc), "设备号": self.mounts[0]["设备号"], "文件系统": "proc",
                            "挂载选项": "rw,hidepid=2", "超级选项": "rw"})
        report = self.collect()
        self.assertEqual(report["状态"], "部分完成")

    def test_unreadable_process_name_marks_result_partial(self):
        process = self.add_process(101, [self.hold_file()])
        original = builtins.open

        def restricted(path, *args, **kwargs):
            if os.fspath(path) == str(process / "comm"):
                raise PermissionError("无权限")
            return original(path, *args, **kwargs)

        with mock.patch("builtins.open", side_effect=restricted):
            report = self.collect()
        self.assertEqual(report["状态"], "部分完成")
        self.assertEqual(report["数据"]["文件数"], 1)

    def test_other_filesystem_file_is_not_counted(self):
        handle = self.hold_file()
        self.add_process(101, [handle])
        real = os.fstat(handle.fileno())
        fields = {name: getattr(real, name) for name in dir(real) if name.startswith("st_")}
        fields["st_dev"] = real.st_dev + 1
        report = self.collect(fd_snapshot=SimpleNamespace(**fields))
        self.assertEqual(report["数据"]["文件数"], 0)

    def test_timeout_preserves_files_found_before_deadline(self):
        self.add_process(101, [self.hold_file()])
        self.add_process(102, [])
        original = os.scandir
        clock = [0.0]

        proc_root = str(self.proc)

        class DelayedScan:
            def __init__(self, path):
                self.path = path
                self.entries = original(path)

            def __enter__(self):
                self.entries.__enter__()
                return self

            def __exit__(self, *args):
                return self.entries.__exit__(*args)

            def __iter__(self):
                for entry in sorted(self.entries, key=lambda item: item.name):
                    yield entry
                    if os.fspath(self.path) == proc_root:
                        clock[0] = 2.0

        with mock.patch.object(deleted.os, "scandir", side_effect=DelayedScan), \
                mock.patch.object(deleted.time, "monotonic", side_effect=lambda: clock[0]):
            report = self.collect(timeout=1)
        self.assertEqual(report["状态"], "超时")
        self.assertEqual(report["数据"]["文件数"], 1)

    @unittest.skipUnless(sys.platform == "linux", "真实 /proc 采集须在 Linux 验证")
    def test_linux_real_proc_finds_current_process_unlinked_file(self):
        handle = self.hold_file()
        target_inode = os.fstat(handle.fileno()).st_ino
        report = deleted.collect_deleted(str(self.root), timeout=5)
        identities = [row["文件节点"] for row in report["数据"]["文件列表"]]
        self.assertIn(target_inode, identities)


if __name__ == "__main__":
    unittest.main()
