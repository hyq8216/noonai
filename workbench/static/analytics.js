'use strict';
let analyticsFilters = {};
function analyticsQuery(){return new URLSearchParams(analyticsFilters)}
function analyticsPage(){
 const a=state.analytics;
 if(!a)return head('运营分析与报表','按证据期间查看订单、库存和已登记贡献。')+'<section class="panel"><p>正在读取运营分析。</p></section>';
 const f=a.filters,o=a.orders,i=a.inventory,p=a.purchases,n=a.finance,s=a.sku_page;
 analyticsFilters={...f};
 const amount=(v,c='CNY')=>`${c} ${(Number(v||0)/100).toFixed(2)}`;
 const metric=(title,value,detail='')=>`<section class="panel analytics-contained"><span>${title}</span><h2>${esc(value)}</h2>${detail?`<p class="sub">${esc(detail)}</p>`:''}</section>`;
 const options=(kind,current)=>'<option value="">全部'+(kind==='shop'?'店铺':'仓库')+'</option>'+a.entities.filter(e=>e.kind===kind).map(e=>`<option value="${esc(e.id)}" ${e.id===current?'selected':''}>${esc(e.name)}</option>`).join('');
 const names={new:'待处理',reserved:'已占用',shipped:'已发货',delivered:'已签收',cancelled:'已取消'};
 return head('运营分析与报表','订单、出库与财务使用各自证据日期；库存为当前快照。','<button class="btn" id="analytics-refresh">刷新</button><button class="btn" id="analytics-export">导出本页报告 CSV</button>')+`
 <form id="analytics-filter" class="panel"><div class="grid2 analytics-contained">
 <div class="field"><label for="analytics-from">开始日期（UTC）</label><input class="analytics-date analytics-contained" id="analytics-from" name="from" type="date" value="${esc(f.from)}" required></div>
 <div class="field"><label for="analytics-to">结束日期（UTC）</label><input class="analytics-date analytics-contained" id="analytics-to" name="to" type="date" value="${esc(f.to)}" required></div>
 <div class="field"><label for="analytics-shop">店铺</label><select id="analytics-shop" name="shop_id">${options('shop',f.shop_id)}</select></div>
 <div class="field"><label for="analytics-warehouse">仓库</label><select id="analytics-warehouse" name="warehouse_id">${options('warehouse',f.warehouse_id)}</select></div></div>
 <p class="sub">范围包含起止日，最多366日。切换筛选会回到SKU第一页。</p><button class="btn primary">应用筛选</button></form>
 ${a.empty?'<div class="notice neutral">本范围暂无业务或财务记录。报表不会填充样例或把未知成本当作零成本。</div>':''}
 <div class="ops-metrics">${metric('本期创建订单',o.total,'含取消订单，状态为当前状态')}${metric('本期发货件数',o.shipped_units,'按发货日期统计，不扣退货')}${metric('本期退货件数',o.returned_units,`${o.returned_orders}个订单登记退货`)}${metric('当前可用库存',i.available,`在库 ${i.on_hand} · 占用 ${i.reserved}`)}</div>
 <div class="grid2 analytics-contained"><section class="panel analytics-contained"><h2>订单状态与原币金额</h2><p>${Object.entries(o.statuses).map(([k,v])=>`${esc(names[k]||k)} ${v}`).join(' · ')}</p><p class="sub">本期未取消订单订购 ${o.ordered_units} 件。退货订单与发货/签收状态可重叠。</p><div class="ops-scroll analytics-scroll"><table><thead><tr><th>原币</th><th>未取消订单数</th><th>订单金额</th></tr></thead><tbody>${a.order_currencies.map(c=>`<tr><td>${esc(c.currency)}</td><td>${c.orders}</td><td>${amount(c.amount_cents,c.currency)}</td></tr>`).join('')||'<tr><td colspan="3">暂无未取消订单金额。</td></tr>'}</tbody></table></div><p class="sub">逐币种展示；订单金额不是已确认收入。</p></section>
 <section class="panel analytics-contained"><h2>采购与库存估值</h2>${p.shop_attributable?`<p>本期采购 ${p.documents} 单 · 订购 ${p.ordered_units} 件<br>当前累计到货 ${p.received_units} 件 · 待收在途 ${p.in_transit_units} 件</p>`:'<p>采购未记录店铺归属；请取消店铺筛选查看采购统计。</p>'}<p>参考成本估算库存价值 ${amount(i.estimated_value_cents)}<br>未知成本 ${i.unknown_cost_skus} 个SKU / ${i.unknown_cost_units} 件<br>无可用库存提醒 ${i.stockout_skus} 个SKU</p><p class="sub">当前库存快照，参考采购成本估值，不是期末成本账。未知部分不计入估值。</p></section></div>
 <div class="ops-metrics">${metric('本期已确认收入',amount(n.income_cents),n.filtered?'仅匹配订单分摊份额':'按登记凭证与汇率')}${metric('本期已登记费用',amount(n.expense_cents),'收付款不重复计收入费用')}${metric('当前已核贡献 · 非净利润',amount(n.verified_contribution_cents),`${n.verified_orders}单，本期凭证差额`)}${metric('待核已登记差额',amount(n.pending_difference_cents),`${n.pending_orders}单，完整成本待核对`)}</div>
 <section class="panel section-break"><h2>账务缺口与证据限制</h2><p>本期未取消订单：未登记费用 ${n.missing_cost_orders} 单 · 未登记卖家结算收入 ${n.missing_revenue_orders} 单。</p><p>全局尚未分摊收入 ${amount(n.global_unallocated_income_cents)} · 费用 ${amount(n.global_unallocated_expense_cents)}。${n.filtered?'未分摊金额无法归属到当前筛选店铺或仓库。':''}</p><p class="sub">已核状态复用财务完整记录指纹，新增费用、退款、收付款或业务变化后须重新核对。本期贡献仅包含本期确认凭证；已核贡献、待核差额和库存估值均不是净利润。</p></section>
 <section class="panel section-break analytics-contained"><h2>SKU销量、出库与库存</h2><p class="sub">共 ${s.total} 个SKU · 第 ${s.page+1}/${s.pages} 页 · 每页50行。按本期发货件数排序。</p><div class="ops-scroll analytics-scroll"><table><thead><tr><th>SKU / 商品</th><th>订购</th><th>发货</th><th>退货</th><th>在库 / 占用 / 可用</th><th>估算价值CNY</th><th>库存提醒</th></tr></thead><tbody>${s.items.map(r=>`<tr><td class="analytics-sku-cell">${esc(r.sku)}<div class="cell-note">${esc(r.title)}</div></td><td>${r.ordered_units}</td><td>${r.shipped_units}</td><td>${r.returned_units}</td><td>${r.on_hand} / ${r.reserved} / ${r.available}</td><td>${r.estimated_value_cents==null?'成本待确认':amount(r.estimated_value_cents)}</td><td>${esc(r.stock_warning||'暂无提醒')}</td></tr>`).join('')||'<tr><td colspan="7">本期没有关联SKU或库存记录。</td></tr>'}</tbody></table></div><div class="actions"><button class="btn" id="analytics-prev" ${s.page<=0?'disabled':''}>上一页</button><button class="btn" id="analytics-next" ${s.page>=s.pages-1?'disabled':''}>下一页</button></div></section>
 <details class="panel section-break"><summary>报表口径与证据时期</summary><p>生成时间 ${esc(a.generated_at)} · UTC ${esc(f.from)} 至 ${esc(f.to)}</p>${a.basis.map(b=>`<p class="sub">${esc(b)}</p>`).join('')}</details>`;
}
function bindAnalytics(){
 if(view!=='analytics')return;
 const load=async()=>{state.analytics=await api('/api/analytics/state?'+analyticsQuery());render()};
 const form=document.getElementById('analytics-filter');
 if(form)form.onsubmit=e=>{e.preventDefault();perform(async()=>{analyticsFilters={...Object.fromEntries(new FormData(form)),page:0};await load()})};
 on('analytics-refresh',load);
 on('analytics-prev',async()=>{analyticsFilters.page=Math.max(0,Number(analyticsFilters.page||0)-1);await load()});
 on('analytics-next',async()=>{analyticsFilters.page=Number(analyticsFilters.page||0)+1;await load()});
 on('analytics-export',async()=>download(await api('/api/analytics/export?'+analyticsQuery(),undefined,true),`noon-analytics-${analyticsFilters.from}-${analyticsFilters.to}-page${Number(analyticsFilters.page||0)+1}.csv`));
}
