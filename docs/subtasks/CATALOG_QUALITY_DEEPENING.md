# SPU规格组质量深化（2026-10-03）

已有父子规格页面新增草稿检查、确认预览/已存组质检、规格矩阵和CSV导出。质检只读商品事实与归属记录；保存继续使用原先的版本、事实摘要、人工确认、预览摘要和请求幂等门禁，不改商品事实或内容审核。

- 组合口径：仅按本组实际填写的轴值集合推算笛卡尔积；精确报告已覆盖组合、缺口与覆盖率。缺口是人工核对提示，不能视为供应商承诺供货，不能自动创建SKU或发布Noon变体。
- 草稿问题：重复SKU、重复组合、缺/多规格字段、成员不存在、商品版本变化、事实摘要变化、跨组归属、品牌类目缺失/不一致。保存预览依然严格拒绝不合法规格组合。
- 已存组问题：额外诊断成员归属索引缺失、其他组归属、组内未记录的归属索引，以及商品删除/当前事实变化；错误要求本地重新复核。
- 矩阵限制：最多3轴、100成员，每轴值长度100；100×100×100极稀疏组合也只物化前200格。页面和CSV注明截断，全部成员与全部诊断仍导出。矩阵占用描述已记录的组合，并不代表Noon可售。
- CSV：UTF-8 BOM、标准CSV引号转义，防止`= + - @`及前导制表/换行成为表格公式。导出重新读取当前商品/归属快照，不改变商品数据。

## 接口协议

由主任务集成至server：

- `POST /api/catalog-groups/diagnose` → `CatalogGroups.diagnose(body)`。只接受`id/revision/name/source_channel/source_parent_id/axes/members`；成员只接受`product_id/revision/values/facts_digest`。允许缺规格值与重复组合以产生可操作诊断，限制字段类型/长度/数量并拒绝未知字段。返回`quality/connection/noon_variants_published`。
- `GET /api/catalog-groups/export?id=...` → `CatalogGroups.export_csv(group_id)`。返回CSV字符串，server负责`text/csv`附件响应。
- 原state/preview/save响应新增`quality`。`quality.matrix`最多200格，`expected_combinations/missing_combinations`是完整统计，`basis=observed_axis_values`。

## 验证

- `.venv/bin/python -m unittest discover -s workbench/tests -p 'test_catalog_groups*.py' -q`：19项通过（14项原契约、5项深化测试）。覆盖真实临时SQLite数据、精确缺口、百万组合有界预览、不合法草稿可诊断但不可保存、当前事实/归属损坏/删除诊断、CSV公式/逗号/换行安全，以及商品事实不变。
- `node scripts/browser/catalog_groups_deepening.cjs`：真实本地HTTP与Chromium桌面/390px窄屏通过。通过页面选择三SKU/两规格轴、缺字段诊断、编辑后旧诊断失效、矩阵缺口预览、人工确认保存、CSV内容、事实和审核状态保留、商品版本变更后重新诊断。
- `node --check workbench/static/catalog_groups.js`：通过。
- 所有测试使用临时数据目录，未使用`workbench/data`，无卖家账户、平台发布或外部经营调用。Linux结果不验证macOS发行。

## 限制与下一步

缺口只能来自已观测规格值，不发现供应商未提供的其他规格。真实Noon变体绑定、真实货源规格覆盖、完整矩阵按条件分页及供应商确认仍待后续授权/实现。当前功能只完成本地规格组可操作质检，不等于完整SPU/刊登平台。
