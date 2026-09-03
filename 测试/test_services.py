"""服务采集测试：只替换外部命令，日志使用真实临时文件元数据。"""

import importlib
from contextlib import contextmanager
import gzip
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch


try:
    services = importlib.import_module("space_report.services")
except ModuleNotFoundError:
    services = None


TEMP_ROOT = Path(__file__).resolve().parents[1] / "测试环境" / "临时"
CID = "a" * 64


def completed(stdout="", returncode=0, stderr=""):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(services, "服务采集尚未实现，不能给出采集结果")
        self.temp = tempfile.TemporaryDirectory(prefix="服务测试-", dir=TEMP_ROOT)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def journal(self, response, uid=0):
        with patch.object(services, "run_command", side_effect=response if isinstance(response, Exception) else None,
                          return_value=response), patch.object(services.os, "geteuid", return_value=uid):
            return services.collect_journal(str(self.root))

    def test_journal_reports_disk_usage_without_reading_messages(self):
        def command(argv, timeout, env=None):
            self.assertEqual(argv, ["journalctl", "--disk-usage", "--no-pager"])
            self.assertEqual(env["LC_ALL"], "C")
            return completed("Archived and active journals take up 8.0M in the file system.\n")
        with patch.object(services, "run_command", side_effect=command), patch.object(services.os, "geteuid", return_value=0):
            result = services.collect_journal(str(self.root))
        self.assertEqual(result["状态"], "完成")
        self.assertEqual(result["数据"]["字节估算"], 8388608)
        self.assertTrue(result["数据"]["数值为近似"])
        self.assertIn("正文", " ".join(result["说明"]))

    def test_nonroot_journal_is_not_claimed_complete_even_without_warning(self):
        result = self.journal(completed("Archived and active journals take up 1.0K in the file system."), uid=1000)
        self.assertEqual(result["状态"], "部分完成")
        self.assertIn("可见", result["范围"])

    def test_journal_permission_missing_timeout_are_distinguished(self):
        cases = [(completed(returncode=1, stderr="Permission denied: private token"), "权限不足"),
                 (FileNotFoundError(), "工具缺失"),
                 (subprocess.TimeoutExpired("journalctl", 1, output="private token"), "超时")]
        for response, expected in cases:
            with self.subTest(expected=expected):
                result = self.journal(response)
                self.assertEqual(result["状态"], expected)
                self.assertNotIn("private token", json.dumps(result))

    def test_unrecognized_journal_output_cannot_be_zero_success(self):
        result = self.journal(completed("unexpected output private token"))
        self.assertEqual(result["状态"], "失败")
        self.assertNotIn("字节估算", result["数据"])
        self.assertNotIn("private token", json.dumps(result))

    def docker(self, rows=None, ids=None, df_error=None, info_error=None, list_error=None,
               root_override=None, timeout=30):
        rows = rows if rows is not None else [
            {"Type": "Images", "TotalCount": "3", "Active": "1", "Size": "1.2GB", "Reclaimable": "800MB (66%)"},
            {"Type": "Containers", "TotalCount": "1", "Active": "1", "Size": "3.2kB", "Reclaimable": "0B (0%)"},
            {"Type": "Local Volumes", "TotalCount": "2", "Active": "1", "Size": "10MB", "Reclaimable": "5MB (50%)"},
            {"Type": "Build Cache", "TotalCount": "2", "Active": "0", "Size": "1MB", "Reclaimable": "1MB"},
        ]
        real_isdir = os.path.isdir
        def command(argv, timeout, env=None):
            self.assertEqual(argv[:5], ["docker", "--config", "/proc/self/fd", "--host", "unix:///var/run/docker.sock"])
            self.assertFalse(any(name.startswith("DOCKER_") or "PROXY" in name.upper() for name in env))
            self.assertEqual(env["LC_ALL"], "C")
            self.assertNotIn(".Env", " ".join(argv))
            self.assertNotIn(".Config}}", " ".join(argv))
            if argv[5] == "info":
                if isinstance(info_error, Exception):
                    raise info_error
                return info_error or completed(json.dumps({"DockerRootDir": str(root_override or self.root), "LoggingDriver": "json-file"}))
            if argv[5:7] == ["system", "df"]:
                if isinstance(df_error, Exception):
                    raise df_error
                return df_error or completed("\n".join(json.dumps(row) for row in rows))
            if argv[5:7] == ["container", "ls"]:
                if isinstance(list_error, Exception):
                    raise list_error
                if list_error is not None:
                    return list_error
                return completed("\n".join(json.dumps(cid) for cid in (ids or [])))
            self.fail("不允许的外部命令：" + repr(argv))
        with patch.object(services, "run_command", side_effect=command), patch.object(services.os.path, "isdir", side_effect=lambda p: p == "/proc/self/fd" or real_isdir(p)):
            return services.collect_docker(str(self.root), timeout=timeout)

    def test_container_log_metadata_does_not_request_container_config(self):
        logdir = self.root / "containers" / CID
        logdir.mkdir(parents=True)
        current = logdir / (CID + "-json.log")
        current.write_bytes(b"123")
        result = self.docker(ids=[CID])
        self.assertEqual(result["数据"]["容器日志"][0]["逻辑字节"], 3)

    def test_directory_swap_at_scan_cannot_redirect_to_outside_logs(self):
        logdir = self.root / "containers" / CID
        logdir.mkdir(parents=True)
        current = logdir / (CID + "-json.log")
        current.write_bytes(b"123")
        outside = self.root / "非容器资料"
        outside.mkdir()
        (outside / (CID + "-json.log")).write_bytes(b"PRIVATE" * 100)
        original_scan = os.scandir
        swapped = False
        def swap_then_scan(location):
            nonlocal swapped
            if not swapped:
                swapped = True
                logdir.rename(logdir.with_name(CID + "-原目录"))
                logdir.symlink_to(outside, target_is_directory=True)
            return original_scan(location)
        with patch.object(services.os, "scandir", side_effect=swap_then_scan):
            result = self.docker(ids=[CID])
        self.assertEqual(result["数据"]["容器日志"][0]["逻辑字节"], 3,
                         "目录名在检查后被交换，仍应只读取已打开原目录的元数据")

    def test_symlink_in_docker_root_ancestors_is_not_followed(self):
        actual = self.root / "实际数据目录"
        logdir = actual / "containers" / CID
        logdir.mkdir(parents=True)
        current = logdir / (CID + "-json.log")
        current.write_bytes(b"PRIVATE")
        alias = self.root / "目录链接"
        alias.symlink_to(actual, target_is_directory=True)
        result = self.docker(ids=[CID], root_override=alias)
        self.assertEqual(result["状态"], "部分完成")
        self.assertFalse(any("逻辑字节" in row for row in result["数据"]["容器日志"]))

    def test_timeout_during_directory_open_does_not_report_empty_success(self):
        (self.root / "containers" / CID).mkdir(parents=True)
        now = [0.0]
        real_open = os.open
        def expire_on_containers(name, flags, *args, **kwargs):
            fd = real_open(name, flags, *args, **kwargs)
            if name == "containers":
                now[0] = 100.0
            return fd
        with patch.object(services.time, "monotonic", side_effect=lambda: now[0]), patch.object(services.os, "open", side_effect=expire_on_containers):
            result = self.docker(ids=[CID], timeout=10)
        self.assertEqual(result["数据"]["容器日志状态"], "超时")
        self.assertEqual(result["状态"], "部分完成")
        self.assertNotIn("逻辑字节", result["数据"]["容器日志"][0])

    def test_no_supported_logs_does_not_claim_zero_storage_or_known_driver(self):
        logdir = self.root / "containers" / CID
        logdir.mkdir(parents=True)
        (logdir / "config.v2.json").write_bytes(b"PRIVATE_CONFIG")
        result = self.docker(ids=[CID])
        row = result["数据"]["容器日志"][0]
        self.assertEqual(row["状态"], "不适用")
        self.assertNotIn("逻辑字节", row)
        self.assertNotIn("日志驱动", row)
        self.assertIn("其他日志驱动未统计", row["说明"])

    def test_existing_empty_json_log_keeps_measured_zero(self):
        logdir = self.root / "containers" / CID
        logdir.mkdir(parents=True)
        (logdir / (CID + "-json.log")).write_bytes(b"")
        result = self.docker(ids=[CID])
        row = result["数据"]["容器日志"][0]
        self.assertEqual(row["状态"], "完成")
        self.assertEqual(row["文件数"], 1)
        self.assertEqual(row["逻辑字节"], 0)
        self.assertEqual(row["已分配字节"], 0)

    def test_unreadable_log_directory_has_no_fabricated_size(self):
        real_open = os.open
        def deny_containers(name, flags, *args, **kwargs):
            if name == "containers":
                raise PermissionError("拒绝读取")
            return real_open(name, flags, *args, **kwargs)
        with patch.object(services.os, "open", side_effect=deny_containers):
            result = self.docker(ids=[CID])
        row = result["数据"]["容器日志"][0]
        self.assertEqual(row["状态"], "权限不足")
        self.assertNotIn("逻辑字节", row)
        self.assertNotIn("已分配字节", row)

    def test_deadline_after_last_metadata_read_keeps_observed_bytes(self):
        logdir = self.root / "containers" / CID
        logdir.mkdir(parents=True)
        (logdir / (CID + "-json.log")).write_bytes(b"123")
        now = [0.0]
        real_stat = os.stat
        def expire_after_metadata(name, *args, **kwargs):
            result = real_stat(name, *args, **kwargs)
            if name == CID + "-json.log" and kwargs.get("dir_fd") is not None:
                now[0] = 100.0
            return result
        with patch.object(services.time, "monotonic", side_effect=lambda: now[0]), patch.object(services.os, "stat", side_effect=expire_after_metadata):
            result = self.docker(ids=[CID], timeout=10)
        self.assertEqual(result["数据"]["容器日志状态"], "超时")
        self.assertEqual(result["数据"]["容器日志"][0]["逻辑字节"], 3)

    def test_docker_summary_keeps_four_categories_without_adding_shared_layers(self):
        with patch.dict(os.environ, {"DOCKER_HOST": "ssh://private-host", "DOCKER_CONTEXT": "production", "DOCKER_TLS_VERIFY": "1", "HTTPS_PROXY": "secret-proxy"}):
            result = self.docker()
        self.assertEqual(result["状态"], "完成")
        categories = result["数据"]["分类"]
        self.assertEqual([row["类别"] for row in categories], ["镜像", "容器可写层", "本地卷", "构建缓存"])
        self.assertEqual(categories[0]["字节估算"], 1200000000)
        self.assertTrue(categories[0]["数值为近似"])
        self.assertNotIn("合计字节", result["数据"])
        self.assertTrue(result["数据"]["数据目录与目标同文件系统"])
        text = json.dumps(result, ensure_ascii=False)
        self.assertNotIn("secret-proxy", text)
        self.assertNotIn("private-host", text)
        self.assertIn("共享", text)

    def test_docker_json_logs_include_rotations_but_not_contents_or_unrelated_files(self):
        logdir = self.root / "containers" / CID
        logdir.mkdir(parents=True)
        current = logdir / (CID + "-json.log")
        current.write_bytes(b"PRIVATE_TOKEN=abc")
        rotated = logdir / (CID + "-json.log.1")
        rotated.write_bytes(b"0123456789")
        (logdir / "config.v2.json").write_bytes(b"DO_NOT_READ")
        (logdir / (CID + "-json.log.2")).symlink_to(rotated)
        result = self.docker(ids=[CID])
        logs = result["数据"]["容器日志"]
        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0]["文件数"], 2)
        self.assertEqual(logs[0]["逻辑字节"], 27)
        self.assertNotIn("PRIVATE_TOKEN", json.dumps(result))
        self.assertIn("轮转", logs[0]["范围"])

    def test_missing_standard_log_directory_is_explicitly_out_of_scope(self):
        result = self.docker(ids=[CID])
        self.assertEqual(result["状态"], "部分完成")
        self.assertEqual(result["数据"]["容器日志"][0]["状态"], "不适用")

    def test_symlink_container_directory_is_not_followed(self):
        parent = self.root / "containers"
        parent.mkdir()
        outside = self.root / "其他资料"
        outside.mkdir()
        (outside / (CID + "-json.log")).write_bytes(b"PRIVATE")
        (parent / CID).symlink_to(outside, target_is_directory=True)
        result = self.docker(ids=[CID])
        self.assertEqual(result["状态"], "部分完成")
        self.assertEqual(result["数据"]["容器日志"][0]["状态"], "不适用")
        self.assertNotIn("逻辑字节", result["数据"]["容器日志"][0])

    def test_docker_unavailable_is_not_zero_usage(self):
        for response, status in [(FileNotFoundError(), "工具缺失"),
                                 (subprocess.TimeoutExpired("docker", 1), "超时"),
                                 (completed(returncode=1, stderr="permission denied secret"), "权限不足"),
                                 (completed(returncode=1, stderr="Cannot connect to Docker daemon secret"), "失败")]:
            with self.subTest(status=status):
                result = self.docker(info_error=response)
                self.assertEqual(result["状态"], status)
                self.assertNotIn("分类", result["数据"])
                self.assertNotIn("secret", json.dumps(result))

    def test_docker_info_checks_server_errors_even_with_zero_exit(self):
        for code in (0, 1):
            with self.subTest(code=code):
                result = self.docker(info_error=completed(
                    json.dumps({"ServerErrors": ["permission denied secret"]}), returncode=code))
                self.assertEqual(result["状态"], "权限不足")
                self.assertNotIn("分类", result["数据"])
                self.assertNotIn("secret", json.dumps(result))

    def test_docker_info_other_server_errors_are_failure_without_details(self):
        for errors in (["Cannot connect secret"], "permission denied secret", [None], []):
            with self.subTest(errors=errors):
                result = self.docker(info_error=completed(json.dumps({"ServerErrors": errors})))
                self.assertEqual(result["状态"], "失败")
                self.assertNotIn("secret", json.dumps(result))

    def test_info_template_handles_missing_server_before_reading_info_fields(self):
        self.assertTrue(services._INFO.startswith('{{if .ServerErrors}}'))
        self.assertIn('{{json .ServerErrors}}', services._INFO)
        self.assertIn('{{else}}', services._INFO)
        self.assertNotIn('{{json .}}', services._INFO)

    def test_malformed_docker_category_does_not_fabricate_zero(self):
        result = self.docker(rows=[{"Type": "Images", "Size": "not-size"}])
        self.assertEqual(result["状态"], "部分完成")
        self.assertNotIn("字节估算", result["数据"]["分类"][0])

    def test_df_timeout_preserves_daemon_scope_but_reports_partial(self):
        result = self.docker(df_error=subprocess.TimeoutExpired("docker", 1))
        self.assertEqual(result["状态"], "部分完成")
        self.assertEqual(result["数据"]["分类状态"], "超时")
        self.assertEqual(result["数据"]["数据目录"], str(self.root))

    def test_missing_proc_config_directory_never_uses_user_config(self):
        with patch.object(services.os.path, "isdir", return_value=False), patch.object(services, "run_command", side_effect=AssertionError("不应启动 Docker")):
            result = services.collect_docker(str(self.root))
        self.assertEqual(result["状态"], "不适用")

    def test_invalid_root_metadata_returns_failure_not_an_exception(self):
        for payload in ({"DockerRootDir": "/tmp/PRIVATE\x00"}, [], None, {"DockerRootDir": 42}):
            with self.subTest(kind=type(payload).__name__):
                try:
                    result = self.docker(info_error=completed(json.dumps(payload)))
                except (ValueError, TypeError) as error:
                    self.fail("畸形守护进程输出不应中断整个报告：" + type(error).__name__)
                self.assertEqual(result["状态"], "失败")
                self.assertNotIn("PRIVATE", json.dumps(result))

    def test_malformed_numeric_metadata_cannot_escape_as_huge_numbers_or_unitless_bytes(self):
        for size, count in (("123", "1"), ("0.1B", "1"), ("9" * 5000 + "GB", "1"), ("1MB", "9" * 5000)):
            with self.subTest(size_length=len(size), count_length=len(count)):
                rows = [{"Type": "Images", "TotalCount": count, "Active": "0", "Size": size, "Reclaimable": "0B (0%)"}]
                try:
                    result = self.docker(rows=rows)
                    json.dumps(result)
                except (ValueError, OverflowError) as error:
                    self.fail("异常数字应标为未知，不能破坏报告：" + type(error).__name__)
                row = result["数据"]["分类"][0]
                if count == "1":
                    self.assertNotIn("字节估算", row)
                else:
                    self.assertNotIn("总数", row)

    def test_conflicting_journal_totals_do_not_choose_a_successful_zero(self):
        result = self.journal(completed(
            "Archived and active journals take up 0B in the file system.\n"
            "Archived and active journals take up 8.0M in the file system.\n"))
        self.assertEqual(result["状态"], "失败")
        self.assertNotIn("字节估算", result["数据"])

    def test_late_directory_enumeration_error_preserves_known_log_size(self):
        logdir = self.root / "containers" / CID
        logdir.mkdir(parents=True)
        (logdir / (CID + "-json.log")).write_bytes(b"123")
        original_scan = os.scandir
        for error_type, expected in ((PermissionError, "权限不足"), (OSError, "部分完成"), (TimeoutError, "超时")):
            with self.subTest(error=error_type.__name__):
                @contextmanager
                def interrupted_scan(fd):
                    with original_scan(fd) as entries:
                        def interrupted_entries():
                            yield next(entries)
                            raise error_type("PRIVATE")
                        yield interrupted_entries()
                with patch.object(services.os, "scandir", side_effect=interrupted_scan):
                    result = self.docker(ids=[CID])
                row = result["数据"]["容器日志"][0]
                self.assertEqual(row["状态"], expected)
                self.assertEqual(row.get("逻辑字节"), 3)
                self.assertNotIn("PRIVATE", json.dumps(result))

    def test_rotated_name_replacement_is_detected_instead_of_silent_complete(self):
        logdir = self.root / "containers" / CID
        logdir.mkdir(parents=True)
        current = logdir / (CID + "-json.log")
        current.write_bytes(b"123")
        original_scan = os.scandir
        @contextmanager
        def rotate_after_listing(fd):
            with original_scan(fd) as entries:
                listed = list(entries)
                current.rename(logdir / (CID + "-json.log.1"))
                current.write_bytes(b"123456789")
                yield iter(listed)
        with patch.object(services.os, "scandir", side_effect=rotate_after_listing):
            result = self.docker(ids=[CID])
        self.assertEqual(result["数据"]["容器日志"][0]["状态"], "部分完成")

    def test_all_matching_files_denied_are_not_mislabeled_as_unsupported_driver(self):
        logdir = self.root / "containers" / CID
        logdir.mkdir(parents=True)
        (logdir / (CID + "-json.log")).write_bytes(b"123")
        original_stat = os.stat
        def denied_stat(name, *args, **kwargs):
            if name == CID + "-json.log" and kwargs.get("dir_fd") is not None:
                raise PermissionError("PRIVATE")
            return original_stat(name, *args, **kwargs)
        with patch.object(services.os, "stat", side_effect=denied_stat):
            result = self.docker(ids=[CID])
        row = result["数据"]["容器日志"][0]
        self.assertEqual(row["状态"], "权限不足")
        self.assertNotIn("逻辑字节", row)
        self.assertNotIn("其他日志驱动", row["说明"])

    def test_excessive_json_nesting_is_failure_without_interrupting_other_categories(self):
        nested = "[" * 20000 + '"PRIVATE"' + "]" * 20000
        for stage in ("info", "df", "list"):
            with self.subTest(stage=stage):
                parameters = {stage + "_error": completed(nested)}
                try:
                    result = self.docker(**parameters)
                except RecursionError:
                    self.fail("JSON 嵌套过深应按格式错误处理，不应中断报告")
                self.assertEqual(result["状态"], "失败" if stage == "info" else "部分完成")
                self.assertNotIn("PRIVATE", json.dumps(result))
                if stage == "list":
                    self.assertEqual(len(result["数据"]["分类"]), 4)

    def test_malformed_json_and_container_ids_never_become_paths(self):
        for raw in ('{"DockerRootDir":', '"PRIVATE"', '{"ServerErrors":null}'):
            with self.subTest(info=raw):
                result = self.docker(info_error=completed(raw))
                self.assertEqual(result["状态"], "失败")
                self.assertNotIn("PRIVATE", json.dumps(result))
        for raw in ('"../../PRIVATE"', "null", "[]", '"' + CID + '"\ninvalid PRIVATE'):
            with self.subTest(ids=raw):
                result = self.docker(list_error=completed(raw))
                self.assertEqual(result["数据"]["容器日志状态"], "失败")
                self.assertEqual(result["数据"]["容器日志"], [])
                self.assertNotIn("PRIVATE", json.dumps(result))
        result = self.docker(df_error=completed(
            '[]\n{"Type":{}}\nPRIVATE\n'
            '{"Type":"Build Cache","TotalCount":"2","Active":"0","Size":"8.388MB"}'))
        self.assertEqual(result["数据"]["分类状态"], "部分完成")
        self.assertEqual(result["数据"]["分类"][0]["字节估算"], 8388000)
        self.assertNotIn("PRIVATE", json.dumps(result))

    def test_nested_count_fields_are_rejected_without_recursive_string_conversion(self):
        nested = "[" * 2000 + '"PRIVATE"' + "]" * 2000
        raw = ('{"Type":"Images","TotalCount":' + nested +
               ',"Active":' + nested + ',"Size":"1MB","Reclaimable":"0B"}')
        try:
            result = self.docker(df_error=completed(raw))
        except RecursionError:
            self.fail("非数字数量字段应拒绝，不应递归转换为字符串")
        self.assertEqual(result["状态"], "部分完成")
        self.assertNotIn("PRIVATE", json.dumps(result))
        for row in result["数据"]["分类"]:
            self.assertNotIn("总数", row)
            self.assertNotIn("活动数量", row)

    def test_nonempty_build_cache_stays_separate_from_shared_image_estimate(self):
        result = self.docker()
        image, container, volume, cache = result["数据"]["分类"]
        self.assertEqual(cache["类别"], "构建缓存")
        self.assertEqual(cache["总数"], 2)
        self.assertEqual(cache["字节估算"], 1000000)
        self.assertTrue(all(row["数值为近似"] for row in (image, container, volume, cache)))
        self.assertNotIn("合计字节", result["数据"])
        self.assertIn("分类与日志不合计", " ".join(result["说明"]))

    def test_gzip_rotations_use_compressed_metadata_and_deduplicate_hardlinks(self):
        logdir = self.root / "containers" / CID
        logdir.mkdir(parents=True)
        current = logdir / (CID + "-json.log")
        current.write_bytes(b"123")
        packed = gzip.compress(b"PRIVATE" * 10000)
        rotated = logdir / (CID + "-json.log.1.gz")
        rotated.write_bytes(packed)
        os.link(rotated, logdir / (CID + "-json.log.2.gz"))
        (logdir / (CID + "-json.log.backup")).write_bytes(b"PRIVATE" * 100)
        result = self.docker(ids=[CID, CID])
        self.assertEqual(len(result["数据"]["容器日志"]), 1)
        row = result["数据"]["容器日志"][0]
        self.assertEqual(row["文件数"], 2)
        self.assertEqual(row["逻辑字节"], 3 + len(packed))
        self.assertEqual(row["已分配字节"], (current.stat().st_blocks + rotated.stat().st_blocks) * 512)
        self.assertNotIn("PRIVATE", json.dumps(result))

    def test_shared_hardlink_between_containers_is_not_reported_as_unsupported(self):
        other_id = "b" * 64
        directories = [self.root / "containers" / cid for cid in (CID, other_id)]
        for directory in directories:
            directory.mkdir(parents=True)
        first = directories[0] / (CID + "-json.log")
        first.write_bytes(b"123")
        os.link(first, directories[1] / (other_id + "-json.log"))
        result = self.docker(ids=[CID, other_id])
        first_row, second_row = result["数据"]["容器日志"]
        self.assertEqual(first_row["逻辑字节"], 3)
        self.assertEqual(second_row["状态"], "部分完成")
        self.assertEqual(second_row["重复硬链接数"], 1)
        self.assertNotIn("其他日志驱动", second_row["说明"])

    def test_container_listing_failures_keep_successful_category_metadata(self):
        for error, status in ((completed(returncode=1, stderr="permission denied PRIVATE"), "权限不足"),
                              (subprocess.TimeoutExpired("docker", 1, output="PRIVATE"), "超时"),
                              (RuntimeError("PRIVATE"), "失败")):
            with self.subTest(status=status):
                result = self.docker(list_error=error)
                self.assertEqual(result["状态"], "部分完成")
                self.assertEqual(result["数据"]["容器日志状态"], status)
                self.assertEqual(len(result["数据"]["分类"]), 4)
                self.assertNotIn("PRIVATE", json.dumps(result))

    def test_deleted_entry_keeps_other_observations_and_reports_partial(self):
        logdir = self.root / "containers" / CID
        logdir.mkdir(parents=True)
        current = logdir / (CID + "-json.log")
        current.write_bytes(b"123")
        rotated = logdir / (CID + "-json.log.1")
        rotated.write_bytes(b"45")
        original_scan = os.scandir
        @contextmanager
        def delete_after_listing(fd):
            with original_scan(fd) as entries:
                listed = list(entries)
                rotated.unlink()
                yield iter(listed)
        with patch.object(services.os, "scandir", side_effect=delete_after_listing):
            result = self.docker(ids=[CID])
        row = result["数据"]["容器日志"][0]
        self.assertEqual(row["状态"], "部分完成")
        self.assertEqual(row["逻辑字节"], 3)

    def test_journal_valid_units_zero_and_warning_visibility(self):
        for value, amount in (("0B", 0), ("1.0K", 1024), ("1.5M", 1572864), ("1.5GiB", 1610612736)):
            with self.subTest(value=value):
                result = self.journal(completed("Archived and active journals take up " + value + " in the file system.\n"))
                self.assertEqual(result["状态"], "完成")
                self.assertEqual(result["数据"]["字节估算"], amount)
        result = self.journal(completed("Archived and active journals take up 0B in the file system.", stderr="PRIVATE"))
        self.assertEqual(result["状态"], "部分完成")
        self.assertEqual(result["数据"]["字节估算"], 0)
        self.assertNotIn("PRIVATE", json.dumps(result))

    def test_journal_invalid_values_never_become_zero_or_expose_raw_text(self):
        for value in ("-1B", "0.1B", "NaN", "infinity", "12", "9" * 5000 + "M"):
            with self.subTest(value_length=len(value)):
                result = self.journal(completed("Archived and active journals take up " + value + " in the file system."))
                self.assertEqual(result["状态"], "失败")
                self.assertNotIn("字节估算", result["数据"])


if __name__ == "__main__":
    unittest.main()
