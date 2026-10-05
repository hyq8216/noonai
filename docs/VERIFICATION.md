# 云端迁移验证记录

日期：2026-10-03（Asia/Shanghai）。

## 六模块并行深化与集成（2026-10-03）

- 用户要求交给其他子任务继续深化，六个 GPT-6.1 Sol medium 子任务分别负责国内采集、规格组、供应商报价、补货、分析、异常；主任务统一接口与回归，集成分支 `codex/erp-deepening-0.44.0`，详情见[深化交付](ERP_DEEPENING.md)。沿用21模块/40页面，不调用真实卖家、银行或模型，不生成外部采购/付款/刊登。
- 接入只读规格草稿诊断、规格质检CSV、异常过滤及CSV；新增HTTP检查核对诊断不写商品事实、保存后真实附件、403令牌保护及恢复409、非法筛选400。13项HTTP端到端通过。
- 第一次HTTP集成发现分析返回字典的键误写为tuple，JSON序列化使接口断连；已纠正为字符串并复验13项HTTP通过，原错误日志 `/tmp/noonai-deepening-http.log` 与修复后 `/tmp/noonai-deepening-http-fixed.log` 均保留。没有降低检查。
- 首轮 `bash scripts/check.sh` 返回0：816业务测试、21桌面编排测试、启动/重启/守护/防重、40页1440/390px、全部26个Chromium脚本通过；子任务最后增加一个报表空状态回归后，冻结源码全量817项测试115.742秒通过。日志 `/tmp/noonai-deepening-full-check.log` 与 `/tmp/noonai-deepening-stable-backend.log`。随后因容量证据追加SQL筛选优化，最终复测结果见下方。
- **最终 `bash scripts/check.sh` 返回0**：819业务测试107.222秒、21桌面编排测试0.856秒全部通过；真实服务启动/重启、全部JS语法、40页面1440/390px检查与26个真实临时Chromium业务脚本全部通过，无跳过或总检自动重试。最终日志 `/tmp/noonai-deepening-final-check.log`。新增单测共43项（六模块41项+HTTP2项），原776项保持通过；此为Linux软件验证，不冒充新源码macOS原生或真实账户验收。
- [容量优化前](benchmarks/supply-deepening-baseline-10000.json)实际10000SKU/10000库存/100采购、100SKU×10供应商报价、61汇率版、5条人工补货规则。新筛选逐SKU查询三次最佳15708.651ms，成为可复现优化依据；默认页135.196ms。
- [容量优化后](benchmarks/supply-deepening-optimized-10000.json)同样规模，按仓/SKU聚合供给与未占用需求、SQL精确计数/分页，只为返回页构建来源明细。所需补货筛选5件三次最佳177.952ms（195.336/177.952/183.579），普通50件124.051ms；未知筛选9995件越界最后45件、库存和采购不变。新增SQL分类与详细计算口径交叉及分页构建次数2项测试；补货15项、仓储12、采购14共41项相关回归与两条浏览器流程通过。
- 1000报价摘要13924字节；明确展开的完整履约/采购草稿详情1479724字节、生成18.613ms，不是小详情或免费全量加载证明。整个容量脚本3.208秒，外部连接器/网络调用均0；未测tracemalloc/多人SLA，导出仍完整扫描。新源码原生窗口、目标Mac与真实平台验收未执行；已有0.43.0安装包证据单独保留。

## macOS DMG实际交付（0.43.0）

- **实际macOS构建通过** https://github.com/hyq8216/noonai/actions/runs/37114268786 ，源码提交 `04556d91a37c0175a714b5a0cf35ca50b00762b2`，树 `b05d557dbf4a63bf60e802c0b943b33986ece9f8` 与本地一致。macOS 26.6.2/arm64，21项桌面测试36.216秒全部通过；Swift编译、架构、临时签名及冻结后端实际启动、21模块接口、扩展ZIP、库存占用/发货守恒、FFmpeg成片通过。
- DMG生成、hdiutil verify、只读挂载后的应用版本/所有Mach-O架构/签名及拖动安装捷径/指南核对通过；生成DMG、备用ZIP及各自SHA256。Artifact `11270788090`，125221040字节，archive SHA256 `01e2904671ef17f2298820038573d1cfbc92c1f31ee3385718a0ed5dce9972e3`。此为产物ZIP摘要，不冒充DMG自身摘要。
- DMG已下载到本地：`/tmp/noonai-release-0.43.0/Noon-Studio-0.43.0-macOS-arm64.dmg`，70816720字节，SHA256 `03eb8ed92891e1a50490cf1cb02db0a95c70fc551646837f26c412bbfdd103c0`。传输运行 https://github.com/hyq8216/noonai/actions/runs/37114577174 成功；3个分段ZIP各自摘要、分段长度/摘要、完整DMG长度/摘要及UDIF尾部标识全部核对通过；与原Mac构建清单一致。二进制没有提交到Git。
- 运行时代码的GitHub Linux验收 https://github.com/hyq8216/noonai/actions/runs/37114270586 ：backend与browser均success。
- 未做Apple Developer ID签名/公证，未原生窗口逐页点击，未验证Mac12或Intel运行，也未验证真实Noon账号。
- 首次macOS原生运行 https://github.com/hyq8216/noonai/actions/runs/37114062386 ：20项桌面测试通过、arm64 PyInstaller和Swift编译完成；lipo输入路径位于可变架构列表后，工具将路径误当架构而失败。已修正输入路径置前，并追加独立命令参数回归；随后运行37114268786通过。
- 本地环境Linux/x86_64，没有Swift/Xcode/hdiutil。终端GitHub API请求Forbidden；GitHub连接器仓库访问成功，仓库public且具有push权限，改用连接器同步独立构建分支，不合并main、不发布Release。
- 新增一键Mac构建入口、0.43.0/43版本元数据、原生arm64/x86_64检查、临时构建与失败保留旧程序、冻结运行时隔离检查、DMG/ZIP/SHA256打包及只读挂载检查；包含当前21模块、40页和国内采集扩展五文件。
- `desktop/tests` 20项通过（0.845秒）：14项模拟打包编排及6项构建前提/真实源码协议检查。源码包装器实际临时建单、占用/发货数量守恒、下载扩展和FFmpeg视频导出通过；不是macOS冻结程序或WKWebView验收。原有历史 verify_bundle.py 保留。
- **`bash scripts/check.sh` 返回0**：业务776 tests，103.463秒，全部通过；桌面20项编排测试通过；40页桌面/窄屏与20个本地Chromium脚本均通过。实际macOS构建证据见本节。日志 `/tmp/noonai-dmg-full-check.log`。

## 时间预算内追加：供应链与汇率集成

