"""受控子进程与安全的终端文字；不继承凭据或远程连接配置。"""
import math
import os
import select
import selectors
import signal
import subprocess
import time
import unicodedata


class DirectoryTarget(str):
    """报告保留输入路径，采集进程继承已打开的目录描述符。"""

    def __new__(cls, path, descriptor):
        target = super().__new__(cls, path)
        target.descriptor = descriptor
        return target

    def __reduce__(self):
        # 检查结果通过管道传回时只传路径文字，描述符由父进程持有。
        return str, (str(self),)


def open_directory(path, *, metadata=False):
    """逐层打开目录，不跟随任一层符号链接；已选目标从描述符重新打开。"""
    traversal = getattr(os, "O_PATH", os.O_RDONLY) | os.O_DIRECTORY | os.O_NOFOLLOW
    flags = traversal if metadata else os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    if isinstance(path, DirectoryTarget):
        return os.open(".", flags, dir_fd=path.descriptor)
    parts = os.path.abspath(path).split(os.sep)
    directory = os.open(os.sep, traversal)
    try:
        for part in filter(None, parts):
            child = os.open(part, traversal, dir_fd=directory)
            os.close(directory)
            directory = child
        return os.open(".", flags, dir_fd=directory)
    finally:
        os.close(directory)


def target_is_current(path):
    if not isinstance(path, DirectoryTarget):
        return True
    try:
        descriptor = open_directory(str(path), metadata=True)
        try:
            return os.path.samestat(os.fstat(descriptor), os.fstat(path.descriptor))
        finally:
            os.close(descriptor)
    except OSError:
        return False


class OutputLimitError(RuntimeError):
    """辅助命令输出超过允许上限。"""


def safe_text(value):
    """将换行、终端控制符和双向文字控制符显示为可见转义。"""
    output = []
    for char in str(value):
        if unicodedata.category(char) in {"Cc", "Cf", "Cs"}:
            number = ord(char)
            output.append(f"\\u{number:04x}" if number > 255 else f"\\x{number:02x}")
        else:
            output.append(char)
    return "".join(output)


def run_command(argv, timeout, env=None, *, max_output=8 * 1024 * 1024):
    """执行参数列表，限时、限输出；只终止本函数创建的子进程组。"""
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("超时必须为有限正数")
    if not isinstance(argv, (list, tuple)) or not argv:
        raise ValueError("命令必须是非空参数列表")
    if signal.getsignal(signal.SIGCHLD) != signal.SIG_DFL:
        raise RuntimeError("当前进程存在外部子进程回收策略，不能安全保持进程组身份")
    use_waitid = all(hasattr(os, name) for name in ("waitid", "P_PID", "WEXITED", "WNOHANG", "WNOWAIT"))
    if not use_waitid and not hasattr(select, "kqueue"):
        raise RuntimeError("当前系统不支持保留进程身份的退出检查")
    controlled = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C", "LANG": "C",
                  "PYTHONIOENCODING": "utf-8", "PYTHONDONTWRITEBYTECODE": "1"}
    for key, value in (env or {}).items():
        if key in {"LC_ALL", "LANG", "DOCKER_CONFIG", "DOCKER_HOST"}:
            controlled[key] = value
    selector = selectors.DefaultSelector()
    process = watcher = None
    chunks = {"stdout": bytearray(), "stderr": bytearray()}
    deadline = time.monotonic() + timeout
    total = 0
    exited = group_stopped = False
    try:
        process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, env=controlled, shell=False,
                                   start_new_session=True)
        if not use_waitid:
            watcher = select.kqueue()
            watcher.control([select.kevent(process.pid, filter=select.KQ_FILTER_PROC,
                            flags=select.KQ_EV_ADD | select.KQ_EV_ONESHOT,
                            fflags=select.KQ_NOTE_EXIT)], 0, 0)
        for name in chunks:
            stream = getattr(process, name)
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ, name)
        while True:
            if not exited:
                if use_waitid:
                    status = os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
                    exited = status is not None and status.si_pid == process.pid
                else:
                    exited = bool(watcher.control(None, 1, 0))
                if exited:
                    # 组长仍是未回收的子进程，PID 不会被复用；先结束同组后代。
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    except PermissionError:
                        # macOS 对只剩退出组长的组返回 EPERM；Linux 不使用此兼容分支。
                        if use_waitid:
                            raise
                    group_stopped = True
            if exited and not selector.get_map():
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(argv, timeout,
                    output=chunks["stdout"].decode("utf-8", "replace"),
                    stderr=chunks["stderr"].decode("utf-8", "replace"))
            for key, _ in selector.select(min(remaining, .1)):
                block = os.read(key.fileobj.fileno(), 65536)
                if not block:
                    selector.unregister(key.fileobj)
                    continue
                total += len(block)
                if total > max_output:
                    raise OutputLimitError("辅助命令输出过多，已停止该项检查")
                chunks[key.data].extend(block)
        return subprocess.CompletedProcess(argv, process.wait(),
            chunks["stdout"].decode("utf-8", "replace"), chunks["stderr"].decode("utf-8", "replace"))
    finally:
        if watcher is not None:
            watcher.close()
        selector.close()
        if process is not None:
            # 异常/取消时也先清理进程组，再回收组长；不向已回收的编号发信号。
            if not group_stopped and process.returncode is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            process.wait()
            process.stdout.close()
            process.stderr.close()
