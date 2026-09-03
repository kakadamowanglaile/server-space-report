"""进程变化、文件描述符复用与异常边界的独立回归。"""

import errno
import os
import unittest
from unittest import mock
from types import SimpleNamespace

from space_report import deleted
import test_deleted as baseline


class OrderedScan:
    """保留 scandir 生命周期，只固定进程顺序以确定性重现竞态。"""
    def __init__(self, iterator, fail_after=None, fail_before=False):
        self.iterator = iterator
        self.fail_after = fail_after
        self.fail_before = fail_before

    def __enter__(self):
        self.iterator.__enter__()
        return self

    def __exit__(self, *args):
        return self.iterator.__exit__(*args)

    def __iter__(self):
        if self.fail_before:
            raise PermissionError("开始读取时权限变化")
        for index, entry in enumerate(sorted(self.iterator, key=lambda entry: entry.name)):
            yield entry
            if index == self.fail_after:
                raise PermissionError("读取期间权限变化")


class DeletedExtraTests(unittest.TestCase):
    setUp = baseline.DeletedTests.setUp
    hold_file = baseline.DeletedTests.hold_file
    add_process = baseline.DeletedTests.add_process
    collect = baseline.DeletedTests.collect

    def test_exited_process_error_does_not_hide_later_readable_process(self):
        ended = self.add_process(101, []) / "fd"
        handle = self.hold_file()
        self.add_process(102, [handle])
        original = os.scandir

        def exited(path):
            if os.fspath(path) == str(ended):
                raise ProcessLookupError(errno.ESRCH, "进程已经退出")
            return OrderedScan(original(path))

        with mock.patch.object(deleted.os, "scandir", side_effect=exited):
            report = self.collect()
        self.assertEqual(report["数据"]["文件数"], 1)
        self.assertEqual(report["数据"]["已分配字节"], os.fstat(handle.fileno()).st_blocks * 512)

    def test_process_io_error_preserves_other_processes_and_marks_partial(self):
        unreadable = self.add_process(101, []) / "fd"
        self.add_process(102, [self.hold_file()])
        original = os.scandir

        def io_failure(path):
            if os.fspath(path) == str(unreadable):
                raise OSError(errno.EIO, "读取异常")
            return OrderedScan(original(path))

        with mock.patch.object(deleted.os, "scandir", side_effect=io_failure):
            report = self.collect()
        self.assertEqual(report["数据"]["文件数"], 1)
        self.assertEqual(report["状态"], "部分完成")

    def test_fd_enumeration_loses_permission_but_later_process_is_still_checked(self):
        first = self.hold_file(name="第一份")
        second = self.hold_file(name="第二份")
        partial = self.add_process(101, [first]) / "fd"
        self.add_process(102, [second])
        original = os.scandir

        def unstable(path):
            return OrderedScan(original(path), fail_after=0 if os.fspath(path) == str(partial) else None)

        with mock.patch.object(deleted.os, "scandir", side_effect=unstable):
            report = self.collect()
        self.assertEqual(report["状态"], "部分完成")
        self.assertEqual(report["数据"]["文件数"], 2)

    def test_reused_descriptor_does_not_attach_another_files_path_to_old_size(self):
        old = self.hold_file(name="已删除的原文件")
        replacement = self.hold_file(name="未删除的新文件", unlink=False)
        self.add_process(101, [old])
        snapshots = iter([os.fstat(old.fileno()), os.fstat(replacement.fileno())])
        last = os.fstat(replacement.fileno())
        report = self.collect(fd_snapshot=lambda path: next(snapshots, last))
        self.assertEqual(report["数据"]["文件数"], 0)
        self.assertEqual(report["状态"], "部分完成")

    def test_size_change_during_metadata_read_uses_last_observation_and_marks_partial(self):
        handle = self.hold_file()
        self.add_process(101, [handle])
        original = deleted._path_label

        def truncated(path):
            handle.truncate(0)
            handle.flush()
            return original(path)

        with mock.patch.object(deleted, "_path_label", side_effect=truncated):
            report = self.collect()
        self.assertEqual(report["数据"]["已分配字节"], os.fstat(handle.fileno()).st_blocks * 512)
        self.assertEqual(report["数据"]["逻辑字节"], 0)
        self.assertEqual(report["状态"], "部分完成")

    def test_enumeration_denied_before_any_descriptor_does_not_claim_zero(self):
        process = self.add_process(101, [self.hold_file()]) / "fd"
        original = os.scandir

        def inaccessible(path):
            return OrderedScan(original(path), fail_before=os.fspath(path) == str(process))

        with mock.patch.object(deleted.os, "scandir", side_effect=inaccessible):
            report = self.collect()
        self.assertNotEqual(report["状态"], "完成")
        self.assertIsNone(report["数据"]["已分配字节"])

    def test_large_block_count_stays_exact_integer(self):
        handle = self.hold_file()
        self.add_process(101, [handle])
        real = os.fstat(handle.fileno())
        values = {name: getattr(real, name) for name in dir(real) if name.startswith("st_")}
        values["st_blocks"] = 2 ** 54 + 1
        values["st_size"] = 2 ** 63 - 1
        report = self.collect(fd_snapshot=SimpleNamespace(**values))
        self.assertEqual(report["数据"]["已分配字节"], 9223372036854776320)
        self.assertIsInstance(report["数据"]["已分配字节"], int)

    def test_fifo_is_not_reported_as_deleted_regular_disk_file(self):
        path = self.root / "管道"
        os.mkfifo(path)
        descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        self.addCleanup(os.close, descriptor)
        path.unlink()
        self.add_process(101, [])
        os.symlink(f"/dev/fd/{descriptor}", self.proc / "101" / "fd" / "0")
        report = self.collect()
        self.assertEqual(report["数据"]["文件数"], 0)
        self.assertEqual(report["数据"]["已分配字节"], 0)


if __name__ == "__main__":
    unittest.main()
