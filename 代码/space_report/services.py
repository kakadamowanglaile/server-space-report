"""只读采集日志和本机 Docker 空间，不读取日志正文或用户 Docker 配置。"""

import json
import os
import re
import stat
import subprocess
import time
from decimal import Decimal, InvalidOperation

from .common import run_command


_ENV = {"LC_ALL": "C"}
_DOCKER = ["docker", "--config", "/proc/self/fd", "--host", "unix:///var/run/docker.sock"]
_INFO = ('{{if .ServerErrors}}{"ServerErrors":{{json .ServerErrors}}}{{else}}'
         '{"DockerRootDir":{{json .DockerRootDir}},"LoggingDriver":{{json .LoggingDriver}}}{{end}}')
_DF = ('{"Type":{{json .Type}},"TotalCount":{{json .TotalCount}},'
       '"Active":{{json .Active}},"Size":{{json .Size}},"Reclaimable":{{json .Reclaimable}}}')
_CATEGORIES = {"Images": "镜像", "Containers": "容器可写层", "Local Volumes": "本地卷", "Build Cache": "构建缓存"}
_SIZE = re.compile(r"^(\d+(?:\.\d+)?)\s*([kKMGTPE]?)(i?B)?$")


def _report(name, scope):
    return {"项目": name, "状态": "完成", "范围": scope, "数据": {}, "说明": []}


def _size(value, base=1000):
    """只接受可识别的大小，不把未知文本当作零。"""
    if not isinstance(value, str) or len(value) > 64:
        return None
    match = _SIZE.fullmatch(value.strip())
    if not match or not (match[2] or match[3]):
        return None
    power = "KMGTPE".find(match[2].upper()) + 1 if match[2] else 0
    multiplier = 1024 if match[3] == "iB" else base
    try:
        amount = Decimal(match[1]) * multiplier ** power
        if 0 < amount < 1:
            return None
        return int(amount)
    except (InvalidOperation, OverflowError, ValueError):
        return None


def _command(argv, timeout, *, server_errors=False):
    try:
        if timeout <= 0:
            return None, "超时"
        result = run_command(argv, timeout, env=dict(_ENV))
    except FileNotFoundError:
        return None, "工具缺失"
    except subprocess.TimeoutExpired:
        return None, "超时"
    except PermissionError:
        return None, "权限不足"
    except (OSError, RuntimeError):
        return None, "失败"
    if server_errors:
        # 旧 Docker 的模板命令可在服务查询失败时返回 0，只检查指定错误字段。
        try:
            fields = json.loads(result.stdout)
        except (ValueError, TypeError, RecursionError):
            fields = None
        if isinstance(fields, dict) and "ServerErrors" in fields:
            errors = fields["ServerErrors"]
            if isinstance(errors, list) and errors and all(isinstance(item, str) for item in errors):
                error = "\n".join(errors).lower()
                return None, "权限不足" if "permission denied" in error or "access denied" in error else "失败"
            return None, "失败"
    if result.returncode:
        # 错误文本仅用于分类，不把可能包含配置或路径信息的 stderr 放进报告。
        error = result.stderr.lower()
        return None, "权限不足" if "permission denied" in error or "access denied" in error else "失败"
    return result, "完成"


def _same_filesystem(path, target):
    try:
        return os.stat(path).st_dev == os.stat(target).st_dev
    except OSError:
        return None


