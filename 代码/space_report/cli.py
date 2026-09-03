"""中文命令入口、隔离限时与报告保存。"""
import argparse
from datetime import datetime, timezone
import json
import math
import multiprocessing
import os
from pathlib import Path
import platform
import signal
import sys
import time
import uuid

from . import __version__
from .common import DirectoryTarget, open_directory, safe_text, target_is_current


GOOD = {"完成", "未启用", "不适用"}
NOTICE = "各项范围可能重叠，请勿相加。报告不代表可以安全删除；权限不足或未检查不等于没有占用。"


def empty_result(name, status, path, note):
    return {"项目": name, "状态": status, "范围": path, "数据": {}, "说明": [note]}


def _worker(connection, collector, name, path, timeout):
    def expired(signum, frame):
        raise TimeoutError("外部时间上限已到")
    signal.signal(signal.SIGTERM, expired)
    # 终端信号由父进程统一处理，避免整组 Ctrl+C 让采集进程打印异常堆栈。
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    try:
        try:
            if not target_is_current(path):
                result = empty_result(name, "失败", str(path), "选定目录的路径已经变化，未执行该项检查。")
            else:
                result = collector(path, timeout)
                if not target_is_current(path):
                    if result["状态"] == "完成":
                        result["状态"] = "部分完成"
                    result["说明"].append("检查期间选定目录的路径发生变化；数据属于原先选定的目录。")
        except TimeoutError:
            result = empty_result(name, "超时", path, "该项未能在时间上限内完成。")
        except PermissionError:
            result = empty_result(name, "权限不足", path, "当前用户无权完成该项检查。")
        except Exception as error:
            result = empty_result(name, "失败", path, f"{type(error).__name__}：{safe_text(error)[:400]}")
        connection.send(result)
    except (BrokenPipeError, EOFError, TimeoutError):
        pass
    finally:
        connection.close()


def _receive_until(connection, deadline):
    """管道可读可能只有消息头；完整接收仍使用同一个截止时间。"""
    previous_handler = signal.getsignal(signal.SIGALRM)
    previous_timer = signal.getitimer(signal.ITIMER_REAL)
    started = time.monotonic()
    def expired(signum, frame):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("结果传输达到时间上限")
        signal.setitimer(signal.ITIMER_REAL, min(remaining, 1.0))
    signal.signal(signal.SIGALRM, expired)
    try:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("结果传输达到时间上限")
        # 周期检查避免极大的合法时间使系统计时器溢出。
        signal.setitimer(signal.ITIMER_REAL, min(remaining, 1.0))
        return connection.recv()
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)
        if previous_timer[0] > 0:
            signal.setitimer(signal.ITIMER_REAL,
                             max(.000001, previous_timer[0] - (time.monotonic() - started)),
                             previous_timer[1])


def run_bounded(name, collector, path, timeout):
    """在独立进程运行采集；无法自行结束的系统读取也不阻塞整份报告。"""
    receiver = sender = process = None
    previous_handler = signal.getsignal(signal.SIGINT)
    phase = "启动"
    cancelled = False
    pending_interrupt = None

    def deliver_interrupt(signum, frame):
        if callable(previous_handler):
            previous_handler(signum, frame)
        elif previous_handler == signal.SIG_DFL:
            signal.signal(signal.SIGINT, signal.SIG_DFL)
            signal.raise_signal(signal.SIGINT)

    def interrupt(signum, frame):
        nonlocal cancelled, pending_interrupt
        if previous_handler == signal.SIG_IGN:
            return
        if phase != "等待" or cancelled:
            # 先完成进程归属登记或清理；重复取消不能中断 terminate/kill/join。
            pending_interrupt = (signum, frame)
            return
        cancelled = True
        deliver_interrupt(signum, frame)

    signal.signal(signal.SIGINT, interrupt)
    try:
        try:
            context = multiprocessing.get_context("fork")
            receiver, sender = context.Pipe(duplex=False)
            process = context.Process(target=_worker, args=(sender, collector, name, path, timeout))
            process.start()
        except OSError as error:
            return empty_result(name, "失败", path, f"无法启动该项检查：{safe_text(error)}")
        sender.close()
        phase = "等待"
        if pending_interrupt is not None:
            interrupt(*pending_interrupt)
        # 给采集器返回已收集的部分结果留出很短的交接时间。
        deadline = time.monotonic() + timeout + .25
        while (remaining := deadline - time.monotonic()) > 0:
            # 分段等待，避免合法但极大的有限时间在系统毫秒计数中溢出。
            if receiver.poll(min(remaining, 1.0)):
                try:
                    return _receive_until(receiver, deadline)
                except EOFError:
                    return empty_result(name, "失败", path, "检查进程退出，未取得有效结果。")
                except TimeoutError:
                    return empty_result(name, "超时", path, "结果未能在时间上限内完整传回；其他已完成项目保留。")
        return empty_result(name, "超时", path, "该项系统读取未能结束，已停止等待；其他已完成项目保留。")
    finally:
        phase = "清理"
        try:
            if receiver is not None:
                receiver.close()
            if sender is not None:
                sender.close()
            if process is not None:
                if process.pid is not None:
                    process.join(.05)
                    if process.is_alive():
                        process.terminate()
                        process.join(.25)
                    if process.is_alive():
                        process.kill()
                        process.join(.25)
                if not process.is_alive():
                    process.close()
        finally:
            signal.signal(signal.SIGINT, previous_handler)
            if pending_interrupt is not None and not cancelled:
                # 首次取消若在正常清理阶段到达，仍须交还调用方处理。
                deliver_interrupt(*pending_interrupt)


