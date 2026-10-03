'use strict';
let stockPlanDraft={warehouse_id:'',warehouse_code:'',buffer:'0',cap:''},stockPlanResult=null,stockPlanPage=0;

function stockPlanPanel(){
 const warehouses=(state.ops?.entities||[]).filter(x=>x.kind==='warehouse');
 const r=stockPlanResult,pages=r?Math.max(1,Math.ceil(r.rows.length/50)):1;
 stockPlanPage=Math.min(stockPlanPage,pages-1);
 return `<section class="panel"><h2>noon 库存同步候选预览</h2><p>先按本地仓库计算可用上限：实物 − 已占用 − 尚未占用的本地订单需求 − 安全缓冲。采购待收、调拨在途和供应商可供数量不计入。这里只生成本地候选，不发送 noon。</p><form id="stock-plan-form"><div class="grid2"><div class="field"><label for="stock-plan-warehouse">本地仓库</label><select id="stock-plan-warehouse" name="warehouse_id"><option value="">选择仓库</option>${warehouses.map(w=>`<option value="${esc(w.id)}" ${stockPlanDraft.warehouse_id===w.id?'selected':''}>${esc(w.name)}</option>`).join('')}</select></div>${field('warehouse_code','noon 集成仓库编码（可暂留空）',stockPlanDraft.warehouse_code)}${field('buffer','每件商品安全缓冲量',stockPlanDraft.buffer,{number:true})}${field('cap','单件商品展示上限（可留空）',stockPlanDraft.cap,{number:true})}</div><p class="sub">仅对已标为本地模式的商品计算；是否适用 FBPI、仓库编码和权限须在开店后确认。暂留空编码时仍能查看数量，但所有商品会标为待配置。</p><button class="btn primary">计算候选库存</button></form>${r?`<div class="section-break" id="stock-plan-result"><p>计算时间：${date(r.created_at)} · 候选 ${r.candidates} 件 · 待配置或核对 ${r.blocked} 件。库存台账变化后请重新计算。</p><div class="table-scroll"><table><thead><tr><th>商品 / SKU</th><th>本地实物 / 占用</th><th>未占用订单 / 缓冲</th><th>候选绝对数量</th><th>说明</th></tr></thead><tbody>${r.rows.slice(stockPlanPage*50,(stockPlanPage+1)*50).map(x=>`<tr><td><button class="product-name" data-open="${x.id}">${esc(x.title)}</button><p class="sku">${esc(x.sku)}</p></td><td>${x.on_hand} / ${x.reserved}</td><td>${x.unreserved_order_demand} / ${x.buffer}</td><td><strong>${x.qty}</strong><p class="sub">供应商可供：${x.supplier_available??'未知'}（未计入）</p></td><td>${x.status==='candidate'?'仅本地候选，须确认FBPI模式和集成仓库':esc(x.reasons.join('；'))}</td></tr>`).join('')||'<tr><td colspan="5">没有本地模式商品。</td></tr>'}</tbody></table></div><div class="actions"><span>第 ${stockPlanPage+1} / ${pages} 页</span><button class="btn small" id="stock-plan-prev" ${!stockPlanPage?'disabled':''}>上一页</button><button class="btn small" id="stock-plan-next" ${stockPlanPage+1>=pages?'disabled':''}>下一页</button></div></div>`:''}</section>`;
}

function bindStockPlan(){
 if(view!=='inventory')return;
 const form=document.getElementById('stock-plan-form');
 if(form){
  form.oninput=()=>{stockPlanDraft=Object.fromEntries(new FormData(form));stockPlanResult=null;document.getElementById('stock-plan-result')?.remove()};
  form.onchange=form.oninput;
  form.onsubmit=e=>{e.preventDefault();perform(async()=>{
   stockPlanDraft=Object.fromEntries(new FormData(form));
   stockPlanResult=await api('/api/stock-plan/preview',{warehouse_id:stockPlanDraft.warehouse_id,warehouse_code:stockPlanDraft.warehouse_code.trim(),buffer:Number(stockPlanDraft.buffer),cap:stockPlanDraft.cap===''?null:Number(stockPlanDraft.cap)});
   stockPlanPage=0;render();toast('已计算本地库存候选；没有向 noon 写入');
  })};
 }
 for(const [id,delta] of [['stock-plan-prev',-1],['stock-plan-next',1]]){const el=document.getElementById(id);if(el)el.onclick=()=>{stockPlanPage+=delta;render()}}
}