def collect_journal(path: str, timeout: float = 30) -> dict:
    report = _report("系统日志", "本机 journalctl 当前身份可见的活动及归档日志；不是目标目录的独立占用")
    report["说明"] = ["仅调用 journalctl --disk-usage，不读取日志正文。",
                    "该命令汇总可见日志；/var/log/journal 与 /run/log/journal 可能位于不同文件系统，不能将汇总归入目标目录。"]
    result, status = _command(["journalctl", "--disk-usage", "--no-pager"], timeout)
    report["状态"] = status
    if result is None:
        report["说明"].append("未取得日志占用：" + status + "；不按零占用处理。")
        return report
    match = re.fullmatch(r"Archived and active journals take up ([\d.]+\s*[KMGTPEk]?(?:i?B)?) in the file system\.", result.stdout.strip())
    amount = _size(match[1], base=1024) if match else None
    if amount is None:
        report["状态"] = "失败"
        report["说明"].append("无法识别日志占用输出，未产生数值。")
        return report
    report["数据"] = {"磁盘占用显示": match[1], "字节估算": amount, "数值为近似": True,
                    "目标路径": os.path.abspath(path), "日志目录": []}
    for location in ("/var/log/journal", "/run/log/journal"):
        report["数据"]["日志目录"].append({"路径": location, "与目标同文件系统": _same_filesystem(location, path)})
    if os.geteuid() != 0 or result.stderr.strip():
        report["状态"] = "部分完成"
        report["说明"].append("当前身份或命令警告使完整可见范围无法证明；仅报告当前可见日志。")
    return report


def _summary(stdout):
    rows = []
    seen = set()
    partial = False
    for line in stdout.splitlines():
        if not line.strip():
            continue
        try:
            item = json.loads(line)
            if not isinstance(item, dict) or item.get("Type") not in _CATEGORIES:
                partial = True
                continue
        except (ValueError, TypeError, RecursionError):
            partial = True
            continue
        kind = item["Type"]
        if kind in seen:
            partial = True
            continue
        seen.add(kind)
        row = {"类别": _CATEGORIES[kind], "数值为近似": True}
        amount = _size(item.get("Size"))
        if amount is not None:
            row.update({"占用显示": item["Size"], "字节估算": amount})
        else:
            partial = True
            row["说明"] = "大小格式无法识别，不能当作零。"
        for source, dest in (("TotalCount", "总数"), ("Active", "活动数量")):
            value = str(item.get(source, ""))
            if len(value) <= 20 and re.fullmatch(r"[0-9]+", value):
                row[dest] = int(value)
            else:
                partial = True
        reclaimable = item.get("Reclaimable", "")
        if isinstance(reclaimable, str) and re.fullmatch(r"[\d.]+\s*[kKMGTPE]?(?:i?B)(?:\s+\(\d+%\))?", reclaimable):
            row["Docker报告可回收显示"] = reclaimable
        rows.append(row)
    return rows, partial or len(seen) != len(_CATEGORIES)


def _directory_at(name, parent=None):
    """只打开常规目录；名称解析相对已持有目录，禁止符号链接。"""
    if not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY"):
        raise NotImplementedError("当前系统不支持安全目录打开")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    fd = os.open(name, flags, dir_fd=parent)
    try:
        if not stat.S_ISDIR(os.fstat(fd).st_mode):
            raise NotADirectoryError("目标不是目录")
    except BaseException:
        os.close(fd)
        raise
    return fd


