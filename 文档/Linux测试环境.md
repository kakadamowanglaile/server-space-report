# 本机专用 Linux 测试环境

创建日期：2026-09-03。用于本项目功能验证，没有业务数据，没有使用已有服务器或 Docker 环境。

## 位置与隔离范围

- 所有工具、下载、配置、日志、临时文件、SSH 专用密钥和虚拟磁盘位于 `/Users/kaka/Desktop/服务器/测试环境`。
- `LIMA_HOME` 指向其中的 `lima`；`TMPDIR` 指向其中的 `临时`。不修改 `HOME`、`CODEX_HOME`，没有全局安装软件。
- 两套 ARM 虚拟机使用 macOS 自带的 Apple VZ 虚拟化能力，宿主没有安装 QEMU 或额外服务。0.1.1 扩大验收另在专用 Debian 来宾内部使用官方 APT QEMU 软件模拟完整 x86_64 系统，详见后文。
- `plain: true`，不挂载任何主机目录，不转发 SSH 代理，不导入 `~/.ssh` 中的公钥，不启动 Lima guest agent 或内置 containerd，不继承主机代理环境变量。
- 唯一主机入站通道是自动分配的 `127.0.0.1` SSH 端口；无业务端口转发。来宾仍可通过虚拟网络出站联网，以便安装测试所需的软件包。
- 新生成的测试密钥位于 `测试环境/lima/_config/`；不得上传 GitHub。
- Lima 的 macOS 缓存默认位于用户 `Library/Caches`，本环境通过事先下载并校验镜像、配置本地 `images.location` 避免使用该缓存。源码明确本地文件不缓存。
- 初次验证前后，宿主机 `/Users/kaka/.lima` 和 `/Users/kaka/Library/Caches/lima` 均不存在。

## 已准备的版本

| 实例 | 系统 | 内核 | systemd | Python | Docker / 存储驱动 |
|---|---|---|---|---|---|
| `u24` | Ubuntu 24.04.4 LTS | 6.8.0-138-generic | 255.4-1ubuntu8.17 | 3.12.3 | 29.1.3 / overlayfs |
| `d12` | Debian 12.15 | 6.1.0-52-cloud-arm64 | 252.39-1~deb12u2 | 3.11.2 | 20.10.24+dfsg1 / overlay2 |

两套均为原生 arm64，各配置 2 核、2 GiB 内存、16 GiB 稀疏磁盘，使用 Lima 2.2.0 官方便携归档。依次运行，不同时占用两套虚拟机内存。初次基础环境占用约 4.1 GiB；后续测试安装的最终占用另见验收记录。

0.1.0 初次验收时，Docker 仅安装在这两套专用虚拟机中，来自各自官方 APT 仓库，未使用外部安装脚本。Ubuntu 的软件包为 `docker.io 29.1.3-0ubuntu3~24.04.2`、containerd 2.2.1；Debian 为 `docker.io 20.10.24+dfsg1-1+deb12u1+b6`、containerd 1.6.20。0.1.1 扩大验收又在其内部的专用 x86 客户机安装官方 Debian Docker。测试镜像均由对应系统官方 `busybox-static` 包生成，没有拉取第三方镜像。

## 0.1.0 历史验收与当时停止状态

2026-09-03 15:26:57（UTC+8）实际查询确认：`u24`、`d12` 均为 `Stopped`。测试完成后没有继续占用虚拟机内存。虚拟磁盘、专用密钥和失败记录保留在本项目内，未删除用户文件，也未上传 GitHub。

| 项目 | Ubuntu | Debian |
|---|---|---|
| 第四候选单元测试 | 83 项全部通过 | 83 项全部通过 |
| 第四候选真实隔离验收 | 9 组、66 条断言通过（04b） | 9 组、66 条断言通过（04） |
| 生产源码前后 SHA-256、权限和修改时间 | 一致 | 一致 |
| 退出前遗留循环设备或挂载清理错误 | 无 | 无 |
| 停机前根分区已用字节 | 2,863,230,976 | 1,503,002,624 |
| 相对安装前已用增量 | 1,002,348,544 字节，低于 2 GiB | 731,594,752 字节，低于 2 GiB |

