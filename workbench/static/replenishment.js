'use strict';
let replenishmentFilters={},replenishmentDraft=null,replenishmentPreview=null,replenishmentRequest=null;
function replenishmentQuery(){return new URLSearchParams(replenishmentFilters)}
function resetReplenishmentDraft(){replenishmentDraft=null;replenishmentPreview=null;replenishmentRequest=null}
function replenishmentPage(){
 const s=state.replenishment;if(!s)return head('本地补货建议','人工参数与实际库存、采购单结合。')+'<section class="panel">正在读取补货建议。</section>';
 replenishmentFilters={...s.filters};const p=s.sku_page,d=replenishmentDraft,v=replenishmentPreview;
 const value=x=>x===null||x===undefined?'未知':esc(x);
 return head('本地补货建议','按仓库与SKU人工设定参数，保存规则前核对当前库存和采购事实。','<button class="btn" id="replenishment-refresh">刷新建议</button><button class="btn" id="replenishment-export">导出当前筛选</button>')+`
 <div class="notice neutral">仅本地只读建议，不会建立采购单、向供应商下单或付款。未知参数与未建库存账保留未知；提前天数不构成精确预测。</div>
 <form id="replenishment-filter" class="panel"><div class="field"><label for="replenishment-warehouse">仓库</label><select id="replenishment-warehouse" name="warehouse_id"><option value="">全部仓库</option>${s.entities.map(e=>`<option value="${esc(e.id)}" ${e.id===s.filters.warehouse_id?'selected':''}>${esc(e.name)}</option>`).join('')}</select></div><div class="field"><label for="replenishment-search">SKU / 商品 / 仓库名称</label><input id="replenishment-search" name="search" type="search" value="${esc(s.filters.search)}"></div><button class="btn">筛选</button></form>
 <section class="panel analytics-contained"><h2>当前库存与建议</h2><p class="sub">最低库存作预警；建议数量=max(目标−可用−采购未收,0)。隔离库存、调拨在途与采购分配关联不计入可用库存。</p><div class="ops-scroll analytics-scroll"><table><thead><tr><th>仓库 / SKU</th><th>良品在库 / 占用 / 可用</th><th>采购未收（实际单据）</th><th>最低 / 目标 / 提前天数</th><th>建议 / 依据</th><th></th></tr></thead><tbody>${p.items.map((r,i)=>`<tr><td class="analytics-sku-cell">${esc(r.warehouse_name)}<div>${esc(r.sku)}</div><div class="cell-note">${esc(r.title)}</div></td><td>${value(r.on_hand)} / ${value(r.reserved)} / ${value(r.available)}${r.below_minimum?'<div class="cell-note">低于最低库存</div>':''}</td><td>${r.purchase_pending}<details><summary>采购来源 ${r.purchases.length}单</summary>${r.purchases.map(x=>`<p class="sub">${esc(x.purchase_id)} · 版本${x.revision} · ${esc(x.status)} · 订购${x.quantity} / 已收${x.received} / 未收${x.pending}</p>`).join('')||'<p class="sub">无采购单；采购未收为0。</p>'}</details></td><td>${value(r.minimum)} / ${value(r.target)} / ${value(r.lead_days)}天</td><td>${value(r.quantity)}<div class="cell-note">${esc(r.calculation)}</div><details><summary>来源版本 · 规则${r.revision}</summary><p class="sub">${esc(r.source_version)}</p><p class="sub">库存版本 ${esc(r.stock_version)}</p><p class="sub">采购版本 ${esc(r.purchase_version)}</p></details></td><td><button class="btn small" data-replenishment-edit="${i}">设置参数</button></td></tr>`).join('')||'<tr><td colspan="6">暂无匹配商品与仓库，请先建立非示例商品及仓库档案。</td></tr>'}</tbody></table></div><div class="actions"><button class="btn" data-replenishment-page="-1" ${p.page===0?'disabled':''}>上一页</button><span>第${p.page+1}/${p.pages}页 · 共${p.total}条 · 每页50条</span><button class="btn" data-replenishment-page="1" ${p.page>=p.pages-1?'disabled':''}>下一页</button></div></section>
 <section class="panel section-break prewrap" id="replenishment-editor"><h2>人工规则预检与确认</h2>${d?`<p>${esc(d.warehouse_name)} · ${esc(d.sku)} · 规则版本 ${d.revision}</p><form id="replenishment-rule"><div class="ops-form-grid">${[['minimum','最低库存'],['target','目标库存'],['lead_days','采购提前天数']].map(([key,label])=>`<div class="field"><label for="replenishment-${key}">${label}（空白=未知）</label><input id="replenishment-${key}" name="${key}" type="number" min="0" max="${key==='lead_days'?3650:1000000}" step="1" value="${esc(d[key]??'')}"></div>`).join('')}</div><div class="field"><label for="replenishment-note">人工设置依据</label><textarea id="replenishment-note" name="note" maxlength="2000">${esc(d.note||'')}</textarea></div><button class="btn" id="replenishment-preview">预检规则</button><button class="btn" type="button" id="replenishment-discard">放弃编辑</button></form>`:'<p>从建议表选择仓库SKU，人工填写已知参数。</p>'}
 ${v?`<div id="replenishment-preview-panel" class="notice neutral"><p>可用 ${value(v.available)}；采购未收 ${v.purchase_pending}；新规则建议 ${value(v.quantity)}。</p><p>最低 ${value(v.minimum)} · 目标 ${value(v.target)} · 提前 ${value(v.lead_days)} 天；规则版本 ${v.revision}</p><p class="sub prewrap">来源版本 ${esc(v.source_version)}</p><p>${esc(v.notice)}</p><label><input type="checkbox" id="replenishment-confirm"> 我已核对参数、库存及采购依据，确认保存本地规则</label><p><button class="btn primary" id="replenishment-apply" disabled>确认保存规则</button></p></div>`:''}</section>
 <details class="panel section-break"><summary>计算口径与限制</summary>${s.basis.map(x=>`<p class="sub">${esc(x)}</p>`).join('')}<p class="sub">生成时间 ${esc(s.generated_at)}</p></details>`;
}
function bindReplenishment(){
 if(view!=='replenishment'||!state.replenishment)return;
 const s=state.replenishment;
 const load=async()=>{state.replenishment=await api('/api/replenishment/state?'+replenishmentQuery());render()};
 const discard=()=>{resetReplenishmentDraft();dirty=false};
 const canChange=()=>{if(dirty&&!confirm('放弃尚未保存的补货规则修改？'))return false;discard();return true};
 on('replenishment-refresh',async()=>{if(canChange())await load()});
 on('replenishment-export',async()=>download(await api('/api/replenishment/export?'+replenishmentQuery(),undefined,true),'本地补货建议.csv'));
 const filter=document.getElementById('replenishment-filter');filter.onsubmit=e=>{e.preventDefault();if(!canChange())return;perform(async()=>{replenishmentFilters={...Object.fromEntries(new FormData(filter)),page:0};await load()})};
 document.querySelectorAll('[data-replenishment-page]').forEach(el=>el.onclick=()=>{if(!canChange())return;perform(async()=>{replenishmentFilters.page=Math.max(0,Number(replenishmentFilters.page||0)+Number(el.dataset.replenishmentPage));await load()})});
 document.querySelectorAll('[data-replenishment-edit]').forEach(el=>el.onclick=()=>{if(!canChange())return;replenishmentDraft={...s.sku_page.items[Number(el.dataset.replenishmentEdit)]};dirty=true;render();document.getElementById('replenishment-editor').scrollIntoView({block:'start'})});
 on('replenishment-discard',async()=>{discard();render()});
 const form=document.getElementById('replenishment-rule');
 if(form){const read=()=>{for(const k of ['minimum','target','lead_days'])replenishmentDraft[k]=form.elements[k].value===''?null:Number(form.elements[k].value);replenishmentDraft.note=form.elements.note.value;dirty=true};
 form.oninput=()=>{read();replenishmentPreview=null;replenishmentRequest=null;document.getElementById('replenishment-preview-panel')?.remove()};
 form.onsubmit=e=>{e.preventDefault();read();perform(async()=>{replenishmentPreview=null;replenishmentRequest=null;try{const d=replenishmentDraft;replenishmentPreview=await api('/api/replenishment/preview',{product_id:d.product_id,warehouse_id:d.warehouse_id,revision:d.revision,minimum:d.minimum,target:d.target,lead_days:d.lead_days,note:d.note||''})}finally{render()}})};}
 const checkbox=document.getElementById('replenishment-confirm');if(checkbox)checkbox.onchange=()=>document.getElementById('replenishment-apply').disabled=!checkbox.checked;
 on('replenishment-apply',async()=>{if(!replenishmentPreview||!document.getElementById('replenishment-confirm')?.checked)throw Error('请先核对并确认预检');
 if(!replenishmentRequest)replenishmentRequest={...replenishmentPreview,confirmed:true,request_id:crypto.randomUUID()};
 try{await api('/api/replenishment/apply',replenishmentRequest);discard();await load()}
 catch(error){if(/变化|版本|预检|编号已用于/.test(error.message)){replenishmentPreview=null;replenishmentRequest=null;render()}throw error}
 });
}