- 新增供应商询价比较、汇率档案、库存补货建议，累计21模块、40导航页；三个追加子任务均为 GPT-6.1 Sol medium。供应商需求绑定已登记SKU与供应商，汇率保留不可变历史，补货只读取现有库存与采购。不发送询价、下单、付款或修改已有财务。
- 追加28项单测：询价10、汇率10、补货8；包括5000报价矩阵、历史分页、Decimal方向/舍入、未知字段、库存往返、采购未收不重复、原子审计与幂等。**`bash scripts/check.sh` 返回0：776 tests，100.418秒，全部通过**；日志 `/tmp/noonai-supply-final-check.log`。独立启动/重启、全部JS语法、40页桌面/390px和20个真实本地Chromium脚本全部通过；未跳过测试或在总检自动重试。
- 真实HTTP跨模块11项通过，日志 `/tmp/noonai-extension-http.log`；旧ZIP缺20模块结构的准确迁移与现存回执保存2项通过，日志 `/tmp/noonai-additional-recovery.log`。恢复全局拒写覆盖追加确认与换算接口。
- 三个追加Chromium脚本各自通过。补货预览来源指纹在390px产生溢出，首次inline修正受CSP阻止；现使用既有 `prewrap` 类，预检和保存后窄屏均通过，没有放宽CSP。原始来源指纹完整保留。
- [供应链容量报告](benchmarks/supply-10000.json)：真实服务建立10000SKU/10000库存/100采购、100SKU×10供应商的1000未知报价草稿、61版人工汇率、5条明确补货规则。库存不变、采购未自动增加；61版按50+11返回、报价默认摘要且详情显式GET，测试无外部连接。
- 本地三次最佳：补货50/10000项120.818ms、78055字节；1000报价列表摘要92.404ms、13924字节；详情11.850ms、468400字节；汇率50/61版0.670ms、29520字节。此次未启用tracemalloc，不能直接比较上一容量内存/时延或宣称多人服务SLA；详情仍可较大，业务源扫描依赖单据规模。

## 前轮：国内采集与18模块ERP集成验收

- 集成分支 `codex/erp-delivery`；六个 GPT-6.1 Sol medium 子任务三轮并行，新增18模块，共37个导航页面。范围见[交付说明](ERP_DELIVERY.md)。用户确认暂无账号，先完成软件能力；主渠道为1688、淘宝、拼多多。
- **`bash scripts/check.sh` 返回0：748 tests，103.276秒，全部通过**，没有跳过测试。完整日志 `/tmp/noonai-release-check.log`。隔离启动/重启、令牌拒写、导入防重、全部工作台及扩展JS语法通过。
- 17个真实本地 Chromium 脚本通过；37页在1440/390px检查，无文档横向溢出或未捕获JS错误。覆盖订单、结算、SPU、备份、银行核销、占库/发货、退货隔离/释放/退款、采购、批量编辑、异常、盘点、包装交接、价格预检、广告、字段模板和国内采集。全部使用临时合成资料库，不调用卖家、银行或模型账户。
- 国内扩展通过实际MV3安装、popup/tab/scripting执行和downloads完成回执，并读回真实下载JSON；仅平台页面DOM由本地合成夹具替代。验证三个主渠道、真实零值与未知值区分、敏感字段排除、当前商品绑定、缺规格阻断、人工校对留证、候选及商品两次确认。不能据此声称线上页面、登录权限或官方接口兼容。
- 跨模块HTTP验证共享账务与库存守恒、请求防重、版本失效、待恢复全局拒写。旧ZIP缺17模块的真实schema迁移通过；现存ZIP保留付款回执与SPU且不含凭证，恢复暂停周期规则。冻结资源目录的扩展ZIP下载通过模拟打包目录验证；这是资源路径检查，不是macOS构建验收。
- 上一次总检 `/tmp/noonai-delivery-final-check.log` 在国内采集浏览器流程出现一次 `0 !== null`，旧包装丢失断言原位置，原因尚未确定。已保留原始错误堆栈并加入断言上下文；独立多次复验和本次完整总检通过，未弱化断言、跳过或在总检中自动重试。不能把诊断改进称为已定位并修复该失败。
- [容量报告](benchmarks/erp-10000.json)：实际10000SKU、1000订单、200采购、300凭证、10000库存、25导入回执；实际占库/发货100订单、两份各500SKU价格草稿、一份100包裹清单、1000广告报告。完整脚本67.186秒，外部连接器及网络连接均0。
- 目录首500件1,145,194字节，生成41.002ms、编码59.721ms；20页+增量收尾共21请求、2369.334ms（含编码）、22,903,504字节。旧全量22,900,083字节；总数据及浏览器最终商品缓存没有减少。
- 盘点列表50/10000项约100.814ms；两份500SKU价格方案列表42,765字节，展开单份才读取500行；100包裹清单列表20,644字节，展开才读明细；广告50/1000项约49.112ms，遍历20页覆盖全部1000行。详情与列表各自实测，不以分页宣称数据库扫描恒定。
- 分页建库/遍历阶段Python峰值21,843,028字节；全过程Python峰值181,221,436字节、RSS303,736KiB，包含建库和序列化。时延取本地三次最佳值，tracemalloc有额外开销，不是服务等级承诺。财务仍读取全部凭证和订单贡献，不是百万SKU、多用户云端或真实平台负载证明。
- 当前Linux没有外部凭证或出站账号身份，网络限依赖/浏览器下载。真实Noon/国内平台、生成素材质量、远端CI、团队部署与macOS发行尚未验收；完整商用ERP尚未完成。

以下为历史验收记录。

## 前轮：多渠道采集集成验收

- 用户授权核心任务并行拆分；五个子任务均为 GPT-6.1 Sol、medium：渠道账号、
  只读适配器、候选与导入、中文界面、备份恢复。集成分支 `codex/multichannel-platform`。
- 实现12类渠道目录与独立账号配置；Shopify Admin GraphQL、eBay Browse、授权
  JSON GET 三类只读适配器。1688等八个平台显示待接入，保留文件导入路径。
- 保留来源与变体身份、原币价格、零/未知库存、原始与最新快照。采集不自动创建商品，
  明确预检与确认后最多500件入库；持久回执防重复。来源矛盾隔离、快照核对有版本和
  依据审计；重新采集及核对不覆盖已有商品。商品编辑保留入库溯源。
- 凭证不回显、不进入SQLite或备份；文件绑定账号/平台/完整地址，更改地址必须
  换令牌或明确清除；异常绑定阻止取凭证。恢复停用账号并暂停采集，等待在途读取结束。
  老备份按实际模块定义补缺失表/索引；改变已有表定义仍拒绝。
- 新增55项Python回归：账号13、只读适配器12、真实HTTP6、恢复5、候选/入库19。
  覆盖并发版本、凭证/端点隔离、DNS/分页地址限制、零库存、原币与成本区分、
  续采/取消/重启、预览失效、跨账号冲突、导入防重、核对审计、已有商品不变、
  在途取消仍阻止恢复、投递失败不滞留排队，以及旧备份与凭证排除。
- **`bash scripts/check.sh` 返回0：440 tests，69.811秒，全部通过**；没有跳过测试。
  完整日志 `/tmp/noonai-multichannel-check.log`，使用独立临时资料库，没有读取经营资料。
- 实际服务器启动、调度器、无令牌拒写、导入去重、关闭重开持久化通过；全部静态JS
  语法通过；Chromium覆盖19个页面，1440px与390px文档横向溢出和未捕获异常为0。
