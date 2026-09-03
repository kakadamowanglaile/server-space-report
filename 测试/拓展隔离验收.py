#!/usr/bin/env python3
"""显式启动的扩展 Linux 实测；只允许带专用标记的虚拟机，不参与默认发现。"""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import threading
import time

spec = importlib.util.spec_from_file_location('spaceprobe_base_suite', Path(__file__).with_name('隔离验收.py'))
base = importlib.util.module_from_spec(spec)
spec.loader.exec_module(base)
BASE, ENV, MIB = base.BASE, base.ENV, base.MIB

MULTIPROCESS = r'''import json,os,signal,sys
from pathlib import Path
p=Path(sys.argv[1]);fd=os.open(p,os.O_CREAT|os.O_EXCL|os.O_RDWR,0o600)
os.posix_fallocate(fd,0,4*1024*1024);dup=os.dup(fd);os.unlink(p);st=os.fstat(fd);children=[]
for i in range(2):
 child=os.fork()
 if child==0:
  while True:signal.pause()
 children.append(child)
print(json.dumps({'pids':[os.getpid()]+children,'dev':st.st_dev,'ino':st.st_ino,'allocated':st.st_blocks*512}),flush=True)
try:sys.stdin.readline()
finally:
 for pid in children:
  try:os.kill(pid,signal.SIGTERM)
  except ProcessLookupError:pass
 for pid in children:os.waitpid(pid,0)
 os.close(fd);os.close(dup)
'''


def namespace_worker(args):
    """私有挂载命名空间只临时改变该子进程的视图，不删除或改写系统日志。"""
    base.guard(args)
    root = base.safe_child(args.namespace_root)
    sys.path.insert(0, args.code_root)
    from space_report.services import collect_journal, collect_docker
    if args.namespace_worker == 'missing':
        subprocess.run(['mount', '--bind', str(root / 'empty-bin'), '/usr/bin'], check=True, env=ENV)
        result = {'系统日志': collect_journal(str(root), 10), 'Docker': collect_docker(str(root), 10)}
    else:
        subprocess.run(['mount', '--bind', str(root / 'empty-var-log'), '/var/log'], check=True, env=ENV)
        subprocess.run(['mount', '--bind', str(root / 'empty-run-log'), '/run/log'], check=True, env=ENV)
        native = subprocess.run(['journalctl', '--disk-usage', '--no-pager'], capture_output=True, text=True, env=ENV)
        result = {'原生命令': {'退出码': native.returncode, '标准输出': native.stdout, '标准错误': native.stderr},
                  '系统日志': collect_journal(str(root), 10)}
    print(json.dumps(result, ensure_ascii=False))


