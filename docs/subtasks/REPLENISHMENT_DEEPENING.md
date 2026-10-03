# 补货建议深化（2026-10-03）

范围：仅 `workbench/replenishment.py`、`workbench/static/replenishment.js`、新增专用单测、专用浏览器脚本与本文。沿用 `/api/replenishment/state`、`preview`、`apply`、`export`；不新增路由、不改变仓储模块原有采购生成规则。

有意变化：现有人工规则字段保持兼容，建议数量从 `max(目标−可用−采购未收,0)` 深化为 `max(目标−预计净库存,0)`。预计净库存为 `可用+采购未收+调拨在途−未占用订单需求`。可用仍仅是良品在库减占用，不等于预计库存，也不代表平台实际可售。

- 调拨仅以目标仓同SKU的 `in_transit/partial` 单据已发减已收计入供给；草稿、取消、已收货不计在途。部分到仓将供给从在途转入在库，预计净库存守恒。
- 订单仅 `new` 单据计未占用需求；`reserved` 已从可用扣减，不重复扣需求；出库/取消不计当前需求。占库改变需求口径、取消释放与发货均立即使旧预检失效。
- 采购仅实际 `open/partial` 单据订购减已收。采购关联仍只是分配计划，不增加供给、不删除真实未占用需求，不据关联推断到货归属。
- 无库存台账保持库存、可用、预计库存与建议为未知，即使有明确需求或在途单据；缺任一人工参数仍不计算建议；显式0参数保留0。
- 增加每行采购、调拨、订单明细、状态、版本与来源指纹。来源版本包含相关订单/调拨台账以及原库存/采购依据；申请预检后任一相关台账变化要求重新预检。

风险筛选：参数/库存未知、预计净库存为负、可用低于最低、无上述风险。风险优先级依次如上，保持最低库存仅为预警；建议筛选：需补货、无需补货、无法计算。筛选在SQL中先按仓/SKU聚合采购、调拨和new需求，与库存及规则保留NULL关联后计算风险/建议，精确计数并按50行分页；只为当前页读取完整来源明细。越界页由总数直接钳制，不再逐SKU扫描详细来源。聚合仍随商品×仓库及单据量增长；CSV仍沿用完整只读流式扫描，不承诺恒定时间或服务SLA。

CSV沿用整批导出，在一个只读SQLite事务中导出当前筛选全部行（忽略分页范围），包含新增库存口径、风险、计算式、来源版本和三种单据明细；所有数据及明细经过统一公式注入转义，UTF-8 BOM保留。

验证（全部使用临时数据，未读取 `workbench/data`）：

1. `.venv/bin/python -m unittest discover -s workbench/tests -p 'test_replenishment*.py' -v`：15通过（原8＋新增7）。覆盖新订单/占库/出库/取消释放防重、调拨部分到仓守恒、采购关联防重、采购收货守恒、未知库存/参数和显式零、来源预检过期、53行风险筛选越界页与整批安全CSV；新增SQL筛选与_row逐项交叉、采购部分收货/取消未收、缺参数优先unknown、目标和projected边界、第二页仅4次明细读取/空筛选0次。
2. `.venv/bin/python -m unittest discover -s workbench/tests -p 'test_warehouse.py' -v`：12通过。
3. `.venv/bin/python -m unittest discover -s workbench/tests -p 'test_procurement.py' -v`：14通过。
4. `node --check workbench/static/replenishment.js`：通过。
5. `node scripts/browser/replenishment_deepening.cjs`：真实临时HTTP＋Chromium通过，桌面/390px检查、订单取消409失效、调拨部分到仓守恒、筛选分页、53行整批导出、公式转义、无采购生成。
6. `node scripts/browser/replenishment.cjs`：旧真实浏览器流程通过，保留原人工规则、预检确认、库存变化阻断、分页与安全CSV契约。

限制：41项相关单测与两条本地合成浏览器流程证明软件行为；没有供应商/Noon经营请求，不自动采购、不推算销量或到货日期，不代表真实经营成功；macOS打包、多人服务、账户权限与真实外部单据未验证。全量仓库集成回归由主任务统一验收。