- 新增真实Chromium合成流程：账号保存/留空编辑/清除令牌、只读采集→候选→预检→
  明确确认入库、重复请求回执、零库存/非CNY成本留空/溯源、来源变更冲突→
  快照核对与确认、已有商品资料不变；展开冲突详情的390px布局通过。
  夹具仅替代外部采集返回，其余为实际HTTP服务和页面操作，无真实平台请求。
  `scripts/check.sh` 和 GitHub browser job 已纳入该流程；新的远端CI尚未回读。
- Shopify官方文档工具因重新认证不可用，公开文档请求HTTP403；当前GraphQL
  请求只通过合成响应协议验收，没有当前线上schema或真实店铺权限验收。
  未调用真实渠道/Noon账号、付费模型，未部署或重新构建macOS。

说明见 `docs/MULTICHANNEL.md`。本轮完成多渠道采集的本地闭环，完整ERP目标仍为
active；下一步按可取得的官方授权资料实现其余渠道并验证实际读取回执，继续订单、
供应链、财务与团队能力。以下为历史验收记录，旧测试数与旧失败状态不代表当前结果。

## 前一轮：云端 P0 回归修复

- 环境：当前 `/workspace/noonai` 云端，Python 3.12.14，Pillow 12.3.0、
  cryptography 47.0.0、imageio-ffmpeg 0.6.0、boto3 1.43.107；
  FFmpeg 7.0.2；阿文字体绘制诊断 `arabic_layout=true`。
- 独立复现：373 tests，24 failures、16 errors，65.131秒。
  日志 `/tmp/noonai-baseline.log`，所有测试采用临时资料库，没有读取经营资料。
- 实现修复：自动化运行、预约、轮转、状态扫描与等待使用统一可注入的时钟；
  首次状态扫描不受单调时钟起点影响；视觉队列容量等待、托管/视觉步骤时间记录
  沿用同一时钟；明确人工重试清除原审批轮询时间，额度和版本仍在发送前核对。
- 旧契约调整：未通过货源预检的商品不能直接建批；测试改为先合法建批、再模拟
  并发事实变更以验证逐件隔离。提交测试显式模拟已配置店铺；托管测试在货源身份
  变更后重新确认成图；不改生产门禁、不跳过测试、不触发真实平台或付费调用。
- 图片协议使用与原图不同、白角背景的合成输出；分别验证原图字节回传、JPEG
  重编码近似回传和非白底主图被拒绝，且正常场景图继续。保留成图和发送回执，
  服务结果不明仍暂停、不自动重发。不同镜头的流程夹具也使用不同输出。
- 视频测试新增“完成编码后仍停在播放验收”，逐项验收后才完成流程；已有图片
  对照产生差异时保留人工再次验收节点，不自动加入商品。
- 分页验收覆盖250件批次的全部5页，以及超过500个增量事件时继续翻页、翻页期间
  新增修改不丢失；15秒时间桶变化不强制整库下载，超过24小时游标才回退快照。
- 新增12项测试：8项独立调度时间/恢复/轮转/重试测试，3项图片输出门禁测试，
  1项跨时间桶紧凑刷新测试。工作流专项23项通过、图片协议专项29项通过。
- 最终 `bash scripts/check.sh` 返回0：**385 tests，63.919秒，全部通过**。
  真实服务器启动、调度器、无令牌拒写、导入去重、关闭重开持久化通过；
  全部静态JS语法检查通过；Chromium覆盖18个导航页面，1440px及390px、900px高度，
  文档横向溢出与未捕获JS异常均为0。完整日志 `/tmp/noonai-check.log`。
- 浏览器初次缺少安装二进制；经受限网络允许的Playwright下载域安装Chromium后完成
  最终验收。该安装只属于开发环境，不代表macOS软件重新打包。
- 分支：`codex/platform-reliability`。上述为当前云端执行证据；尚未回读新的GitHub CI，
  未做真实Noon店铺、1688授权、真实商品模型调用、正式部署或macOS原生验收。

本轮 P0 回归阻塞已消除；完整平台目标仍为 active。下一轮优先处理可观测的
调度/外部不确定回执恢复及大批导入容量，按路线图推进交易与供应链、财务和团队功能。

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


## 2026-10-03：云端六模块集成与紧凑导航 0.44.0

本轮基于用户提供的云端分支`codex/erp-deepening-0.44.0`，fetch并读取基线
`d17afcfdbc9071566097ba675a22b43b2050cb60`的PRODUCT、路线图、深化资料及相关源码。
在独立工作树`codex/navigation-compact`实现，不覆盖原目录未提交记录。
本轮用户将侧栏归类简化列为优先；明确选择“只展开当前组，切换时自动展开”。

### 改动与复现

原导航40入口全部展开。现为运营总览及六组单组展开；采集/素材、采购/库存、
财务对账与资料备份按二/三级组织。默认批量铺货页只显示5个页面入口，六组仍可发现。
搜索展示匹配层级，Escape清空，单结果Enter进入；成功跳转展开父组，取消离开保留
当前表单。折叠只更新导航；数据刷新保留折叠与滚动。390px默认侧栏高68px，菜单按需打开。
详细层级与交互见[NAVIGATION.md](NAVIGATION.md)。

初轮新navigation.js未加入HTTP静态白名单，真实浏览器发现404和启动错误；已补入，
专项浏览器与实际冻结后端检查均要求该资源返回成功。原采集回归在DB完成、界面回执
尚未刷新时断言按钮禁用，复现失败；保留原有禁用、确认与幂等回放断言，等待busy结束
及channelImportResult后再断言。独立复测及最终全量通过。
原导航脚本使用真实按钮展开父菜单；窄屏菜单收起时route是attached，实际目标页面标题
仍需可见。没有通过隐藏控件、直接调用navigate或删除断言绕过新UI。

### 最终验证

- `bash scripts/check.sh`最终返回0：819业务单测、21桌面编排单测全部通过；
  隔离启动/调度/写保护/去重/持久化/重启通过；27个真实Chromium脚本通过，含40页
  在1440px及390px的逐页点击；无文档横向溢出或未捕获JS错误。
- 新专项`node scripts/browser/navigation.cjs`通过：全部40路由、单组展开、三级父路径、
  搜索与空态、Escape/Enter、数据刷新折叠保持、1440/900/390px、菜单开关、
  未保存表单在折叠及取消离开时保留、确认放弃后没有写入草稿。
- Impeccable机械检测一次：28条advisory、0条非advisory；存量辅助字号与配色提示保留，
  新导航颜色和层级在DESIGN记录。桌面/窄屏截图实际查看，没有更换原视觉世界。
- 真正macOS arm64构建成功：lipo架构、ad-hoc签名及codesign结构检查通过；
  冻结后端21模块、MV3扩展、库存守恒、内置FFmpeg和navigation.js读取通过。
- 原生WKWebView在新的临时`--data`工作空间启动，读到标题“批量铺货”，商品0条，
  页面总导航按钮43（侧栏40及正文3），overflow=false。首次测试驱动误认为输出不带
  后缀，已读取Swift实际生成的`.json`及内层ui JSON完成核对；没有改写业务结果。
- DMG真实创建、只读挂载、版本/架构/签名、Applications快捷方式、安装说明一致性
  检查及卸载通过；DMG和ZIP生成的SHA256在下载目录再次核对一致。

