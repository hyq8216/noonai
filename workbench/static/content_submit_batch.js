'use strict';
let contentSubmitPreview=null,contentSubmitRequest=null,contentSubmitResult=null,contentSubmitPage=0,contentSubmitResultPage=0;

function contentSubmitPanel(){
 const p=contentSubmitPreview,pages=p?Math.max(1,Math.ceil(p.rows.length/50)):1;
 contentSubmitPage=Math.min(contentSubmitPage,pages-1);
 if(!p&&!contentSubmitResult&&state.content_submit_batch_latest)contentSubmitResult=state.content_submit_batch_latest;
 const result=contentSubmitResult;
 const resultPages=result?Math.max(1,Math.ceil(result.jobs.length/50)):1;
 contentSubmitResultPage=Math.min(contentSubmitResultPage,resultPages-1);
 return `<section class="panel"><h2>批量提交已审核内容</h2><p class="sub">从下方商品列表跨页选择，最多500件。先逐件预检，再提交可用商品。提交的是商品内容；平台接收后仍需审核、价格、库存与可售状态回查。</p><div class="actions"><span>已选 ${platformChosen.size} 件</span><button class="btn" id="content-submit-preview" ${!platformChosen.size?'disabled':''}>预检批量提交</button></div>${result?`<p>本批排队 ${result.jobs.length} 件，跳过 ${result.skipped.length} 件。${result.replayed?'已返回同一请求的原回执，没有重复排队。':''}</p><p>等待 ${result.counts?.queued||0} · 提交中 ${result.counts?.running||0} · 已接收 ${result.counts?.done||0} · 待处理 ${result.counts?.needs_attention||0} · 结果不明 ${result.counts?.uncertain||0} · 失败 ${result.counts?.failed||0} · 已取消 ${result.counts?.cancelled||0} · 中断 ${result.counts?.interrupted||0}</p>${result.counts?.queued?'<button class="btn" id="content-submit-cancel">停止尚未发送的任务</button>':''}<details><summary>查看本批任务</summary>${result.jobs.slice(contentSubmitResultPage*50,(contentSubmitResultPage+1)*50).map(j=>`<p><button class="product-name" data-open="${j.product_id}">${esc(state.products.find(x=>x.id===j.product_id)?.title_zh||j.product_id)}</button> · ${esc(j.status)} · ${esc(j.message)}</p>`).join('')}<div class="actions"><span>第 ${contentSubmitResultPage+1} / ${resultPages} 页</span><button class="btn small" id="content-submit-result-prev" ${!contentSubmitResultPage?'disabled':''}>上一页</button><button class="btn small" id="content-submit-result-next" ${contentSubmitResultPage+1>=resultPages?'disabled':''}>下一页</button></div></details>`:''}${p?`<h3>提交前检查</h3><p>可提交 ${p.ready} 件 · 暂不能提交 ${p.blocked} 件${!p.capacity_ok?' · 当前队列容量不足':''}</p><div class="table-scroll"><table><thead><tr><th>商品 / SKU</th><th>检查结果</th></tr></thead><tbody>${p.rows.slice(contentSubmitPage*50,(contentSubmitPage+1)*50).map(x=>`<tr><td>${esc(x.title)}<p class="sku">${esc(x.sku)}</p></td><td>${x.status==='ready'?'可提交':esc(x.reasons.join('；'))}</td></tr>`).join('')}</tbody></table></div><div class="actions"><span>第 ${contentSubmitPage+1} / ${pages} 页</span><button class="btn small" id="content-submit-prev" ${!contentSubmitPage?'disabled':''}>上一页</button><button class="btn small" id="content-submit-next" ${contentSubmitPage+1>=pages?'disabled':''}>下一页</button><button class="btn primary" id="content-submit-apply" ${!p.ready||!p.capacity_ok?'disabled':''}>确认提交 ${p.ready} 件内容</button></div>`:''}</section>`;
}

function bindContentSubmit(){
 if(view!=='platform')return;
 const preview=document.getElementById('content-submit-preview');
 if(preview)preview.onclick=()=>perform(async()=>{
  const product_ids=[...platformChosen].sort();
  contentSubmitPreview=await api('/api/content-submit-batch/preview',{product_ids});
  contentSubmitPage=0;contentSubmitResult=null;
  contentSubmitRequest={product_ids,preview_token:contentSubmitPreview.token,request_id:crypto.randomUUID(),confirmed:true};
  render();
 });
 const apply=document.getElementById('content-submit-apply');
 if(apply)apply.onclick=()=>perform(async()=>{
  contentSubmitResult=await api('/api/content-submit-batch/apply',contentSubmitRequest);
  contentSubmitPreview=null;contentSubmitRequest=null;
  await sync();toast('可用商品已排队提交 noon 内容，请继续核对平台反馈');
 });
 const cancel=document.getElementById('content-submit-cancel');
 if(cancel)cancel.onclick=()=>perform(async()=>{
  contentSubmitResult=await api('/api/content-submit-batch/cancel',{request_id:contentSubmitResult.request_id});
  await sync();toast(`已停止 ${contentSubmitResult.cancelled_now} 件尚未发送的任务`);
 });
 for(const [id,delta,field] of [['content-submit-prev',-1,'preview'],['content-submit-next',1,'preview'],['content-submit-result-prev',-1,'result'],['content-submit-result-next',1,'result']]){
  const el=document.getElementById(id);
  if(el)el.onclick=()=>{if(field==='preview')contentSubmitPage+=delta;else contentSubmitResultPage+=delta;render()};
 }
}
