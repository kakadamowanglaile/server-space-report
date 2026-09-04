# Contributing to Server Space Report

**English** | [简体中文](CONTRIBUTING.zh-CN.md) | [Project overview](README.md)

Issues and pull requests in English or Chinese are welcome. Describe the problem or intended behavior, and keep unrelated changes in separate contributions.

## Report a problem

Include the operating system, architecture, Python version, relevant Docker version, command, expected behavior, and actual result. Use a small synthetic example when possible. Remove credentials, private paths, container identifiers, and other sensitive details from anything you share. An incomplete check is not automatically a bug: permission limits, changing files, and unavailable tools must remain visible.

## Choose the repository to work in

- **Maintainers with write access:** use a clone of `kakadamowanglaile/server-space-report`. Here, `origin` refers to the project repository.
- **External contributors:** fork the repository on GitHub, then use the Code button on your fork to copy its clone URL. In that clone, `origin` refers to your fork. Add the original repository as `upstream` if that remote does not already exist:

```sh
git remote add upstream https://github.com/kakadamowanglaile/server-space-report.git
```

Check remote URLs with `git remote -v` before pushing. Do not try to push directly to the original repository without write access.

## Make a change

Start with a clean working tree, or commit your existing work on its current branch. Do not discard unfinished changes to follow these examples.

Maintainers can update their local `main` with:

```sh
git switch main
git pull --ff-only
```

External contributors can update from the original repository with:

```sh
git fetch upstream
git switch main
git merge --ff-only upstream/main
```

Create a new branch with a descriptive name. The following example is for a documentation change; use an unused branch name for each separate change:

```sh
git switch -c docs/improve-usage
```

Edit the relevant files. Changes to the project overview and contribution guide should update both English and Chinese versions. Runtime help, reports, and JSON field names are currently Chinese; a documentation translation does not add runtime language support. Any future localization must preserve compatibility with the documented report format.

## Test and build

From the project root, with Python 3.10 or later:

```sh
python3 -B -m unittest discover -s 测试 -v
python3 -B 工具/构建发布包.py
```

Test temporary files live under `测试环境/临时`. The build command creates artifacts under `发布包/`; these directories are ignored by Git. The source archive must contain both README files and both contribution guides, with working relative links. Preserve the builder's explicit allowlist.

Source archives also include `工具/核对交付包.py` for checking release hashes, source contents, runtime consistency with a validated candidate, and the extracted test suite. Run `python3 -B 工具/核对交付包.py --help` for its options. Supply the release directory with `--release`, the validated candidate directory with `--candidate`, and the expected total test count from that candidate's validation record with `--test-count`. The count is required: do not reuse a historical number or change it merely to make a failed check pass. Revalidate a changed test suite before using its new count. The checker must run from the source tree used to build the release; later source changes require a new build in a separate directory.

GitHub Actions runs unit and interface tests on Ubuntu 24.04 with Python 3.10, 3.11, and 3.12 for pushes and pull requests. It does not run the privileged integration scripts. A passing CI result does not replace actual Docker, mount, exhaustion, or sustained-load validation. See the [validation requirements (Chinese)](文档/验收说明.md) before performing integration tests; use a disposable, isolated environment without business data. Record failures and skips accurately.

## Commit and open a pull request

Review changes before staging. For a change to the two overview files, these are real paths you can use; select the actual files you edited for other tasks:

```sh
git diff
git add -- README.md README.zh-CN.md
git diff --cached
git diff --cached --check
git commit -m "docs: clarify usage in both languages"
git push -u origin HEAD
```

Open the branch on GitHub and choose **Compare & pull request**. External contributors should target `kakadamowanglaile/server-space-report:main` from their fork's branch. Explain what changed, why, and what was tested. Wait for CI, address review feedback, and merge only when the change is ready. Saving a local file or making a local commit alone does not update GitHub.

## Files and release boundaries

Commit source code, relevant tests, general documentation, synthetic examples, the license, and CI configuration. Keep `测试环境/`, `报告/`, local `发布包/`, virtual disks, caches, SSH keys, passwords, API tokens, cookies, webhooks, and real machine reports out of commits. Share redacted excerpts rather than uploading raw reports.

`.gitignore` cannot detect a credential pasted into source code or remove content from earlier commits. Inspect the staged diff every time; do not force-add ignored files. If a credential has already been exposed, stop using it and replace it before addressing the repository history.

Maintainers create a version tag and GitHub Release after the corresponding validation is complete. Attach only reviewed, allowlisted source and single-file builds with release notes and hashes. Do not mark a version ready solely because its source is public or its unit tests pass.
