# 供应商报价可履约比较深化（2026-10-03）

本轮延伸既有供应商询价页面和资料结构，不新增导航，不联系供应商，不创建采购、不付款。复用原 `state/get/preview/save/export`，无新增HTTP路由或资料表。

## 已实现

- SKU需求新增可选到货日期 `required_by` 和人工所选供应商 `selected_supplier_id`；报价新增包装倍数 `pack_quantity`、可供数量 `available_quantity`，未知保持null。
- `fulfillment`逐行计算 `ceil(max(需求, MOQ)/包装倍数)*包装倍数`、整批原币预算、超购、需求缺口及预计到货（从当日加交期天数）。包装、可供数量、价格、依据不完整、报价过期、交期晚于需求、整批容量不足或取整超过100万件均阻断履约。
- 同SKU、同币种内按可履约整批预算比较；不猜汇率，不跨币排序，不将未知金额/容量变为0。原单价比较字段/检查契约保留，历史报价缺少新字段时仍可看单价，但不可声明可履约。
- `procurement_draft`返回所选报价、按原币分组整批预算、已知小计、未知行数和未选择数量。整批预算未知时分组总额为null；所选失败报价保留金额与阻塞供复核。该对象为只读草稿，不对应正式采购单。
- 选择、需求日期和报价随询价版本保存，修改后重新预览/人工确认；商品/供应商依据漂移、撤销均阻断草稿。持久预览还绑定当日，跨日确认需重新预览。
- 原CSV追加包装、容量、日期、整批数量、超购、缺口、整批预算、选入草稿标识与门禁；保留原列顺序及公式注入防护，可筛选所选行形成可审阅采购草稿。

## 验证

所有Python fixture 使用TemporaryDirectory；真实HTTP/Chromium由现有harness创建独立临时 `--data` 并清理，不使用workbench/data，不调用真实平台。

- `.venv/bin/python -m unittest discover -s workbench/tests -p 'test_supplier_quotes*.py' -v`：16项通过（原10项+新增6项）。新增覆盖MOQ/包装取整、4位单价预算、未知不归零、容量缺口、日期/过期、跨日预览、来源漂移、选择有效性、撤销、导出、数量上限及不创建采购。
- `node --check workbench/static/supplier_quotes.js`：通过。
- `node scripts/browser/supplier_quotes_deepening.cjs`：真实本地HTTP及Chromium通过；桌面和390px窄屏实际操作选择、日期、包装、容量、预览失效、保存、详情、下载CSV，核对正式采购数量未变化。
- `node scripts/browser/supplier_quotes.cjs`：原询价真实本地HTTP及Chromium回归通过（来源变化、未知、导出、撤销与窄屏）。

- `.venv/bin/python -m unittest discover -s workbench/tests -p 'test_procurement.py' -v`：14项通过。
- `.venv/bin/python -m unittest discover -s workbench/tests -p 'test_operations.py' -v`：14项通过。

本子任务最终执行6条验证命令，共44项Python测试、2项独立Chromium流程和1项JS语法检查通过。早期浏览器发现旧选择器歧义及错误测试路由，修复为兼容既有单个数量输入/详情表格，并使用真实既有state路径后全部通过。

## 边界

供应商容量为人工录入报价事实，不等同实际锁货；交期不含未提供的运输和清关时间。预算不含未知运费/税费/质量差异。需另行人工确认仓库及费用。正式采购目前为CNY且两位价格，本次不自动转换四位价格或其他原币草稿，不写入正式采购。真实供应商询价/下单、库存锁定、付款、Noon业务及macOS发行未验证。