def _root_directory(path, deadline):
    """从根目录逐层打开，包括 DockerRootDir 的所有祖先。"""
    if not os.path.isabs(path) or ".." in path.split(os.sep):
        raise ValueError("数据目录路径不安全")
    fd = _directory_at(os.sep)
    try:
        for part in path.split(os.sep):
            if time.monotonic() >= deadline:
                raise TimeoutError("目录验证超时")
            if not part or part == ".":
                continue
            child = _directory_at(part, fd)
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def _log_metadata(cid, root_fd, root, target, seen_files, deadline):
    row = {"容器ID": cid[:12], "状态": "完成", "范围": "json-file 当前文件及同目录数字编号轮转文件的元数据，不含正文"}
    row["日志目录"] = os.path.join(root, "containers", cid)
    logical = allocated = count = duplicates = 0
    allocated_known = True
    denied = False
    pattern = re.compile(re.escape(cid + "-json.log") + r"(?:\.\d+(?:\.gz)?)?$")
    container_fd = directory = None
    try:
        if time.monotonic() >= deadline:
            raise TimeoutError("日志扫描超时")
        container_fd = _directory_at("containers", root_fd)
        if time.monotonic() >= deadline:
            raise TimeoutError("日志扫描超时")
        directory = _directory_at(cid, container_fd)
        if time.monotonic() >= deadline:
            raise TimeoutError("日志扫描超时")
        try:
            row["与目标同文件系统"] = os.fstat(directory).st_dev == os.stat(target).st_dev
        except OSError:
            row["与目标同文件系统"] = None
        with os.scandir(directory) as entries:
            for entry in entries:
                if time.monotonic() >= deadline:
                    row["状态"] = "超时"
                    break
                if not pattern.fullmatch(entry.name):
                    continue
                try:
                    metadata = os.stat(entry.name, dir_fd=directory, follow_symlinks=False)
                except PermissionError:
                    denied = True
                    row["状态"] = "部分完成"
                    continue
                except TimeoutError:
                    raise
                except OSError:
                    row["状态"] = "部分完成"
                    continue
                if not stat.S_ISREG(metadata.st_mode) or metadata.st_ino != entry.inode():
                    row["状态"] = "部分完成"
                    continue
                identity = (metadata.st_dev, metadata.st_ino)
                if identity in seen_files:
                    duplicates += 1
                    continue
                seen_files.add(identity)
                logical += metadata.st_size
                count += 1
                blocks = getattr(metadata, "st_blocks", None)
                if blocks is None:
                    allocated_known = False
                else:
                    allocated += blocks * 512
        if time.monotonic() >= deadline:
            row["状态"] = "超时"
    except TimeoutError:
        row["状态"] = "超时"
    except PermissionError:
        row["状态"] = "权限不足"
    except (FileNotFoundError, NotADirectoryError):
        row["状态"] = "部分完成" if directory is not None else "不适用"
    except (OSError, NotImplementedError):
        row["状态"] = "部分完成"
    finally:
        if directory is not None:
            os.close(directory)
        if container_fd is not None:
            os.close(container_fd)
    row["文件数"] = count
    if duplicates:
        row["重复硬链接数"] = duplicates
    if count:
        row.update({"逻辑字节": logical, "已分配字节": allocated if allocated_known else None})
    if denied and count == 0 and row["状态"] != "超时":
        row["状态"] = "权限不足"
    if duplicates and count == 0 and row["状态"] == "完成":
        row["状态"] = "部分完成"
    if count == 0 and row["状态"] in ("完成", "不适用"):
        row["状态"] = "不适用"
        row["说明"] = "未发现支持的 json-file 文件，其他日志驱动未统计；不推断具体日志驱动。"
    if row["状态"] == "超时":
        row["说明"] = "日志元数据扫描达到时间上限，仅保留已完成部分。"
    elif row["状态"] == "部分完成":
        row["说明"] = "部分文件发生变化、无法访问或是符号链接；已跳过，未读取正文。"
    elif row["状态"] == "权限不足":
        row["说明"] = "无权读取全部日志元数据，仅保留已完成部分；未按零处理。"
    if duplicates:
        row["说明"] = row.get("说明", "") + "存在已统计过的硬链接，共享文件不重复计入字节。"
    return row


