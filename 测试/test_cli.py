"""命令入口与报告保存的行为测试，不连接真实业务服务。"""
import contextlib
import errno
import importlib
import io
import json
import multiprocessing.connection
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "代码"))


def complete(path, timeout):
    return {"项目": "测试项目", "状态": "完成", "范围": path, "数据": {"可用字节": 4096}, "说明": []}


def hanging(path, timeout):
    time.sleep(15)


class CliTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue((ROOT / "代码/space_report/cli.py").is_file(), "尚未实现报告命令")
        self.mod = importlib.import_module("space_report.cli")
        parent = ROOT / "测试环境/临时"
        parent.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=parent)
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)

    def invoke(self, args, collectors=None):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), patch.object(self.mod.sys, "platform", "linux"):
            code = self.mod.main(args, collectors=collectors or [("分区容量", complete)])
        return code, out.getvalue(), err.getvalue()

    def test_default_report_does_not_create_files(self):
        before = sorted(self.path.iterdir())
        code, out, err = self.invoke(["--path", str(self.path)])
        self.assertEqual(code, 0)
        self.assertIn("服务器空间去哪了", out)
        self.assertIn("4.00 KiB", out)
        self.assertEqual(sorted(self.path.iterdir()), before)

    def test_export_has_two_readable_reports_without_overwrite(self):
        destination = self.path / "报告"
        for _ in range(2):
            code, out, err = self.invoke(["--path", str(self.path), "--output", str(destination)])
            self.assertEqual(code, 0, err)
        folders = list(destination.iterdir())
        self.assertEqual(len(folders), 2)
        for folder in folders:
            data = json.loads((folder / "报告.json").read_text())
            self.assertEqual(data["报告格式版本"], 1)
            self.assertEqual(data["检查结果"][0]["数据"]["可用字节"], 4096)
            self.assertIn("4.00 KiB", (folder / "报告.txt").read_text())
            self.assertEqual(folder.stat().st_mode & 0o777, 0o700)
            self.assertEqual((folder / "报告.json").stat().st_mode & 0o777, 0o600)

    def test_output_failure_is_explicit_and_not_success(self):
        destination = self.path / "已有文件"
        destination.write_text("保留我")
        code, out, err = self.invoke(["--path", str(self.path), "--output", str(destination)])
        self.assertEqual(code, 2)
        self.assertIn("保存失败", err)
        self.assertNotIn("报告已保存", out)
        self.assertEqual(destination.read_text(), "保留我")

    def test_report_directory_swap_never_redirects_writes(self):
        # 保存目录在创建后被替换时，不能把两份报告写到替换的目录。
        destination = self.path / "输出"
        outside = self.path / "其他资料"
        outside.mkdir()
        original_open = os.open
        swapped = False
        def swap_then_open(name, flags, *args, **kwargs):
            nonlocal swapped
            if Path(name).name == "报告.txt" and not swapped:
                folder = next(destination.glob("空间报告-*"))
                folder.rename(destination / "原始目录")
                folder.symlink_to(outside, target_is_directory=True)
                swapped = True
            return original_open(name, flags, *args, **kwargs)
        with patch.object(self.mod.os, "open", side_effect=swap_then_open):
            code, out, err = self.invoke(["--path", str(self.path), "--output", str(destination)])
        self.assertEqual(list(outside.iterdir()), [], "不能沿被交换的父目录写报告")
        self.assertEqual(code, 2)
        self.assertNotIn("报告已保存", out)

    def test_report_output_symlink_ancestor_is_rejected(self):
        actual = self.path / "实际目录"
        actual.mkdir()
        shortcut = self.path / "目录链接"
        shortcut.symlink_to(actual, target_is_directory=True)
        code, out, err = self.invoke(["--path", str(self.path), "--output", str(shortcut / "不能创建")])
        self.assertEqual(code, 2)
        self.assertEqual(list(actual.iterdir()), [])

    def test_invalid_arguments_do_not_start_checks(self):
        for args in [["--timeout", "0"], ["--timeout", "nan"], ["--timeout", "inf"], ["--timeout", "-1"], ["--path", str(self.path / "不存在")]]:
            with self.subTest(args=args):
                code, out, err = self.invoke(args)
                self.assertEqual(code, 2)
                self.assertNotIn("测试项目", out)

    def test_unsupported_os_does_not_scan(self):
        with patch.object(self.mod.sys, "platform", "darwin"), contextlib.redirect_stderr(io.StringIO()) as err:
            code = self.mod.main([], collectors=[("分区容量", complete)])
        self.assertEqual(code, 2)
        self.assertIn("Linux", err.getvalue())

    def test_partial_check_is_not_reported_as_complete(self):
        def denied(path, timeout):
            return {"项目": "已删除文件", "状态": "权限不足", "范围": path, "数据": {"字节": None}, "说明": ["部分进程不可见"]}
        code, out, err = self.invoke(["--path", str(self.path)], [("已删除文件", denied)])
        self.assertEqual(code, 1)
        self.assertIn("未取得", out)
        self.assertIn("权限不足", out)

    def test_terminal_control_characters_are_visible_not_executed(self):
        def unsafe(path, timeout):
            return {"项目": "目录", "状态": "完成", "范围": path, "数据": {"路径": "危险\x1b[2J\n名字"}, "说明": []}
        code, out, err = self.invoke(["--path", str(self.path)], [("目录", unsafe)])
        self.assertNotIn("\x1b", out)
        self.assertIn("\\x1b", out)

    def test_hung_collector_has_outer_deadline(self):
        before = time.monotonic()
        result = self.mod.run_bounded("卡住的检查", hanging, str(self.path), .1)
        self.assertLess(time.monotonic() - before, 2)
        self.assertEqual(result["状态"], "超时")

    def test_process_start_failure_is_reported_without_aborting_other_checks(self):
        with patch("multiprocessing.process.BaseProcess.start", side_effect=OSError(errno.EAGAIN, "临时资源不足")):
            code, out, err = self.invoke(["--path", str(self.path)])
        self.assertEqual(code, 1)
        self.assertIn("失败", out)

    def test_real_file_descriptor_exhaustion_returns_failure_and_recovers(self):
        # 真正耗尽子进程的描述符额度，不能只模拟 fork 失败而遗漏 Pipe 创建失败。
        wrapper = (f"import errno,json,multiprocessing.connection,os,resource,sys; sys.path.insert(0,{str(ROOT / '代码')!r})\n"
                   "from space_report.cli import run_bounded\n"
                   "def complete(path, timeout): return {'状态':'完成'}\n"
                   "resource.setrlimit(resource.RLIMIT_NOFILE,(64,resource.getrlimit(resource.RLIMIT_NOFILE)[1]))\n"
                   "held=[]\n"
                   "try:\n"
                   " while True:\n"
                   "  try: held.append(os.open(os.devnull,os.O_RDONLY))\n"
                   "  except OSError as error:\n"
                   "   if error.errno!=errno.EMFILE: raise\n"
                   "   break\n"
                   " result=run_bounded('额度耗尽',complete,'.',.1)\n"
                   "finally:\n"
                   " for fd in held: os.close(fd)\n"
                   " recovered=run_bounded('额度恢复',complete,'.',1)\n"
                   "print(json.dumps([result['状态'],recovered['状态']]))\n")
        child = subprocess.run([sys.executable, "-B", "-c", wrapper],
                               capture_output=True, text=True, timeout=5)
        self.assertEqual(child.returncode, 0, child.stderr)
        self.assertEqual(json.loads(child.stdout), ["失败", "完成"])

    def test_report_keeps_non_utf8_name_bytes_as_json_escapes(self):
        name = os.fsdecode(b"raw-\xff-name")
        def unusual(path, timeout):
            return {"项目": "目录", "状态": "完成", "范围": path,
                    "数据": {"路径": name}, "说明": []}
        destination = self.path / "字节名字报告"
        code, out, err = self.invoke(["--path", str(self.path), "--output", str(destination)], [("目录", unusual)])
        self.assertEqual(code, 0, err)
        data = json.loads(next(destination.glob("*/报告.json")).read_text(encoding="utf-8"))
        self.assertEqual(os.fsencode(data["检查结果"][0]["数据"]["路径"]), b"raw-\xff-name")

    def test_large_finite_timeout_does_not_overflow_system_wait(self):
        # 合法的有限正数不能让底层等待毫秒数溢出并产生未处理异常。
        code, out, err = self.invoke(["--path", str(self.path), "--timeout", "1e300"])
        self.assertEqual(code, 0, err)
        self.assertIn("完成", out)

    def test_large_result_does_not_deadlock_pipe(self):
        def many(path, timeout):
            result = complete(path, timeout)
            result["数据"] = {"行": ["普通名称" * 100 for _ in range(2000)]}
            return result
        result = self.mod.run_bounded("大量结果", many, str(self.path), 2)
        self.assertEqual(result["状态"], "完成")
        self.assertEqual(len(result["数据"]["行"]), 2000)

    def test_partial_pipe_message_cannot_bypass_collection_deadline(self):
        original_send = multiprocessing.connection.Connection._send
        def slow_body(connection, buffer, *args, **kwargs):
            if len(buffer) > 16384:
                time.sleep(1.2)
            return original_send(connection, buffer, *args, **kwargs)
        def many(path, timeout):
            result = complete(path, timeout)
            result["数据"] = {"合成内容": "x" * 65536}
            return result
        started = time.monotonic()
        with patch.object(multiprocessing.connection.Connection, "_send", slow_body):
            result = self.mod.run_bounded("慢传输", many, str(self.path), .05)
        self.assertLess(time.monotonic() - started, .8)
        self.assertEqual(result["状态"], "超时")

    def test_transfer_deadline_does_not_round_up_to_next_timer_tick(self):
        original_send = multiprocessing.connection.Connection._send
        def slow_body(connection, buffer, *args, **kwargs):
            if len(buffer) > 16384:
                time.sleep(2.2)
            return original_send(connection, buffer, *args, **kwargs)
        def many(path, timeout):
            result = complete(path, timeout)
            result["数据"] = {"内容": "x" * 65536}
            return result
        started = time.monotonic()
        with patch.object(multiprocessing.connection.Connection, "_send", slow_body):
            result = self.mod.run_bounded("非整数秒传输", many, str(self.path), 1.1)
        self.assertLess(time.monotonic() - started, 1.7)
        self.assertEqual(result["状态"], "超时")

    def test_cancel_saves_only_finished_checks(self):
        destination = self.path / "取消报告"
        out, err = io.StringIO(), io.StringIO()
        with patch.object(self.mod.sys, "platform", "linux"), patch.object(self.mod, "run_bounded", side_effect=[complete(str(self.path), 1), KeyboardInterrupt()]), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = self.mod.main(["--path", str(self.path), "--output", str(destination)], collectors=[("已完成", complete), ("尚未完成", complete)])
        self.assertEqual(code, 130)
        data = json.loads(next(destination.glob("*/报告.json")).read_text())
        self.assertTrue(data["已取消"])
        self.assertEqual(len(data["检查结果"]), 1)

    def test_symlink_target_is_rejected_without_scan(self):
        target = self.path / "实际目录"
        target.mkdir()
        shortcut = self.path / "快捷目录"
        shortcut.symlink_to(target, target_is_directory=True)
        code, out, err = self.invoke(["--path", str(shortcut)])
        self.assertEqual(code, 2)
        self.assertNotIn("测试项目", out)

    def test_help_uses_chinese_usage_heading(self):
        result = subprocess.run([sys.executable, "-B", str(ROOT / "代码/空间去哪了.py"), "--help"], capture_output=True, text=True, timeout=3)
        self.assertEqual(result.returncode, 0)
        self.assertIn("用法", result.stdout)
        self.assertNotIn("usage:", result.stdout)

    def test_closed_output_pipe_exits_without_a_python_traceback(self):
        # 接收端主动关闭管道时，应停止输出，不打印 Python 异常堆栈。
        wrapper = (f"import runpy,sys; sys.stdin.read(1); sys.path.insert(0,{str(ROOT / '代码')!r}); "
                   f"sys.argv=[{str(ROOT / '代码/空间去哪了.py')!r},'--help']; runpy.run_path(sys.argv[0],run_name='__main__')")
        process = subprocess.Popen([sys.executable, "-B", "-c", wrapper],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        process.stdout.close()
        process.stdout = None
        _, error = process.communicate("x", timeout=5)
        self.assertEqual(process.returncode, 141)
        self.assertNotIn("Traceback", error)

    def test_terminal_group_interrupt_does_not_print_worker_traceback(self):
        # 终端 Ctrl+C 发给整个前台进程组，不能只测试父进程单独收到信号。
        wrapper = (f"import sys,time; sys.path.insert(0,{str(ROOT / '代码')!r}); "
                   "from space_report import cli; cli.sys.platform='linux'\n"
                   "def wait_for_interrupt(path, timeout):\n"
                   " print('worker-ready',flush=True)\n"
                   " time.sleep(20)\n"
                   f"raise SystemExit(cli.main(['--path',{str(self.path)!r}],collectors=[('等待取消',wait_for_interrupt)]))\n")
        process = subprocess.Popen([sys.executable, "-B", "-c", wrapper], stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True, start_new_session=True)
        try:
            import selectors
            watcher = selectors.DefaultSelector()
            watcher.register(process.stdout, selectors.EVENT_READ)
            received = ""
            deadline = time.monotonic() + 5
            # 直接读底层字节，避免 TextIOWrapper 缓冲使就绪通知与数据不同步。
            while "worker-ready" not in received and time.monotonic() < deadline:
                if watcher.select(.1):
                    chunk = os.read(process.stdout.fileno(), 65536)
                    if not chunk:
                        break
                    received += chunk.decode("utf-8", "replace")
            watcher.close()
            self.assertIn("worker-ready", received)
            os.killpg(process.pid, signal.SIGINT)
            _, error = process.communicate(timeout=5)
            self.assertEqual(process.returncode, 130)
            self.assertNotIn("Traceback", error)
            self.assertNotIn("KeyboardInterrupt", error)
        finally:
            if process.returncode is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.communicate(timeout=5)

    def test_repeated_terminal_interrupt_cannot_skip_worker_cleanup(self):
        # 一次取消作正对照；清理期间第二次取消不能让合成辅助命令继续执行。
        for count in (1, 2):
            with self.subTest(interrupts=count):
                folder = self.path / f"真实取消-{count}"
                folder.mkdir()
                helper = ("import pathlib,sys,time; folder=pathlib.Path(sys.argv[1]); "
                          "(folder/'已启动').write_text('合成进程'); time.sleep(1); "
                          "(folder/'仍执行').write_text('不应出现')")
                wrapper = (f"import signal,sys; sys.path.insert(0,{str(ROOT / '代码')!r})\n"
                           "from space_report.cli import run_bounded\n"
                           "from space_report.common import run_command\n"
                           "def collector(path, timeout):\n"
                           f" run_command([sys.executable,'-B','-c',{helper!r},path],3)\n"
                           " return {'状态':'完成'}\n"
                           "previous=signal.getsignal(signal.SIGINT)\n"
                           "try:\n"
                           f" run_bounded('真实取消',collector,{str(folder)!r},3)\n"
                           "except KeyboardInterrupt: print('取消已传回',flush=True)\n"
                           "assert signal.getsignal(signal.SIGINT)==previous, '未恢复原信号策略'\n")
                child = subprocess.Popen([sys.executable, "-B", "-c", wrapper],
                                         stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                         text=True, start_new_session=True)
                try:
                    deadline = time.monotonic() + 5
                    while not (folder / "已启动").exists() and time.monotonic() < deadline:
                        if child.poll() is not None:
                            break
                        time.sleep(.005)
                    self.assertTrue((folder / "已启动").exists(), "合成辅助命令未启动")
                    started = time.monotonic()
                    for index in range(count):
                        if index:
                            time.sleep(.01)
                        os.killpg(child.pid, signal.SIGINT)
                    out, err = child.communicate(timeout=5)
                    # 即使主进程已退出，仍给短寿命合成命令机会证明是否遗漏了清理。
                    time.sleep(max(0, 1.2 - (time.monotonic() - started)))
                    self.assertEqual(child.returncode, 0, err)
                    self.assertIn("取消已传回", out)
                    self.assertNotIn("Traceback", err)
                    self.assertFalse((folder / "仍执行").exists(), "取消清理被再次中断，辅助命令仍执行")
                finally:
                    if child.poll() is None:
                        os.killpg(child.pid, signal.SIGKILL)
                        child.communicate(timeout=3)

    def test_first_interrupt_during_cleanup_is_delivered_after_child_reaping(self):
        # 只固定信号到达时机；实际创建、等待、终止和回收均使用真实进程。
        original_join = multiprocessing.process.BaseProcess.join
        previous_handler = signal.getsignal(signal.SIGINT)
        observed = []

        def interrupt_at_first_join(process, timeout=None):
            if not observed:
                observed.append((process.pid, process))
                os.kill(os.getpid(), signal.SIGINT)
            return original_join(process, timeout)

        try:
            with patch.object(multiprocessing.process.BaseProcess, "join", interrupt_at_first_join):
                with self.assertRaises(KeyboardInterrupt):
                    self.mod.run_bounded("清理时取消", hanging, str(self.path), .01)
            self.assertEqual(signal.getsignal(signal.SIGINT), previous_handler)
            self.assertEqual(len(observed), 1)
            with self.assertRaises(ChildProcessError):
                os.waitpid(observed[0][0], os.WNOHANG)
        finally:
            for _, process in observed:
                try:
                    if process.is_alive():
                        process.kill()
                        process.join(2)
                    process.close()
                except ValueError:
                    pass  # 生产清理已经关闭了进程对象。


if __name__ == "__main__":
    unittest.main()