完整日志与截图保存在忽略目录`output/navigation-0.44.0/`及`output/playwright/navigation-*`。
测试均使用临时数据目录；未打开workbench/data或默认真实业务目录，未调用卖家、模型、
银行、供应商消息或真实采购。`real_noon_verified=false`，尚未做Apple公证。
当前机器与版本的原生启动证据不代表所有目标macOS版本已验收。

### 本机安装包

- `/Users/mac/Downloads/Noon-Studio-0.44.0/Noon-Studio-0.44.0-macOS-arm64.dmg`
  71676745 bytes；SHA256 `9a2994f9b17d700e3a3d661f3424e7b7dc7f362a3b80f6750de525ecb6448862`。
- 同目录ZIP：61306488 bytes；SHA256
  `f15f8841773edeab16e7a65077def454d300dc0e0b157aabf369acc911583a84`。
- 原生.app在`desktop/dist-navigation-0.44.0/Noon Studio.app`；安装说明
  [INSTALL-0.44.0.md](../desktop/INSTALL-0.44.0.md)。本机安装包、环境和数据不提交到公共仓库。

下一步仍沿用全平台路线；本轮导航与分发不是完整真实经营闭环验收。

## MiniMax订阅配置增补（2026-10-03）

用户要求可填写接口、API Key与选择模型的MiniMax订阅菜单。原有MiniMax仅按API费率估算，缺少独立订阅配置和模型菜单；新增`minimax-subscription`，保留原Codex与API配置和历史。

- 系统设置→模型服务→新增模型服务：选择MiniMax Coding Plan / M Plan订阅，预填中国官方OpenAI兼容Base URL，可改为国际官方地址。提供M3、M3.1 Flash Preview、M2.7与自定义模型编号。
- 订阅专用Key按sk-cp-前缀校验，普通按量Key、非官方地址及Anthropic路径在保存前拒绝。Key独立保存为0600文件，不回显；留空保留原Key。订阅与其他授权通道转换需新建配置。
- 保存不调用模型；订阅不要求美元费率或每日美元预算，账本标注MiniMax订阅，记录返回token用量，实际扣减仍需平台回读。每天/每分钟调用上限继续执行。
- 默认M3；文本资料保留120KB上限，额外以UTF-8长度和输出上限保守控制在512000以内。这里控制的是发送资料上限，不是新增服务端512K参数或特定模型别名；未验证真实套餐折扣。
- 超时或解析异常保留待核对记录，同一请求不重发；版本、幂等、人工批准与noon写入门禁保留。

验证：`.venv/bin/python -m unittest discover -s workbench/tests -p 'test_automation*.py'`36项通过；`bash scripts/check.sh`退出0，822项业务单测、21项桌面单测和28个Chromium流程全部通过。新增浏览器流程覆盖保存、模型选择/自定义、编辑留空Key、密钥不回显、保存无外部调用、390px与Codex字段切换。已刷新当前64904预览并显示订阅菜单，未填写真实Key或发起付费调用。

Mac arm64真实构建、内置运行环境的订阅配置保存/回读、21模块与库存/FFmpeg检查通过；DMG签名结构、只读挂载和安装指引核验通过。增补沿用0.44.0版本号，交付目录`~/Downloads/Noon-Studio-0.44.0-MiniMax/`，不覆盖前一份安装包。DMG SHA256：`519b53891b87ba812ac6a8a4d42efb6b673db596c6fc6f4bf68aa80d51f26662`。本轮未验原生窗口操作、Intel、Apple公证、真实MiniMax用量或真实noon经营。

本地证据（忽略，不提交）：`output/minimax-subscription/`日志与`output/playwright/minimax-subscription.png`。官方协议依据：https://platform.minimax.cn/docs/m-plan/other-tools；模型权限和套餐扣减以账户实际状态为准。

## 批量流程调度与恢复优化（2026-10-03）

本轮按路线图推进“调度器可控时间、事件唤醒与到期扫描”。旧逻辑在最后商品完成后仍可能保留running状态直到五秒汇总窗口；使用原汇总条件运行新增回归复现`running != done`，新逻辑在有处理项的轮次及时汇总。

- 调度线程改为事件等待：空闲30秒回扫，有待执行项按预约/重试时间等待，最长5秒回扫；不是30秒运行一次。新建、恢复、重试与成功的模型配置/自动化HTTP操作唤醒线程。
- 单件和整批人工审核成功唤醒approval步骤，清除其本地五秒检查延迟；额度等待期限保留。process仍核对商品版本、人工审核、资料和提交配置，唤醒不等于批准或自动重发。
- 事件在扫描前清理，扫描期间到达的唤醒不会丢失；关闭时唤醒等待线程。processing恢复仍变成attention，不重发未知在途请求。
- 商品卡片显示下次检查时间及暂停说明；实际运行轮次仍限100件，避免等待商品阻塞新商品。

验证：42项定向自动化回归通过；新增6项覆盖空闲/预约期限、审核不解除额度等待、完成状态及时回读、关闭线程、真实线程从30秒空闲被新任务唤醒、扫描中收到事件不丢失。第一次新测试使用错误的步骤索引，修正为本计划的approval=2/finish=3，保留全部状态和不提前调用断言。

`bash scripts/check.sh`退出0：826业务单测、21桌面单测、28Chromium流程通过；在最后两项线程回归加入后，重新运行全业务单测828项全部通过。另新增`node scripts/browser/scheduler.cjs`通过真实HTTP/Chromium验证预约批次重复请求、暂停/恢复保留未来时间、移动端取消，以及无模型调用；已加入今后的全量检查，总浏览器脚本29个。

Mac arm64真实构建、内置后端21模块/MiniMax配置/库存/FFmpeg检查及DMG只读挂载通过；交付目录`~/Downloads/Noon-Studio-0.44.0-Scheduler/`，沿用0.44.0版本号，不覆盖前一增补包。DMG SHA256：`62b0962f77888923220556dd3a2cd32d89729bebb827dc04e0454ee3f644bda6`。64904临时预览已重启为本轮后端，浏览器加载新前端需刷新；未打开经营数据。

范围：本轮是调度优化，不是完整平台验收；应用关闭/休眠时不运行。未验证真实MiniMax额度、真实卖家写入、原生窗口操作、Intel或Apple公证。忽略的本地日志保存在`output/scheduler-increment/`。

## 跨版本提交回执防重（2026-10-03）

按P1“外部调用幂等与不确定回执对账”复测发现缺口：单件提交队列和批量预检只按
商品当前revision查找`uncertain`、`needs_attention`、`interrupted`任务。若提交回执未决后编辑
商品，新revision可绕开检查并再发一次卖家写入，违反未知回执先核对再重试的契约。

修复后，单件任务创建和内容刊登批量预检都会按商品检查所有版本的未知/中断回执，并给出
按SKU回查、人工核对的提示。后续边界复测发现`needs_attention`同时表示“有有效平台回执但
内容待改”，若跨版本永久拦截会妨碍修正刊登。因此未知/中断回执跨版本阻止重发；已确认
回执只阻止同一版本重复请求，编辑后的新版本仍须重新审核才能提交。实际回查及人工核对
能力仍需卖家账号集成，本次没有把本地状态当成远程证据。

