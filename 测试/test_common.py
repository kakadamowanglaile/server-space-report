"""真实子进程验证：防止泄露继承环境、无限等待及过量输出。"""
import importlib
import os
from pathlib import Path
import subprocess
import sys
import time
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "代码"))


class CommandTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue((ROOT / "代码/space_report/common.py").is_file(), "尚未实现受控命令执行")
        self.mod = importlib.import_module("space_report.common")

    def test_real_stdout_and_nonzero_exit_are_preserved(self):
        result = self.mod.run_command([sys.executable, "-c", "import sys; print('中文'); print('错误',file=sys.stderr); sys.exit(3)"], 2)
        self.assertEqual(result.returncode, 3)
        self.assertEqual(result.stdout, "中文\n")
        self.assertEqual(result.stderr, "错误\n")

    def test_parent_secrets_and_docker_context_not_inherited(self):
        from unittest.mock import patch
        with patch.dict(os.environ, {"DOCKER_HOST": "ssh://secret", "TOKEN": "秘密"}):
            result = self.mod.run_command([sys.executable, "-c", "import os,json; print(json.dumps(dict(os.environ)))"], 2)
        self.assertNotIn("秘密", result.stdout)
        self.assertNotIn("ssh://secret", result.stdout)

    def test_no_shell_interpolation(self):
        value = "$(不要执行);特殊\n名字"
        result = self.mod.run_command([sys.executable, "-c", "import sys; print(sys.argv[1])", value], 2)
        self.assertEqual(result.stdout, value + "\n")

    def test_timeout_stops_child_and_keeps_output(self):
        before = time.monotonic()
        with self.assertRaises(subprocess.TimeoutExpired) as ctx:
            self.mod.run_command([sys.executable, "-c", "import time; print('已经输出',flush=True); time.sleep(10)"], .2)
        self.assertLess(time.monotonic() - before, 2)
        self.assertIn("已经输出", ctx.exception.stdout)

    def test_successful_command_cannot_leave_same_group_descendants_running(self):
        parent = ROOT / "测试环境/临时"
        parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=parent) as folder:
            marker = Path(folder) / "后代仍执行"
            child = f"import pathlib,time; time.sleep(.35); pathlib.Path({str(marker)!r}).write_text('存在')"
            leader = ("import subprocess,sys; "
                      f"subprocess.Popen([sys.executable,'-c',{child!r}], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL); print('完成')")
            result = self.mod.run_command([sys.executable, "-c", leader], 2)
            self.assertEqual(result.returncode, 0)
            time.sleep(.7)
            self.assertFalse(marker.exists(), "辅助命令返回后不能留有同组后代继续执行")

    def test_exited_leader_with_descendant_holding_pipes_keeps_original_exit(self):
        # 同组后代继承输出管道，不能迫使已经退出的组长被错误报告为超时。
        leader = ("import subprocess,sys; "
                  "subprocess.Popen([sys.executable,'-c','import time; time.sleep(.5)']); "
                  "print('原始输出'); sys.exit(7)")
        started = time.monotonic()
        result = self.mod.run_command([sys.executable, "-c", leader], .3)
        self.assertEqual(result.returncode, 7)
        self.assertEqual(result.stdout, "原始输出\n")
        self.assertLess(time.monotonic() - started, .4)

    def test_closed_pipes_do_not_prematurely_kill_a_live_leader(self):
        result = self.mod.run_command([sys.executable, "-c",
            "import os,time,sys; os.close(1); os.close(2); time.sleep(.15); sys.exit(11)"], 2)
        self.assertEqual(result.returncode, 11)

    def test_missing_tool_is_not_success(self):
        with self.assertRaises(FileNotFoundError):
            self.mod.run_command(["/不存在的工具-空间报告"], 1)

    def test_output_has_memory_bound(self):
        with self.assertRaises(self.mod.OutputLimitError):
            self.mod.run_command([sys.executable, "-c", "print('x'*100000)"], 2, max_output=4096)

    def test_invalid_timeout_rejected_before_start(self):
        for value in [0, -1, float('nan'), float('inf')]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.mod.run_command([sys.executable, "-c", "pass"], value)

    def test_external_child_reaping_policy_is_rejected_before_launch(self):
        # 自动/外部回收会使 PID 无法一直归本函数持有；不能再安全清理进程组。
        import signal
        from unittest.mock import patch
        previous = signal.getsignal(signal.SIGCHLD)
        try:
            signal.signal(signal.SIGCHLD, signal.SIG_IGN)
            with patch.object(self.mod.subprocess, "Popen", side_effect=AssertionError("不应启动子进程")):
                with self.assertRaises(RuntimeError):
                    self.mod.run_command([sys.executable, "-c", "pass"], 1)
        finally:
            signal.signal(signal.SIGCHLD, previous)

    def test_never_signals_a_reaped_process_group(self):
        # 如果在 wait/poll 已回收进程后仍按旧 PID 发信号，本测试会失败。
        from unittest.mock import patch
        original_popen, original_killpg = subprocess.Popen, os.killpg
        created = []
        def launch(*args, **kwargs):
            child = original_popen(*args, **kwargs)
            created.append(child)
            return child
        def guarded_signal(pid, signum):
            self.assertIsNone(created[0].returncode, "已回收的 PID 不能继续作为进程组身份")
            return original_killpg(pid, signum)
        with patch.object(self.mod.subprocess, "Popen", side_effect=launch), patch.object(self.mod.os, "killpg", side_effect=guarded_signal):
            result = self.mod.run_command([sys.executable, "-c", "print('正常结束')"], 2)
        self.assertEqual(result.returncode, 0)

    def test_formatting_does_not_emit_terminal_controls(self):
        self.assertEqual(self.mod.safe_text("普通中文"), "普通中文")
        rendered = self.mod.safe_text("假\x1b[2J\n报告\r\u202e")
        self.assertNotIn("\x1b", rendered)
        self.assertNotIn("\n", rendered)
        self.assertNotIn("\r", rendered)
        self.assertNotIn("\u202e", rendered)


if __name__ == "__main__":
    unittest.main()
