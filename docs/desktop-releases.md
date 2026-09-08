# ChatArch 桌面版本发布

`Desktop release` 工作流构建真正的桌面安装包，而不只是 GitHub 自动提供的源码归档。
这是基于 Nous Research 原作的社区分支构建，保留 Hermes 署名与许可证；应用资源内包含
`Hermes-LICENSE.txt`，不使用官方 CDN，也不宣称拥有官方签名身份。

## 版本与来源

- Tag 沿用 CalVer：`vYYYY.M.D[.N]`。日期必须有效，月、日不得补零；可选 `.N`
  为同日发布的正整数序号。Tag 解引用后的 commit 必须位于远端 `main` 或 `release`
  的历史中；任意独立功能分支上的 tag 会在构建前被拒绝。
- 软件版本仍用 SemVer。`pyproject.toml` 与 `hermes_cli/__init__.py` 必须一致；
  工作流通过 electron-builder 的 `extraMetadata.version` 将它写入安装包和 install stamp。
  不改写桌面工作区的本地开发版本，也不自动提交版本升级。
- 四个构建目标使用同一个固定 commit，清单和 stamp 记录 repository、commit、version。
  首次安装从该 repository 的该 commit 下载 `install.sh` 或 `install.ps1`，再将仓库和
  commit 传给安装器。先克隆 `main`，再获取精确 commit，包括 `release` 分支上的 commit。
- 安装器缓存按仓库隔离。固定来源下载失败不会回退到已安装的上游脚本；bootstrap 遇到
  不同仓库的托管 checkout 会拒绝操作，应连接已有后端或选择独立安装目录。
  正常更新跟随托管 checkout 的 origin，保留已有的防降级行为。

## 安装包与校验

统一命名：

```text
ChatArch-Hermes-<SemVer>-<CalVer-or-preview>-<platform>-<arch>-unsigned.<format>
```

| 原生 runner | Platform / 架构 | 必需格式 |
| --- | --- | --- |
| `macos-15` | `darwin` / `arm64` | `.dmg`、`.zip` |
| `macos-15-intel` | `darwin` / `x64` | `.dmg`、`.zip` |
| `windows-2025` | `win32` / `x64` | `.exe`（NSIS）、`.msi` |
| `ubuntu-24.04` | `linux` / `x64` | `.AppImage`、`.deb`、`.rpm` |

**九个安装包缺一不可。** 工作流检查 runner 与可执行文件架构，复用现有打包 hooks 和
原生依赖 staging；Linux CI 安装 RPM 工具。任一平台、格式或检查失败都会阻止发布，
不会缩减矩阵。构建器始终使用 `--publish never`，只有单独的 tag-only job 发布 Release。

完整 bundle 还包含 `release-manifest.json`（来源、版本、平台、架构、大小、签名状态及
每个安装包的 SHA256）、`release-notes.md` 和 `SHA256SUMS`。校验文件覆盖全部安装包、
manifest 和发布说明。

按系统和 CPU 选择安装包：macOS 通常用 DMG，也可下载含 app bundle 的 ZIP；Windows
可选交互式 NSIS 或 MSI；Linux 可选便携 AppImage 或对应发行版的 DEB/RPM。打开前校验：

```bash
sha256sum -c SHA256SUMS           # Linux
shasum -a 256 -c SHA256SUMS       # macOS
```

Windows 使用 `Get-FileHash -Algorithm SHA256 <installer>`，与 `SHA256SUMS` 对照。

## 明确未签名；首次启动需要后端

PR 预览与 tag 构建均**按设计不签名**，macOS **未公证**。Gatekeeper、SmartScreen 或
组织设备策略可能警告或拒绝运行；不要为安装而关闭系统级安全控制。

此工作流**没有启用任何签名 secrets**。`CSC_IDENTITY_AUTO_DISCOVERY=false` 和 macOS
`identity: null` 防止误用 runner 身份。已有手动构建 hooks 支持 `CSC_LINK`、
`CSC_KEY_PASSWORD`；公证 hook 支持 `APPLE_API_KEY`、`APPLE_API_KEY_ID`、
`APPLE_API_ISSUER` 或本地 `APPLE_NOTARY_PROFILE`。这些只是已有 hook 的名称，
**添加同名 repository secrets 不会使本工作流签名**。Windows 保留
`signAndEditExecutable: false`。签名交付需要另行审查配置，并使用发布方自有证书，
绝不借用 Nous Research 的身份或凭据。