复现与回归：新增测试覆盖旧revision未决后编辑、拦截新revision提交，以及批量预检报告旧
revision中断回执；还覆盖已确认异常回执同版本防重、改成新版本后允许重新审核并再次排队。
`.venv/bin/python -m unittest discover -s workbench/tests -p 'test_workbench.py'`27项通过；
`... -p 'test_platform_batch.py'`14项通过。最终`bash scripts/check.sh`退出0：834项业务
单测、21项桌面编排单测及29个真实本机Chromium流程通过，包含40页桌面/窄屏
导航回归；隔离启动、调度、写保护、导入去重与重启检查通过。完整日志：
`/tmp/noon-inflight-cancel-check.log`。`git diff --check`通过。

范围：这是防止未知回执跨版本盲目重发的本地保护，不是noon真实回执对账或刊登成功验证；
`real_noon_verified`仍为false。没有调用真实卖家、模型、银行或供应商服务。

### 重启时区分尚未发送与在途提交

复测App启动恢复路径发现，原`recover_jobs`把所有`queued`与`running`任务一律标记为
`interrupted`。已排队但尚未被执行器领取的提交任务必然未发送，却被提示按SKU回查，并会被
未知回执门禁挡住，导致安全重排也无法进行。

现在队列里的非视觉检查任务记录为`failed`并明确标注“尚未发送外部请求，可安全重新安排”；
执行器已领取的任务仍记录为`interrupted`。在途提交提示必须先按SKU回查，旧版商品即使编辑
成新revision也不能重发。视觉检查任务维持其专用恢复策略：仅由阶段记录证明未发送时自动重排。

新增重启回归分别验证排队提交可用新任务安全重排、执行中断仍需回查；现有视觉检查重启测试
确认只恢复确定未发送的检查。定向`test_workbench.py`27项和`test_visual_checks.py`11项通过；
最终`bash scripts/check.sh`退出0，834项业务单测、21项桌面编排单测及29个真实本机Chromium
流程全部通过，完整日志`/tmp/noon-inflight-cancel-check.log`。隔离启动/重启、写保护与40页
桌面/窄屏导航流程也通过。测试只使用临时合成数据。

边界：正在执行的请求仍可能在服务端已接收后失联，不能根据本地进程状态猜测。真实回执核对
依然需要有效noon店铺接入；`real_noon_verified=false`。

### 在途提交的取消竞争

批量刊登的取消操作只会原子取消仍为`queued`的行。新增本机工作线程级别的
合成回归：在假客户端已进入`submit`后触发取消，返回`cancelled_now=0`且任务仍为`running`；
让写请求超时后任务变成`uncertain`。再次把相同任务交给worker不会发送第二次请求，假客户端
调用次数保持1。定向`test_platform_batch.py`14项通过；最终全量检查834项业务、21项桌面及
29个Chromium流程通过。未访问真实noon账号；记录在`/tmp/noon-inflight-cancel-check.log`。

### 5000行货源投递的错误行下载

依据自动化路线图P1验收补齐大文件导入反馈：5000行批次回执原先只保留最多5条跳过原因，操作者无法整理其余异常行。现在详细回执保存所有跳过行的行号、商品名称、供应商规格货号、货源链接、库存、采购成本、状态和原因；记录列表仍只返回错误总数，避免把全部异常行带入每次列表响应。查看该文件明细后可下载 UTF-8 BOM CSV，公式危险前缀使用既有CSV转义规则处理。

扩展5000行跨批回归验证：4997条归集，3条跨批重复/身份冲突在明细数据中可定位；浏览器以相同导出组件验证错误CSV字段与下载。原有0库存和空成本语义、防重回放不变。定向测试通过；最终`bash scripts/check.sh`退出0：835项业务单测、21项桌面编排测试，40个页面的1440/390px布局检查及30个真实本地Chromium流程全部通过；新增错误行CSV浏览器流程验证UTF-8 BOM、行号、重复原因及0库存字段。全量日志`/tmp/noon-source-error-export-check.log`；`git diff --check`通过。

边界：浏览器自动化使用本地临时合成资料，没有下载或发布到真实noon店铺；CSV是错误行诊断清单，修正后仍需重新投递和人工确认。`real_noon_verified=false`。

### 图片托管 PUT 结果未明后的安全重试

路线图P1外部调用幂等边界补充端到端回归：模拟图片存储已接受确定性对象写入、客户端随后丢失PUT响应。首个托管任务进入异常，商品未回填公网地址；仅在操作者显式重试后建立新任务，先对同一内容哈希URL执行匿名读回。读回与本地原图字节一致时跳过第二次PUT，再绑定地址；商品成图确认和商品审核状态仍保持未通过。该行为使“重试”先对账，再决定是否需要重写，不把上传回执冒充noon可售回执。

`test_workflow_host.py` 8项与`test_image_host.py` 10项定向测试通过。最新全量`bash scripts/check.sh`退出0：836项业务测试、21项桌面测试、40页1440/390px布局检查和30个真实本地Chromium流程全部通过；日志`/tmp/noon-uncertain-upload-check.log`。`git diff --check`通过。

边界：PUT、CDN读回均为故障注入合成协议，无真实存储桶、供应商或noon账号请求；真实公开托管可读性与平台回执仍待授权环境验证，`real_noon_verified=false`。

### 未决 Noon 内容提交的人工回执对账

继续推进路线图P1“外部调用幂等与不确定回执对账”。复测确认任务记录虽提示操作员按 SKU 回查，实际并无入口将回查结论写回；`uncertain`/`interrupted`任务会因此永久挡住再次预检。新增任务历史回查表单和`/api/content-submit-batch/reconcile`接口，要求明确选择“已找到/未找到”、核对说明及确认。找到时必须填 Noon 商品编号，写入人工来源、回查证据和任务revision；任务保持`needs_attention`，并显式保留`live_verified=false`。未找到时写入审计并将原任务标记失败，后续仍须重新预检/审核。不会自动访问店铺或重放卖家请求。

核对请求使用独立request id与内容指纹：相同请求回放返回原receipt，不重复记事件；同key不同资料冲突。商品回执、任务状态、操作请求receipt、审计事件在同一事务提交。旧revision不能覆盖新版本回执。真实浏览器测试发现样式覆盖HTML`hidden`属性，导致未找到分支仍显示平台商品编号；补充表单样式并由Chromium断言隐藏和非必填。

`test_platform_batch.py`17项和真实本机HTTP集成`test_erp_http.py`14项通过，覆盖接收回执幂等、防重复审计、不置可售、未找到分支、审计失败原子回滚及未认证写请求拒绝；Chromium核对表单两分支和载荷通过。最终`bash scripts/check.sh`退出0：840项业务测试、21项桌面测试、31个本机Chromium流程通过；导航覆盖40个入口和1440/390px布局。日志`/tmp/noon-submit-reconcile-final-check.log`；`git diff --check`通过。

边界：此能力只记录操作者在 Noon 卖家中心完成的回查，没有自动读取 Noon，也没有真实卖家店铺测试。`real_noon_verified=false`；人工确认内容已接收仍不证明审核完成、价格库存正确、可购买、产生订单或盈利。回查说明由操作者填写，应按内部审计要求留存可复核证据。

### 内容提交预检限流与渠道目录首屏竞态

