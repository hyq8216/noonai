# 异常中心优先级与人工处置深化

2026-10-03，本地软件功能验收；并非真实Noon经营或外部接口验证。

## 已实现

- 保留来源、自动流程、模型、订单、采购、库存、财务、备份八类观察；每条新增可解释优先级依据和人工建议动作，继续保留来源JSON和原模块入口。
- 订单创建加24小时、发运加7日、备份成功加配置间隔显示本地阈值时间与超过阈值秒数；财务到期按原始有效日期、UTC日界计算。采购无承诺到货日期，不推断逾期；不新增无依据的占库滞留阈值。
- 分类、严重度、提醒状态组合筛选与严重度/种类/到期/新旧时间排序全部在SQL完成后再分页。分类计数保留原全量口径；严重度及提醒状态计数对应当前分类，未叠加其他筛选，并在界面说明。有效页码越界钳制最后一页。
- 全筛选结果CSV导出，与当前页无关，最多10000条；超限明确拒绝，不导出部分文件。UTF-8 BOM，复用已有CSV公式防护，包含依据、建议、阈值时间、观察时间及本地范围说明。
- 不修改业务事实、不自动解决、不外发通知；已阅/延后保持确认、指纹、幂等请求、审计及恢复门禁。时间流逝只改变显示逾期时间，不改变指纹；来源证据变化重新提醒。

## 接口

`Alerts.state(page=0, group='all', severity='all', sort='severity', mode='all')`保留原位置参数。严重度支持critical/warning/info；提醒状态open/ack/snooze；排序severity/category/overdue/latest/oldest。新增priority_reason、suggested_action、deadline_at、deadline_basis、overdue_seconds、severity_counts、mode_counts、facet_scope。

`Alerts.export(params)`由集成负责GET `/api/alerts/export`；与state筛选及默认严重度排序相同，忽略当前page，返回CSV字节。

## 验证

- `.venv/bin/python -m unittest discover -s workbench/tests -p 'test_alerts*.py' -v`：20/20通过，其中新增8项。覆盖原始时间依据、无依据采购及非法历史日期、SQL全量筛选与排序、分页钳制、CSV安全与全量范围、上限拒绝、原业务数据不变、来源变化/延后到期与幂等、时间流逝不撤销已阅。
- `node scripts/browser/alerts_deepening.cjs`：真实本地HTTP与临时Chromium通过；54项合成财务异常，严重度/状态计数、已阅后open/ack/snooze筛选、到期排序、真实下载1条CSV及第二页导出全部53条，390px延后操作，财务单据、付款和汇总不变，零外部请求。
- `node scripts/browser/alerts.cjs`：原真实HTTP/Chromium回归通过，55条财务分页50+5、明确确认与已阅/延后、来源修订重新提醒、过期指纹HTTP409、全部分摊异常消失、390px实际操作、库存/现金不变、四条审计且零外部请求。
- `node --check workbench/static/alerts.js`、`node --check scripts/browser/alerts_deepening.cjs`及`.venv/bin/python -m py_compile workbench/alerts.py`通过。

全部测试与浏览器仅使用临时数据目录，不打开workbench/data。账号诊断仍限制每次200个启用账号，UI及CSV说明覆盖范围；无真实账号、供应商或银行请求，无macOS验证。
