# Server Space Report

**English** | [简体中文](README.zh-CN.md)

**服务器空间去哪了** is a read-only disk-space reporting tool for a local Linux server. It helps you inspect filesystem usage, large directories, system journals, Docker storage, and deleted files that are still held open by a process.

It does not delete files, clear logs, restart services, request elevated privileges, or upload information. It needs no AI service, account, or third-party Python package. By default, it prints results without saving a report.

**Language support:** the documentation is available in English and Simplified Chinese. CLI help, terminal output, and JSON field names are currently Chinese. There is no `--lang` option yet. Keep the Chinese filenames in the commands below unchanged.

**Development status:** version 0.1.2 is still undergoing validation. It includes fixes for low file-descriptor limits, repeated cancellation, path replacement during a check, and memory growth across repeated scans. The 0.1.1 results below are historical records, not evidence that 0.1.2 has completed validation.

## Quick start

Requires **Linux and Python 3.10 or later**. Clone this repository or extract a source archive, then run these commands from the project root:

```sh
python3 -B 代码/空间去哪了.py
python3 -B 代码/空间去哪了.py --path /var --deep
python3 -B 代码/空间去哪了.py --path / --timeout 30 --output ./reports
```

The tool creates an independent `空间报告-*` directory when `--output` is supplied, containing `报告.txt` and `报告.json`. Existing reports are not overwritten.

The single-file `.pyz` build also requires Python. If you have downloaded or built that artifact, run:

```sh
python3 服务器空间去哪了-0.1.2.pyz --help
python3 服务器空间去哪了-0.1.2.pyz --path /var --deep --output ./reports
```

See [Releases](https://github.com/kakadamowanglaile/server-space-report/releases) for published artifacts and their validation notes. A version mentioned in the source tree may still be a development version.

The system-journal check requires `journalctl`. Docker checks require the local Docker CLI and `/var/run/docker.sock`. Missing tools are reported explicitly; other checks continue. Nothing is installed automatically. An ordinary user can run the tool, but may not be able to inspect all journals, container data, or other users' processes. The tool never asks for a password or elevates privileges itself.

## Options and exit codes

| Option | Behavior |
|---|---|
| No options | Basic checks without traversing the directory tree |
| `--path DIRECTORY` | Check the filesystem containing the directory; defaults to `/`; symlink components are rejected |
| `--deep` | Traverse the selected directory and show the top 20 large directories; do not follow symlinks or cross submounts |
| `--timeout SECONDS` | Per-check time limit, default 30 seconds; returning partial results may require a short additional handoff interval |
| `--output DIRECTORY` | Save text and JSON reports in a new timestamped subdirectory; symlink components are rejected |
| `--version` | Show the version |
| `Ctrl+C` | Cancel remaining checks; preserve displayed results and save partial results if an output directory was requested |

Exit codes: `0` means all executed checks completed; `1` means at least one check is incomplete, denied, timed out, or failed; `2` means invalid arguments, an unsupported operating system, or a report-saving failure; `130` means cancellation. Exit code `1` can occur for ordinary users with limited permissions; completed results remain useful.

## Interpreting the report

- **Filesystem usage** describes the entire filesystem. Selecting `/var` does not make those totals exclusive to `/var`.
- **Large directories** are collected only with `--deep` and use allocated blocks. Hard links are counted once; attribution across directories depends on traversal order.
- **System journals** cover active and archived journals visible to the current user. Journal contents are not read, and their usage cannot be assigned precisely to the selected directory.
- **Docker categories** describe the local daemon as a whole. Values derived from Docker's formatted output are estimates. A Docker value marked reclaimable is not a recommendation to delete it.
- **Deleted but open files** are located through Linux process file descriptors on the selected filesystem. Multiple references are deduplicated. Files retained only by memory mappings are not covered.

These numbers can overlap: **do not add them together or treat them as deletion instructions**. Unknown, denied, timed-out, and failed results do not mean zero usage.

Reports may contain paths, process names, and container identifiers. Review them before sharing. Read-only means the tool does not intentionally modify the inspected objects; directory reads may update access times or produce OS audit records. Deep scans also generate read load, so use a narrower path for very large trees.

## Validation and limitations

The following are **historical version 0.1.1 integration results**, recorded on 2026-09-03. Each combination passed 147 automated tests and 133 assertions across 19 real scenarios, with no skips or failures. Those scenarios covered disk and inode exhaustion, permissions, mount boundaries, Docker storage and log rotation, concurrent object disappearance, timeouts, cancellation, and report saving.

| System | Architecture | Python | Docker |
|---|---|---|---|
| Ubuntu 24.04.4 | ARM64 | 3.12.3 | 29.1.3 |
| Debian 12.15 | ARM64 | 3.11.2 | 20.10.24 |
| Debian 12.15 | ARM64 | 3.10.21, built from official source | 20.10.24 |
| Debian 12.15 | x86_64, full-system software emulation | 3.11.2 | 20.10.24 |

The x86_64 integration environment used a full Debian kernel under software emulation; it did not validate native hardware performance. The historical 0.1.1 macOS development run passed 144 of 147 tests and skipped three Linux-only tests. It is not evidence of Linux integration behavior. GitHub Actions unit tests are separate from these integration records.

Windows, remote or rootless Docker, using a container as a substitute for host inspection, and snapshot/storage-pool reconciliation are outside the initial scope. Real production workloads, long-term large-scale behavior, and adoption by external users have not been validated. Passing tests cannot establish correctness in every environment.

Detailed references are currently in Chinese: [known limitations](文档/已知限制.md), [report format](文档/报告格式.md), [validation requirements](文档/验收说明.md), and a [synthetic report example](文档/报告示例.txt).

## Contributing and support

Open an [issue](https://github.com/kakadamowanglaile/server-space-report/issues) with your operating system, Python version, command, expected behavior, and actual result. Share only the necessary, redacted details.

See the [contribution guide](CONTRIBUTING.md) for development, tests, the fork workflow, and what can be committed. Keep both language versions of the documentation in sync. The [changelog](更新记录.md) is currently in Chinese.

## License

[MIT](LICENSE). Source archives use an explicit allowlist and exclude virtual machines, test keys, download caches, and real machine reports.