继续检查P1外部调用边界时，补齐提交前 Noon 类目属性预检遇到429的隔离回归：该只读请求被限流时任务为`failed`，商品 upsert 调用次数为0；确认之后可以明确重排，不留下“写入结果未知”的状态。未决对账的“未找到”测试也改为直接断言未知回执阻断已解除，避免只依据“没有可提交商品”间接判断。

上一轮全量检查偶发失败于`domestic_sources.cjs`：40页导航后立即添加渠道账号，偶尔渲染出空平台选项。单项重跑通过，确认根因是导航渲染早于`/api/channels/state`返回；添加账号处理器现在会先确保渠道目录已载入再渲染表单。浏览器回归主动清空前端目录状态后打开表单，验证首帧恢复全部四个预期入口。

`test_platform_batch.py`18项、`test_erp_http.py`14项定向通过；最终`bash scripts/check.sh`退出0：841项业务测试、21项桌面测试、31个Chromium流程通过，包含强制目录未载入状态的国内来源录入和40入口桌面/窄屏导航。完整回归通过后，将该竞态浏览器流程连续独立运行5次，5次均通过。日志`/tmp/noon-continuous-regression-check.log`；`git diff --check`通过。

边界：限流与渠道采集都使用合成、本机服务；没有触发真实 Noon 写入，也没有打开1688等商家账号。提交写入发出后超时仍保守标记结果未知，须人工回查，不因本地测试通过推断平台已接收或可售。

### Noon 已接收但客户端丢失提交响应的端到端对账

为加强“外部调用幂等与不确定回执对账”的故障注入，新增完整工作线程级模拟：假 Noon 在 upsert 后先保存远端 SKU 和接收结果，再抛出连接关闭错误，模拟响应丢失。工作台保留`uncertain`且不保存虚假的本地回执；再次执行同一 job 时 worker 因状态不是`queued`而不发请求，upsert调用总数仍为1。操作者录入远端SKU及回查依据后，本地补记该revision的平台接收回执；商品仍为`live_verified=false`，同revision预检也阻止重复提交。

`test_platform_batch.py`19项通过，新增场景验证远端先提交再丢响应、同job重跑不重发、人工回查后恢复本地回执。最新`bash scripts/check.sh`退出0：842项业务测试、21项桌面测试、31个Chromium流程通过。日志`/tmp/noon-remote-accept-lost-response-check.log`；`git diff --check`通过。

边界：远端状态由合成假客户端模拟，不能证明真实 Noon 对应字段或读回行为；没有卖家凭证，也没有外部平台写入。真实内容审核、库存价格和可售状态仍需独立验证，`real_noon_verified=false`。

### 已发生模型调用后的显式重试确认

继续验证自动化路线图中的持久步骤与不确定结果恢复时，发现人工重试 translate/review 会递增
attempt并创建新request key；即使旧调用已经成功、失败或结果不确定，系统此前都没有在该入口
要求确认，可能再次消耗 MiniMax/Codex 订阅额度或 API 费用。现在只要同一商品步骤和attempt已有
模型调用账本记录，后端就返回409并要求显式确认；确认事件写入 automation audit。页面提示涵盖
“再次重试必然新发起模型调用”和“前次结果/用量可能未核清”，取消确认不会发送第二个请求。
保护适用于主模型和备用模型重试；无既有调用记录的步骤仍可直接重试。不会自动重发模型调用。

新增单测覆盖订阅超时形成uncertain记录与已成功调用后的流程中断，两种情况均阻止未确认重试；
确认后使用新attempt，并保留审计记录。HTTP路由测试验证409/200及token认证，真实Chromium本机
页面测试验证拒绝确认无第二个请求、接受后携带确认标志。`test_automation.py`26项、
`test_erp_http.py`15项通过。最终`bash scripts/check.sh`退出0：845项业务测试、21项桌面编排测试，
32个本机Chromium工作流通过；完整日志`/tmp/noon-model-retry-confirmation-check.log`，
`git diff --check`通过。

边界：模型服务由本地合成profile和故障注入响应模拟；没有使用真实 MiniMax/Codex额度、卖家账号或
Noon接口。确认仅代表操作者同意再次发起调用，不证明旧调用是否计费或结果是否已被其他流程采用。
`real_noon_verified=false`。

### 商品详情翻译入口的重启后重复调用确认

复查同一P1幂等边界时发现，上一项保护仅覆盖自动化中心；商品详情里的独立翻译按钮在同版本
旧任务重启后成为`interrupted`时，仍能直接创建新的翻译任务。现在`Store.add_job`在事务中检查该
商品/版本既有翻译任务关联的模型调用账本；只要存在任何调用回执，默认拒绝新任务。页面收到409
后提示可能重复消耗额度，拒绝确认不产生第二个请求；明确确认才创建新任务，并在同一事务写入
操作事件。商品版本变化后重新翻译仍走正常流程，因为这不是同版本重放。

新增恢复状态单测、真实本机HTTP 409/202测试及Chromium取消/确认测试，使用合成MiniMax配置和调用
回执，不发真实模型请求。`test_workbench.py`28项、`test_erp_http.py`16项通过；最终
`bash scripts/check.sh`退出0：847项业务测试、21项桌面测试、32个本机Chromium工作流通过。
完整日志`/tmp/noon-product-translation-retry-check.log`，`git diff --check`通过。

边界：模型回执和重启由临时本机数据库合成；没有使用真实订阅额度、店铺凭证或Noon接口。
确认只允许操作员有意新发一次模型调用，不证明此前请求是否计费。`real_noon_verified=false`。

### 图片托管重试的公网回读结果分类

故障注入复查发现，图片托管在PUT前会先检查内容哈希地址，但此前把任意回读失败都当作对象
不存在并继续PUT。若公网读取超时、返回403/其他HTTP错误，或已有地址内容不匹配，程序可能在
对象状态不明或冲突时覆盖写入。现在只有明确HTTP 404会产生专用`PublicObjectMissing`结果并允许
上传；超时、非404 HTTP错误、非图片响应和字节不一致都停止处理，保留商品原地址，不再PUT。
PUT结果不明后的下一次重试仍先读回同一确定性地址；读回无法确认时不重复写入。

新增HTTP 404/403分类、冲突内容不PUT及“PUT超时后公网读回超时不进行第二次PUT”的故障注入回归。
`test_image_host.py`12项、`test_workflow_host.py`9项通过；完整`bash scripts/check.sh`退出0：
850项业务测试、21项桌面测试及32个本机Chromium工作流通过。日志
`/tmp/noon-host-readback-classification-check.log`；`git diff --check`通过。

边界：对象存储与CDN响应为合成测试，没有访问真实存储桶或Noon。明确404仅允许对内容哈希地址
尝试同内容上传；最终仍需公网读取逐字节匹配，才会回填商品链接。`real_noon_verified=false`。

### Noon HTTP 传输层单次尝试约束

继续沿刊登不确定回执边界向连接器底层核验：当前 Noon 适配器使用标准 `urllib` 并禁止重定向，
未挂载自动重试策略。新增传输层回归，分别注入429、503与连接超时，断言每种错误只调用一次
`open`；307重定向直接拒绝，避免请求及凭证转发到新地址。该契约与应用层“写入回执不明则停止、
人工核对后再决定”的策略一致。