安装包只含 Electron shell/UI 与其原生依赖，不含 Python 后端环境。首次启动需联网访问
GitHub 和安装器依赖源来安装后端，或连接已有兼容后端；模型凭据由用户自行配置。
这不是离线后端发行包。打包来自干净 checkout，不读取用户 Hermes home；检查实际 ASAR
版本、来源及资源，拒绝私有配置、profiles、tokens、缓存路径和文本资源中的构建主机路径。
仍须确保私密数据从未进入源码。

## PR 预览

相关 `pull_request` 运行四个原生构建，固定到 PR head commit 和 head repository，
不使用临时 merge commit 作为 bootstrap 来源。标识为 `preview-pr-N`；同一 PR 的新提交
会取消过时构建，**tag 发布运行不会被自动取消**。

在 Actions run 下载 `desktop-<platform>-<arch>`，或全矩阵通过后的
`desktop-release-bundle`；保留 14 天。预览来源 commit 必须继续可公开获取，才能首次安装。
PR 只有 `contents: read`，不保留 checkout 凭据，不使用签名/发布 secrets，不进入发布
environment，不创建 tag 或 Release。使用 `pull_request` 而非 `pull_request_target`，
不要把这些不可信代码构建迁往有特权或持久化的 self-hosted runner。

## 维护者操作

1. 将审查后的源码合入 `main` 或 `release`，确认原生预览与打包检查结果；如需升级 SemVer，
   先在经过审查的提交中完成。选择 CalVer tag 和精确 commit。
2. **仅在真实发布另获授权后**创建 annotated tag 并推送该 tag。工作流本身不创建、移动或
   强制更新 Git tag。无需使用旧的 `scripts/release.py --publish`。
3. 等待来源检查、全部构建和完整 bundle 校验通过。最后的 job 使用作用域内的 GitHub token
   与 `contents: write` 调用 GitHub API；无需 PAT、gh 登录、npm token 或 CDN 凭据。
4. 发布器检查远端 tag、创建或续传自有 draft、校验资产 SHA256，再设置 `draft=false`。
   随后按确切 release id 回读状态、来源和完整资产名称/摘要；只有全部一致才返回并输出
   已验证的 GitHub Release URL。维护者仍应回读结果并在真实系统试装。

发布 job 使用名为 `desktop-release` 的 environment。**为该 environment 配置 reviewers、
tag-only deployment restrictions，或为分支/tag 设置 rulesets，均是维护者在真实发布前
可自行选择的治理策略，不是本流程强制新增的逐次人工审批门槛。** 未配置审批规则就不需
人工批准；若仓库已有保护规则，则遵循已有规则。本指南不要求修改 repository/environment
配置，也不启用签名 secrets。

## 失败与重跑

- 在同一次运行的 artifacts 尚未过期时使用 **Re-run failed jobs**。发布中断后不要重跑已成功
  的构建：时间戳等会改变二进制摘要。Artifacts 过期后应停止并规划新的发布身份，而不是覆盖。
- 中断会留下 draft。先按 tag 快速查询；若未返回，最多分页检查 10 页、每页 100 个 Release，
  找到对应 tag 的唯一候选后验证所有权。查询未穷尽或结果有歧义就失败，不能擅自新建替代品。
- 用同一已验证 bundle 重试，只接受摘要相同的已有资产，只上传缺失文件，不删除、替换或覆盖。
  已发布且来源、说明、名称和摘要完全一致时，回读验证后直接返回 URL，不进行写操作。
- 服务端发布响应或后续回读仍为 draft、来源变化、资产缺失/重复/摘要不符，都会失败，
  不会报告成功。GitHub API 不提供所需 SHA256 时也失败。
- 不认识的 draft、不同内容的资产、变动的 tag 或不完整的已发布 Release 都需维护者调查；
  不自动修复或删除发布证据。本流程不改动定时 Install & Update E2E，也不虚构历史 tag。
