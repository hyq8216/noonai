Warning: truncated output (original token count: 21650)
Total output lines: 502

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

账号环境发布与恢复回读：
- noonai 新云端 发布页显示 Environment published / Published；
  设置列表独立显示 Private，无 Unpublished 标记。旧 noonai Unpublished 仍存在。
- 发布后新建编辑草稿容器，会话 01a10043-6668-7316-a261-a7388adc403f，
  检出 main 6d2276e，env -i / 不加载 profile 后显式恢复路径和缓存。
  Python 3.12.14 / Node 22.23.3 / arabic_layout=true / Chromium 145.0.7632.6；
  ready.json 端口 43623 HTTP 200、独立服务/重启 smoke 与六页 browser smoke 退出 0。
  tracked / staged diff 为 0；未重新跑全量回归，333/40 的边界保持。
- 极简 PATH 需显式加入镜像 Python runtime bin 才能使用 python3.12 全局命令；
  .venv/bin/python 与保存的 Node 缓存入口独立可用，不依赖上一会话 exports。
- 新草稿仅验收，不改变或发布配置；临时数据已清理，无运行中验收服务。
  原生桌面任务选择器仍未独立验收，不能把设置列表当作选择器证明。

## 普通云端任务创建与浏览器准备资产修复

用户截图显示“无法开始此聊天”；截图没有失败任务 ID 或底层错误码，不能将其直接
归因于 GitHub 合并冲突。此前编辑草稿验收不能替代普通任务创建验收。

- Chrome 普通新聊天实际选择 noonai 新云端并创建任务
  https://chatgpt.com/local/01a10077-8c9e-7408-96c0-449ba28c5004 。
  main 6d2276e、Python 3.12.14、Node 22.23.3；仓库读取及命令实际执行成功。
- 该任务的 .cloud-runtime/cache/playwright 不存在；Chromium Headless Shell 缺失。
  本地 socket 初次被命令沙箱拒绝；正常命令审批后服务 smoke 通过。
  官方安装 Chromium 后，服务/重启及六页 browser smoke 均退出 0。
- 修复账号准备资产：浏览器改用 /workspace/.noonai-assets/playwright；
  准备 /workspace/.noonai-tools/install.sh 和 start.md，普通任务可直接读取。
  安装和启动说明读回与磁盘文件一致；新路径 Chromium 启动、服务及六页 smoke 通过。
  保存并重新发布环境，未改变网络/安全策略，不使用 sudo/--with-deps/TLS 绕过。
- 仓库 runtime 保留显式浏览器路径，自动选择仓库旁准备资产；独立 browser smoke
  使用同样的资产选择。配置路径保留断言、本机实际 Chromium 六页 smoke 通过。
  本机完整回归 373 tests in 278.997s，24 failures / 16 errors（333 通过）；未绕过断言。
- 新普通任务 https://chatgpt.com/local/01a10082-ac07-74b6-b0df-eb672899f014
  初次读取即发现两个准备文件和 chromium_headless_shell-1208，无需重新下载。
  最终普通任务实际运行通过：正常命令审批后 doctor / service smoke / browser smoke
  各退出 0；ready.json 实际端口 37233 HTTP 200，六页导航无未捕获 JS 错误。
  没有下载或补装浏览器；tracked 文件无改动，临时业务数据及活动服务已清理。
  主动记录首次 socket 拒绝，不修改安全策略；不把原截图失败原因记为已定位。

### 2026-10-03 桌面云端启动失败的直接日志与 CI 核对

- 桌面日志北京时间 14:54:01：`thread/start` 返回
  `AppServerRequestDeliveryError: Codex app-server is not available`；同刻
  `hostId=durable` / `thread_start_failed`。云端 WebSocket 反复
  `code=1006` / `failureReason=open_timeout`，截至 15:02:46 仍失败。
  故障发生在聊天创建/连接阶段，不能归因于仓库 setup 执行。
- 对公开云端连接域名只读测试：经系统代理 0.74 秒返回 HTTP 403（仅证明可达，
  不证明认证/握手成功）；直接连接 15 秒超时。系统代理已开，Clash Verge TUN
  未开；安装包静态 durable WebSocket transport 未为此公开域名显式配置代理。
  未走可用代理路由是最有力解释，仍需路由修正后桌面新任务及日志确认。
- 截图提交 `972b15b` 的 GitHub Actions run `37104018213`：browser 成功；
  backend 完整回归 373 项，24 failures / 16 errors。依赖、doctor、服务启动
  和重启均成功。CI 的业务/契约回归失败与桌面连接失败分别处理。
- fetch 最新远端提交 `60055827` 后，`git ls-tree` 确认 setup/check 均为
  `100755`；旧审查指出的执行权限问题已在后续树恢复。
- 本轮未改变全机路由、重启活动应用、合并 PR 或发出真实经营调用。
  详细诊断保存在忽略目录 output/cloud-environment-2026-10-03/桌面云端启动故障诊断.md。

### 2026-10-03 开启虚拟网卡后的桌面恢复回读

- 用户开启虚拟网卡后，只读配置确认 `enable_tun_mode: true` 与
  核心 `tun.enable: true`，自动路由启用。
- 北京时间 15:30:34.762，桌面 `hostId=durable` 连接初始化成功：
  `initialized=true`、`connectionError=null`、`next=connected`、
  `targetReadyState=1`；界面连接管理器随即清除此前连接失败状态。
- 15:30:43.272 记录云端 `thread_start_requested`；15:30:45.478
  `thread/start` 响应 `errorCode=null`，耗时 2204 ms。聊天创建阶段已恢复。
- 同一公开云端域名不显式设置代理的 HTTPS 对照现为 0.908343 秒返回
  HTTP 403、TLS 验证 0；此前相同方式 15 秒超时。HTTP 测试仅证明可达，
  桌面初始化与 thread/start 日志才是恢复连接及创建阶段的直接证据。
- 此结果不表示原失败聊天自动续跑，也不表示完整业务任务执行或后端
  CI 的 40 项失败已经修复；不再需要为此连接故障重建仓库或环境。

### 2026-10-04 调度器可控时钟、事件唤醒与首轮 P0 回归清理

- 自动化调度器现在可注入时钟，统一按 UTC 计算定时运行与 `retry_at` 到期；用户成功的 POST
  操作会唤醒调度线程，失败响应不会唤醒。后台媒体/模型工作线程仍由有界到期扫描兜底。
- 新增真实线程测试：未来任务不提前执行，推进假时钟并发出 wake 后处理；关闭并重建调度器后从
  已提交的下一步继续，`source`步骤审计事件只写一次。另验证 HTTP 202 响应唤醒、409 响应不唤醒。
