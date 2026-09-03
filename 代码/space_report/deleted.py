"""通过 Linux procfs 元数据检查已删除但仍被进程持有的普通文件。"""

import errno
import math
import os
import stat
import sys
import time

from .filesystem import _mount_for_path, _read_mountinfo


PROC_ROOT = "/proc"
_DISK_FILESYSTEMS = {"ext2", "ext3", "ext4", "xfs"}


def _report(path):
    return {"项目": "已删除仍占用文件", "状态": "完成",
            "范围": os.path.abspath(os.fspath(path)),
            "数据": {"文件列表": [], "文件数": None, "已分配字节": None,
                     "逻辑字节": None, "检查进程数": 0, "不可访问进程数": 0,
                     "变化条目数": 0},
            "说明": []}


def _deadline(timeout):
    seconds = float(timeout)
    if not math.isfinite(seconds):
        raise ValueError("超时秒数必须是有限数值")
    return time.monotonic() + max(0, seconds)


class _Timeout(Exception):
    pass


def _check_time(deadline):
    if time.monotonic() >= deadline:
        raise _Timeout()


def _read_comm(process_path):
    try:
        with open(os.path.join(process_path, "comm"), encoding="utf-8",
                  errors="replace") as handle:
            return handle.readline(256).rstrip("\r\n") or "未知"
    except FileNotFoundError:
        return "已退出"
    except PermissionError:
        return "当前权限不可见"
    except OSError:
        return "无法读取"


def _path_label(fd_path):
    try:
        label = os.readlink(fd_path)
    except (OSError, ValueError):
        return "当前权限不可见"
    suffix = " (deleted)"
    return label[:-len(suffix)] if label.endswith(suffix) else label


