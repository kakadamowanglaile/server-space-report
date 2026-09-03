"""单文件包入口：将检查失败与取消状态传递给调用者。"""
import sys

sys.dont_write_bytecode = True

from space_report.cli import main

raise SystemExit(main())