class Extended(base.Suite):
    def cache_and_shared_layers(self):
        # 会揭示把非空构建缓存遗漏成零、按镜像逐个累加共享层等错误。
        native = [json.loads(line) for line in self.command(['docker', 'system', 'df', '--format', '{{json .}}']).stdout.splitlines()]
        truth = {row['Type']: row for row in native}
        first = json.loads(self.command(['docker', 'image', 'inspect', '--format', '{{json .RootFS.Layers}}', 'spaceprobe-cache-first:1']).stdout)
        second = json.loads(self.command(['docker', 'image', 'inspect', '--format', '{{json .RootFS.Layers}}', 'spaceprobe-cache-second:1']).stdout)
        self.assert_true('两个实际镜像具有共享层', len(set(first) & set(second)) >= 2)
        self.assert_true('两个镜像也有各自独立层', first[-1] != second[-1])
        report = self.collect('services', 'collect_docker', BASE)
        rows = {row['类别']: row for row in report['数据']['分类']}
        self.assert_equal('构建缓存显示与原生汇总相同', rows['构建缓存']['占用显示'], truth['Build Cache']['Size'])
        self.assert_true('构建缓存具有真实数据而非空项', rows['构建缓存']['字节估算'] > MIB)
        self.assert_equal('镜像使用守护进程去重汇总', rows['镜像']['占用显示'], truth['Images']['Size'])
        self.current['原始Docker汇总'] = native
        self.current['共享层真值'] = {'first': first, 'second': second}

    def log_drivers_and_reader(self):
        # 会揭示把未支持驱动写成零、遗漏真实压缩轮转、把组成员视为文件系统管理员等错误。
        root = Path(self.command(['docker', 'info', '--format', '{{.DockerRootDir}}']).stdout.strip())
        expected = {}
        for suffix in ('local', 'journald', 'compressed', 'empty'):
            name = 'spaceprobe-' + suffix + '-extra'
            cid = self.command(['docker', 'inspect', '--format', '{{.Id}}', name]).stdout.strip()
            driver = self.command(['docker', 'inspect', '--format', '{{.HostConfig.LogConfig.Type}}', name]).stdout.strip()
            files = sorted((root / 'containers' / cid).glob(cid + '-json.log*'))
            expected[suffix] = {'cid': cid, 'driver': driver, 'files': files}
        compressed = expected['compressed']['files']
        self.assert_true('Docker 确实生成压缩轮转', any(p.name.endswith('.gz') for p in compressed))
        watched = compressed + expected['empty']['files']
        before = base.snapshot(watched)
        report = self.collect('services', 'collect_docker', BASE)
        rows = {row['容器ID']: row for row in report['数据']['容器日志']}
        for suffix in ('local', 'journald'):
            sample = expected[suffix]
            self.assert_equal(suffix + ' 为原生驱动', sample['driver'], suffix)
            row = rows[sample['cid'][:12]]
            self.assert_equal(suffix + ' 不支持时明确不适用', row['状态'], '不适用')
            self.assert_true(suffix + ' 未把未统计伪装为零', '已分配字节' not in row and '逻辑字节' not in row)
        row = rows[expected['compressed']['cid'][:12]]
        self.assert_equal('压缩轮转文件数量', row['文件数'], len(compressed))
        self.assert_equal('压缩后逻辑字节按实际文件统计', row['逻辑字节'], sum(p.stat().st_size for p in compressed))
        self.assert_equal('压缩后分配字节按实际文件统计', row['已分配字节'], sum(p.stat().st_blocks * 512 for p in compressed))
        empty = rows[expected['empty']['cid'][:12]]
        self.assert_equal('真实空日志可以报告完成', empty['状态'], '完成')
        self.assert_equal('真实空日志逻辑大小为零', empty['逻辑字节'], 0)
        self.assert_equal('静态日志内容权限修改时间不变', base.snapshot(watched), before)
        reader = self.collect('services', 'collect_docker', BASE, user='spaceprobe-docker-reader')
        self.assert_equal('docker 组用户可查询分类', reader['数据']['分类状态'], '完成')
        self.assert_equal('docker 组用户仍不能读取管理员日志元数据', reader['数据']['容器日志状态'], '权限不足')
        self.assert_equal('根目录拒绝后没有伪造逐容器日志', reader['数据']['容器日志'], [])
        self.assert_true('根目录拒绝后不伪造日志字节',
                         '已分配字节' not in reader['数据'] and '逻辑字节' not in reader['数据'])
        self.assert_equal('受限组用户整体不能声称完成', reader['状态'], '部分完成')

    def multiple_processes(self):
        # 会揭示跨进程重复累加一个已删除 inode，以及进程释放后仍报告旧记录等错误。
        child = subprocess.Popen([sys.executable, '-B', '-c', MULTIPROCESS, str(self.run / 'shared-unlinked.bin')],
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 text=True, start_new_session=True, env=ENV)
        try:
            truth = json.loads(child.stdout.readline())
            self.current['系统真值'] = truth
            device = f"{os.major(truth['dev'])}:{os.minor(truth['dev'])}"
            report = self.collect('deleted', 'collect_deleted', BASE)
            found = [r for r in report['数据']['文件列表'] if r['设备号'] == device and r['文件节点'] == truth['ino']]
            self.assert_equal('三个进程共享已删除文件只计一条', len(found), 1)
            self.assert_equal('跨进程唯一分配量为 4 MiB', found[0]['已分配字节'], 4 * MIB)
            self.assert_equal('三个实际持有进程全部记录', sorted({p['进程号'] for p in found[0]['持有进程']} & set(truth['pids'])), sorted(truth['pids']))
            child.communicate('退出\n', timeout=10)
            self.assert_equal('样本进程正常结束', child.returncode, 0)
            later = self.collect('deleted', 'collect_deleted', BASE)
            self.assert_true('所有持有者退出后不保留旧文件', not any(r['设备号'] == device and r['文件节点'] == truth['ino'] for r in later['数据']['文件列表']))
        finally:
            if child.poll() is None:
                child.communicate('退出\n', timeout=10)

    def concurrent_directory(self):
        # 会揭示真实目录条目消失时崩溃，或把路径重新出现当成旧文件的问题。
        target = self.run / 'concurrent-directory'; target.mkdir()
        stable = target / 'stable.bin'; stable.write_bytes(b'stable' * 4096)
        watched = base.snapshot([stable])
        for i in range(32):
            directory = target / f'directory-{i}'; directory.mkdir()
            for j in range(32):
                (directory / f'initial-{j}').touch()
        done = threading.Event(); count = [0]; errors = []
        def churn():
            try:
                while not done.is_set() and count[0] < 50000:
                    n = count[0]; directory = target / f'directory-{n % 32}'
                    path = directory / f'churn-{n % 128}'
                    path.write_bytes(b'c' * 4096)
                    path.unlink(); count[0] += 1
            except Exception as error:
                errors.append(str(error))
        producer = threading.Thread(target=churn); producer.start()
        try:
            start = count[0]
            responses = [self.collect('filesystem', 'collect_directories', target) for _ in range(3)]
            self.assert_true('采集区间确有并发创建与删除', count[0] > start)
            self.assert_true('三次真实并发扫描均未崩溃', all(r['状态'] in {'完成', '部分完成', '超时'} for r in responses))
            self.assert_equal('稳定文件内容权限修改时间未变', base.snapshot([stable]), watched)
            self.current['实际并发变更次数'] = count[0] - start
        finally:
            done.set(); producer.join(timeout=5)
        self.assert_equal('并发样本线程没有错误', errors, [])

    def namespace_states(self):
        # 会揭示命令实际不存在仍填零，以及真正空日志仍报告未知的问题。
        for name in ('empty-bin', 'empty-var-log', 'empty-run-log'):
            (self.run / name).mkdir()
        for kind in ('missing', 'empty'):
            argv = ['unshare', '--mount', '--propagation', 'private', sys.executable, '-B', str(Path(__file__).resolve()),
                    '--execute', '--confirm-test-vm', base.CONFIRM, '--code-root', self.args.code_root,
                    '--pyz', self.args.pyz, '--report', self.args.report, '--namespace-worker', kind,
                    '--namespace-root', str(self.run)]
            actual = json.loads(self.command(argv, timeout=30).stdout)
            self.current.setdefault('命名空间实测', {})[kind] = actual
            if kind == 'missing':
                for section in ('系统日志', 'Docker'):
                    self.assert_equal(section + ' 命令缺失明确标记', actual[section]['状态'], '工具缺失')
                    self.assert_equal(section + ' 命令缺失不伪造大小', actual[section]['数据'], {})
            else:
                self.assert_equal('空日志原生命令成功', actual['原生命令']['退出码'], 0)
                self.assert_true('原生命令实际报告 0B', 'take up 0B' in actual['原生命令']['标准输出'])
                self.assert_equal('原生命令明确提示无日志文件', actual['原生命令']['标准错误'], 'No journal files were found.\n')
                self.assert_equal('空日志仍按命令警告保守标记', actual['系统日志']['状态'], '部分完成')
                self.assert_equal('实际空日志大小确为零', actual['系统日志']['数据']['字节估算'], 0)
                self.assert_true('空日志说明完整可见范围未获证明',
                                 any('完整可见范围无法证明' in note for note in actual['系统日志']['说明']))

    def non_utf8_cli(self):
        # 会揭示 Linux 原始字节文件名导致 CLI 崩溃、保存无效 UTF-8 或路径信息丢失。
        target = self.run / 'raw-name-target'; target.mkdir()
        raw_path = os.fsencode(target) + b'/raw-\xff-\x1b-\n'
        os.mkdir(raw_path)
        sample = Path(os.fsdecode(raw_path)) / 'unchanged'
        sample.write_bytes(b'raw-name-file' * 1024)
        before = base.snapshot([sample])
        output = self.run / 'raw-name-output'
        result = self.command([sys.executable, '-B', self.args.pyz, '--path', target, '--deep',
                               '--timeout', '10', '--output', output], timeout=90, allowed=(0, 1))
        self.assert_true('原始字节文件名不会写入终端 ESC', '\x1b' not in result.stdout)
        reports = list(output.glob('*/报告.json'))
        self.assert_equal('原始字节文件名仍保存完整报告', len(reports), 1)
        text = reports[0].read_bytes().decode('utf-8', errors='strict')
        payload = json.loads(text); self.current['原始文件名报告'] = payload
        directory = next(x for x in payload['检查结果'] if x['项目'] == '大目录')
        self.assert_equal('特殊目录采集完成', directory['状态'], '完成')
        self.assert_true('结构化路径可以还原真实原始字节',
                         any(os.fsencode(row['路径']) == raw_path for row in directory['数据']['目录列表']))
        self.assert_equal('原始文件名样本内容权限修改时间未改', base.snapshot([sample]), before)

    def concurrent_process_exit(self):
        # 会揭示 /proc 枚举期间真实进程退出后异常中断整个采集。
        stop = threading.Event(); counts = [0]; failures = []
        def burst():
            try:
                while not stop.is_set() and counts[0] < 200:
                    result = subprocess.run([sys.executable, '-B', '-c', 'import os,time;f=os.open("/dev/null",os.O_RDONLY);time.sleep(.003);os.close(f)'],
                                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, env=ENV, timeout=5)
                    if result.returncode:
                        failures.append(result.returncode)
                    counts[0] += 1
            except Exception as error:
                failures.append(str(error))
        producer = threading.Thread(target=burst); producer.start()
        try:
            responses = [self.collect('deleted', 'collect_deleted', BASE) for _ in range(5)]
            self.assert_true('采集区间确实有进程退出', counts[0] > 0)
            self.assert_true('真实进程退出期间采集未崩溃', all(r['状态'] in {'完成', '部分完成', '超时'} for r in responses))
            self.current['实际退出进程数'] = counts[0]
        finally:
            stop.set(); producer.join(timeout=10)
        self.assert_equal('退出样本执行正常', failures, [])

    def concurrent_log_rotation(self):
        # 会揭示真实 Docker 轮转/压缩期间枚举崩溃；不把动态日志要求为固定字节数。
        emit = "i=0;while [ $i -lt 40 ];do /bin/busybox awk 'BEGIN{s=\"\";for(j=0;j<1000;j++)s=s\"r\";for(k=0;k<200;k++)print s;}' > /proc/1/fd/1;/bin/busybox sleep .02;i=$((i+1));done"
        argv = ['docker', 'exec', 'spaceprobe-compressed-extra', '/bin/busybox', 'sh', '-c', emit]
        cid = self.command(['docker', 'inspect', '--format', '{{.Id}}', 'spaceprobe-compressed-extra']).stdout.strip()
        root = Path(self.command(['docker', 'info', '--format', '{{.DockerRootDir}}']).stdout.strip())
        directory = root / 'containers' / cid
        before = {p.name: (p.stat().st_ino, p.stat().st_mtime_ns) for p in directory.glob(cid + '-json.log*')}
        writer = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=ENV)
        try:
            responses = [self.collect('services', 'collect_docker', BASE) for _ in range(3)]
            stdout, stderr = writer.communicate(timeout=15)
            self.result['命令'].append({'参数': argv, '退出码': writer.returncode, '标准输出': stdout, '标准错误': stderr})
            self.assert_equal('真实轮转写入进程完成', writer.returncode, 0)
            after = {p.name: (p.stat().st_ino, p.stat().st_mtime_ns) for p in directory.glob(cid + '-json.log*')}
            self.assert_true('原生轮转文件身份或修改时间确实变化', before != after)
            self.assert_true('动态轮转采集未崩溃', all(r['状态'] in {'完成', '部分完成', '超时'} for r in responses))
            self.assert_true('动态轮转仍保留容器记录', all(any(x['容器ID'] == cid[:12] for x in r['数据']['容器日志']) for r in responses))
        finally:
            if writer.poll() is None:
                writer.terminate(); writer.communicate(timeout=5)

    def stopped_service(self):
        # 会揭示守护进程真正停止仍被视为可用或零占用的错误；只停本专用 VM。
        running = self.command(['docker', 'ps', '--quiet', '--no-trunc']).stdout.splitlines()
        self.command(['systemctl', 'stop', 'docker.service', 'docker.socket'], timeout=90)
        try:
            state = self.command(['systemctl', 'is-active', 'docker.service'], allowed=(3,))
            self.assert_equal('Docker 服务实际停止', state.stdout.strip(), 'inactive')
            report = self.collect('services', 'collect_docker', BASE)
            self.assert_equal('服务停止明确失败', report['状态'], '失败')
            self.assert_equal('服务停止不报告零分类', report['数据'], {})
        finally:
            self.command(['systemctl', 'start', 'docker.service'])
            if running:
                self.command(['docker', 'start'] + running)
            self.assert_equal('测试后恢复专用 Docker 服务', self.command(['systemctl', 'is-active', 'docker.service']).stdout.strip(), 'active')

    def cli_defaults_and_root(self):
        # 会揭示默认运行擅自写报告，以及根目录深扫描忽略时间边界等错误。
        work = self.run / 'default-cwd'; work.mkdir()
        anchor = work / 'unchanged'; anchor.write_bytes(b'unchanged')
        before = base.snapshot([work, anchor])
        start = time.monotonic()
        process = subprocess.run([sys.executable, '-B', self.args.pyz], cwd=work, env=ENV,
                                 capture_output=True, text=True, timeout=180)
        self.result['命令'].append({'参数': [sys.executable, '-B', self.args.pyz], '工作目录': str(work),
                                  '退出码': process.returncode, '标准输出': process.stdout, '标准错误': process.stderr,
                                  '用时秒': time.monotonic() - start})
        self.assert_true('无参数默认运行有明确可接受退出码', process.returncode in (0, 1))
        self.assert_true('默认报告确实包含主要检查', all(name in process.stdout for name in ('分区空间', '系统日志', 'Docker', '已删除')))
        self.assert_equal('默认运行不生成额外文件', sorted(p.name for p in work.iterdir()), ['unchanged'])
        self.assert_equal('默认运行不改变当前目录和文件', base.snapshot([work, anchor]), before)
        output = self.run / 'root-deep-output'
        started = time.monotonic()
        self.command([sys.executable, '-B', self.args.pyz, '--path', '/', '--deep', '--timeout', '0.05', '--output', output],
                     allowed=(0, 1), timeout=30)
        reports = list(output.glob('*/报告.json'))
        self.assert_equal('根目录限时扫描保存唯一报告', len(reports), 1)
        payload = json.loads(reports[0].read_text()); self.current['根目录命令行报告'] = payload
        self.assert_equal('实际目标是根目录', payload['目标目录'], '/')
        self.assert_equal('根目录深扫描仍返回所有分类', len(payload['检查结果']), 5)
        self.assert_true('根目录有限时扫描在外部界限内结束', time.monotonic() - started < 30)
        self.assert_equal('根目录深扫描未改变稳定样本', base.snapshot([anchor]), {str(anchor): before[str(anchor)]})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--confirm-test-vm', required=True)
    parser.add_argument('--code-root', required=True)
    parser.add_argument('--pyz', required=True)
    parser.add_argument('--report', required=True)
    parser.add_argument('--namespace-worker', choices=['missing', 'empty'])
    parser.add_argument('--namespace-root')
    parser.add_argument('--only', action='append', choices=['cache_and_shared_layers', 'log_drivers_and_reader',
                        'multiple_processes', 'concurrent_directory', 'namespace_states', 'stopped_service',
                        'cli_defaults_and_root', 'non_utf8_cli', 'concurrent_process_exit', 'concurrent_log_rotation'])
    args = parser.parse_args()
    base.guard(args)
    if args.namespace_worker:
        namespace_worker(args); return 0
    suite = Extended(args)
    for label, action in [('非空构建缓存与共享镜像层', suite.cache_and_shared_layers),
                          ('更多日志驱动压缩轮转与受限组用户', suite.log_drivers_and_reader),
                          ('已删除文件跨三个进程去重与释放', suite.multiple_processes),
                          ('目录真实并发消失', suite.concurrent_directory),
                          ('真实进程退出期间采集', suite.concurrent_process_exit),
                          ('Docker 原生并发轮转', suite.concurrent_log_rotation),
                          ('命令真实缺失与实际空系统日志', suite.namespace_states),
                          ('原始非UTF8文件名命令行保存', suite.non_utf8_cli),
                          ('Docker 服务真实停止和恢复', suite.stopped_service),
                          ('默认命令行与根目录限时深扫描', suite.cli_defaults_and_root)]:
        if args.only and action.__name__ not in args.only:
            continue
        suite.case(label, action)
    return suite.finish()


if __name__ == '__main__':
    raise SystemExit(main())
