# 参与维护服务器空间去哪了

[English](CONTRIBUTING.md) | **简体中文** | [项目介绍](README.zh-CN.md)

欢迎使用中文或英文提交 Issue 和 Pull Request。说明问题或预期行为，不相关的修改请分别提交。

## 反馈问题

提供系统、架构、Python 版本、相关 Docker 版本、执行命令、预期行为和实际结果。尽量使用小型人工样例复现。分享前删除凭据、私人路径、容器标识等敏感信息。检查未完成不一定是程序错误：权限不足、文件变化、工具缺失等情况应如实显示。

## 选择工作仓库

- **有写权限的维护者：**使用 `kakadamowanglaile/server-space-report` 的本地克隆，此时 `origin` 指向项目仓库。
- **外部贡献者：**在 GitHub 上 Fork 项目，从自己 Fork 的 Code 按钮复制地址并克隆。此时 `origin` 指向自己的 Fork。如果还没有 `upstream`，用下面的命令添加原项目：

```sh
git remote add upstream https://github.com/kakadamowanglaile/server-space-report.git
```

推送前通过 `git remote -v` 核对地址。没有写权限时不能直接推送到原项目。

## 开始修改

开始前应没有未提交改动，或已将现有工作提交到其所在分支。不要为了执行示例而丢弃未完成的修改。

维护者更新本地 `main`：

```sh
git switch main
git pull --ff-only
```

外部贡献者从原项目更新：

```sh
git fetch upstream
git switch main
git merge --ff-only upstream/main
```

创建名称能说明用途的新分支。下面以修改文档为例；每项独立修改请使用尚未存在的分支名：

```sh
git switch -c docs/improve-usage
```

修改相关文件。项目介绍和贡献指南需要同步更新中英文版本。当前命令行帮助、报告和 JSON 字段仍是中文；翻译文档不会让程序自动支持英文。后续增加语言选择时，应保持已公开报告格式的兼容性。

## 测试和构建

使用 Python 3.10 或更新版本，在项目根目录执行：

```sh
python3 -B -m unittest discover -s 测试 -v
python3 -B 工具/构建发布包.py
```

测试临时文件位于 `测试环境/临时`，构建产物位于 `发布包/`，这些目录均被 Git 忽略。源码包必须包含两份 README 和两份贡献指南，并保证相对链接可用。保留构建脚本的明确白名单。

每次推送或创建 Pull Request，GitHub Actions 会在 Ubuntu 24.04 上使用 Python 3.10、3.11、3.12 执行单元与接口测试，不执行需要管理员身份的隔离验收脚本。CI 通过不能替代实际 Docker、挂载、资源耗尽和持续负载验收。进行真实验收前阅读 [验收说明](文档/验收说明.md)，使用可重建且不含业务数据的隔离环境，如实记录失败和跳过项。

## 提交和创建 Pull Request

暂存前检查改动。下面以修改两份项目介绍为例，命令里的文件路径真实存在；其他任务请选择自己实际修改的文件：

```sh
git diff
git add -- README.md README.zh-CN.md
git diff --cached
git diff --cached --check
git commit -m "docs: clarify usage in both languages"
git push -u origin HEAD
```

在 GitHub 打开该分支，选择 **Compare & pull request**。外部贡献者以自己 Fork 的分支为来源，以 `kakadamowanglaile/server-space-report:main` 为目标。说明修改内容、原因和测试结果，等待 CI，处理审阅意见后再合并。仅在本地保存文件或执行 commit 不会更新 GitHub。

## 上传范围与版本发布

可以提交源码、相关测试、通用文档、人工示例、许可证和 CI 配置。`测试环境/`、`报告/`、本地 `发布包/`、虚拟磁盘、缓存、SSH 密钥、密码、API Token、Cookie、Webhook 和真实机器报告不得直接提交。反馈问题时使用已脱敏的必要片段。

`.gitignore` 不能识别粘贴到源码中的凭据，也不会移除历史提交中的内容。每次提交前检查暂存区，不要强制添加被忽略的文件。若凭据已泄露，应停止使用并更换，再处理仓库历史。

对应验收完成后，由维护者创建版本标签和 GitHub Release，附经过检查的白名单源码包、单文件包、更新说明和摘要。不能仅因源码公开或单元测试通过，就标记版本已完成验收。