主机目录 `测试环境` 最终实际占用 5,680,124 KiB，约 5.42 GiB；其中 Ubuntu 实例约 2.78 GiB、Debian 实例约 1.63 GiB。包括原始镜像、工具及保留的合成测试数据；不把各虚拟磁盘声明的 16 GiB 当作实际宿主占用。终检时主机默认 `.lima` 和 `Library/Caches/lima` 仍不存在。

真实场景包括：普通文件、稀疏文件、硬链接去重、同设备绑定挂载边界、普通用户权限、只读挂载、64 MiB 循环文件系统容量耗尽、32 MiB 循环文件系统文件节点耗尽、32 MiB 已删除文件的双描述符去重、32 MiB Docker 卷、8 MiB 容器可写层、三份 json-file 轮转日志、none 日志驱动不适用、系统日志原生命令对照、真实极短超时、真实 SIGINT 取消。填充前校验循环设备的 backing file、设备身份与挂载位置，不填充根分区。被检查样本以内容摘要、权限和修改时间比较，不拿访问时间不变作为只读证明。

两套实测使用同一第四候选生产代码与运行包；代码也与最终本地生产源码逐文件一致。运行包 SHA-256 为 `08129a37f4a640dd542b20a8f3758645eee6b721ea7caded64d131d2532a3610`；对应候选源码归档 SHA-256 为 `c10f2479f330f00481505c99449cab968944eb361212b8dc6a1368d9afe10fe6`。后续正式源码归档如包含更新后的验收脚本或文档，其归档摘要可以不同，不能沿用这个值。

关键原始记录：

- `报告/Linux验收原始结果-Ubuntu-04b.json`、`报告/Linux验收原始结果-Debian-04.json`。
- `报告/单元测试-Ubuntu-候选04.log`、`报告/单元测试-Debian-候选04.log`。
- `报告/命令行报告-Ubuntu-候选04b/`、`报告/命令行报告-Debian-候选04/` 保存 CLI 实际生成的文本和结构化报告。
- `测试环境/日志/双系统生产源码最终一致性核对.json` 保存逐文件 SHA-256 比较。
- `测试环境/日志/最终双虚拟机停止及空间核对.log` 保存两台停止状态及主机空间核对。

历史失败未覆盖：候选 02 的单文件入口曾忽略退出码，候选 03 已修复；候选 03 在旧版 Docker 无权限时曾显示“失败”，候选 04 已修复。Ubuntu 首次 04 验收的容量样本在 ENOSPC 后仍有一个可用块，属于样本前置条件失败、尚未执行该项生产采集；04b 以有界 4096 字节收尾写入达到真实零剩余，原断言未降低。相应 `Ubuntu-附加03`、`Debian-03`、`Ubuntu-04` 原始失败 JSON 全部保留。

验收脚本为 `测试/隔离验收.py`，不会被默认单元测试发现。执行必须提供显式确认参数，并通过 Linux、root、专用主机名、机器标识和 root 所有 0600 标记校验。停机后已删除文件的持有进程会结束，Docker 测试容器也不自动恢复；再次复测需明确重新建立这些合成场景，不能直接假设仍然存活。

## 0.1.1 扩大验收

2026-09-03 使用冻结的“扩大候选 02”，增加真实并发、更多日志驱动、非空构建缓存、Python 3.10 和完整 x86_64 系统验证。所有生产文件仍与该候选逐字节一致；首次失败记录没有覆盖。

| 实际运行组合 | 单元测试 | 基础真实场景 | 拓展真实场景 |
|---|---|---|---|
| Debian ARM / Python 3.11.2 | 147 项通过，无跳过 | 9 组、66 条断言通过 | 10 组、67 条断言通过 |
| Debian ARM / 自建 Python 3.10.21 | 147 项通过，无跳过 | 9 组、66 条断言通过 | 10 组、67 条断言通过 |
| Debian x86_64 / Python 3.11.2 / 完整系统软件模拟 | 147 项通过，无跳过 | 9 组、66 条断言通过 | 10 组、67 条断言通过 |
| Ubuntu ARM / Python 3.12.3 | 147 项通过，无跳过 | 9 组、66 条断言通过 | 10 组、67 条断言通过 |