def human_bytes(value):
    number = float(value)
    for unit in ["B", "KiB", "MiB", "GiB", "TiB", "PiB"]:
        if abs(number) < 1024 or unit == "PiB":
            return f"{number:.2f} {unit}（{value:,} 字节）"
        number /= 1024


def _lines(value, indent=0, label=""):
    prefix = "  " * indent
    title = safe_text(label)
    if isinstance(value, dict):
        output = [f"{prefix}{title}："] if title else []
        for key, item in value.items():
            output.extend(_lines(item, indent + bool(title), key))
        return output
    if isinstance(value, list):
        output = [f"{prefix}{title}："]
        if not value:
            output.append(f"{prefix}  无已记录条目（完整性见检查状态）")
        for index, item in enumerate(value[:20], 1):
            output.extend(_lines(item, indent + 1, str(index)))
        if len(value) > 20:
            output.append(f"{prefix}  另有 {len(value)-20} 项，保存结构化报告查看全部已采集记录。")
        return output
    if value is None:
        rendered = "未取得"
    elif isinstance(value, bool):
        rendered = "是" if value else "否"
    elif isinstance(value, int) and "字节" in title:
        rendered = human_bytes(value)
    else:
        rendered = safe_text(value)
    return [f"{prefix}{title}：{rendered}"]


def render_section(section):
    lines = [f"[{safe_text(section['状态'])}] {safe_text(section['项目'])}",
             f"检查范围：{safe_text(section['范围'])}"]
    lines.extend(_lines(section.get("数据", {})))
    lines.extend(f"说明：{safe_text(note)}" for note in section.get("说明", []))
    return "\n".join(lines)


def render_report(report):
    lines = [f"服务器空间去哪了 {report['工具版本']}",
             f"采集开始：{report['开始时间']}",
             f"目标目录：{safe_text(report['目标目录'])}", NOTICE, ""]
    for section in report["检查结果"]:
        lines.extend([render_section(section), ""])
    if report.get("已取消"):
        lines.append("用户已取消，报告仅包含取消前得到的结果。")
    lines.append(f"采集结束：{report['结束时间']}；总用时 {report['用时秒']:.2f} 秒")
    lines.append("分享前请检查路径、进程名和容器名是否包含敏感信息。")
    return "\n".join(lines) + "\n"


def _open_output_root(root):
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    directory = os.open(root.anchor, flags)
    try:
        for part in root.parts[1:]:
            try:
                os.mkdir(part, mode=0o700, dir_fd=directory)
            except FileExistsError:
                pass
            child = os.open(part, flags, dir_fd=directory)
            os.close(directory)
            directory = child
        return directory
    except BaseException:
        os.close(directory)
        raise


def save_report(report, destination):
    root = Path(os.path.abspath(destination))
    payloads = {"报告.txt": render_report(report),
                "报告.json": json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n"}
    directory = _open_output_root(root)
    folder_fd = None
    try:
        name = "空间报告-" + datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:12]
        folder = root / name
        os.mkdir(name, mode=0o700, dir_fd=directory)
        folder_fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
        identity = os.fstat(folder_fd)
        if identity.st_uid != os.geteuid() or identity.st_mode & 0o077:
            raise PermissionError("新报告目录的归属或权限不符合要求")
        for name, payload in payloads.items():
            fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         0o600, dir_fd=folder_fd)
            with os.fdopen(fd, "w", encoding="utf-8", errors="backslashreplace") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
        os.fsync(folder_fd)
        os.fsync(directory)
        if (not os.path.samestat(folder.lstat(), identity)
                or not os.path.samestat(root.lstat(), os.fstat(directory))):
            raise OSError("保存期间目录发生变化；报告可能保留在原目录，未宣称路径有效")
        return folder
    finally:
        if folder_fd is not None:
            os.close(folder_fd)
        os.close(directory)


class ChineseParser(argparse.ArgumentParser):
    def error(self, message):
        message = message.replace("unrecognized arguments:", "无法识别参数：").replace("expected one argument", "需要提供一个值").replace("argument ", "参数 ")
        raise ValueError(f"参数有误：{message}。使用 --help 查看中文说明。")


class ChineseHelpFormatter(argparse.HelpFormatter):
    def add_usage(self, usage, actions, groups, prefix=None):
        return super().add_usage(usage, actions, groups, prefix="用法：")