def collect_deleted(path, timeout=30):
    report = _report(path)
    data = report["数据"]
    records = {}
    measured = False
    target = report["范围"]
    try:
        deadline = _deadline(timeout)
        _check_time(deadline)
        from .common import DirectoryTarget
        target_info = os.fstat(path.descriptor) if isinstance(path, DirectoryTarget) else os.stat(target)
        target_device = target_info.st_dev
        mounts = _read_mountinfo()
        mount = _mount_for_path(target, mounts, device=target_device)
        data["挂载点"] = mount["挂载点"]
        data["文件系统"] = mount["文件系统"]
        if mount["文件系统"] not in _DISK_FILESYSTEMS:
            report["状态"] = "部分完成"
            report["说明"].append("第一版只统计 ext2、ext3、ext4 和 XFS；目标文件系统未扫描。")
            return report
        if PROC_ROOT == "/proc" and sys.platform != "linux":
            raise OSError("当前系统没有受支持的 Linux procfs")
        proc_mount = _mount_for_path(os.path.realpath(PROC_ROOT), mounts)
        options = (proc_mount["挂载选项"] + "," + proc_mount["超级选项"]).split(",")
        if any(option.startswith("hidepid=") and option not in {"hidepid=0", "hidepid=off"}
               for option in options):
            report["状态"] = "部分完成"
            report["说明"].append("进程文件系统启用了隐藏进程选项，不能声称已查看所有进程。")
        _check_time(deadline)
        try:
            processes = os.scandir(PROC_ROOT)
        except PermissionError:
            report["状态"] = "权限不足"
            report["说明"].append("当前权限不能读取进程文件描述符。")
            return report
        with processes:
            for process in processes:
                _check_time(deadline)
                if not process.name.isascii() or not process.name.isdigit():
                    continue
                try:
                    if not process.is_dir(follow_symlinks=False):
                        continue
                except FileNotFoundError:
                    continue
                except OSError as error:
                    if error.errno not in {errno.ENOENT, errno.ESRCH}:
                        data["不可访问进程数"] += 1
                    continue
                pid = int(process.name)
                process_path = os.path.join(PROC_ROOT, process.name)
                name = _read_comm(process_path)
                try:
                    descriptors = os.scandir(os.path.join(process_path, "fd"))
                except FileNotFoundError:
                    continue
                except PermissionError:
                    data["不可访问进程数"] += 1
                    continue
                except OSError as error:
                    if error.errno not in {errno.ENOENT, errno.ESRCH}:
                        data["不可访问进程数"] += 1
                    continue
                data["检查进程数"] += 1
                denied = name in {"当前权限不可见", "无法读取"}
                saw_descriptor = False
                enumerated = False
                with descriptors:
                    iterator = iter(descriptors)
                    while True:
                        _check_time(deadline)
                        try:
                            descriptor = next(iterator)
                        except StopIteration:
                            enumerated = True
                            break
                        except OSError as error:
                            if error.errno not in {errno.ENOENT, errno.ESRCH, errno.EBADF}:
                                denied = True
                            break
                        saw_descriptor = True
                        try:
                            # 文件描述符会在扫描期间复用，不依赖 DirEntry 的缓存元数据。
                            info = os.stat(descriptor.path, follow_symlinks=True)
                        except FileNotFoundError:
                            measured = True
                            continue
                        except PermissionError:
                            denied = True
                            continue
                        except OSError as error:
                            if error.errno in {errno.ENOENT, errno.ESRCH, errno.EBADF}:
                                measured = True
                                continue
                            denied = True
                            continue
                        measured = True
                        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 0:
                            continue
                        if info.st_dev != target_device:
                            continue
                        label = _path_label(descriptor.path)
                        try:
                            checked = os.stat(descriptor.path, follow_symlinks=True)
                        except OSError as error:
                            data["变化条目数"] += 1
                            if error.errno not in {errno.ENOENT, errno.ESRCH, errno.EBADF}:
                                denied = True
                            continue
                        # readlink 与 stat 不是原子操作，至少识别可观察的 fd 复用和文件变化。
                        if ((checked.st_dev, checked.st_ino) != (info.st_dev, info.st_ino)
                                or not stat.S_ISREG(checked.st_mode) or checked.st_nlink != 0):
                            data["变化条目数"] += 1
                            continue
                        if (checked.st_blocks, checked.st_size) != (info.st_blocks, info.st_size):
                            data["变化条目数"] += 1
                        info = checked
                        identity = (info.st_dev, info.st_ino)
                        record = records.get(identity)
                        if record is None:
                            try:
                                device = f"{os.major(info.st_dev)}:{os.minor(info.st_dev)}"
                            except (OSError, ValueError):
                                device = str(info.st_dev)
                            record = {"设备号": device, "文件节点": info.st_ino,
                                      "已分配字节": max(0, info.st_blocks) * 512,
                                      "逻辑字节": max(0, info.st_size),
                                      "原路径": label,
                                      "持有进程": {}}
                            records[identity] = record
                        elif (record["已分配字节"], record["逻辑字节"]) != (max(0, info.st_blocks) * 512, max(0, info.st_size)):
                            data["变化条目数"] += 1
                            record["已分配字节"] = max(0, info.st_blocks) * 512
                            record["逻辑字节"] = max(0, info.st_size)
                        record["持有进程"][pid] = {"进程号": pid, "进程名": name}
                if enumerated and not saw_descriptor:
                    measured = True
                if denied:
                    data["不可访问进程数"] += 1
        _check_time(deadline)
    except _Timeout:
        report["状态"] = "超时"
        report["说明"].append("达到检查时间限制；保留超时前发现的文件。")
    except PermissionError:
        report["状态"] = "部分完成" if records else "权限不足"
        report["说明"].append("当前权限不能读取完整进程信息。")
    except (OSError, ValueError, TypeError) as error:
        report["状态"] = "部分完成" if records else "失败"
        report["说明"].append("无法完成已删除文件检查：" + type(error).__name__)

    rows = []
    for record in records.values():
        record["持有进程"] = sorted(record["持有进程"].values(), key=lambda row: row["进程号"])
        rows.append(record)
    rows.sort(key=lambda row: (-row["已分配字节"], row["设备号"], row["文件节点"]))
    data["文件列表"] = rows
    if measured:
        data["文件数"] = len(rows)
        data["已分配字节"] = sum(row["已分配字节"] for row in rows)
        data["逻辑字节"] = sum(row["逻辑字节"] for row in rows)
        report["说明"].append("数值仅代表本次可读取范围；零表示该范围未发现符合条件的已删除文件，不代表整机不存在占用。")
    else:
        if report["状态"] == "完成":
            report["状态"] = "权限不足" if data["不可访问进程数"] else "部分完成"
        report["说明"].append("没有取得可检查的文件描述符范围，占用大小未知，不以零表示。")
    if data["不可访问进程数"] and report["状态"] == "完成":
        report["状态"] = "部分完成"
        report["说明"].append("部分进程或文件描述符当前权限不可见。")
    if data["变化条目数"]:
        if report["状态"] == "完成":
            report["状态"] = "部分完成"
        report["说明"].append("扫描期间发现文件或描述符变化；排除无法对应的记录，大小使用最后一次可见元数据。")
    if os.geteuid() != 0 and report["状态"] == "完成":
        report["状态"] = "部分完成"
        report["说明"].append("未使用管理员权限，只能报告当前权限可见的进程。")
    report["说明"].append("仅检查当前进程命名空间内可见的普通文件；不读取目标文件内容、进程命令行或环境变量。")
    report["说明"].append("按设备号和文件节点去重；已分配字节使用 st_blocks × 512，不等于一定可立即释放的空间。")
    report["说明"].append("检查不是原子快照，路径标签及进程状态仍可能在读取之间变化。")
    return report