每个组合的 133 条真实场景断言独立执行，不能把跨系统重复次数当作不同功能数量。源码前后内容摘要、权限、修改时间一致，循环设备清理错误为空。非 UTF-8 文件名、Linux O_PATH 和 mount ID、waitid 进程生命周期、前台进程组 SIGINT 等适用测试均在真实 Linux 环境执行。

新增真实场景包括：BuildKit 缓存目录实际写入 2 MiB、两个镜像共享层与独立层、Docker 自行产生压缩轮转日志并对照 stat、local/journald 驱动未统计时不填零、真实空 json-file、Docker 组用户无管理员文件权限、三个进程共享一个已删除文件、实际文件删除和进程退出并发、Docker 自行轮转期间采集、私有挂载命名空间内命令缺失和空系统日志、Docker 服务停止后恢复、默认无参数以及根目录深扫描限时。所有模拟写入和服务变动都发生在专用虚拟机里，不是产品执行的操作。

### 新增环境与资源

- Python 3.10.21 从 [Python 官方发布页](https://www.python.org/downloads/release/python-31021/) 的源码包构建。下载 `Python-3.10.21.tar.xz`，SHA-256 为 `a0da1e72132e950154eca0f6f47d5db828454700de20e5113667940d81e0db04`，与官方发布页一致。使用 Debian 官方 APT 编译依赖，GCC 12.2、`--without-ensurepip`、两线程编译，安装前缀为专用来宾目录 `扩展环境/python310`，没有安装宿主 Python。
- QEMU 来自 Debian 官方 APT：7.2.22（`1:7.2+dfsg-7+deb12u18+b3`），只安装在 `d12` 内。嵌套客户机运行 Debian 12、内核 `6.1.0-52-cloud-amd64`、Docker `20.10.24+dfsg1`、overlay2，SSH 与 `uname -m` 均实际确认 x86_64。它不是原生 x86 硬件，也不是只模拟一个 Python 进程。
- x86 镜像来自 [Debian 官方云镜像目录](https://cloud.debian.org/images/cloud/bookworm/latest/)，`debian-12-genericcloud-amd64.qcow2` 下载大小 348,520,448 字节。SHA-512 为 `c602f42a374c097bafcbc77c2d034fb06cb8a831d791bcbaa5d043f029874b0c32d41cb72ba8b6d50ccfd64c9b4b0dc9ade5b6e4065712f3eb152338e532721f`，与当次官方 SHA512SUMS 一致。镜像原件只读，另建 12 GiB 稀疏覆盖盘与全新测试 SSH 密钥。
- 嵌套客户机最终采用单线程 TCG、512 MiB 来宾内存、64 MiB 翻译缓存，QEMU 进程 MemoryMax 1200 MiB、CPUQuota 180%。资源均包含在原 `d12` 的 2 核、2 GiB 内存内，没有同时启动 Ubuntu。
- Ubuntu 使用官方 `docker-buildx 0.30.1-0ubuntu1~24.04.1`，下载 15.1 MB、安装额外占用 68.0 MB。没有拉取第三方容器镜像。APT 输出出现无交互终端的 debconf 提示，安装退出码为 0。
- 16:04 前后实测 `测试环境` 占用 8,144,328 KiB，约 7.77 GiB；宿主可用约 287 GiB。该占用包含 Python 编译目录、嵌套系统、原始下载和历史失败样本，不是本工具需要的运行空间。Ubuntu 根分区已用 3,051,847,680 字节；Debian 扩大验收期间根分区已用 3,878,109,184 字节。

### 保留的失败与限制

- x86 第一次使用 768 MiB 来宾、多线程 TCG，在安装 Docker 时触发 QEMU 内存限制被终止；同时 QEMU 提示 ARM 主机与 x86 来宾的内存顺序差异。失败日志保留。停止后 `qemu-img check` 没有发现磁盘错误，改为上述单线程配置，恢复官方包配置并完成全部验证。没有放宽测试的时间期限断言。
- 拓展验收第一次有两项预期不符合现有状态契约：Docker 根目录被拒绝后没有逐容器记录；完全无 journal 文件时，原生命令 stderr 明确提示无日志，产品仍保守标记部分完成、已知大小为零。共同核对生产代码与原始结果后修正验收预期，增加总体权限状态、无伪造字节和警告说明的严格断言；生产代码没有因此修改。
- 产品非 UTF-8 文件名报告保存已通过；验收工具自身汇总这些原始名称时曾发生 UnicodeEncodeError。首次不完整 JSON 与堆栈保留，验收写入器增加 `errors='backslashreplace'` 后以新文件名复测。详见 `报告/扩大验收02原始失败说明.md`。
- 嵌套 x86 已发送 poweroff；发送后立即查询仍显示 active，独立关机完成没有观察到。随后包含它的 `d12` 整体停止，因此没有嵌套进程继续运行；没有把瞬时 active 输出描述为已证实正常关机。

### 正式包抽查及最终停止

正式 0.1.1 的运行包与解压源码在 Ubuntu 各自实际验证帮助、版本、深扫描保存、UTF-8 JSON、五个检查项目、重复导出不覆盖以及被检查文件内容/权限/修改时间不变，两组共 16 条断言通过。原始记录为 `报告/正式包Linux最终实测.json`；两种入口实际生成的四组文本与 JSON 保存在 `报告/正式包实际生成报告.tar.gz`。

正式运行包 SHA-256 为 `2c9ec1c86f90f7c867cf953d0c8d409d381a7210e5cc12d7536c5b6017ce19af`；正式源码归档为 `04cf7ce7947d808b50981ec883d2f2fdd73de87846b167310cc219cf52b00779`，均与正式发布清单和 Ubuntu 收到的文件一致。正式归档重新构建后的整体摘要与候选不同，不可混用；生产成员仍与冻结候选相同。

2026-09-03 16:08:05（UTC+8）最终查询：`d12`、`u24` 均为 `Stopped`，不再占用虚拟机运行内存。Ubuntu 停止前没有任何 loop 设备，也没有专用样本遗留挂载；所有八份有效隔离报告的清理错误均为空。宿主 `/Users/kaka/.lima` 与 `/Users/kaka/Library/Caches/lima` 仍不存在。

最终 `测试环境` 实际占用 8,146,348 KiB（约 7.77 GiB），其中 Debian 实例 3,997,988 KiB、Ubuntu 实例 3,089,224 KiB；宿主可用 301,090,956 KiB（约 287 GiB）。Ubuntu 最终根分区已用 3,052,609,536 字节。完整停止/空间证据保存在 `测试环境/日志/扩大验收最终双VM停止及空间.log`，默认目录没有创建、历史文件没有删除、未上传 GitHub。

### 原始证据

- `报告/扩大单元测试-{Debian311,Debian310,DebianX86,Ubuntu312}-候选02.log`。
- `报告/Linux验收原始结果-Debian311-扩大02-基础.json`，其余七份有效结果使用 `扩大02b` 后缀。
- `测试环境/日志/扩大验收生产代码及原始报告一致性.json`：逐个读取八份完整 JSON，核对全部断言、生产文件前后摘要、当前源码与候选源码一致性。
- `测试环境/日志/扩展验收-*`：官方来源下载、编译、x86 启动和故障、安装版本、资源及场景命令原始输出。

生产运行包 SHA-256：`cc571244332639fbc83f40f5f40b51e4fa9acf871477b614734e5290372b38cc`。候选源码归档 SHA-256：`499bd5be1790d409dd2375382d1f4d9c8133c504ead9474cf85ac3800e519351`。最终源码归档包含后续测试说明时摘要可以不同，生产成员须与该候选保持一致。

## 使用方式

以下命令在 `/Users/kaka/Desktop/服务器/测试环境` 执行，必须通过本目录包装脚本调用，避免误用默认数据目录。

```sh
# 查看状态
sh ./环境命令.sh list

# Ubuntu 启停；启动前确保另一套已停止
sh ./环境命令.sh start --tty=false u24
sh ./环境命令.sh shell --workdir=/tmp u24 uname -a
sh ./环境命令.sh stop u24

# Debian 启停
sh ./环境命令.sh start --tty=false d12
sh ./环境命令.sh shell --workdir=/tmp d12 uname -a
sh ./环境命令.sh stop d12

# 仅复制本项目明确需要的测试文件，不使用目录挂载
sh ./环境命令.sh copy --backend=scp ./配置/基础验证.sh u24:/tmp/基础验证.sh
sh ./环境命令.sh shell --workdir=/tmp u24 sh /tmp/基础验证.sh
```

SSH 端口在重启后可能变化，使用包装命令或对应实例内的 `ssh.config`，不要固定写死端口。虚拟机用户是 `tester`，仅专用环境内具备免密 sudo。

## 下载与校验

从官方 HTTPS 地址下载，并核对相应摘要文件。没有跳过摘要失败继续启动。这里验证的是 HTTPS 来源和摘要一致性，未独立验证发行方 GPG 签名。

- [Lima 2.2.0 发布页](https://github.com/lima-vm/lima/releases/tag/v2.2.0)
  - 文件：`lima-2.2.0-Darwin-arm64.tar.gz`
  - SHA-256：`bbdef91774885a0d05f7b048c4eb89ae2bcf3a0c252ae7ca7934e63df76d93c3`
  - `Lima-SHA256SUMS` 本身的 SHA-256 与发布页公布值相同：`7da5160ee9b22de8eec4222e581334ee6326881e20d5aa8eb29b22f897312a5f`
- [Ubuntu 24.04 官方镜像目录，构建 20260826](https://cloud-images.ubuntu.com/releases/noble/release/)
  - 文件：`ubuntu-24.04-server-cloudimg-arm64.img`
  - SHA-256：`afa139bac6f2629c1e1f2f8f34215f3a9ad9779801bcb945521ba1a45016743f`
- [Debian 12 官方云镜像目录](https://cloud.debian.org/images/cloud/bookworm/latest/)
  - 文件：`debian-12-genericcloud-arm64.qcow2`
  - SHA-512：`525c2ead4b8a905cab07106696761fda61e2431480bc70b1b1bcc5cab93823f1c04268918fc5e7a40b128e751a572a346416f5b829c0f926d29d71ccc40baac6`

官方 `latest` 或 `release` 内容可能更新；本项目保留下载原件和当次摘要，后续不能直接把新镜像当成本次镜像。

## 已验证与限制

- 两套 Lima YAML 都通过配置验证，两套镜像都实际完成启动并通过 SSH 执行命令。
- 初次基础检查包括：2 核、免密 sudo、SSH 服务活动、没有 `/Users/kaka`、没有 9p/virtiofs/sshfs 主机共享挂载、没有已有 Docker socket。此后仅为本项目测试新装 Docker。
- Debian 初始化完成且无失败系统服务；访问官方软件仓库返回 HTTP 200。
- Ubuntu 初始化完成，`errors` 为空；cloud-init 报告两类弃用字段警告，来自 Lima 生成的 `ssh-authorized-keys` 和字符串 `uid`。因此其状态中包含 `degraded done`，没有隐瞒为完全无警告。
- 首次启动时短暂出现 SSH 尚未就绪的连接失败，随后就绪检查通过。关闭时网络监听结束可能记录 `use of closed network connection`；以最终实例 `Stopped` 为准。
- Lima 启动日志提示 `audio.device` 为实验字段；配置为 `none`，不使用音频设备。该警告未造成启动或验收失败。
- Docker、systemd 日志及磁盘测试已有实际记录；本工具不提供安装、卸载或清理业务服务的功能，不把这些操作列为已通过的产品功能。
- 0.1.0 历史验收仅验证原生 arm64；0.1.1 扩大验收另有完整 x86_64 软件模拟系统。软件模拟通过不代表原生 x86 硬件性能或真实公网业务服务器已经验证。

## 参考依据

- [官方便携安装方法](https://lima-vm.io/docs/installation/)
- [普通模式说明](https://lima-vm.io/docs/config/plain/)
- [2.2.0 配置字段](https://github.com/lima-vm/lima/blob/v2.2.0/templates/default.yaml)
- [下载器本地文件不缓存的源码](https://github.com/lima-vm/lima/blob/v2.2.0/pkg/downloader/downloader.go)

具体启动和基础检查输出保存在 `测试环境/日志`；Lima 自身运行日志位于对应实例目录。