`test_offer_status.py`8项定向测试通过。最终`bash scripts/check.sh`退出0：851项业务测试、
21项桌面测试、32个本机Chromium工作流通过；覆盖40个导航入口的桌面和窄屏检查。日志
`/tmp/noon-noon-transport-single-attempt-check.log`，`git diff --check`通过。

边界：HTTP错误与超时均由合成本地模拟，不连接Noon，不构成真实账号/API可用性验证；
`real_noon_verified=false`。

### Noon 提交任务的只读预检与写入阶段恢复

补测P1发送前/后断电边界时发现，任务一进入`running`就会在重启时被当成“平台可能收到请求”。
如果进程实际停在认证或类目属性只读预检阶段，会产生不必要的人工平台回查阻塞。现在提交任务
领取时原子持久化`preflight`阶段；只读预检完成后、调用`client.submit`之前，先将阶段更新为
`submit_dispatching`。重启恢复时，`preflight`确定未发商品写请求，标记失败并允许重新预检；
`submit_dispatching`保持`interrupted`并要求SKU回查。没有新阶段字段的旧版在途任务仍保守保持
`interrupted`，不会因升级而误判未发送。

重启测试覆盖排队、只读预检、即将提交、旧版无阶段字段四种状态，并断言预检项可重新安排而后两者
受回查门禁。工作线程故障注入在`client.submit`入口读取数据库，确认写入阶段已先持久化；既有
响应丢失测试仍验证单次upsert和人工对账。`test_workbench.py`28项与`test_platform_batch.py`19项
定向通过；最终`bash scripts/check.sh`退出0：851项业务测试、21项桌面测试、32个本机Chromium工作流
通过，40个入口桌面/窄屏检查通过。日志`/tmp/noon-submit-durable-preflight-phase-check.log`；
`git diff --check`通过。

边界：Noon登录、类目读取和upsert用本机合成协议，不访问卖家账号。标记`submit_dispatching`代表
写请求可能即将或已经发出，不代表平台接收；恢复时仍需人工回查，`real_noon_verified=false`。

### Noon 只读预检期间重启的安全恢复

继续检查持久化阶段边界时，补充验证“重启发生在类目属性读取期间”的真实线程时序：工作线程
先持久化`preflight`，阻塞在只读`attributes`调用时触发恢复，再让只读调用返回。恢复将任务标为
确定未发送，可重新预检；原工作线程随后尝试写入`submit_dispatching`时因任务不再是`running`
而停止，Noon `submit`调用次数为零。这样避免把只读中断误标成平台写请求不确定，同时不会让恢复
竞态越过发送门禁。此前的`submit_dispatching`及旧数据无阶段标记仍需SKU人工回查，不会自动重发。

新增线程级恢复竞态测试，并保留排队/预检/即将写入/旧数据恢复、发送前阶段落盘及远端已接收但
响应丢失不重发测试。`test_workbench.py`28项、`test_platform_batch.py`20项通过；最终
`bash scripts/check.sh`退出0：852项业务测试、21项桌面测试通过；Chromium检查覆盖40个导航页面的
桌面和窄屏布局及32个浏览器工作流，均通过。完整日志`/tmp/noon-submit-preflight-recovery-race-check.log`；
`git diff --check`通过。

边界：重启竞态由本机线程与合成Noon协议验证，没有访问真实卖家账号或发送Noon写请求；类目属性
读取为模拟只读调用。`real_noon_verified=false`。

### 2026-10-05 日期漂移回归修复与当前PR延续工作树复测

- 目标工作树：已有管理 worktree `codex/navigation-compact`，HEAD `c66a7999d2c0c0775450ebb09df69c9d768456d2`（在远端0.44.0 PR #5 head之后2个本地提交），并保留既有未提交更改。没有重置、清理或切换这份工作树。
- 复现：完整回归852项中，`test_shared_order_and_settlement_ledger_with_durable_replay`失败；订单导入使用当前`created_at`参与经营报表日期过滤，测试却固定查`2026-10-03`。在2026-10-05再次独立运行同一测试稳定复现订单数0而断言为1。Noon、结算和账本本身仍保留一笔合成订单。
- 修复：测试在导入订单后读取其持久化`created_at`日期，并让结算样例和经营报表日期区间共用该日；注释说明订单分析按创建时间筛选。使用已保存订单时间可避免固定日期过期和跨午夜的二次时钟读取。只更正过期测试日期，没有改动生产账务口径。
- 定向回归：`PYTHONPATH=workbench:workbench/tests .venv/bin/python -m unittest test_erp_http.ERPHTTPTests.test_shared_order_and_settlement_ledger_with_durable_replay -v`最终修复后通过（1项，0.578秒）。
- 完整回归：`bash scripts/check.sh`最终退出0；852项业务测试70.653秒通过，21项桌面打包编排测试0.737秒通过，隔离启动/调度/写保护/导入幂等/持久化重启通过。JS语法和所有配置的真实Chromium工作流通过；含40个导航页在1440px/390px、MiniMax设置菜单与只读回显、账务/仓储/采购/采集/回执以及多项深度流程。没有真实店铺、供应商、银行或模型请求。
- `git diff --check`在验证记录更新后通过。工作树其他既有修改和未跟踪项均保留，没有提交或推送。
- 新发现的独立功能缺口：本机诊断仍报`arabic_layout=false`；对`draw_text(..., 'منتج عالي الجودة', ...)`的隔离调用稳定抛出“当前图片引擎不支持阿文排版”。当前requirements没有`arabic-reshaper`/`python-bidi`，功能仅以错误门禁拒绝，不能生成阿文卖点模板。此项未在本轮改动，列为下一次图片文字排版优先修复；在修复和视觉像素核验前，不声称Mac本地阿文模板可用。
- 此worktree的852项结果覆盖其本机未提交代码；前次Ubuntu成功run `37128043274`覆盖的仍是PR #5旧head `f2ac7ba`，没有Linux运行证据证明当前`c66a799`及未提交增量。

### Linux CI 覆盖增补与复核（2026-10-05）

- 复核PR #5延续工作树对应的`.github/workflows/verify.yml`时发现，本地`bash scripts/check.sh`已运行、但Ubuntu CI浏览器作业遗漏三个新增流程：大批货源错误行CSV下载、noon不确定提交的人工核对表单、可能重复计费的模型重试确认。
- 将`source_inbox_errors`、`submit_reconciliation`和`uncertain_model_retry`加入Ubuntu浏览器作业。它们使用真实本地HTTP/Chromium和临时合成数据，不触碰真实店铺、模型、供应商或银行。
- 补充前当前本地工作树通过852项业务测试、21项桌面编排测试及本地全量浏览器工作流。更新PR #5分支后，精确提交`c7409f2e2dd4f071ea4e062ca0f2cc32998c30c8`的GitHub Actions push run `37239390585`和PR run `37239393121`均为`success`，两次的backend与browser job均通过。Ubuntu执行了全量业务测试、桌面编排测试、隔离启动/重启、JS语法和浏览器流程，包括本轮补入的三项回归。详见[push run](https://github.com/hyq8216/noonai/actions/runs/37239390585)与[PR run](https://github.com/hyq8216/noonai/actions/runs/37239393121)。
- 这证明PR当前SHA的Ubuntu CI通过；真实店铺、供应商、模型或银行服务仍未调用。macOS DMG workflow是独立的构建工作流，不纳入此Linux结论。

