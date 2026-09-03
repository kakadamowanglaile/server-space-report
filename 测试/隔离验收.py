#!/usr/bin/env python3
"""真实 Linux 隔离验收；不参加默认 unittest discovery，不得在业务机执行。"""
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import errno
import hashlib
import json
import os
from pathlib import Path
import re
import selectors
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time
import traceback

BASE = Path('/var/tmp/spaceprobe-integration')
MARKER = BASE / '专用虚拟机标记.json'
CONFIRM = 'SPACEPROBE-LIMA-ONLY'
MIB = 1024 * 1024
ENV = {'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'LC_ALL': 'C',
       'LANG': 'C', 'PYTHONDONTWRITEBYTECODE': '1'}
WORKER = '''import importlib,json,sys
sys.path.insert(0,sys.argv[1])
module=importlib.import_module('space_report.'+sys.argv[2])
print(json.dumps(getattr(module,sys.argv[3])(sys.argv[4],float(sys.argv[5])),ensure_ascii=True))
'''


def check(condition, message):
    if not condition:
        raise AssertionError(message)


def safe_child(path):
    path = Path(path)
    check(path.is_absolute() and path.resolve() == path, '路径必须为实际绝对路径，不能经过符号链接')
    check(path != BASE and BASE in path.parents, '路径必须位于专用验收目录内部')
    return path


def guard(args):
    check(args.execute and args.confirm_test_vm == CONFIRM, '缺少显式执行确认')
    check(sys.platform == 'linux' and os.geteuid() == 0, '只允许 Linux 专用虚拟机管理员执行')
    check(socket.gethostname() in {'lima-u24', 'lima-d12', 'spaceprobe-x86'}, '不是允许的专用虚拟机')
    check(BASE.resolve() == BASE and not BASE.is_symlink(), '专用目录不能是路径别名')
    info = MARKER.lstat()
    check(stat.S_ISREG(info.st_mode) and info.st_uid == 0 and stat.S_IMODE(info.st_mode) == 0o600,
          '必须具有 root 所有、0600 权限的专用虚拟机标记')
    marker = json.loads(MARKER.read_text())
    check(marker.get('确认') == CONFIRM and marker.get('允许小型循环设备测试') is True, '标记内容未授权')
    check(marker.get('主机名') == socket.gethostname(), '虚拟机主机名不匹配')
    check(marker.get('机器标识') == Path('/etc/machine-id').read_text().strip(), '虚拟机机器标识不匹配')
    check(os.statvfs(BASE).f_bavail * os.statvfs(BASE).f_frsize > 4 * 1024 ** 3, '根分区可用空间不足 4 GiB')
    safe_child(args.code_root)
    safe_child(args.report)
    safe_child(args.pyz)
    check(Path(args.code_root, 'space_report/filesystem.py').is_file(), '缺少稳定源码')
    check(not Path(args.report).exists(), '原始报告已存在，拒绝覆盖')


def snapshot(paths):
    result = {}
    for path in paths:
        info = path.lstat()
        row = {'权限': stat.S_IMODE(info.st_mode), '修改时间纳秒': info.st_mtime_ns,
               '逻辑字节': info.st_size, '设备': info.st_dev, '文件编号': info.st_ino}
        if stat.S_ISREG(info.st_mode):
            digest = hashlib.sha256()
            with path.open('rb') as stream:
                for block in iter(lambda: stream.read(MIB), b''):
                    digest.update(block)
            row['SHA256'] = digest.hexdigest()
        elif stat.S_ISLNK(info.st_mode):
            row['符号链接目标'] = os.readlink(path)
        result[str(path)] = row
    return result


