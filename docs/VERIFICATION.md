# 云端迁移验证记录

日期：2026-10-03（Asia/Shanghai）。

## 初始基线

已有0.42源码首次执行全量回归：373 tests，25 failures，17 errors。
测试采用现有桌面打包虚拟环境（Python 3.12；Pillow 12.3.0；cryptography 47.0.0；
boto3 1.43.107；imageio-ffmpeg 0.6.0）。没有真实店铺提交。

主要失败组：自动化直接创建的预检契约、调度器到期时机、分页/差量读取契约、
旧备份结构兼容、图片协议夹具与输出检查、图片托管/视频/视觉子任务推进。
这些是初步分类，不能将全部失败认定为旧测试，也不能为通过而绕开安全限制。

全量测试日志由 GitHub Actions 保存在 backend-verification artifact；仓库 CI
保留真实退出码，失败时也继续尝试独立重启检查与保存日志。

## 新增验证入口

- scripts/doctor.py：运行依赖、FFmpeg 和 Node 诊断。
- scripts/smoke.py：真实服务器启动、调度器运行、无令牌拒写、货源去重、重启持久化。
- scripts/browser/smoke.cjs：真实 Chromium 启动、六个页面切换、未捕获JS异常检测。
- scripts/check.sh：全量测试、实际服务器验收、JS语法和浏览器检查。

## 尚未验证

Codex Cloud 账号环境创建与 setup 回读；GitHub Linux CI 首次结果；
真实 noon 店铺、真实订阅生图、1688授权、商用云服务与多人权限。

## 本轮已修复与验收

- 修复自动化中心读取摘要状态后立即渲染导致 undefined.map 报错；先请求明细再渲染。
- 兼容保留视觉/视频规则而缺少旧投递箱主表的备份；原有迁移测试现已通过。
- 投递箱大文件规则已是20MB，将旧4MB测试的超限样例更新至真实上限；仍验证错误隔离和符号链接拒绝。
- 本地诊断通过；实际服务启动/调度器/写保护/去重/重启持久化通过；真实Chromium六页切换通过。
- 全量回归失败继续保留，不绕过或跳过。上述本地验证不代表Linux云端或真实店铺验证。

## 复测与GitHub Linux执行

- 修复后本地全量：373 tests，24 failures，16 errors（333通过，40项待修复）。
- 独立Python3.12环境 setup.sh 安装成功；pip check 无冲突；10项投递箱回归全部通过。
- 首次GitHub运行：https://github.com/hyq8216/noonai/actions/runs/37092039943 ，提交059b934。
- Linux依赖安装、pip check、环境诊断、实际服务器启动/重启：全部成功。
- Linux JS语法检查与真实Chromium六页导航：成功。
- Linux全量回归：失败；日志artifact已成功保存。失败不能标记为发布通过。
- Codex本地聊天心跳 noonai 已启用，每6小时推进一项；这不是已验证的云端定时开发。
- Codex Cloud账号环境仍需在设置页创建或选择并确认setup结果，本轮UI工具两次超时。

Linux全量日志核对为25 failures、16 errors（41项）：比本地多一项图片模板失败，
原因是字体路径仅支持macOS。本轮补充Linux字体选择及中/英/阿文字绘制环境诊断，
setup和CI安装Noto CJK与DejaVu。后续CI结果必须独立回读。
跟踪问题：https://github.com/hyq8216/noonai/issues/1 。

本机新建PyPI运行环境的Pillow未提供RAQM；诊断明确记录arabic_layout=false，
英文/中文模板通过。阿文模板沿用阻止错误排版的门禁，不能据此声称本机阿文图片已可用。

## 最新Linux回读（代码提交6ff24d1）

https://github.com/hyq8216/noonai/actions/runs/37092258230

- Linux中/英/阿文字绘制诊断成功，arabic_layout=true；字体兼容缺陷已消除。
- 373 tests，24 failures、16 errors，与本地同为333通过、40项待修复。
- 实际启动、调度器、写保护、去重和重启持久化成功。
- 独立browser job成功，包含JS语法和真实Chromium六页切换。
- 完整工作流仍为failure，不能宣称发布或商用验证通过。

## 2026-10-03 新版云端兼容修复

复现来源：新版环境实际为 Debian 13.6、普通用户、无 sudo；仓库原 setup.sh
在 Linux 调用 sudo apt-get 和 Playwright --with-deps。准备任务单独安装成功不代表
仓库脚本可在新任务复用；Node 22 和 Playwright 缓存的 shell exports 也不会跨阶段保存。

- setup.sh 在无管理员权限时使用镜像已有库，不调用 sudo；缺少字体时明确失败。
- Node 22 与 npm/pip/Playwright 缓存位于忽略提交的 .cloud-runtime；
  with-runtime.sh 在每个 shell 恢复运行配置，check.sh 与浏览器检查使用同一缓存。
- 增加仓库启动技能，始终用临时 --data、ready.json 与 HTTP 200 检查。
- 本机 Python 3.12.14 安装成功、依赖诊断通过；服务启动/重启 smoke 通过。
- 本机阿文 RAQM 仍为 false；不能将本机检查说成 Linux 或阿文图片验收。
- 本机完整回归：373 tests / 24 failures / 16 errors，即 333 通过；未修改业务测试或断言。
- 真实新版云端复验提交 82bf0556ac3173cab9c4a360d4905f09514ff884：
  Debian 13.6 / uid 1000 / 无 sudo / Python 3.12.14；setup 退出 0。
  全新 shell Node 22.23.3，恢复 .cloud-runtime 内 npm/pip/Playwright 缓存；
  Chromium 145.0.7632.6 实际启动，六页 products/import/automation/models/recovery/batch 通过。
  独立 Python smoke 的启动、调度器、写保护、去重、持久化与重启通过。
  check.sh 373 tests in 56.226s，24 failures / 16 errors；因失败返回 1，未绕过。
  云端验收会话：https://chatgpt.com/local/01a10039-0dff-747f-9e07-1b3270401030
- GitHub CI https://github.com/hyq8216/noonai/actions/runs/37099780962 ：
  browser job 与独立启动/重启通过；373 tests in 75.116s，同样 24 failures / 16 errors。
  backend job 及总体工作流失败，不能声称完整系统通过或具备真实卖家验收。
- 修复 PR：https://github.com/hyq8216/noonai/pull/2 ，未合并；main 仍需单独授权同步。
  账号环境的兼容内联配置及发布状态另行验收，不能由此 PR 推断已发布。

账号环境 main 基线独立复验：
- 在 6d2276e572e9ddcdd3d3ec71a9985a1ade7c1e2f 上执行等价内联安装，
  不依赖尚未合并的 runtime.sh / with-runtime.sh，也不修改 tracked 文件。
- Python 3.12.14 / Node 22.23.3 / Chromium 145.0.7632.6；pip check、
  ready.json 实际端口 HTTP 200、独立启动/重启及六页浏览器检查全部通过。
- 373 tests in 58.217s，24 failures / 16 errors，40项失败名称与修复分支相同。
- 账号内联安装和启动技能草稿已保存；环境发布需以账号界面独立回读为准。