- 全量首跑重现 37 项旧测试失败/错误后，按当前契约修复测试夹具和断言：图片生成夹具必须通过
  白底、最短边、画幅及与原图差异门槛；低质输出只隔离该 SKU，不暂停其它商品；图片不匹配及视频
  成品仍需人工验收；等待步骤使用假时钟；商品提交测试需显式模拟已配置店铺；目录变更页按 500
  条增量续页，500 商品流程验证查询持久化总数而非首屏 50 条。
- 最新 `bash scripts/check.sh` 退出 0：375 项业务测试全部通过；隔离服务实际启动、导入去重、
  服务关闭重开后持久化读回通过；真实 Chromium 启动及六个导航页面检查通过；所有静态 JS
  `node --check` 通过。日志 `/tmp/noon-automation-scheduler-event-wake-check-3.log`，
  `git diff --check` 通过。
- 边界：Noon、模型及对象存储没有真实账号调用；Chromium smoke 覆盖本机六页启动路径，不代表
  所有业务流程真实浏览器验收或真实 Noon 商品可售，`real_noon_verified=false`。

### 2026-10-04 调度器事件唤醒与冷却期回归

- 复现并修复等待项的唤醒缺陷：普通周期扫描仍遵守持久化 `retry_at`；审批/配置等成功状态变更触发事件扫描时，立即重查等待项，不必等冷却到期。用户审批重试、模型路由保存可及时推进；提交步骤的外部回执/人工对账闸门保持不变。
- `POST` 响应唤醒现在排除预览、模型探测/状态、导出、类目读取与媒体详情等只读操作，避免查看或试算意外绕过冷却。失败状态仍不唤醒。
- 假时钟/线程测试覆盖未来 retry_at 下周期扫描不执行、用户事件唤醒后执行、审批重试即时处理、路由变更事件扫描、未来计划不提前运行，以及调度器重建后从持久步骤继续且 source 审计事件不重复。自动化专项测试 30 项通过。
- 最新 `bash scripts/check.sh` 退出 0：378 项业务测试通过（44.091 秒）；隔离服务启动、调度器、写入保护、导入去重与持久化重启通过；真实 Chromium 启动及六个导航页面通过。`git diff --check` 通过。日志 `/tmp/noon-scheduler-wake-retry-check.log`。
- 边界：路由变更使用本地模拟配置及调度器/HTTP 单测，没有调用真实模型或 Noon 账号；自动化与接口测试不证明线上刊登或可售，`real_noon_verified=false`。下一项按路线图继续验收外部调用不确定回执对账。

### 2026-10-04 不确定 Noon 提交跨版本重发拦截

- 复现到一条安全缺口：之前 `uncertain` / `interrupted` 提交只锁定产生该回执的修订号，编辑商品形成新修订后，单件任务与批量预检都可能放行。由于原请求仍可能已到达 Noon，这不能视为安全重试。
- 现在提交保护按商品身份检查所有修订：同 SKU 任一 `uncertain`、`needs_attention` 或 `interrupted` 提交任务未处理前，单件 `add_job` 和批量预检均阻止再次排队。错误信息明确说明仅编辑商品不能解除拦截。
- 新增隔离数据库测试，模拟 Noon 请求已发送但客户端超时、服务重启时任务处于 running、改版后再尝试单件与批量提交。专项 2 项通过。
- 随后补上人工核对接口与任务页流程：必须确认已登录 Seller Lab、核对完全匹配的 SKU、填写至少8字依据；“已找到”还需记录 Noon 商品编号；“未找到”不能用于已有商品编号或平台已明确返回需处理的任务。结论及依据写入独立审计表和商品操作记录。已找到会登记平台编号和原提交版本但保持 `live_verified=false`；确认未找到只解除该项回执阻断，商品仍需人工重新排队。
- 页面读回保留原任务的 `uncertain` / `interrupted` 状态，在同一历史行显示人工核对结论、依据和时间，并隐藏重复核对表单。发现并修复侧栏进入“任务与记录”未刷新任务明细的问题。
- 新增隔离数据库/API 测试 4 项通过，覆盖失配 SKU/短依据/缺确认、absent 解锁只允许后续手动排队、found 登记 parent 且不标记可售、重启中断和修订变更仍需核对。Chromium 临时数据 smoke 实际完成人工 absent 表单提交并独立 SQLite 读回 `absent|uncertain`，确认回执持久化且原 job 状态未被伪造。
- 同一 job 的完全相同人工核对请求幂等返回原回执；不同结论或依据不能覆盖既有审计。任务历史显示核对结果/依据/时间并保留原始 job 状态，避免把本地声明伪装成平台返回。
- 本机/模拟路径不连接 Noon；真实店铺核对依赖操作员在 Seller Lab 的人工声明，不构成平台独立验证或真实回读。`real_noon_verified=false`。
- 最新 `bash scripts/check.sh` 退出 0：382 项业务测试通过（43.793 秒）；隔离服务启动、调度器、写入保护、导入去重与持久化重启检查通过；Chromium 启动、六个导航页面及完整人工提交对账操作路径通过。`git diff --check` 通过。日志 `/tmp/noon-manual-submit-reconciliation-check-2.log`。

### 2026-10-04 外部提交恢复的发送边界

- 复核 `App.run` 的原子领取边界后，发现启动恢复把仍在 `queued` 的任务和已 `running` 任务都标为 `interrupted`，让尚未交给 worker 的提交也必须人工查 Seller Lab。现改为：除视觉检查专属队列外，queued → `failed` 并记录“尚未开始外部调用”，可由操作员重新安排；running → `interrupted`，仍需要回执对账。worker 只有将 queued 原子更新为 running 后才会开始业务处理。视觉检查保持自己的持久 phase 恢复契约，仅恢复 phase 明确为 queued 的任务。
- 执行器关闭时拒绝 `submit()` 的窗口也已处理：队列任务记为明确未发送的 `failed`，接口返回503和可操作提示，不留永久 queued 任务，也不会标成 uncertain。
- 首次全量回归发现视觉检查依赖专属恢复逻辑；修正后专项回归和完整视觉检查套件通过。新增/更新隔离测试覆盖 queued 与 running 重启差异、执行器拒绝派发后审计和手动重新排队、发送后超时仍跨版本阻止重复提交。专项 7 项通过。
- 该区分基于本地 worker 领取状态，不代表确认 running 请求已到达 Noon；所有 running 中断、超时仍保守要求人工核对。真实账户未调用，`real_noon_verified=false`。
- 最终 `bash scripts/check.sh` 退出 0：384 项业务测试通过（43.600 秒）；隔离服务启动、调度器、写入保护、导入去重与持久化重启检查通过；Chromium 启动、六个导航页和人工提交对账操作路径通过。`git diff --check` 通过。日志 `/tmp/noon-external-dispatch-boundary-check-2.log`。

### 2026-10-04 批量 Noon 提交取消的领取竞争