### 在途提交恢复与服务进程互斥（2026-10-05）

- 对`submit_dispatching`边界做故障注入时，直接在同一Python进程里调用内部`recover_jobs()`会把任务改成`interrupted`，随后原工作线程仍能进入Noon适配器。这种调用绕过了服务的真实启动入口，不符合部署契约；不能据此认定生产入口存在相同并发。
- 核对真实启动路径发现`start()`先对工作目录`.server.lock`取得非阻塞独占锁，之后才执行备份恢复、创建`App`及其`recover_jobs()`。第二服务实例无法并发恢复第一个实例的活动数据库。
- 将`smoke.py`扩展为真实子进程验证：首个服务运行时，在临时数据库放入合成`submit_dispatching`任务；第二实例退出并报告已有实例，ready file未创建，任务仍为`running`。首实例退出后再启动，任务按未知回执恢复为`interrupted`。该测试没有创建真实卖家请求，也没有使用工作区经营数据。
- 定向`.venv/bin/python scripts/smoke.py`通过。最终`bash scripts/check.sh`退出0：852项业务测试72.940秒、21项桌面打包编排测试0.725秒；新增的进程互斥/恢复场景通过，JS语法、隔离启动检查、40个导航入口桌面/窄屏检查以及全量浏览器业务流程通过。浏览器均使用临时合成数据，没有真实卖家或供应商请求。`git diff --check`通过。
- GitHub Actions对精确SHA `e1f5e0cf9f922f996abd880cb696200567ce20e2`的push run `37240091910`和PR run `37240095331`均为`success`，两个工作流中的backend、browser jobs全部通过。新增smoke场景包含于backend的隔离启动步骤，并验证了不确定写恢复和进程互斥。详见[push run](https://github.com/hyq8216/noonai/actions/runs/37240091910)与[PR run](https://github.com/hyq8216/noonai/actions/runs/37240095331)。
- 边界：服务进程互斥证明来自本机临时工作目录与合成任务；Noon写入、真实回执和真实店铺仍未验证，`real_noon_verified=false`。

### 发送请求后的回执写入失败不能降级为可重试失败（2026-10-05）

- 故障注入复现：模拟Noon提交超时（可能已发送），再让第一次本地`job_result(..., 'uncertain')`写入短暂失败。旧异常处理会将任务标成`failed`，且`Store.add_job`允许为同商品/版本创建第二个提交任务；单次复现记录为`status=failed`、`second_job_created=true`。这是可造成重复卖家写入的真实软件缺陷，尽管网络客户端未被重试。
- 修复：新增`Store.fail_job_safely`，在一个SQLite写事务中读取job持久阶段并决定失败状态。提交仍在`preflight`时可标记`failed`并安全重排；状态已越过预检边界时只能标`uncertain`并提示先按SKU回查；若任务已被其他路径恢复或改写，则不覆盖。若本地数据库仍不能记录异常状态，原`running`行保留，下一次独占启动将其恢复成`interrupted`，继续阻止重发。
- 新增`test_transient_uncertain_receipt_write_failure_keeps_submit_blocked`，验证超时后首次不确定回执持久化失败会安全落为`uncertain`，并断言相同商品版本不能创建第二提交任务。与限流前置只读预检及重启恢复定向回归共3项通过。
- 完整`bash scripts/check.sh`两轮均退出0：每轮853项业务测试、21项桌面测试、隔离服务启动/恢复检查及全量本机Chromium浏览器流程通过，包含MV3扩展安装/执行、uncertain Noon提交人工对账和付费模型重试二次确认。Ubuntu首次运行的browser job在扩展下载回执处稳定复现时序：`downloads.search()`观察到`state=complete`时`filename`仍未就绪，随后读取为空。测试现在等`complete`且本地路径非空，再核验路径和文件；改动后连续5次本机MV3流程通过，Ubuntu修复版push与PR CI均通过（详见下一条）。该修复仅收紧测试等待条件，不改变扩展或产品行为。
- 远端最终验证：精确SHA `e0387eef0638a0018276570a9abf917259f65ff6`的Ubuntu push run `37241333575`和PR run `37241335645`均成功，两个run各自的backend/browser jobs全部通过；浏览器job实际执行了MV3扩展下载检查。修复前SHA `a25da674342dfab287d5eff3003c38f1ad085014`的Ubuntu backend run `37240974879`通过，browser仅因该已修复的测试时序问题失败。macOS DMG run `37240974867`在修复前提交上成功完成打包回归、构建和挂载检查；最新提交只调整浏览器测试等待条件与文档，不改应用构建内容。成功记录：[Ubuntu push](https://github.com/hyq8216/noonai/actions/runs/37241333575)、[Ubuntu PR](https://github.com/hyq8216/noonai/actions/runs/37241335645)、[macOS DMG](https://github.com/hyq8216/noonai/actions/runs/37240974867)。本机没有安装/运行CI导出的DMG。
- `git diff --check`通过；没有真实Noon凭证/账号或外部写入。

### 5000件目录铺货预览与分批安排（2026-10-05）

- 发现：`CatalogCampaign`最多允许5000件并按500件分段预检/建流程，但原回归没有对应测试文件，CI也没有浏览器操作脚本覆盖“目录分批安排筛选结果”；工作区README将该功能标为未测试。
- 新增3项后端回归：5000件预览生成10个500件chunk且只读、不建流程；1001件应用跨500边界，逐件隔离已有活动任务和缺货源事实行，只为999件新建流程，最多每流程500项；请求重放不重复建流程，完整异常快照保留；资料版本变更后旧token被拒，request id冲突也不能改写既有流程。特设最后一块只有异常行仍跳过建空流程。
- 新增真实本地HTTP/Chromium工作流：载入合成可安排、缺规格事实、已有活动任务三件商品，关闭翻译调用后经界面预览并人工确认；核对历史和两条异常快照，随后改动商品事实验证旧预检失效。无模型或卖家请求。
- 定向`.venv/bin/python -m unittest discover -s workbench/tests -p 'test_catalog_campaign.py' -v`：3项通过；`node scripts/browser/catalog_campaign.cjs`通过。全量`bash scripts/check.sh`退出0：856项业务测试、21项桌面测试、隔离启动/恢复 smoke与全部浏览器流程通过；40页导航、MV3、Noon人工对账等流程以及新增铺货流程均通过。精确SHA `acd9ff11956ff721d5071935887acca417ed1ad5`的Ubuntu push run `37242108398`与PR run `37242112062`均成功，backend/browser jobs全绿；macOS DMG run `37242108346`成功。记录：[Ubuntu push](https://github.com/hyq8216/noonai/actions/runs/37242108398)、[Ubuntu PR](https://github.com/hyq8216/noonai/actions/runs/37242112062)、[macOS DMG](https://github.com/hyq8216/noonai/actions/runs/37242108346)。`git diff --check`通过。该批结果使用临时合成工作区，不能证明真实目录的长期性能或真实noon可售。
