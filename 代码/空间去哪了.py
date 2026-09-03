"""源码运行入口：默认不产生 Python 字节码缓存。"""
import sys

sys.dont_write_bytecode = True

from space_report.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