- 增加批量内容提交专属取消回归：取消与 worker `queued -> running` 原子领取之前发生时，任务转为 `cancelled`，随后即使 worker callback 被调用也不会构造 Noon client 或发请求；领取已完成后取消返回 `cancelled_now=0` 并保留 `running` 状态，不把在途请求谎报为已停止。
- 隔离专项 8 项通过，包括上述两种竞态、执行器关闭、重启状态、超时与人工回执对账。
- `bash scripts/check.sh` 退出 0：386 项业务测试通过（50.714 秒）；隔离服务启动、调度器、写入保护、导入去重和持久化重启检查通过；Chromium 启动、六页导航及人工回执对账路径通过。`git diff --check` 通过。日志 `/tmp/noon-batch-submit-cancel-race-check.log`。没有真实 Noon 请求。

### 2026-10-04 Noon 返回成功后本地回执持久化失败

- 通过故障注入模拟 Noon 已返回有效 `sku_parent` 后，两种本地故障：`record_platform` 提交前失败；`record_platform` 已提交、但 job 状态更新前失败。两种情况下接口请求均只调用一次，job 保留 `uncertain`，直接再次排队被阻止；人工记录“已找到”可补齐平台编号，`live_verified` 仍为 false，同版本再提交继续被回执保护拦截。
- 最新 `bash scripts/check.sh` 退出 0：387 项业务测试通过（74.943 秒）；隔离服务启动、调度器、写入保护、导入去重与持久化重启检查通过；Chromium 启动、六页导航及人工回执对账流程通过。`git diff --check` 通过。日志 `/tmp/noon-receipt-persistence-window-check.log`。
- 本测试覆盖可注入的异常边界，未模拟物理断电、文件系统损坏或真实 Noon 账号；不会据此认定商品可售，`real_noon_verified=false`。

### 2026-10-04 5000行投递跨批冲突与异常行导出

- 实测精确5000行新商品 CSV 自动分成10批；跨500行边界的相同货源规格只保留一行，身份资料冲突的两行均拦截，错误价格行单独拦截。最终归集4996件，4条异常分别保留原文件数据行号10、500、501、502及原因；行号升序，软件重建后仍可从持久化回执读回。
- 修复前投递记录只持久化最多5条跳过示例，无法取得所有异常行。现在新商品和大批供货更新回执分别保存完整 `issue_rows`，保留短摘要供列表预览；历史详情新增“下载全部异常行”CSV，旧回执和无异常文件不显示误导性下载按钮。
- 真实 Chromium 流程用独立临时数据库处理跨批冲突样例，打开投递历史详情并实际下载 CSV；读取下载文件确认4条原行号、状态和原因。没有连接 Noon、供应商或模型账号。
- 最终 `bash scripts/check.sh` 退出0：388项测试通过（50.628秒）；隔离服务、调度器、写入保护、去重、持久化重启通过；Chromium 六页导航、人工提交对账及异常行 CSV 下载通过。日志 `/tmp/noon-bulk-issue-report-check-final.log`；`git diff --check` 通过。
- 截至本节记录时，尚未覆盖供货更新大文件恢复与0/空白语义；后续进展见下一节。真实供应商文件及 Noon 账号仍未接入，`real_noon_verified=false`。

### 2026-10-04 大批投递中断恢复与供货更新语义

- 将5000行验证推进到真实本地入口：启用投递箱，经 `deposit` 写入临时 CSV，再经目录扫描和稳定文件门槛处理；共10个500行批次。样例含8条无效成本、1条跨批重复、2条身份冲突，归集4989件；完整11条异常按原数据行升序持久化，列表摘要仍限制5条，历史详情完整CSV导出由前一节的真实 Chromium 下载验证覆盖。
- 新商品恢复故障注入：第一批提交及处理进度回执写入后抛出模拟进程终止；重建 `SourceInbox` 并再次扫描，第一批使用既有幂等收据、第二批完成，最终501件且只有两条批次收据，没有重复建品。
- 大批供货更新用501行（跨500行批次边界）验证零值/空白：成本空白保留原值、库存 `0` 清零；成本 `0` 被保留为有效清零、库存空白不变；双空白行拦截。同一SKU出现在第4和501行时两行均隔离，其他498行更新成功，报告含全部3条问题行。
- 供货更新中断恢复：首批499项事务写入后模拟进程终止并重建处理器；重扫后499项保持各自已更新版本，未被二次写入，重复SKU仍被拦截；完成回执如实显示重扫时499项与文件一致、2行跳过。
- 最终 `bash scripts/check.sh` 退出0：391项测试通过（46.986秒）；隔离服务、调度器、写入保护、导入去重、持久化及重启通过；真实 Chromium 六页导航、提交对账和异常行 CSV 下载通过。日志 `/tmp/noon-bulk-resume-and-supply-semantics-check.log`，`git diff --check` 通过。
- 中断由测试故障注入模拟并重建本地处理器，不是操作系统断电测试；供应商数据为合成值，未连接1688或 Noon，`real_noon_verified=false`。

### 2026-10-04 1万SKU分页与增量性能

- 固定夹具：临时 SQLite 中导入10,000件合成商品，20批×500行；标题、货源 URL、规格 SKU、供应商、规格事实、成本和库存保持同一生成规则。数据不来自真实供应商，不写 `workbench/data`。
- 后端本次实测：夹具写入0.484秒；首个 `catalog_snapshot` 0.546秒，21,997,861字节；Python `tracemalloc` 峰值165,706,594字节；SQLite trace记录4条语句（事务开始、最大事件ID、商品全量读取、事务结束）。这些值是当前运行环境的单次基线，不是跨设备性能保证。
- 增量契约实测：同一 token 无变更刷新0.000506秒、4条SQLite语句，响应仅含 `catalog_token` 与 `catalog_unchanged`，不返回 `products`；修改1件后0.000441秒回传1件，JSON 2,363字节，不退化为整库快照。
- 真实 Chromium 使用相同固定数据：首次 `/api/state` 资源23,733,020字节、260毫秒；应用首次可操作导航416毫秒；商品库首屏76毫秒；精确 SKU 搜索16毫秒；验证共200页、每页50件，并实际翻到第2页再返回，页内各50件。首屏及列表数据只在初次状态读回，后续接口保持事件增量。
- 本轮没有观察到需要改代码的性能缺陷；测试把10,000件数据库/接口/浏览器分页契约固定为可重复验收。`bash scripts/check.sh` 退出0：391项测试通过（48.263秒），隔离服务检查通过，Chromium六页导航、1万SKU首屏/搜索/翻页、提交对账及异常行 CSV 下载通过。日志 `/tmp/noon-10k-catalog-performance-check.log`；`git diff --check` 通过。
- 限制：为本地 macOS/当前运行机上的合成数据结果；没有采样线上多店铺并发、冷盘/低内存设备或真实货源记录，也不构成 Noon 账号性能或可售证明。

