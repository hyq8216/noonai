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
