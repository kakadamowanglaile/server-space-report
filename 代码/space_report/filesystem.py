"""只读取文件元数据的分区和目录检查。"""

import math
import os
import re
import stat
import time

from .common import DirectoryTarget, open_directory


def _parse_mountinfo(text):
    """按内核字段边界解析，再还原挂载路径中的四种八进制转义。"""
    escapes = {"040": " ", "011": "\t", "012": "\n", "134": "\\"}
    rows = []
    # 文件名可含 CR、FF、NEL 等字符；内核只使用 LF 分隔挂载记录。
    for line in text.split("\n"):
        if not line.strip():
            continue
        fields = line.split(" ")
        try:
            separator = fields.index("-")
            if separator < 6 or len(fields) < separator + 4:
                raise ValueError("挂载表字段不完整")
            mountpoint = re.sub(r"\\(040|011|012|134)", lambda match: escapes[match[1]], fields[4])
            if not mountpoint.startswith("/"):
                raise ValueError("挂载点不是绝对路径")
            rows.append({"挂载点": os.path.normpath(mountpoint), "设备号": fields[2], "挂载编号": int(fields[0]),
                         "文件系统": fields[separator + 1], "挂载选项": fields[5],
                         "超级选项": fields[separator + 3]})
        except (ValueError, IndexError) as error:
            raise ValueError("不能完整解析 Linux 挂载表") from error
    if not rows:
        raise ValueError("Linux 挂载表为空")
    return rows


def _read_mountinfo():
    with open("/proc/self/mountinfo", encoding="utf-8", errors="surrogateescape", newline="") as handle:
        return _parse_mountinfo(handle.read())


def _mount_for_path(path, mounts, device=None, mount_id=None):
    matches = [row for row in mounts if path == row["挂载点"] or
               path.startswith(row["挂载点"].rstrip("/") + "/")]
    if not matches:
        raise ValueError("目标路径没有对应的挂载点")
    deepest = max(len(row["挂载点"]) for row in matches)
    device = os.stat(path).st_dev if device is None else device
    device_text = f"{os.major(device)}:{os.minor(device)}"
    visible = [row for row in matches if len(row["挂载点"]) == deepest and row["设备号"] == device_text]
    if mount_id is not None:
        visible = [row for row in visible if row.get("挂载编号") == mount_id]
    if not visible:
        raise ValueError("挂载表与实际可见文件系统不一致，无法确定文件系统类型")
    # 同路径同设备叠加时保留后出现的层；已打开的目录优先通过 mnt_id 精确匹配。
    return visible[-1]


def _fd_mount_id(descriptor):
    try:
        with open(f"/proc/self/fdinfo/{descriptor}", encoding="ascii") as handle:
            for line in handle:
                key, separator, value = line.partition(":")
                if separator and key == "mnt_id":
                    return int(value.strip())
    except (OSError, ValueError, UnicodeError):
        pass
    return None


def _new_report(name, path, data=None):
    return {"项目": name, "状态": "完成", "范围": os.path.abspath(os.fspath(path)),
            "数据": {} if data is None else data, "说明": []}


def _deadline(timeout):
    seconds = float(timeout)
    if not math.isfinite(seconds):
        raise ValueError("超时秒数必须是有限数值")
    return time.monotonic() + max(0, seconds)


class _ScanTimeout(Exception):
    pass


def _check_time(deadline):
    if time.monotonic() >= deadline:
        raise _ScanTimeout()


