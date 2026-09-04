"""本地交付核对：归档安全、源码一致性、运行入口、解压后完整测试。"""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path, PurePosixPath
import subprocess
import sys
import tempfile
import zipfile

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]


def require(condition, message):
    if not condition:
        raise ValueError(message)


def members(path):
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        require(len(names) == len(set(names)), "归档含重复成员")
        for entry in archive.infolist():
            name = PurePosixPath(entry.filename)
            require(not name.is_absolute() and ".." not in name.parts, "归档路径不安全")
            require(((entry.external_attr >> 16) & 0o170000) != 0o120000, "归档含符号链接")
        require(archive.testzip() is None, "归档校验失败")
        return {name: archive.read(name) for name in names}


def check(release, candidate, expected_tests):
    manifest = json.loads((release / "发布清单.json").read_text(encoding="utf-8"))
    version = manifest["版本"]
    prefix = "服务器空间去哪了-" + version
    require(len(manifest["文件"]) == 2, "发布清单必须列出两个文件")
    for entry in manifest["文件"]:
        require(Path(entry["名称"]).name == entry["名称"], "发布清单文件名必须是单层路径")
        content = (release / entry["名称"]).read_bytes()
        require(len(content) == entry["字节"], "发布文件字节数与清单不一致")
        require(hashlib.sha256(content).hexdigest() == entry["SHA256"], "发布文件 SHA256 与清单不一致")

    spec = importlib.util.spec_from_file_location("delivery_builder", ROOT / "工具/构建发布包.py")
    builder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(builder)
    snapshot = builder._snapshot()
    runtime = members(release / (prefix + ".pyz"))
    source = members(release / (prefix + "-源码.zip"))
    require(source == {prefix + "/" + name: data for name, data in snapshot.items()}, "发布源码与当前白名单内容不同")
    require(runtime == {name[len("代码/"):]: data for name, data in snapshot.items() if name.startswith("代码/")}, "运行包与当前白名单内容不同")
    require(runtime == members(candidate / (prefix + ".pyz")), "正式运行代码不同于Linux实测候选")

    commands = []
    def run(argv, cwd, expected, timeout=90):
        result = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, encoding="utf-8",
                                errors="backslashreplace", timeout=timeout)
        commands.append({"命令": [str(x) for x in argv], "退出码": result.returncode,
                         "标准输出": result.stdout, "标准错误": result.stderr})
        require(result.returncode == expected, commands[-1])
        return result

    temporary_parent = ROOT / "测试环境" / "临时"
    temporary_parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="正式交付核对-", dir=temporary_parent) as directory:
        directory = Path(directory)
        with zipfile.ZipFile(release / (prefix + "-源码.zip")) as archive:
            archive.extractall(directory)
        extracted = directory / prefix
        for entry in (release / (prefix + ".pyz"), extracted / "代码/空间去哪了.py"):
            help_result = run([sys.executable, "-B", str(entry), "--help"], extracted, 0)
            require("--deep" in help_result.stdout and "只读空间报告" in help_result.stdout, "运行入口帮助内容不完整")
            require(version in run([sys.executable, "-B", str(entry), "--version"], extracted, 0).stdout, "运行入口版本不匹配")
            run([sys.executable, "-B", str(entry), "--timeout", "0"], extracted, 2)
        tests = run([sys.executable, "-B", "-W", "error::ResourceWarning", "-m", "unittest",
                     "discover", "-s", "测试", "-p", "test_*.py", "-v"], extracted, 0, timeout=180)
        require(f"Ran {expected_tests} test" in tests.stderr, "测试数量变化，需重新核对验收报告")
    return {"版本": version, "核对通过": True, "运行包成员数": len(runtime), "源码包成员数": len(source),
            "运行代码与实测候选逐成员一致": True, "源码与当前白名单逐成员一致": True,
            "发布清单": manifest, "运行环境": sys.version, "命令结果": commands}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="核对本项目正式交付与冻结候选，不修改两者")
    parser.add_argument("--release", type=Path, required=True)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--test-count", type=int, required=True,
                        help="候选验收记录中的预期测试总数；必须明确指定，不沿用历史默认值")
    args = parser.parse_args()
    print(json.dumps(check(args.release.resolve(), args.candidate.resolve(), args.test_count), ensure_ascii=False, indent=2))