### 2026-10-04 图片生成参考副本完整性

- 复核图片生成链路的参考来源约束：任务会核对商品事实、参考素材归属、权利说明与源文件 SHA-256；本轮构造并发替换窗口，模拟参考文件在初次核对后、复制进模型输入目录时被替换。
- 修复：每张参考原图复制后、模型 turn 派发前，重新计算实际输入副本 SHA-256。副本与已确认参考版本不一致时任务阻止、保持 `dispatched_at=null`，且不调用图像生成协议；原素材不被改写。
- 新故障注入测试 `test_reference_copy_is_hash_checked_before_model_dispatch` 通过；`test_visuals` 专项27项通过。`bash scripts/check.sh` 退出0：392项业务测试通过（46.966秒）；隔离服务启动/写保护/持久化重启、10,000 SKU性能及 Chromium六页导航/对账/CSV下载均通过。最新性能记录见前节；本轮文档更新后 `git diff --check` 通过。
- 没有观察到可用合成样本证明当前“重编码相似图”门槛误拒合法改图，因此未放宽该门槛。真实商品小样与真实订阅生成没有执行；不代表图像保真、平台合规或真实 Noon 商品可售，`real_noon_verified=false`。下一步继续路线图中的候选图托管、人工审核与提交回读闭环。

### 2026-10-04 托管图片回执原子提交

- 故障窗口：图片已上传并通过公网匿名读取核对后，旧流程先单独保存商品 `image_urls`，再由调用方单独把托管 job 标记为 done。进程在两次写入之间退出会产生商品已更新、托管任务却中断的矛盾状态。
- 修复：`image-host` 执行路径现将商品地址/版本更新和 job 的 done 回执/图片收据置于同一个 SQLite `BEGIN IMMEDIATE` 事务；旧版本、商品、任务状态不匹配时整笔回滚。普通外部图片上传仍可按确定性内容键重试并先验证既有公网对象。
- 新增两项故障注入/事务测试：正常完成时商品公开地址与任务 JSON 回执的 revision、URL 一致；在商品更新后注入异常时更新回滚，商品仍无公开地址且 job 不会误报 done。托管专项11项、自动流程托管7项通过。
- `bash scripts/check.sh` 退出0：394项业务测试通过（46.574秒）；隔离服务、10,000 SKU分页/搜索、Chromium六页导航、提交核对与异常CSV下载均通过。本轮性能读数：初次页面可操作960ms、状态资源301ms、商品首屏84ms、精确SKU搜索20ms；机器负载会影响单次结果。`git diff --check` 通过。本轮命令结果未另存独立日志文件。
- 本轮未连接真实对象存储/CDN或 Noon；匿名读取模拟只证明软件契约，不代表公网上传、平台获取图片、人工审核实际完成或商品可售，`real_noon_verified=false`。

### 2026-10-04 P0 Linux CI 状态回读