def _timeout(value):
    try:
        number = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError("超时必须是秒数") from None
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("超时必须是有限正数")
    return number


def parser():
    result = ChineseParser(prog="空间去哪了", add_help=False, usage="%(prog)s [参数]", formatter_class=ChineseHelpFormatter,
                           description="一次运行的 Linux 本机只读空间报告；不删除、不重启、不上传。")
    result._optionals.title = "参数"
    result.add_argument("-h", "--help", action="help", help="显示本说明")
    result.add_argument("--version", action="version", version=f"服务器空间去哪了 {__version__}", help="显示版本")
    result.add_argument("--path", default="/", metavar="目录", help="检查目录及其所在分区，默认 /，不接受符号链接目录")
    result.add_argument("--deep", action="store_true", help="遍历指定目录，显示前 20 个大目录；可能产生磁盘读取负载")
    result.add_argument("--timeout", default=30.0, type=_timeout, metavar="秒数", help="每项检查上限，默认 30 秒")
    result.add_argument("--output", metavar="保存目录", help="保存中文文本和结构化报告；默认只显示，不覆盖已有报告")
    return result


def default_collectors(deep):
    from .filesystem import collect_partition, collect_directories
    from .deleted import collect_deleted
    from .services import collect_journal, collect_docker
    return [("分区容量", collect_partition)] + ([("大目录排行", collect_directories)] if deep else []) + [
        ("系统日志", collect_journal), ("Docker 占用", collect_docker), ("已删除仍占用", collect_deleted)]


def _main(argv=None, *, collectors=None):
    descriptor = None
    try:
        args = parser().parse_args(argv)
        if sys.platform != "linux":
            print("当前系统不受支持：第一版仅检查 Linux 主机；未执行扫描。", file=sys.stderr)
            return 2
        candidate = Path(os.path.abspath(args.path))
        if candidate.is_symlink() or any(parent.is_symlink() for parent in candidate.parents):
            raise ValueError("目标路径不能经过符号链接；请直接指定真实目录")
        if not candidate.is_dir():
            raise ValueError("目标目录不存在、不可访问或不是目录")
        descriptor = open_directory(str(candidate), metadata=True)
    except (ValueError, OSError) as error:
        print(safe_text(error), file=sys.stderr)
        return 2
    try:
        return _run_report(args, DirectoryTarget(str(candidate), descriptor), collectors)
    finally:
        os.close(descriptor)


def _run_report(args, candidate, collectors):
    started = time.monotonic()
    report = {"报告格式版本": 1, "工具版本": __version__,
              "开始时间": datetime.now(timezone.utc).isoformat(), "目标目录": str(candidate),
              "系统": {"类型": "Linux", "内核": platform.release(), "架构": platform.machine()},
              "深度扫描": args.deep, "单项超时秒": args.timeout, "检查结果": [], "已取消": False}
    tasks = default_collectors(args.deep) if collectors is None else collectors
    print(f"服务器空间去哪了 {__version__}\n{NOTICE}\n", flush=True)
    try:
        for name, collector in tasks:
            print(f"正在检查：{name}……", file=sys.stderr, flush=True)
            section = run_bounded(name, collector, candidate, args.timeout)
            report["检查结果"].append(section)
            print(render_section(section) + "\n", flush=True)
        if not args.deep and collectors is None:
            section = empty_result("大目录排行", "未启用", str(candidate), "使用 --deep 主动开启目录遍历；未扫描不代表没有大文件。")
            report["检查结果"].append(section)
            print(render_section(section) + "\n", flush=True)
    except KeyboardInterrupt:
        report["已取消"] = True
        print("已取消；保留已完成项目，不继续检查。", file=sys.stderr)
    report["结束时间"] = datetime.now(timezone.utc).isoformat()
    report["用时秒"] = round(time.monotonic() - started, 3)
    print("分享前请检查路径、进程名和容器名是否包含敏感信息。", flush=True)
    if args.output:
        try:
            folder = save_report(report, args.output)
            print(f"报告已保存：{safe_text(folder)}", flush=True)
        except (OSError, ValueError) as error:
            print(f"报告保存失败：{safe_text(error)}。终端结果仍可查看；目标目录可能保留未写完整的文件。", file=sys.stderr)
            return 130 if report["已取消"] else 2
    if report["已取消"]:
        return 130
    return 0 if all(item["状态"] in GOOD for item in report["检查结果"]) else 1


def main(argv=None, *, collectors=None):
    try:
        try:
            return _main(argv, collectors=collectors)
        finally:
            # 帮助等短输出也可能缓存在解释器中，退出前仍需检查接收端。
            sys.stdout.flush()
    except BrokenPipeError:
        null_fd = os.open(os.devnull, os.O_WRONLY)
        try:
            for stream in (sys.stdout, sys.stderr):
                try:
                    os.dup2(null_fd, stream.fileno())
                except (OSError, ValueError):
                    pass
        finally:
            os.close(null_fd)
        return 141


if __name__ == "__main__":
    raise SystemExit(main())
