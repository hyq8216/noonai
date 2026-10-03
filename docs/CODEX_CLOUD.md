# Codex Cloud 开发环境

目标仓库：https://github.com/hyq8216/noonai ，默认分支 main。
官方依据（2026-10-03 核对）：
https://learn.chatgpt.com/docs/environments/cloud-environments

新版环境入口：https://chatgpt.com/settings/codex-cloud 。Legacy 环境是独立入口，
旧版的 setup 成功不代表新版环境可用。新版实际运行机器可能是普通用户的 Debian，
不能假定 Ubuntu、root 或 sudo。

新版 Install script 使用 `bash scripts/setup.sh`；Start skill 使用
`.codex/cloud-start.md` 的启动步骤。每个新 shell 用 `bash scripts/with-runtime.sh`
恢复 Node 22 和浏览器缓存配置。固定浏览器下载需允许官方 CDN 域名
`cdn.playwright.dev`、`playwright.download.prss.microsoft.com`、`storage.googleapis.com`。
具体网络策略以账号配置和下载实测为准，不禁用 TLS 或下载校验。

Legacy 配置：

| 项目 | 配置 |
| --- | --- |
| 仓库 | hyq8216/noonai |
| 镜像 | universal |
| Python / Node | 3.12 / 22 |
| Setup script | `bash scripts/setup.sh` |
| Maintenance script | `bash scripts/maintenance.sh` |
| 验证 | `bash scripts/check.sh` |
| Agent 网络 | 开发和模拟测试可关闭；集成验证按实际授权域名单独配置 |
| 店铺与模型密钥 | 开发环境不需要真实凭证 |

环境脚本的 export 不会自动传到 agent 阶段，所以每次明确使用 `.venv/bin/python`。
缓存恢复后执行维护脚本，以匹配当前分支依赖。

账号环境是否可选、脚本安装成功及新任务恢复均需独立验收；编辑页 Published 提示
不能替代环境列表和运行结果。提交这些文件不会自动创建或发布账号环境。

## 开发任务提示词

> 在 hyq8216/noonai 的 main 基线上，读取 AGENTS.md 和 docs/AUTOMATION_ROADMAP.md。
> 先执行环境诊断和基线测试，选择优先级最高的一项自动化缺陷。
> 保留防重复、商品版本核对、人工审核、异常恢复与调用预算；完成修复和回归验证。
> 在 codex/ 分支提交并创建 PR，说明触发条件、修复行为、测试结果与仍未验证的边界。
> 更新 docs/VERIFICATION.md。不要自动合并，不要写入真实店铺或使用生产凭证。

## 云端开发与业务运行

官方工作流按任务创建容器、检出仓库、执行 setup/maintenance，再运行开发任务。
它不构成本系统已部署为持续运行的商业服务的证据。当前后端仍为单实例 SQLite、
本地令牌与本机监听；不要为了外网访问直接改成 0.0.0.0。

业务长期托管的后续验收：持久化数据库和素材、访问身份与权限、TLS、单实例锁、
进程管理、任务租约/恢复、周期备份和恢复演练、外部调用限流、密钥管理、真实回执
对账、故障报警。多人协作和多进程 worker 需要另行设计，不能直接并行打开 SQLite
业务目录。macOS Codex App Server 的本机订阅授权不会随源码自动迁移到云端；
云端模型供应器必须独立验证，不能复制登录文件或默默切换付费 API。

## 持续改进调度

当前已保存Codex本地聊天心跳：每6小时推进一个有测试证据的改进。
该心跳依赖本地Codex执行环境，不能宣称关机后仍在云端持续开发。
GitHub每日CI可独立在云端运行，但它负责验证，不自行修改代码。
如果需要云端全天候自动开发，须另行验证账号提供的云端任务调度能力与调用预算。