- 通过 GitHub Actions 公开 API 读取 run `37128043274`：https://github.com/hyq8216/noonai/actions/runs/37128043274 。该 run 的 head SHA 为 `f2ac7bac4b47a7f603ec9e38d8666a197fa5f58d`（分支 `codex/navigation-compact`），状态 `completed/success`；`backend` 和 `browser` job 均为 `completed/success`。
- 这满足路线图“CI 首次运行”的基线证据。该 SHA 早于当前工作树 HEAD `6351b6dc678baf138…6650 tokens truncated…认响应编码为空字符串且该行 blocked。库存候选不会因此访问 Noon 或更改库存流水、单据、商品修订号。
- 定向库存计划测试6项通过。最终 `bash scripts/check.sh` 退出0：424项测试通过（57.631秒）；隔离服务启动、调度器、写入保护、导入去重、持久化和重启检查通过；10,000 SKU合成数据检查通过（启动就绪913ms、状态资源496ms、商品列表119ms、精确SKU搜索30ms、50项分页通过）；真实 Chromium 八个导航面、库存预览、零售价/过期报价提醒、调度设置、人工提交对账和跨批问题CSV下载通过。性能数据是本机临时合成数据的单次读数，不代表生产承诺。
- 记录边界：没有 Noon 卖家账号或真实仓库编码；此修复仅统一输入校验并阻止误放行，不表示价格/库存同步、Offer 回读、可购买状态或利润已验证；`real_noon_verified=false`。`git diff --check` 通过。

### 2026-10-04 侧栏按业务分组并跟随当前页面

- 现状核对发现侧栏仍平铺18个页面入口，与已确认的“按业务分组、只展开一组、切换页面自动展开所在组”偏好不符。
- 修改为六组：铺货工作（默认展开，批量铺货为第一入口）、商品内容、刊登自动化、订单与仓储、经营分析、系统管理。组标题是带 `aria-expanded` / `aria-controls` 的原生按钮；单次仅显示一个组的页面入口。侧栏跳转及批量处理、自动化中的跨页快捷入口都会自动打开当前页面所在组，窄屏会把当前入口滚动到可见位置。
- Chromium 回归先发现旧 smoke 代码在新菜单下等待隐藏的“自动化中心”；更新为通过组标题展开目标菜单后点击页面。随后扩展为桌面与390px窄屏分别逐一遍历全部18个入口：校验每个目标只出现一次、从折叠状态可达、当前入口可见、任一时刻仅一个组展开且页面无横向溢出。首次窄屏几何断言在“业务档案”上遇到0.13px子像素取整差异；未观察到页面溢出，故将边界断言调整为容忍1 CSS px舍入。最终 `bash scripts/check.sh` 退出0：424项测试通过（63.941秒）；隔离服务启动/调度/写保护/导入去重/持久化重启通过；10,000 SKU检查通过；Chromium库存、报价、调度、提交对账与异常CSV流程通过。JavaScript语法与 `git diff --check` 通过。
- 检查边界：浏览器运行使用隔离临时数据库与合成商品；没有验证真实 noon 账号、真实刊登或店铺运营结果，`real_noon_verified=false`。Impeccable机械检查完成，输出的是项目既有字号/颜色与设计文档的 advisory。

### 2026-10-04 批量刊登混合回执隔离与重放保护

- 覆盖缺口：单 SKU 的超时、重启与回执对账已有专项，但批量内容提交在一批多件商品分别返回成功、超时不确定、有效但需人工处理的 Noon 回执时，缺少批次级状态隔离和同请求重放回归。
- 新增 `UncertainSubmitTests.test_content_submit_batch_isolates_mixed_results_and_replay_never_resends`。使用临时 SQLite 数据和 mock Noon，对三件已审核合成商品逐项注入：有效 `status_id: 0` 回执、请求超时、有效 `status_id: 17` 回执。批次分别保留 `done`、`uncertain`、`needs_attention`；再次提交相同 request key 返回原任务并标记 replay，不再派发执行器，也没有增加 Noon 请求数。完成后重新预览三件商品均被回执保护拦截，并呈现对应处理原因。
- `workbench.tests.test_submit_uncertain` 专项13项通过；`bash scripts/check.sh` 退出0：425项测试通过（57.005秒）。隔离服务启动、调度、写入保护、导入去重、持久化和重启验证通过；10,000 SKU合成目录检查通过（启动就绪1108ms、全量快照0.647秒/约21.0MiB、峰值 Python 分配165.7MB、精确 SKU 搜索18ms、50件分页通过）；真实 Chromium 桌面与390px窄屏遍历18个导航入口，库存预览、零价格/过期报价提醒、调度设置、提交回执对账和跨批问题CSV下载均通过。`git diff --check` 在文档更新后复核。
- 限制：mock Noon 只证明本地编排、状态保存与避免重复发送的行为，不证明 Noon 线上处理、卖家账号权限、商品可售、订单或利润；没有真实账号写入，`real_noon_verified=false`。超时回执仍须人工查平台并完成对账，不能自动重发。

### 2026-10-04 采购至订单结算跨模块回归

- 覆盖缺口：仓储、订单、退货和财务已有分模块测试，但缺少同一商品与仓库从采购收货、订单履约一直走到结算、退款和复核的端到端契约检查，可能漏掉跨模块库存、现金和订单贡献计算不一致。
- 新增 `workbench/tests/test_business_loop.py`：在临时 SQLite 数据中创建合成采购单并收货5件，创建2件 SAR 50 订单，逐步预占、发货、签收；验证库存由5件到占用2件、发货后在库3件。确认采购/订单单据本身不会自动形成财务分录；明确登记 SAR 50 卖家结算和 CNY 7 商品成本并关联订单后，贡献额为人民币分币8,800。按 SAR/CNY 1.85 录入 SAR 20 部分到账并重放同一 request key，只记录一次收款；另登记 SAR 5 退款，再登记1件退货实物入库。财务变更会使旧订单核对失效，重新勾选完整核对后才恢复已核对状态；签收但未登记收入/成本时也不能完成核对。
- 仓储、财务与跨模块定向回归26项通过。最终 `bash scripts/check.sh` 退出0：426项测试通过（54.126秒）；隔离服务启动、调度、写保护、导入去重、持久化和重启检查通过；10,000 SKU合成数据检查通过（启动就绪1120ms、全量快照0.546秒/21,997,861字节、Python 峰值分配165,706,657字节、精确 SKU 搜索20ms、50件分页通过）；真实 Chromium 桌面及390px窄屏18个导航入口、库存预览、报价异常、调度设置、刊登回执对账与跨批问题CSV检查通过。文档写入后另复核 `git diff --check`。
- 限制：该端到端用例只验证本地合成单据与人工确认逻辑；没有从 Noon 拉订单，也没有真实供应商发货、银行到账或退款凭证。贡献额是已登记收入/费用的本地计算，不代表实际净利润、税务结果或店铺可售；`real_noon_verified=false`。真实单据核对和采购付款授权仍未完成。

### 2026-10-04 macOS 应用关闭后的定时备份检查

- 覆盖缺口：定时备份原先由应用内守护线程驱动；Swift 桌面退出时会终止后端子进程，所以应用关闭期间到期备份不会运行，只会等下次打开后补做。
- macOS PyInstaller 安装版启用定时策略时，后端为当前用户写入权限0600的 `~/Library/LaunchAgents/com.noonstudio.backup.plist` 并调用 `launchctl bootstrap`。Agent 在登录会话启动时及每300秒运行已打包的后端 `--backup-schedule-once` 命令；后台 runner 先尝试持有独立 flock 备份锁，再短暂探测 `.server.lock` 判断应用是否正在运行，并立刻释放启动锁。前台调度器与 runner 共用备份锁防止重复执行，所以长备份期间用户仍可启动应用，后台和前台的第二个到期检查会跳过。若探测后应用刚启动，两者仍可同时访问数据库：SQLite 一致性快照支持并发写入，素材复制继续用前后哈希校验，发现变动则整份备份失败并保留旧备份。禁用策略会 bootout 并删除 plist；注册失败时删掉半成品并把策略回退为关闭、界面显示错误。后台只访问原资料库并在本机写备份，不访问模型、Noon、云盘或付款服务。UI明确区分 macOS 已登记、登记失败与需保持应用运行的环境。
- 新增 `workbench/backup_agent.py` 和 `workbench/tests/test_backup_agent.py`：校验 plist 参数不含凭证、启停注册、权限、launchctl 模拟失败清理、失败时策略不假报已启用、服务打开时跳过、独立锁的竞争跳过、到期单次运行、重复检查不重做、相同配置的重复启动不会 bootout 正在运行的 Agent；再以真实本机 Python 子进程调用后端 CLI，并在临时完整资料库中生成经校验备份。恢复/调度专项及新 Agent 专项30项通过。
- 最终 `bash scripts/check.sh` 退出0：433项测试通过（53.055秒）；隔离服务启动/调度/写保护/导入去重/持久化重启通过；10,000 SKU检查通过（启动就绪1174ms、全量快照0.541秒、精确SKU搜索17ms、50件分页通过）；Chromium桌面与390px窄屏全部18个导航、库存、报价、调度、提交对账和跨批问题CSV路径通过。浏览器 smoke 额外断言非 macOS 打包环境回报 `application_only` 并显示需要应用运行；`node --check scripts/browser/smoke.cjs` 与单独 Chromium smoke 均通过。文档写入后 `git diff --check` 复核。
- 验收边界：当前执行环境为 Linux，`launchctl` 通过注入的假 runner 测试，未在 macOS 安装/注销 LaunchAgent，也未关闭正在运行的桌面 App 实测后台任务；原生安装包需在 Mac 上复验。用户须保持 macOS 登录，电脑睡眠期间任务不会执行，唤醒后最多等待下一个5分钟检查；本机备份未加密且不是异地副本，真实云存储和RPO/RTO仍未验证。

### 2026-10-04 Apple Silicon 临时应用包与后端回归

- 使用 `NOON_BUILD_DIST=/tmp/noon-native-qa.6EDTQl/dist desktop/.venv/bin/python desktop/build.py` 构建临时 `Noon Studio.app`，未覆盖常规 `desktop/dist` 发布目录。PyInstaller 和 Swift 客户端构建成功；`codesign --verify --deep --strict` 报告包在磁盘有效并满足其指定要求。
- 首次原生 GUI smoke 渲染正确但把 `backend` 误报为 false，因为断言要求所有页面都具有 `state.ops`，而批量铺货页按设计裁剪该字段。将断言改成检查完整商品状态/token；重新构建后在隔离 `NOON_STUDIO_DATA=/tmp/noon-native-qa.6EDTQl/gui-data-2` 下从 `.app` 启动原生 GUI。页面标题正确、首屏为“批量铺货”、商品数0、18个导航入口均可见、横向溢出false、backend state true，JavaScript smoke 无错误。通过 macOS 正常退出菜单关闭后，App 进程退出码0并记录 `dirty=0`；其打包后端已退出，ready 文件已清除，隔离测试目录仍保留供检查。
- 同一临时安装包中 `desktop/verify_bundle.py` 仍按非 surface-aware 的旧接口契约读取 `/api/state`，从而把合法的 `ops=null` 误报为包故障；按真实业务页面改为请求 `overview`、`warehouse`、`inventory`、`finance`，并对视觉任务和媒体任务/素材使用其专用分页接口。暂停视觉队列后验证请求去重，不派发订阅生图调用。
- `NOON_VERIFY_APP='/tmp/noon-native-qa.6EDTQl/dist/Noon Studio.app' desktop/.venv/bin/python desktop/verify_bundle.py` 通过：不依赖开发 Python PATH 的独立后端运行；采购收货→订单预占/发货、SAR 部分收款与复核、仓间调拨和补货；锁阻止第二服务实例；关闭/重开后状态持久化；媒体任务完成后得到1080×1080、2秒的 MP4 并支持 Range 读取；在编码中途终止后任务标记为 interrupted、临时文件清理；备份恢复、自动写操作暂停、回滚 ZIP 下载与数据恢复。所有数据均来自临时目录与合成素材。
- 最终 `bash scripts/check.sh` 退出0：434项业务测试通过（74.648秒）；隔离服务启动/重启与写保护通过；10,000 SKU合成目录增量、搜索和50件分页通过（启动就绪1220ms、完整快照0.756秒/21,997,861字节、精确 SKU 搜索70ms）；真实 Chromium 桌面和390px窄屏逐一访问18个分组入口，库存预览、报价异常、调度、提交对账和异常 CSV 均通过。`git diff --check` 通过。
- 边界：GUI smoke 使用隔离数据目录，未读取用户默认资料库。记录该次 package QA 时尚未测试 LaunchAgent；后续隔离实测见下一节。无 Noon 店铺连接或平台写入，`real_noon_verified=false`。

### 2026-10-04 macOS launchd 隔离运行备份

- 使用打包后的 `noon-backend` 建立隔离 SQLite 资料库，导入1条合成商品并将 schedule 标记为到期，随后关闭前台服务。调用真实 `sync_launch_agent(root, True, platform='darwin', frozen=True, launch_agents=<临时目录>)`，让应用自身使用的注册代码生成产品原样 plist（`RunAtLoad=true`、`StartInterval=300`），并将唯一 root-hash 标签加载到当前 `gui/501` launchd 域。未写入 `~/Library/LaunchAgents`。
- launchd 的 RunAtLoad 实际启动打包后端 `--backup-schedule-once`；schedule 写回 `success`，归档 ZIP CRC 正常，manifest 的 `counts.products` 为1。保持Agent加载3秒，没有重复备份。随后调用真实 `sync_launch_agent(root, False, ...)` 成功 bootout，暂存 plist 与临时 LaunchAgents 目录均被清除，`launchctl print gui/501/<label>` 确认标签已卸载。运行读数：备份ID `b0e1fc34516f4a39a91cb0a2fc08496a`，归档11,615字节。
- 首轮短周期 harness 错误读取 `manifest.products`；检查归档确认实际字段为 `manifest.counts.products`，修正后用同一打包 runner 完成第二次端到端运行。该次只观察3秒，未验证300秒周期；之后完整等待的结果见下方“300秒周期等待补测”。没有触碰用户默认数据或真实商家服务。
- 随后 `bash scripts/check.sh` 再次退出0：434项测试通过（53.684秒）；隔离服务启动/重启与写保护通过；10,000 SKU检查通过（启动就绪578ms、完整快照0.526秒/21,997,861字节、精确 SKU 搜索17ms、50件分页通过）；Chromium桌面和390px窄屏18个入口及库存预览、报价异常、调度设置、刊登回执对账和跨批问题 CSV 均通过。`git diff --check` 通过。

#### 300秒周期等待补测

- 在 `/tmp/noon-launchd-300s-evidence` 对原样 plist 进行了完整周期等待，临时 LaunchAgents 路径 `/tmp/noon-launchd-300s-agents`；这两个目录不属于用户默认资料或 `~/Library/LaunchAgents`。schedule 的 `next_run_at` 设为加载后180秒：RunAtLoad 于6.1秒时返回 `not_due`，没有备份；launchd 第二次调用在加载后约301秒发生，并在 schedule 到期后运行备份。
- `last_run_at=2026-10-04T10:57:13.671324+00:00`，相对 plist 加载约301.1秒、相对测试启动306.5秒。生成的 ZIP 为11,610字节，CRC正确，manifest含1件商品；launchd 共调用2次。随后禁用注册并确认临时 plist/目录删除、标签不再加载；`~/Library/LaunchAgents` 下未出现 Noon Studio 测试 plist。
- 该次验证补足了真实300秒唤醒与 due gate；它仍是单次临时机器实测，不涵盖睡眠/掉电时 launchd 的延后语义、长期定时可靠性、异地存储或正式RPO/RTO。

### 2026-10-04 批量货源链接候选池分页与浏览器流程

- 复现：对正好5000条候选调用 CSV 导出，接口以“本页返回5000条”推断 `has_more=true`；即使这已是最后一页，界面也错误提示可下载下一份。筛除已进入商品库的候选后，若源数据仍有5000条，旧判断同样会受原始行数影响。
- 修复：`source_leads.export()` 以候选池总数和当前页偏移判断后续来源页是否存在；过滤已入库链接只改变本页导出内容，不会制造虚假的下一份。新增三项专项测试覆盖 5000/5001 边界、入库筛除、URL规范化及状态隔离、并发幂等、旧预览失效。
- 验证：专项3/3通过；真实 Chromium 中粘贴两条链接、预览去重、登记、列表回读、下载 CSV 并核对文件内容通过。`bash scripts/check.sh` 退出0：437项测试通过（51.169秒），隔离服务启动/调度/写保护/导入去重/持久化重启通过，10,000 SKU目录回归通过；Chromium桌面和390px窄屏18个入口、候选池预览/登记/导出均通过。`git diff --check` 写入后另行复核。
- 边界：候选池只接收供应商URL，不抓取1688、不推断商品事实、不创建商品、不自动刊登；本次使用合成链接和临时资料库，无真实1688授权或Noon店铺写入，`real_noon_verified=false`。

### 2026-10-04 批量铺货搜索与目录分批预检浏览器验收

- 浏览器关键路径首次运行时发现，批量铺货搜索框虽标注搜索“SKU”，前端筛选只匹配商品标题、工作台 SKU 和供应商名称；输入供应商规格货号会错误显示无商品。筛选逻辑已加入 `source_sku`，其余筛选、跨页和预检状态重置行为不变。
- 补充 `test_catalog_campaign.py` 两项回归：502件在500件边界分成500+2，正确统计501件可安排/1件阻塞；只为可安排件创建两个分批流程，保存异常行快照，并验证请求重放不重复建流程。商品在预览后发生修改时，确认操作拒绝旧快照，且不写铺货回执或流程。
- 真实 Chromium 在10,000件合成目录中按供应商规格货号 `PERF-09999` 搜索到唯一商品；目录预览识别主模型未配置、可安排数为0，确认按钮禁用。预览前后数据库回读确认 `automation_runs` 与 `model_calls` 数量完全相同，证明页面没有偷建流程或调用模型。另覆盖候选池预览/登记/CSV下载，以及桌面和390px窄屏全部18个导航入口。
- 最新 `bash scripts/check.sh` 退出0：439项测试通过（48.736秒）；隔离服务启动、调度、写保护、导入去重和持久化重启通过；10,000 SKU性能/分页通过（全量快照0.516秒、精确SKU搜索12毫秒、50件分页通过）；真实 Chromium完整检查通过。`node --check` 与写入后的 `git diff --check` 通过。
- 边界：无模型配置时仅验证了预览阻断路径，没有外部模型调用、流程执行或真实店铺提交。数据全为合成样本，`real_noon_verified=false`。

### 2026-10-04 投递箱目录扫描错误隔离

- 复现：当新商品目录 `iterdir()` 返回 `PermissionError` 时，`tick()` 直接退出，供货更新及之后的原图/视频目录都不再检查；原图 SKU 子目录不可读也会使本轮原图扫描中断。
- 修复：新增 `read_children()`，只把对应目录的读取错误记入投递箱错误状态并返回空清单。新商品、供货更新、原图和视频目录分别继续扫描；原图/视频 SKU 子目录单独隔离，不阻断兄弟目录。投递箱根目录的符号链接拒绝规则不变。
- 验证：新增两个合成文件系统故障回归，确认新商品目录不可读时供货库存更新仍能完成，单个原图 SKU 文件夹不可读时其他 SKU 图片仍进入本轮检查；投递箱专项16/16通过。`bash scripts/check.sh` 退出0：441项测试通过（47.238秒）；隔离服务启动/调度/写保护/导入去重/持久化重启通过；10,000 SKU Chromium检查与全部18个桌面/390px入口及铺货预检通过。全量快照0.516秒，精确SKU搜索13毫秒，50件分页通过；本次不变目录查询/增量回读分别约74毫秒。`git diff --check` 写入后另行复核。
- 边界：故障由临时目录上的 `PermissionError` 注入模拟；未更改真实用户资料权限。没有 Noon 写入，`real_noon_verified=false`。

### 2026-10-04 原图批量归集浏览器路径与目录读取时延复核

- 浏览器补测：在临时10,000件合成商品库，通过“图片与视频→批量归集原图”，多选两张400×400合成 PNG。一张以唯一供应商货号 `PERF-09999__main.png` 命名，另一张使用不存在的 `NO-SUCH-SKU.png`。预览分别显示1张可导入、1张需处理；填写合成素材依据后，只导入匹配项。Chromium页内状态显示原图已关联商品；从临时 SQLite 回读确认只创建一个原图素材，商品归属 source SKU 正确且使用依据已保存，未知 SKU 没有匹配或上传。该流程是浏览器事件与服务端/API贯通检查，不验证实际供应商素材权利或视觉质量。
- 全量检查：`bash scripts/check.sh` 退出0；441项业务测试通过（47.782秒），隔离服务启动/调度/写保护/导入去重/重启检查通过，JS语法检查通过，Chromium 18个导航入口、390px布局及铺货预检等既有路径和上述原图导入路径通过。该次10,000件全量快照0.518秒/21,997,861字节，精确SKU搜索12毫秒、分页50件；不变目录回读0.074469秒/5条SQL，单商品增量回读0.074540秒/5条SQL。
- 时延复核：同一完整检查刚结束后独立运行 `node scripts/browser/smoke.cjs`，再次通过原图与既有浏览器流程；全量快照0.515秒，启动就绪454毫秒，状态请求5毫秒，商品列表49毫秒、精确SKU搜索9毫秒。不变目录回读0.000414秒/4条SQL，单商品增量回读0.000410秒/4条SQL。结果与前几次独立运行约0.4毫秒一致，但完整 `check.sh` 中两次约74毫秒的读数仍无法归因；没有据此更改性能实现，也不将其中任一单次读数视为生产性能保证。
- `node --check scripts/browser/smoke.cjs` 通过；验证文档写入后另行检查 `git diff --check`。
- 边界：商品、图片及权利说明均为合成测试值；未连接真实1688、模型或 Noon 店铺，没有平台提交；`real_noon_verified=false`。

### 2026-10-04 原图预览鼠标点击失效修复

- 复现：选择图片并填写素材使用依据后，用真实 Chromium 鼠标点击“预览商品匹配”没有发出 `/api/media/import-preview` 请求。事件记录只有 `pointerdown` 与 `mousedown`，随后没有 `pointerup`、`mouseup` 或 `click`。原因是使用依据 textarea 的 `change` handler 在失焦时调用 `render()`，在鼠标释放前替换掉被点击的按钮。此前 `button.click()` 的程序化浏览器测试绕过了此问题，未充分覆盖真实鼠标交互。
- 修复：移除该 textarea 上多余的 `change → render`。输入时既有 `input` handler 已负责保存本地依据状态、更新进度提示和启用/禁用导入按钮，整页重绘并非必要。
- 验证：回归浏览器检查改用 Chromium 鼠标 `click()`，等待并确认 `/api/media/import-preview` 返回200；随后验证10,000件合成目录中唯一供应商货号匹配、未知 SKU 隔离、素材依据保存、仅有效图导入以及 SQLite 商品归属回读。`bash scripts/check.sh` 退出0，441项业务测试通过（45.952秒）；隔离服务重启检查、所有 JS 语法检查、18个导航入口及390px布局和浏览器流程通过。性能样本为全量快照0.509秒/21,997,861字节、峰值分配165,706,612字节，启动就绪450毫秒，状态请求5毫秒，商品列表75毫秒，精确SKU搜索12毫秒；不变目录回读0.000449秒/4条SQL，单商品增量0.000409秒/4条SQL。`git diff --check` 写入后通过。
- 性能边界：前两次完整检查及一次独立浏览器检查的不变回读约74毫秒，而本次完整检查与此前独立检查分别约0.45毫秒和0.4毫秒；当前证据说明该指标存在运行上下文波动，但不足以断定成因或承诺生产性能。
- 经营边界：素材及授权说明是合成夹具，没有真实供应商权利核验、模型出图、1688抓取或 Noon 店铺提交；`real_noon_verified=false`。

### 2026-10-04 大目录供货有效期刷新跨桶扫描优化

- 根因：10,000件性能测量的SQL trace对上时间桶差异。令牌仍在同一15秒桶时返回4条SQL、约0.4毫秒；跨桶时多出一次 `SELECT id,supply_checked_at FROM products`，逐行用Python核验，导致约74毫秒读数。该扫描用于把刚超过24小时的供货核验标为过期，也用于把稍微超前未来（最多5分钟）的时间校验为有效。
- 改动：为 `julianday(json_extract(data,'$.supply_checked_at'))` 添加部分表达式索引。跨桶时，仅按“过期边界”和“未来时间有效边界”前后各1秒查询候选值，再使用原 Python 校验函数确认状态是否真的变化；保留相同的有效期和容差规则。多取1秒是为涵盖 SQLite 日期精度舍入，精确边界仍由原函数判定。
- 回归：`test_catalog_delta.py` 的假时钟测试验证刚过期、未来时间刚恢复有效、早已过期不产生变化，以及恰好在刷新起点/终点、离边界500微秒、已位于边界稳定一侧的记录。多个时间使用 `+04:00`、`+05:00`、`-07:00`、`-05:00` 表示，仍须与 Python 权威有效期函数的变更集合完全相等；`EXPLAIN QUERY PLAN` 对 `UNION ALL` 两个边界分支均须命中新索引。完善后专项仍为8/8通过。
- 性能验收：扩展 Chromium 10,000 SKU fixture，在测量时显式构造前一个15秒桶的令牌，强制执行时间边界刷新分支并断言没有商品变化。完整 `bash scripts/check.sh` 退出0：442项测试通过（49.285秒），隔离服务与重启检查通过，Chromium全部18个导航目标、390px布局及铺货/原图等路径通过。测得全量快照0.546秒/21,997,861字节，峰值Python分配165,706,657字节；普通不变回读0.000455秒/4条SQL，强制跨桶回读0.000366秒/5条SQL，单商品增量0.000519秒/4条SQL；启动就绪1,100毫秒，商品列表80毫秒，精确SKU搜索12毫秒，50件分页通过。10,000件导入用时0.473秒；尚无改动前后同环境写入对照，索引对大规模持续写入的额外成本未单独量化。
- `git diff --check`、`node --check scripts/browser/smoke.cjs` 与 `node --check workbench/static/media_import.js` 通过。
- 边界：数据均为合成商品；未访问默认资料库、真实供应商、模型或 Noon 店铺，没有外部提交；`real_noon_verified=false`。Linux完整检查不代表Swift/macOS打包已复验。

### 2026-10-04 大目录供货有效期变更超过500件分页

- 压测复现：把501件合成商品的 `supply_checked_at` 放在24小时有效期边界附近，并让固定假时钟一次跨越边界。索引已避免全表读取，但实现仍把超过500个时间触发变化的结果当作无法增量表达，退回完整 `products` 快照；这会使大目录的定时刷新绕过既有500件增量分页上限。
- 修复：时间边界刷新现在在稳定的目标时间上保存续页游标，按索引排序键 `(julianday(supply_checked_at), id)` 分页，过期与未来有效两个边界阶段分别续读。事件增量仍优先分页；续页令牌保留固定目标时间和阶段/行游标，直到候选全部扫描后才前进15秒桶。新增 501 件回归断言两页为500+1、没有完整目录字段、没有遗漏或重复，并断言最后一页清除 `catalog_has_more`。将查询计划断言收紧为精确索引名。
- 验证：`workbench.tests.test_catalog_delta` 专项9/9通过。最终 `bash scripts/check.sh` 退出0：443项测试通过（52.238秒）；隔离服务启动、调度器、写保护、导入去重、持久化与重启检查通过；10,000件合成目录完整快照0.806秒/21,997,861字节、Python 峰值分配165,706,657字节、普通不变回读0.000656秒/4条SQL、跨桶边界回读0.000543秒/6条SQL、单件增量0.000589秒/4条SQL，启动就绪1,096毫秒、商品列表62毫秒、精确SKU搜索14毫秒及50件分页通过。真实 Chromium 桌面与390px窄屏全部18个分组入口、无横向溢出、铺货预检只读/无模型调用、库存/报价/调度、原图匹配与权利记录、提交回执对账、问题CSV及货源候选池流程均通过。数据为临时合成数据；上述性能是单次本机测量，不构成生产承诺。
- 边界：测试只证明本地SQLite游标、前端已有增量契约和合成目录行为；没有真实供应商抓取、模型出图、Noon 店铺写入/Offer 回读、可购买状态、订单或利润验证，`real_noon_verified=false`。本次不验证 Swift/macOS 安装包。

### 2026-10-04 远端 Linux CI 与当前工作树版本核对

- 复核 GitHub PR #2 最新运行 `37104381778`（head `60055827eeac6e31c16e1bd0461c1531cf084741`，CI merge commit `a07bc3e5b9764431a39967226030ae737466fed0`）。官方 GitHub job 摘要及完整 backend 日志显示：Ubuntu 24.04 / Python 3.12.14 / Node 22.23.3；setup、pip check、doctor 和隔离启动/重启成功，真实 Chromium job成功；全量仅运行373项，其中24 failures、16 errors，workflow为失败。失败日志集中在旧版尚未完成的批量确认、暂停/重试、图片协议/输出校验、视觉工作流与大目录增量等契约，不能按发布通过处理。
- 与本地核对：当前工作树的后续实现已扩展至443项测试，最近 `bash scripts/check.sh` 在 macOS 全量通过，真实 Chromium 桌面/窄屏流程通过；当前分支远端 PR #2 仍停在上述 600558 提交，工作树测试/实现修改未包含在该 CI commit。由于本机为 Apple Silicon macOS 且没有 Docker/Podman/Linux VM，本轮不能把 macOS 结果冒充 Linux 执行；GitHub PR 的绿色 Chromium job也不能替代失败的 backend job。
- 逐项映射 CI 的40个失败/报错：当前工作树中38个同名用例重跑通过；另2个目录增量测试因新契约改名，旧“超500全量快照”改为增量分页、旧游标名称换成当前名称。对应新用例 `test_large_change_set_pages_without_full_snapshot` 和 `test_expired_bucket_refreshes_token_and_invalid_cursor_gives_full_snapshot` 定向重跑2/2通过。此映射能解释当前代码对原失败的覆盖，但Mac重跑仍不等价于Linux CI。
- 结论与后续门槛：P0 Linux/CI 尚未在当前443测试工作树上验收。应先把经审查的当前源码变更整理到 PR 分支或新的 review 分支，然后检查新的 Ubuntu backend 全量日志与 Chromium job；在此之前仍按 Linux CI未通过记录，不重跑或重标记该旧提交为当前代码验证。