def collect_docker(path: str, timeout: float = 30) -> dict:
    report = _report("Docker", "仅本机 /var/run/docker.sock 对应守护进程；分类是守护进程全局量，不是目标目录独立占用")
    report["说明"] = ["未读取用户 Docker 配置、远程上下文、容器环境变量或日志正文。",
                    "分类数值由 Docker 格式化，字节为近似估算；镜像共享层不逐项累加，分类与日志不合计。",
                    "Docker 的可回收显示不表示可以安全删除；本工具不执行清理。",
                    "容器可写层不包括卷和容器日志；外部卷、绑定挂载及其他日志驱动可能在别的文件系统。"]
    if not os.path.isdir("/proc/self/fd"):
        report["状态"] = "不适用"
        report["说明"].append("缺少 Linux /proc/self/fd 空配置入口；未尝试使用用户配置。")
        return report
    deadline = time.monotonic() + timeout
    def call(arguments, *, server_errors=False):
        return _command(_DOCKER + arguments, deadline - time.monotonic(), server_errors=server_errors)
    info, status = call(["info", "--format", _INFO], server_errors=True)
    if info is None:
        report["状态"] = status
        report["说明"].append("无法查询本机 Docker：" + status + "；未尝试其他端点，也未按零处理。")
        return report
    try:
        fields = json.loads(info.stdout)
        root = fields["DockerRootDir"]
        if not isinstance(root, str) or not os.path.isabs(root) or "\x00" in root:
            raise ValueError("数据目录无效")
    except (ValueError, KeyError, TypeError, RecursionError):
        report["状态"] = "失败"
        report["说明"].append("Docker 数据目录返回格式无法识别。")
        return report
    report["数据"].update({"数据目录": root, "目标路径": os.path.abspath(path),
                          "数据目录与目标同文件系统": _same_filesystem(root, path), "容器日志": []})
    if report["数据"]["数据目录与目标同文件系统"] is None:
        report["状态"] = "部分完成"
        report["说明"].append("无法确认 Docker 数据目录与目标是否同文件系统；未把全局数值归入目标。")
    elif not report["数据"]["数据目录与目标同文件系统"]:
        report["说明"].append("Docker 数据目录与目标不在同一文件系统，以上分类不能解释目标分区占用。")
    usage, status = call(["system", "df", "--format", _DF])
    report["数据"]["分类状态"] = status
    if usage is None:
        report["状态"] = "部分完成"
        report["说明"].append("Docker 分类占用未取得：" + status + "。")
    else:
        categories, partial = _summary(usage.stdout)
        report["数据"]["分类"] = categories
        if partial:
            report["状态"] = "部分完成"
            report["数据"]["分类状态"] = "部分完成"
            report["说明"].append("部分 Docker 分类缺失或格式无法识别；未补零。")
    listing, status = call(["container", "ls", "--all", "--no-trunc", "--format", "{{json .ID}}"])
    if listing is None:
        report["状态"] = "部分完成"
        report["数据"]["容器日志状态"] = status
        return report
    try:
        ids = [json.loads(line) for line in listing.stdout.splitlines() if line.strip()]
        if any(not isinstance(cid, str) or not re.fullmatch(r"[a-f0-9]{64}", cid) for cid in ids):
            raise ValueError("容器ID无效")
        ids = list(dict.fromkeys(ids))
    except (ValueError, TypeError, RecursionError):
        report["状态"] = "部分完成"
        report["数据"]["容器日志状态"] = "失败"
        return report
    log_status = "完成"
    seen_files = set()
    root_fd = None
    try:
        if ids:
            root_fd = _root_directory(root, deadline)
        for cid in ids:
            if time.monotonic() >= deadline:
                log_status = "超时"
                break
            row = _log_metadata(cid, root_fd, root, path, seen_files, deadline)
            report["数据"]["容器日志"].append(row)
            if row["状态"] == "超时":
                log_status = "超时"
                break
            if row["状态"] != "完成":
                log_status = "部分完成"
    except TimeoutError:
        log_status = "超时"
    except PermissionError:
        log_status = "权限不足"
    except (OSError, ValueError, NotImplementedError):
        log_status = "部分完成"
        report["说明"].append("Docker 数据目录或其祖先包含无法安全打开的路径；未跟随符号链接统计日志。")
    finally:
        if root_fd is not None:
            os.close(root_fd)
    report["数据"]["容器日志状态"] = log_status
    if log_status != "完成":
        report["状态"] = "部分完成"
    return report
