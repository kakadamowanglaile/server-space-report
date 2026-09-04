"""从明确白名单构建源码和单文件包，不收集测试环境或真实机器报告。"""
import argparse
from datetime import datetime
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import sys
import zipfile

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "代码"))
from space_report import __version__


def _directory(name, parent=None):
    """相对持有目录打开下一层，拒绝任何符号链接。"""
    return os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)


def _absolute_directory(path, create=False):
    path = Path(os.path.abspath(path))
    directory = _directory(path.anchor)
    try:
        for part in path.parts[1:]:
            if create:
                try:
                    os.mkdir(part, mode=0o700, dir_fd=directory)
                except FileExistsError:
                    pass
            child = _directory(part, directory)
            os.close(directory)
            directory = child
        return directory
    except BaseException:
        os.close(directory)
        raise


def _snapshot():
    """只读入指定文档和代码/测试的 Python 文件；两个归档共用同一份字节。"""
    snapshot = {}
    total = 0
    root_fd = _absolute_directory(ROOT)
    def read_file(name, directory, relative):
        nonlocal total
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        try:
            before = os.fstat(fd)
            if not stat.S_ISREG(before.st_mode) or before.st_size > 8 * 1024 * 1024:
                raise ValueError("发布源不是常规文件或超过单文件 8 MiB 限制：" + relative)
            chunks = []
            length = 0
            while chunk := os.read(fd, 65536):
                length += len(chunk)
                total += len(chunk)
                if length > 8 * 1024 * 1024 or total > 32 * 1024 * 1024:
                    raise ValueError("发布源码超过单文件 8 MiB 或合计 32 MiB 限制")
                chunks.append(chunk)
            after = os.fstat(fd)
            if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                raise OSError("读取期间发布源发生变化：" + relative)
            snapshot[relative] = b"".join(chunks)
        finally:
            os.close(fd)
    def read_relative(relative):
        parts = Path(relative).parts
        directory = os.dup(root_fd)
        try:
            for part in parts[:-1]:
                child = _directory(part, directory)
                os.close(directory)
                directory = child
            read_file(parts[-1], directory, relative)
        finally:
            os.close(directory)
    def walk(directory, relative, depth=0):
        if depth > 32:
            raise ValueError("发布源码目录层级超过 32 层")
        with os.scandir(directory) as entries:
            for entry in entries:
                if entry.name == "__pycache__":
                    continue
                child_name = relative + "/" + entry.name
                if entry.is_symlink():
                    raise ValueError("发布源不能包含符号链接：" + child_name)
                if entry.is_dir(follow_symlinks=False):
                    child = _directory(entry.name, directory)
                    try:
                        walk(child, child_name, depth + 1)
                    finally:
                        os.close(child)
                elif entry.name.endswith(".py"):
                    read_file(entry.name, directory, child_name)
    try:
        for name in ["README.md", "README.zh-CN.md", "CONTRIBUTING.md", "CONTRIBUTING.zh-CN.md",
                     "LICENSE", ".gitignore", "更新记录.md", "文档/已知限制.md",
                     "文档/报告格式.md", "文档/验收说明.md", "文档/报告示例.txt", "工具/构建发布包.py",
                     "工具/核对交付包.py"]:
            read_relative(name)
        for name in ("代码", "测试"):
            directory = _directory(name, root_fd)
            try:
                walk(directory, name)
            finally:
                os.close(directory)
    finally:
        os.close(root_fd)
    return snapshot


def build(destination):
    destination = Path(os.path.abspath(destination))
    prefix = f"服务器空间去哪了-{__version__}"
    snapshot = _snapshot()
    if "代码/__main__.py" not in snapshot:
        raise ValueError("缺少代码/__main__.py 运行入口，未生成发布文件")
    payloads = {}
    for filename, runtime in ((prefix + ".pyz", True), (prefix + "-源码.zip", False)):
        buffer = io.BytesIO()
        if runtime:
            buffer.write(b"#!/usr/bin/env python3\n")
        with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name, content in sorted(snapshot.items()):
                if runtime:
                    if name.startswith("代码/"):
                        archive.writestr(name[len("代码/"):], content)
                else:
                    archive.writestr(prefix + "/" + name, content)
        payloads[filename] = buffer.getvalue()
    listing = [{"名称": name, "字节": len(content), "SHA256": hashlib.sha256(content).hexdigest()}
               for name, content in payloads.items()]
    payloads["发布清单.json"] = json.dumps({"版本": __version__, "文件": listing,
        "说明": "不含虚拟机、缓存、凭据或真实机器报告"}, ensure_ascii=False, indent=2).encode("utf-8")
    directory = _absolute_directory(destination, create=True)
    identities = {}
    try:
        for name in payloads:
            try:
                os.stat(name, dir_fd=directory, follow_symlinks=False)
            except FileNotFoundError:
                continue
            raise FileExistsError("目标目录中已有同名发布文件，请使用新目录，不覆盖旧包")
        for name, content in payloads.items():
            # O_EXCL 对应 xb 的独占创建，同时锚定已验证的目录描述符。
            fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
            with os.fdopen(fd, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
                identities[name] = os.fstat(stream.fileno())
        os.fsync(directory)
        for name, identity in identities.items():
            current = os.stat(name, dir_fd=directory, follow_symlinks=False)
            if not os.path.samestat(identity, current) or identity.st_size != current.st_size:
                raise OSError("构建期间发布文件发生变化，未宣称发布成功")
        if not os.path.samestat(destination.lstat(), os.fstat(directory)):
            raise OSError("构建期间输出目录发生变化，发布文件可能留在原目录")
    finally:
        os.close(directory)
    return destination


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="构建本项目的可分享运行包和源码包")
    parser.add_argument("--output", type=Path, default=ROOT / "发布包" / datetime.now().strftime("%Y%m%d-%H%M%S"))
    try:
        print("发布包已生成：" + str(build(parser.parse_args().output)))
    except (OSError, ValueError) as error:
        print("构建失败：" + str(error), file=sys.stderr)
        raise SystemExit(1)