def collect_partition(path: str, timeout: float = 30) -> dict:
    report = _new_report("分区空间", path)
    try:
        deadline = _deadline(timeout)
        _check_time(deadline)
        target = report["范围"]
        info = os.statvfs(f"/proc/self/fd/{path.descriptor}" if isinstance(path, DirectoryTarget) else target)
        block = info.f_frsize or info.f_bsize
        report["数据"] = {
            "总字节": info.f_blocks * block,
            "已用字节": max(0, info.f_blocks - info.f_bfree) * block,
            "可用字节": max(0, info.f_bavail) * block,
            "空闲字节": max(0, info.f_bfree) * block,
            "总文件节点": info.f_files,
            "已用文件节点": max(0, info.f_files - info.f_ffree),
            "可用文件节点": max(0, info.f_favail),
        }
        if info.f_files <= 0:
            for key in ("总文件节点", "已用文件节点", "可用文件节点"):
                report["数据"][key] = None
        _check_time(deadline)
        device = os.fstat(path.descriptor).st_dev if isinstance(path, DirectoryTarget) else None
        mount = _mount_for_path(target, _read_mountinfo(), device=device)
        report["数据"].update({"挂载点": mount["挂载点"], "文件系统": mount["文件系统"]})
        _check_time(deadline)
        report["说明"].append("可用空间按当前文件系统向普通用户报告的数值展示；不检查用户配额。")
        if info.f_files <= 0:
            report["说明"].append("此文件系统未提供文件节点总量，不能据此判断文件数量额度。")
    except _ScanTimeout:
        report["状态"] = "超时"
        report["说明"].append("达到检查时间限制；已取得的数据仍保留。")
    except PermissionError:
        report["状态"] = "部分完成" if report["数据"] else "权限不足"
        report["说明"].append("当前权限不足，部分文件系统信息不可见。")
    except (OSError, ValueError, TypeError) as error:
        report["状态"] = "部分完成" if report["数据"] else "失败"
        report["说明"].append("未能取得完整分区信息：" + type(error).__name__)
    return report