class Suite:
    def __init__(self, args):
        self.args = args
        self.run = Path(tempfile.mkdtemp(prefix='run-', dir=BASE))
        self.run.chmod(0o755)
        self.current = None
        self.result = {'开始时间': datetime.now(timezone.utc).isoformat(),
                       '系统': {'主机': socket.gethostname(), '内核': os.uname().release,
                                '架构': os.uname().machine, 'Python': sys.version},
                       '运行目录': str(self.run), '代码根目录': args.code_root,
                       '源码指纹': snapshot(sorted(Path(args.code_root).rglob('*.py'))),
                       '验收脚本SHA256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                       '场景': [], '命令': [], '清理错误': []}

    def command(self, argv, timeout=90, allowed=(0,)):
        argv = [str(item) for item in argv]
        started = time.monotonic()
        try:
            process = subprocess.run(argv, capture_output=True, text=True, errors='backslashreplace',
                                     timeout=timeout, env=ENV)
            row = {'参数': argv, '退出码': process.returncode, '标准输出': process.stdout,
                   '标准错误': process.stderr, '用时秒': round(time.monotonic() - started, 3)}
            self.result['命令'].append(row)
            check(process.returncode in allowed, '命令失败：' + json.dumps(row, ensure_ascii=False))
            return process
        except subprocess.TimeoutExpired as error:
            self.result['命令'].append({'参数': argv, '失败': '超时', '上限秒': timeout})
            raise AssertionError('系统命令超时：' + str(argv)) from error

    def assert_equal(self, label, actual, expected):
        self.current['断言'].append({'名称': label, '实际': actual, '预期': expected, '通过': actual == expected})
        check(actual == expected, label)

    def assert_true(self, label, value):
        self.assert_equal(label, bool(value), True)

    def case(self, name, callback):
        row = {'名称': name, '状态': '运行中', '断言': []}
        self.result['场景'].append(row)
        self.current = row
        try:
            callback()
            row['状态'] = '通过'
        except Exception as error:
            row.update({'状态': '失败', '失败详情': str(error), '堆栈': traceback.format_exc()})
        print(json.dumps({'场景': name, '状态': row['状态']}, ensure_ascii=False), flush=True)

    def collect(self, module, function, path, user=None, timeout=20):
        argv = [sys.executable, '-B', '-c', WORKER, self.args.code_root, module, function, str(path), str(timeout)]
        if user:
            argv = ['runuser', '-u', user, '--'] + argv
        response = json.loads(self.command(argv, timeout=timeout + 8).stdout)
        self.current.setdefault('生产模块返回', []).append(response)
        return response

    def verify_loop(self, device, image, mountpoint=None):
        check(re.fullmatch(r'/dev/loop[0-9]+', device) is not None, '不是循环设备，禁止格式化')
        check(image.parent == self.run and image.resolve() == image, '后备文件不在本次专用目录')
        info = image.lstat()
        check(stat.S_ISREG(info.st_mode) and 0 < info.st_size <= 128 * MIB, '后备文件类型或容量越界')
        loop = json.loads(self.command(['losetup', '--json', '--list', '--output', 'NAME,BACK-FILE,SIZELIMIT', device]).stdout)['loopdevices']
        check(len(loop) == 1 and loop[0]['name'] == device and Path(loop[0]['back-file']).resolve() == image,
              '循环设备后备文件与本次样本不匹配')
        check(int(Path('/sys/class/block', Path(device).name, 'size').read_text()) * 512 == info.st_size,
              '循环设备容量与样本不匹配')
        check(os.stat(device).st_rdev != os.stat('/').st_dev, '循环设备不能是根分区')
        if mountpoint:
            rows = json.loads(self.command(['findmnt', '--json', '--mountpoint', mountpoint,
                                          '--output', 'SOURCE,TARGET,FSTYPE']).stdout)['filesystems']
            check(len(rows) == 1 and rows[0]['source'] == device and rows[0]['target'] == str(mountpoint)
                  and rows[0]['fstype'] == 'ext4', '挂载点未指向本次循环设备')
            check(os.stat(mountpoint).st_dev == os.stat(device).st_rdev != os.stat('/').st_dev,
                  '填充目标不是独立循环设备文件系统')

    @contextmanager
    def filesystem(self, label, mib, inodes=1024):
        image = self.run / (label + '.img')
        mountpoint = self.run / (label + '-mount')
        mountpoint.mkdir()
        with image.open('xb') as stream:
            os.posix_fallocate(stream.fileno(), 0, mib * MIB)
        device = self.command(['losetup', '--find', '--show', '--nooverlap', '--sizelimit', str(mib * MIB), image]).stdout.strip()
        mounted = False
        try:
            self.verify_loop(device, image)
            self.command(['mkfs.ext4', '-q', '-m', '0', '-N', str(inodes), '-E', 'lazy_itable_init=0,lazy_journal_init=0', device])
            self.command(['mount', '-t', 'ext4', '-o', 'nodev,nosuid,noexec', device, mountpoint])
            mounted = True
            self.verify_loop(device, image, mountpoint)
            yield mountpoint, device, image
        finally:
            try:
                self.verify_loop(device, image)
                if mounted:
                    self.command(['umount', mountpoint])
                self.command(['losetup', '--detach', device])
            except Exception as error:
                self.result['清理错误'].append(str(error))
                raise

    def normal(self):
        # 可抓住按逻辑大小计量、硬链接重复计量、进入同设备 bind 挂载或改写目标的错误。
        with self.filesystem('normal', 64) as (mountpoint, device, image):
            scan, outside = mountpoint / 'scan', mountpoint / 'outside'
            scan.mkdir(); outside.mkdir()
            regular, sparse = scan / 'ordinary.bin', scan / 'sparse.bin'
            regular.write_bytes(b'a' * MIB)
            with sparse.open('xb') as stream:
                stream.seek(16 * MIB); stream.write(b'x')
            link, special = scan / 'hardlink.bin', scan / '换行\n\x1b[31m'
            os.link(regular, link)
            special.write_bytes(b'b' * 4096)
            external = outside / 'must-not-count.bin'
            external.write_bytes(b'c' * (2 * MIB))
            symbolic, bound = scan / 'symbolic', scan / 'bind'
            symbolic.symlink_to(outside)
            bound.mkdir()
            self.command(['mount', '--bind', outside, bound])
            try:
                self.assert_equal('bind 挂载与扫描根是同设备', bound.stat().st_dev, scan.stat().st_dev)
                watched = [scan, regular, sparse, link, special, symbolic, outside, external]
                before = snapshot(watched)
                expected = sum(path.lstat().st_blocks * 512 for path in [scan, regular, sparse, special, symbolic])
                report = self.collect('filesystem', 'collect_directories', scan)
                self.assert_equal('目录扫描状态', report['状态'], '完成')
                self.assert_equal('实际分配且硬链接只计一次', report['数据']['已统计字节'], expected)
                self.assert_true('稀疏文件逻辑大于实际分配', sparse.stat().st_size > sparse.stat().st_blocks * 512)
                self.assert_true('同设备 bind 被跳过', str(bound) in report['数据']['跳过挂载点'])
                self.assert_equal('内容权限和修改时间不变', snapshot(watched), before)
                self.current['只读比较说明'] = '仅比较 SHA256、mode、mtime_ns、大小、设备和 inode；不使用 atime。'
            finally:
                self.command(['umount', bound])
            hidden = scan / 'private'
            hidden.mkdir(); (hidden / 'secret.bin').write_bytes(b'd' * 4096); hidden.chmod(0)
            try:
                denied = self.collect('filesystem', 'collect_directories', scan, user='tester')
                self.assert_equal('无权读取子目录应为部分完成', denied['状态'], '部分完成')
                self.assert_true('无权读取记录不能为零', denied['数据']['未读取条目数'] > 0)
            finally:
                hidden.chmod(0o755)
            self.command(['mount', '-o', 'remount,ro', mountpoint])
            readonly = self.collect('filesystem', 'collect_directories', scan)
            self.assert_equal('只读挂载仍可扫描', readonly['状态'], '完成')
            response = self.command([sys.executable, '-B', Path(self.args.code_root) / '空间去哪了.py',
                                     '--path', scan, '--timeout', '3', '--output', mountpoint / 'cannot-save'],
                                    allowed=(2,), timeout=35)
            self.assert_true('只读分区保存失败明确报告', '报告保存失败' in response.stderr)

    def capacity(self):
        # 可抓住把容量耗尽错报为 inode 耗尽、拿根分区数值代替样本分区等错误。
        with self.filesystem('capacity', 64) as (mountpoint, device, image):
            self.verify_loop(device, image, mountpoint)
            written = 0; failure = None
            fd = os.open(mountpoint / 'fill.bin', os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            tail_fd = None
            try:
                check(os.fstat(fd).st_dev == os.stat(device).st_rdev, '填充文件不在本次循环设备上')
                tail_fd = os.open(mountpoint / 'tail.bin', os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
                check(os.fstat(tail_fd).st_dev == os.stat(device).st_rdev, '收尾文件不在本次循环设备上')
                try:
                    while written <= 128 * MIB:
                        written += os.write(fd, b'x' * MIB)
                    raise AssertionError('超过样本容量仍未耗尽，停止填充')
                except OSError as error:
                    failure = error.errno
                os.fsync(fd)
                remaining = os.statvfs(mountpoint)
                tail_written = 0
                tail_error = None
                # 大块写入 ENOSPC 后可能仍有零散块；使用预先建立的独立文件逐块分配。
                # 保留严格零剩余断言，不把 ENOSPC 自身等同于 statvfs 可用块为零。
                step = min(4096, remaining.f_frsize)
                bound = min(4096, (remaining.f_bavail * remaining.f_frsize + step - 1) // step + 1)
                if failure == errno.ENOSPC:
                    self.verify_loop(device, image, mountpoint)
                    for _ in range(bound):
                        if os.statvfs(mountpoint).f_bavail == 0:
                            break
                        try:
                            tail_written += os.write(tail_fd, b't' * step)
                            os.fsync(tail_fd)
                        except OSError as error:
                            tail_error = error.errno
                            break
                self.current['收尾填充'] = {'首次ENOSPC后可用块': remaining.f_bavail,
                                        '每次字节': step, '最多次数': bound,
                                        '写入字节': tail_written, 'errno': tail_error}
                written += tail_written
            finally:
                if tail_fd is not None:
                    os.close(tail_fd)
                os.close(fd)
            truth = os.statvfs(mountpoint)
            self.current['系统真值'] = {'写入字节': written, 'errno': failure, '可用块': truth.f_bavail, '可用文件节点': truth.f_favail}
            self.assert_equal('真实写入产生 ENOSPC', failure, errno.ENOSPC)
            self.assert_equal('容量确实耗尽', truth.f_bavail, 0)
            self.assert_true('文件节点尚有剩余', truth.f_favail > 0)
            report = self.collect('filesystem', 'collect_partition', mountpoint)
            self.assert_equal('容量耗尽分区采集完成', report['状态'], '完成')
            self.assert_equal('报告可用容量为零', report['数据']['可用字节'], 0)
            self.assert_equal('报告剩余文件节点准确', report['数据']['可用文件节点'], truth.f_favail)

    def inodes(self):
        # 可抓住空间尚多但文件节点耗尽时仍报告健康的错误。
        with self.filesystem('inodes', 32, 128) as (mountpoint, device, image):
            self.verify_loop(device, image, mountpoint)
            directory = os.open(mountpoint, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            created = 0; failure = None
            try:
                check(os.fstat(directory).st_dev == os.stat(device).st_rdev, '文件创建目录不在本次循环设备上')
                try:
                    for index in range(4096):
                        fd = os.open(f'empty-{index}', os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600, dir_fd=directory)
                        os.close(fd); created += 1
                    raise AssertionError('创建 4096 个文件仍未耗尽，停止创建')
                except OSError as error:
                    failure = error.errno
            finally:
                os.close(directory)
            truth = os.statvfs(mountpoint)
            self.current['系统真值'] = {'创建文件数': created, 'errno': failure, '可用文件节点': truth.f_favail,
                                      '可用字节': truth.f_bavail * truth.f_frsize}
            self.assert_equal('真实创建产生 ENOSPC', failure, errno.ENOSPC)
            self.assert_equal('文件节点确实耗尽', truth.f_favail, 0)
            self.assert_true('容量仍有至少 1 MiB', truth.f_bavail * truth.f_frsize > MIB)
            report = self.collect('filesystem', 'collect_partition', mountpoint)
            self.assert_equal('文件节点耗尽分区采集完成', report['状态'], '完成')
            self.assert_equal('报告可用文件节点为零', report['数据']['可用文件节点'], 0)
            self.assert_equal('报告容量仍准确', report['数据']['可用字节'], truth.f_bavail * truth.f_frsize)

    def deleted(self):
        pid = int(self.command(['systemctl', 'show', '--property=MainPID', '--value', 'spaceprobe-unlinked-test.service']).stdout)
        check(pid > 0, '已删除文件场景进程不存在')
        a, b = Path(f'/proc/{pid}/fd/3'), Path(f'/proc/{pid}/fd/4')
        first, second = a.stat(), b.stat()
        self.assert_equal('两个描述符是同一文件', (first.st_dev, first.st_ino), (second.st_dev, second.st_ino))
        self.assert_equal('已删除文件链接数为零', first.st_nlink, 0)
        report = self.collect('deleted', 'collect_deleted', BASE)
        found = [row for row in report['数据']['文件列表'] if row['文件节点'] == first.st_ino
                 and row['设备号'] == f'{os.major(first.st_dev)}:{os.minor(first.st_dev)}']
        self.assert_equal('重复描述符只产生一个条目', len(found), 1)
        self.assert_equal('唯一已删除分配大小', found[0]['已分配字节'], 32 * MIB)
        self.assert_true('记录持有进程', any(row['进程号'] == pid for row in found[0]['持有进程']))

    def docker(self):
        # 测试场景允许使用 inspect 取得独立真值；生产模块本身不应调用 inspect。
        cid = self.command(['docker', 'inspect', '--format', '{{.Id}}', 'spaceprobe-json-fixture']).stdout.strip()
        none = self.command(['docker', 'inspect', '--format', '{{.Id}}', 'spaceprobe-none-fixture'], allowed=(0, 1))
        if none.returncode:
            none_id = self.command(['docker', 'run', '--detach', '--name', 'spaceprobe-none-fixture',
                                   '--network', 'none', '--memory', '32m', '--pids-limit', '16', '--cap-drop', 'ALL',
                                   '--log-driver', 'none', 'spaceprobe-fixture:1', '/bin/busybox', 'sleep', '28800']).stdout.strip()
        else:
            none_id = none.stdout.strip()
        root = Path(self.command(['docker', 'info', '--format', '{{.DockerRootDir}}']).stdout.strip())
        files = sorted((root / 'containers' / cid).glob(cid + '-json.log*'))
        truth = {'文件数': len(files), '逻辑字节': sum(p.stat().st_size for p in files),
                 '已分配字节': sum(p.stat().st_blocks * 512 for p in files)}
        self.assert_equal('真实场景有三份轮转文件', truth['文件数'], 3)
        before = snapshot(files)
        report = self.collect('services', 'collect_docker', BASE)
        self.assert_equal('Docker 分类采集完成', report['数据']['分类状态'], '完成')
        categories = {row['类别']: row for row in report['数据']['分类']}
        self.assert_true('本地卷分类接近真实 32 MiB', abs(categories['本地卷']['字节估算'] - 32 * MIB) < MIB // 100)
        self.assert_true('容器可写层分类接近真实 8 MiB', abs(categories['容器可写层']['字节估算'] - 8 * MIB) < MIB // 10)
        self.assert_true('镜像分类具有非零实际占用', categories['镜像']['字节估算'] > MIB)
        rows = report['数据']['容器日志']
        observed = next(row for row in rows if row['容器ID'] == cid[:12])
        unsupported = next(row for row in rows if row['容器ID'] == none_id[:12])
        for key, expected in truth.items():
            self.assert_equal('json-file ' + key, observed[key], expected)
        self.assert_equal('容器日志内容权限修改时间不变', snapshot(files), before)
        self.assert_equal('none 驱动未统计', unsupported['状态'], '不适用')
        self.assert_true('none 驱动不能填零冒充已统计', '已分配字节' not in unsupported and '逻辑字节' not in unsupported)
        sizes = self.command(['docker', 'exec', 'spaceprobe-json-fixture', '/bin/busybox', 'stat', '-c', '%s',
                              '/volume/volume.bin', '/container.bin']).stdout.splitlines()
        self.assert_equal('测试卷和可写文件大小未改变', [int(item) for item in sizes], [32 * MIB, 8 * MIB])
        denied = self.collect('services', 'collect_docker', BASE, user='tester')
        self.assert_equal('普通用户明确权限不足', denied['状态'], '权限不足')

    def cli(self):
        target = self.run / 'cli-target'; target.mkdir()
        sample = target / 'sample'; sample.write_bytes(b'synthetic' * 1024)
        watched = [target, sample]; before = snapshot(watched)
        for index, entry in enumerate([Path(self.args.code_root) / '空间去哪了.py', Path(self.args.pyz)]):
            output = self.run / f'cli-output-{index}'
            response = self.command([sys.executable, '-B', entry, '--path', target, '--deep', '--timeout', '5', '--output', output],
                                    timeout=45, allowed=(0, 1))
            reports = list(output.glob('*/报告.json'))
            self.assert_equal(f'入口 {index} 生成唯一结构化报告', len(reports), 1)
            payload = json.loads(reports[0].read_text())
            self.current.setdefault('命令行报告', []).append(payload)
            self.assert_true(f'入口 {index} 确实执行所有分类', len(payload['检查结果']) >= 5)
            sections = {row['项目']: row for row in payload['检查结果']}
            self.assert_equal(f'入口 {index} 分区检查成功', sections['分区空间']['状态'], '完成')
            self.assert_equal(f'入口 {index} 目录检查成功', sections['大目录']['状态'], '完成')
            self.assert_true(f'入口 {index} Docker 日志确实返回', len(sections['Docker']['数据']['容器日志']) >= 2)
            self.assert_true(f'入口 {index} 明示范围不能相加', '请勿相加' in response.stdout)
        self.assert_equal('两个入口不修改被检查目录', snapshot(watched), before)

    def journal(self):
        native = self.command(['journalctl', '--disk-usage', '--no-pager'])
        match = re.search(r'Archived and active journals take up (.+?) in the file system\.', native.stdout)
        check(match is not None, '原生命令未返回可识别的占用说明')
        root = self.collect('services', 'collect_journal', BASE)
        self.assert_equal('管理员系统日志采集完成', root['状态'], '完成')
        self.assert_true('日志占用不是未知值', isinstance(root['数据'].get('字节估算'), int))
        self.assert_equal('日志显示与原生命令一致', root['数据']['磁盘占用显示'], match[1])
        ordinary = self.collect('services', 'collect_journal', BASE, user='tester')
        self.assert_equal('普通用户日志范围是部分完成', ordinary['状态'], '部分完成')

    def tiny_timeout(self):
        target = self.run / 'timeout-target'; target.mkdir()
        sample = target / 'unchanged'; sample.write_bytes(b'unchanged-test-file')
        before = snapshot([target, sample])
        output = self.run / 'timeout-output'
        response = self.command([sys.executable, '-B', self.args.pyz, '--path', target, '--deep',
                                 '--timeout', '0.0001', '--output', output], allowed=(1,), timeout=15)
        reports = list(output.glob('*/报告.json'))
        self.assert_equal('极短超时保存一份报告', len(reports), 1)
        payload = json.loads(reports[0].read_text())
        self.current['命令行报告'] = payload
        self.assert_true('报告至少有一项真实超时', any(row['状态'] == '超时' for row in payload['检查结果']))
        self.assert_true('终端明确显示超时', '超时' in response.stdout)
        self.assert_equal('超时后被检查文件未改变', snapshot([target, sample]), before)

    def real_cancel(self):
        target = self.run / 'cancel-target'; target.mkdir()
        sample = target / 'unchanged'; sample.write_bytes(b'unchanged-test-file')
        before = snapshot([target, sample])
        output = self.run / 'cancel-output'
        argv = [sys.executable, '-B', self.args.pyz, '--path', str(target), '--deep',
                '--timeout', '30', '--output', str(output)]
        process = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True, errors='backslashreplace', env=ENV)
        first = ''
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(process.stderr, selectors.EVENT_READ)
                check(bool(selector.select(timeout=10)), '等待正在检查提示超时')
                first = process.stderr.readline()
            check('正在检查' in first, '未观察到首个正在检查提示，不能伪造中断时机')
            process.send_signal(signal.SIGINT)
            stdout, stderr = process.communicate(timeout=15)
            self.result['命令'].append({'参数': argv, '发送信号': 'SIGINT', '信号时机': first.strip(),
                                      '退出码': process.returncode, '标准输出': stdout, '标准错误': first + stderr})
            self.assert_equal('真实 SIGINT 退出码', process.returncode, 130)
            reports = list(output.glob('*/报告.json'))
            self.assert_equal('中断后保存一份报告', len(reports), 1)
            payload = json.loads(reports[0].read_text())
            self.current['命令行报告'] = payload
            self.assert_equal('报告确实标记取消', payload['已取消'], True)
            self.assert_true('终端明确显示取消', '已取消' in stdout + stderr)
            self.assert_equal('中断后被检查文件未改变', snapshot([target, sample]), before)
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=5)

    def finish(self):
        self.result['结束时间'] = datetime.now(timezone.utc).isoformat()
        self.result['源码运行后指纹'] = snapshot(sorted(Path(self.args.code_root).rglob('*.py')))
        self.result['源码未修改'] = self.result['源码指纹'] == self.result['源码运行后指纹']
        self.result['实际通过'] = (all(row['状态'] == '通过' for row in self.result['场景'])
                                   and not self.result['清理错误'] and self.result['源码未修改'])
        with Path(self.args.report).open('x', encoding='utf-8', errors='backslashreplace') as stream:
            json.dump(self.result, stream, ensure_ascii=False, indent=2)
            stream.write('\n')
        return 0 if self.result['实际通过'] else 1


def main():
    parser = argparse.ArgumentParser(description='仅专用 Lima 虚拟机的真实破坏性小样本验收；默认不执行')
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--confirm-test-vm', required=True)
    parser.add_argument('--code-root', required=True)
    parser.add_argument('--pyz', required=True)
    parser.add_argument('--report', required=True)
    parser.add_argument('--only', action='append', choices=['normal', 'capacity', 'inodes', 'deleted', 'docker', 'cli', 'journal', 'tiny_timeout', 'real_cancel'],
                        help='明确指定只运行的场景；可重复。默认运行全部场景。')
    args = parser.parse_args()
    guard(args)
    suite = Suite(args)
    for name, action in [('普通稀疏硬链接挂载边界权限与只读', suite.normal), ('真实容量耗尽', suite.capacity),
                         ('真实文件节点耗尽', suite.inodes), ('已删除文件描述符去重', suite.deleted),
                         ('Docker 实际分类日志和权限', suite.docker), ('源码和运行包命令行', suite.cli),
                         ('系统日志真实对照', suite.journal), ('运行包真实极短超时', suite.tiny_timeout),
                         ('运行包真实中断', suite.real_cancel)]:
        if args.only and action.__name__ not in args.only:
            continue
        suite.case(name, action)
        if suite.result['清理错误']:
            break
    return suite.finish()


if __name__ == '__main__':
    raise SystemExit(main())