def collect_directories(path, timeout=30) -> dict:
    data = {"目录列表": [], "目录总数": None, "根目录普通文件字节": None,
            "根目录普通文件数量": None, "根目录其他条目字节": None,
            "已统计字节": None, "跳过挂载点": [], "未读取条目数": 0}
    report = _new_report("大目录", path, data)
    seen_files = set()
    candidates = []
    root = report["范围"]

    def unreadable():
        data["未读取条目数"] += 1

    def add(info, bucket=None):
        if stat.S_ISREG(info.st_mode):
            identity = (info.st_dev, info.st_ino)
            if identity in seen_files:
                return 0
            seen_files.add(identity)
        allocated = max(0, info.st_blocks) * 512
        data["已统计字节"] += allocated
        if bucket is not None:
            bucket["已分配字节"] = (bucket["已分配字节"] or 0) + allocated
        return allocated

    try:
        deadline = _deadline(timeout)
        _check_time(deadline)
        root_info = os.fstat(path.descriptor) if isinstance(path, DirectoryTarget) else os.lstat(root)
        if not stat.S_ISDIR(root_info.st_mode):
            raise ValueError("目标必须是实际目录，不能是符号链接")
        if not isinstance(path, DirectoryTarget):
            # 兼容直接调用采集器时的祖先路径别名；CLI 使用已选目录描述符。
            root = os.path.realpath(root)
        mounts = _read_mountinfo()
        selected_mount = _mount_for_path(root, mounts, device=root_info.st_dev)
        boundaries = {row["挂载点"] for row in mounts}
        if selected_mount["文件系统"] not in {"ext2", "ext3", "ext4", "xfs"}:
            report["说明"].append("此文件系统可能存在共享块、压缩或快照；目录数值不代表独占物理空间。")
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW

        def walk(descriptor, current_path, bucket, depth):
            _check_time(deadline)
            if depth > 128:
                unreadable()
                return
            try:
                with os.scandir(descriptor) as entries:
                    for entry in entries:
                        _check_time(deadline)
                        child_path = os.path.join(current_path, entry.name)
                        if child_path in boundaries and child_path != root:
                            data["跳过挂载点"].append(child_path)
                            continue
                        try:
                            info = entry.stat(follow_symlinks=False)
                            if info.st_dev != root_info.st_dev:
                                data["跳过挂载点"].append(child_path)
                                continue
                            if stat.S_ISDIR(info.st_mode):
                                child_bucket = bucket
                                top = current_path == root
                                if top:
                                    child_bucket = {"路径": child_path, "已分配字节": None}
                                    data["目录总数"] += 1
                                try:
                                    child_fd = os.open(entry.name, flags, dir_fd=descriptor)
                                    try:
                                        opened = os.fstat(child_fd)
                                        if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                                            unreadable()
                                            continue
                                        opened_mount_id = _fd_mount_id(child_fd)
                                        if opened_mount_id is None:
                                            unreadable()
                                            continue
                                        elif root_mount_id is not None and opened_mount_id != root_mount_id:
                                            data["跳过挂载点"].append(child_path)
                                            unreadable()
                                            continue
                                        add(opened, child_bucket)
                                        walk(child_fd, child_path, child_bucket, depth + 1)
                                    finally:
                                        os.close(child_fd)
                                finally:
                                    if top:
                                        candidates.append(child_bucket)
                                        candidates.sort(key=lambda row: (
                                            row["已分配字节"] is None,
                                            -(row["已分配字节"] or 0), row["路径"]))
                                        del candidates[20:]
                            elif stat.S_ISREG(info.st_mode):
                                if hasattr(os, "O_PATH"):
                                    # Linux O_PATH 只取得路径元数据，不打开文件内容。
                                    file_fd = os.open(entry.name, os.O_PATH | os.O_NOFOLLOW, dir_fd=descriptor)
                                    try:
                                        opened = os.fstat(file_fd)
                                        if ((opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino)
                                                or not stat.S_ISREG(opened.st_mode)):
                                            unreadable()
                                            continue
                                        file_mount_id = _fd_mount_id(file_fd)
                                        if file_mount_id is None:
                                            unreadable()
                                            continue
                                        if file_mount_id != root_mount_id:
                                            data["跳过挂载点"].append(child_path)
                                            unreadable()
                                            continue
                                        info = opened
                                    finally:
                                        os.close(file_fd)
                                counted = add(info, bucket)
                                if current_path == root:
                                    data["根目录普通文件字节"] += counted
                                    data["根目录普通文件数量"] += 1
                            elif stat.S_ISLNK(info.st_mode):
                                counted = add(info, bucket)
                                if current_path == root:
                                    data["根目录其他条目字节"] += counted
                        except OSError:
                            unreadable()
            except OSError:
                unreadable()

        _check_time(deadline)
        root_fd = open_directory(path if isinstance(path, DirectoryTarget) else root)
        try:
            opened_root = os.fstat(root_fd)
            if (opened_root.st_dev, opened_root.st_ino) != (root_info.st_dev, root_info.st_ino):
                report["状态"] = "部分完成"
                report["说明"].append("打开目标前目录已经变化，未继续扫描。")
                return report
            root_mount_id = _fd_mount_id(root_fd)
            if root_mount_id is None:
                unreadable()
                report["状态"] = "部分完成"
                report["说明"].append("无法读取目录的挂载编号，未继续扫描；占用未知，不以零表示。")
                return report
            else:
                _mount_for_path(root, mounts, device=opened_root.st_dev, mount_id=root_mount_id)
            for key in ("目录总数", "根目录普通文件字节", "根目录普通文件数量",
                        "根目录其他条目字节", "已统计字节"):
                data[key] = 0
            add(opened_root)
            walk(root_fd, root, None, 0)
        finally:
            os.close(root_fd)
        _check_time(deadline)
        if data["未读取条目数"]:
            report["状态"] = "部分完成"
            report["说明"].append("部分条目因权限、扫描时变化或目录过深未读取；数值不是完整占用。")
    except _ScanTimeout:
        report["状态"] = "超时"
        report["说明"].append("达到检查时间限制，目录排序和总量仅基于已扫描内容。")
    except PermissionError:
        report["状态"] = "部分完成" if data["已统计字节"] is not None else "权限不足"
        report["说明"].append("无法读取目录或挂载边界，未继续扫描。")
    except (OSError, ValueError, TypeError) as error:
        report["状态"] = "部分完成" if data["已统计字节"] is not None else "失败"
        report["说明"].append("无法完成目录检查：" + type(error).__name__)
    finally:
        # 递归闭包会自引用；及时释放每轮文件索引，避免等待周期性 GC。
        seen_files.clear()
        walk = None
    data["目录列表"] = candidates
    data["跳过挂载点"] = sorted(set(data["跳过挂载点"]))
    if data["已统计字节"] is None:
        report["说明"].append("尚未取得目录大小，未知值不代表零占用。")
    else:
        report["说明"].append("数值仅代表本次已读取元数据的范围；零表示该可读取范围内未计得占用。")
    report["说明"].append("按 st_blocks × 512 统计分配空间；目录与根层文件是同一统计的组成部分，不可与 Docker、日志数值重复相加。")
    report["说明"].append("同一文件系统的硬链接只统计一次；跳过符号链接目标及所有子挂载点。")
    return report
