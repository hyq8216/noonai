'use strict';
let domesticCapturePageIndex=0,domesticPackage=null,domesticPreview=null,domesticResult=null,domesticProductPreview=null,domesticCaptureRequest=null,domesticProductRequest=null,domesticCaptureGeneration=0,domesticCorrections=[],domesticCorrectionsDirty=false;
function domesticCaptureQuery(){return new URLSearchParams({page:domesticCapturePageIndex});}

function domesticQualityPanel(p){
 const q=p.quality_summary,labels={source_sku:'真实规格货号',source_price:'网页售价',stock:'可供数量',supplier:'供应商',facts:'商品事实',images:'图片引用'};
 if(!q)return '';
 const codes=new Map(p.rows.flatMap(r=>r.quality.issues.map(i=>[i.code,i.message])));
 return `<div id="domestic-quality-summary"><p>批量诊断：${q.total_rows} 条观察 · ${q.unique_products} 个来源商品 · ${q.unique_identities} 个规格身份 · ${q.attention_rows} 条需核对 · ${q.blocked_rows} 条存在阻断问题</p><p class="sub">未知字段：${Object.entries(q.unknown_fields).map(([k,n])=>`${esc(labels[k]||k)} ${n}`).join(' · ')||'无'}。零售价 / 零数量是明确值，未计入未知。采购成本与素材权利均待独立核对。</p>${q.package_warnings.length?`<details><summary>扩展采集提示（${q.package_warnings.length}）</summary><ul>${q.package_warnings.map(w=>`<li>${esc(w)}</li>`).join('')}</ul></details>`:''}<label class="field">按问题筛选<select id="domestic-quality-filter"><option value="all">全部观察</option><option value="attention">需核对的问题</option><option value="blocker">阻断问题</option>${[...codes].map(([code,label])=>`<option value="${esc(code)}">${esc(label)} (${q.by_code[code]})</option>`).join('')}</select></label><p id="domestic-quality-visible" class="sub">显示 ${p.rows.length} / ${p.rows.length} 条；筛选只影响显示，确认仍处理完整资料包。</p><button class="btn" id="domestic-quality-export" ${domesticCorrectionsDirty?'disabled':''}>导出当前筛选诊断 CSV</button></div>`;
}
function domesticQualityRow(row){
 const q=row.quality;if(!q)return '';
 return `<ul>${q.issues.map(i=>`<li><strong>${esc({blocker:'阻断',warning:'待核对',info:'提示'}[i.severity])}：${esc(i.message)}</strong><br>${esc(i.action)}</li>`).join('')||'<li>观察字段齐全；后续仍需核对采购成本、素材权利与商品审核。</li>'}</ul>${q.changed_fields.length?`<details><summary>与历史最新观察的变化（${q.changed_fields.length}）</summary><pre>${esc(JSON.stringify(q.changed_fields,null,2))}</pre></details>`:''}<p class="sub">读取方式：${esc(q.source.method)} · 网页原规格：${esc(q.source.observed_sku||'未知')} · 当前规格：${esc(q.source.effective_sku||'未知')}</p>`;
}
function domesticQualityMatches(row,filter){
 return filter==='all'||(filter==='attention'?row.quality.requires_attention:row.quality.issues.some(i=>filter==='blocker'?i.severity==='blocker':i.code===filter));
}
function domesticDiagnosticCsv(rows){
 const cell=value=>{let s=String(value??'未知');if(/^[\s\uFEFF]*[=+\-@]/.test(s)||/^[\t\r\n]/.test(s))s="'"+s;return '"'+s.replaceAll('"','""')+'"';};
 const output=[['行号','状态','商品编号','真实规格','标题','来源商品页','读取方式','网页原规格','网页售价CNY','可供数量','未知字段','问题','建议动作','历史变化']];
 for(const r of rows)output.push([r.row,r.status,r.raw.external_product_id,r.raw.source_sku||'未知',r.raw.title_zh,r.raw.source_url,r.capture.method,r.capture.sku||'未知',r.raw.source_price,r.raw.stock,r.quality.unknown_fields.join('；'),r.quality.issues.map(i=>i.message).join('；'),r.quality.issues.map(i=>i.action).join('；'),JSON.stringify(r.quality.changed_fields)]);
 return '\uFEFF'+output.map(row=>row.map(cell).join(',')).join('\r\n')+'\r\n';
}

function domesticCorrectionFields(row){
 const original=row.capture.original_raw||row.raw,draft=domesticCorrections.find(x=>x.row===row.row)||{},value=(key)=>draft[key]??original[key==='sku'?'source_sku':key]??'';
 return `<details data-domestic-correction-details="${row.row}" ${Object.keys(draft).length?'open':''}><summary>人工校对规格货号 / 供应商 / 事实</summary><p class="sub">原网页规格：${esc(original.source_sku||'未读取到')}。只填写你有依据核对的值，不猜测规格；手填值独立记录，不能冒充网页原值。</p><label class="field">核对后的真实规格货号<input data-domestic-correction="sku" data-domestic-row="${row.row}" maxlength="200" value="${esc(value('sku'))}"></label><label class="field">核对后的供应商<input data-domestic-correction="supplier" data-domestic-row="${row.row}" maxlength="1000" value="${esc(value('supplier'))}"></label><label class="field">核对后的商品事实<textarea data-domestic-correction="facts" data-domestic-row="${row.row}" maxlength="12000">${esc(value('facts'))}</textarea></label><label class="field">校对依据（必填；不要填写登录凭据）<textarea data-domestic-correction="evidence" data-domestic-row="${row.row}" maxlength="2000">${esc(draft.evidence||'')}</textarea></label></details>`;
}
function domesticCapturePage(){
 const s=state.domestic_capture||{},accounts=(s.accounts||[]).filter(a=>a.enabled),p=domesticPreview,r=domesticResult,providers={'1688':'1688',taobao:'淘宝 / 天猫',pinduoduo:'拼多多'};
 return `<div class="pagehead"><div><h1>国内网页采集</h1><p>从你主动打开的1688、淘宝、拼多多商品页提取公开资料，先保存来源候选，再人工确认商品入库。</p></div></div>
 <section class="panel"><h2>在自己的浏览器主动采集</h2><ol><li>下载本地扩展ZIP并解压。在Chrome或Edge的“扩展程序”开启开发者模式，选择“加载已解压的扩展程序”。</li><li>自行打开国内平台明确商品详情页。需要登录或验证码时由你自行完成，扩展不会读取或保存登录会话。</li><li>点击扩展“采集当前商品并下载JSON”，然后在此上传。只提取当前页公开JSON或可见字段，不做后台分页，不下载私有响应。</li></ol><a class="btn" href="/api/domestic-capture/extension" download>下载本地浏览器采集扩展</a><button class="btn" data-nav="channels">登记国内来源名称</button><p class="sub">${esc(s.notice||'真实国内网页兼容性尚未验证。网页售价不是采购成本，缺字段保留待确认。')}</p></section>
 <section class="panel"><h2>上传并预检来源观察</h2>${accounts.length?'':'<p>还没有启用的国内来源。先在“多渠道采集”登记1688、淘宝或拼多多来源名称，无需填写API令牌或保存网页登录信息。</p>'}<label class="field">来源账号名称<select id="domestic-account"><option value="">请选择与采集包一致的平台来源</option>${accounts.map(a=>`<option value="${esc(a.id)}">${esc(providers[a.provider]||a.provider)} · ${esc(a.name)}</option>`).join('')}</select></label><label class="field">扩展下载的JSON包<input id="domestic-file" type="file" accept=".json,application/json"></label><button class="btn primary" id="domestic-preview" ${accounts.length?'':'disabled'}>只预检，不修改商品</button><p class="sub">一次最多8MB、500条观察。拒绝含凭据字段、非平台域名、商品编号不一致的资料；图片仅记录公开引用，素材权利与使用范围仍需核对。</p></section>
 ${p?`<section class="panel" id="domestic-preview-panel"><h2>来源候选预检</h2><p>新增 ${p.counts.new} · 缺规格待补 ${p.counts.blocked} · 事实冲突 ${p.counts.conflict} · 跳过重复 ${p.counts.duplicate}</p>${domesticQualityPanel(p)}<div class="ops-scroll"><table><thead><tr><th>商品 / 真实规格</th><th>读取字段</th><th>处理方式</th></tr></thead><tbody>${p.rows.map(x=>`<tr data-domestic-quality-row="${x.row}"><td>${esc(x.raw.title_zh)}<div class="sub">商品编号 ${esc(x.raw.external_product_id)}<br>规格货号：${esc(x.raw.source_sku||'缺失，未猜测')}</div><a href="${esc(x.raw.source_url)}" target="_blank" rel="noopener noreferrer">打开来源商品页</a></td><td>网页售价：${esc(x.raw.source_price??'未提供')} CNY<br>可供数量：${esc(x.raw.stock??'未提供')}<br>采购成本：待确认<details><summary>公开字段与采集依据</summary><pre>${esc(JSON.stringify({capture:x.capture,raw:x.raw},null,2))}</pre></details></td><td>${esc(x.reason)}${domesticQualityRow(x)}${x.missing.length?`<p class="sub">缺少：${x.missing.map(esc).join('、')}</p>`:''}${domesticCorrectionFields(x)}</td></tr>`).join('')}</tbody></table></div><button class="btn" id="domestic-corrections-preview">按人工校对重新预检</button><p id="domestic-corrections-notice" class="sub">${domesticCorrectionsDirty?'校对草稿已变化，请重新预检后确认。':'人工校对不会修改价格、库存或采购成本；原始观察与校对依据分别留证。'}</p>${!r?'<label class="check"><input id="domestic-confirm" type="checkbox">我确认只将预检资料录入来源候选，缺规格保留待补，已有商品不被覆盖</label><button class="btn primary" id="domestic-apply" disabled>确认录入来源候选</button>':''}</section>`:''}
 ${r?`<section class="panel" id="domestic-result-panel"><h2>来源观察已保存</h2><p>新增候选 ${r.created.length} · 待补 ${r.blocked.length} · 冲突 ${r.conflicts.length} · 跳过重复 ${r.skipped.length}。现有商品未被覆盖。</p><button class="btn" id="domestic-product-preview" ${r.candidate_ids.length?'':'disabled'}>预览这些候选的商品入库</button><button class="btn" data-nav="channels">到来源候选处理冲突</button>${domesticProductPreview?`<p>可入库 ${domesticProductPreview.ready} 件，其他记录保持候选状态。</p><ul>${domesticProductPreview.rows.map(x=>`<li>${esc(x.title||x.id)}：${esc(x.reason)}</li>`).join('')}</ul><label class="check"><input id="domestic-product-confirm" type="checkbox">我确认按当前预览新建商品，来源售价不作为采购成本，仍需补资料和审核</label><button class="btn primary" id="domestic-product-apply" disabled>确认新建可用商品</button>`:''}</section>`:''}
 <section class="panel"><h2>本机采集包录入历史</h2><div class="ops-scroll"><table><thead><tr><th>时间</th><th>平台 / 数量</th><th>来源说明</th></tr></thead><tbody>${(s.runs||[]).map(x=>`<tr><td>${esc(date(x.created_at))}</td><td>${esc(providers[x.provider]||x.provider)} · ${x.collected}</td><td>${esc(x.message)}</td></tr>`).join('')||'<tr><td colspan="3">尚未录入来源观察。</td></tr>'}</tbody></table></div></section>`;
}
function bindDomesticCapture(){
 const on=(id,fn)=>{const e=document.getElementById(id);if(e)e.onclick=()=>perform(fn);};
 const reset=()=>{domesticCaptureGeneration++;domesticPackage=null;domesticPreview=null;domesticResult=null;domesticProductPreview=null;domesticCaptureRequest=null;domesticProductRequest=null;domesticCorrections=[];domesticCorrectionsDirty=false;document.getElementById('domestic-preview-panel')?.remove();document.getElementById('domestic-result-panel')?.remove();};
 const qualityFilter=document.getElementById('domestic-quality-filter');
 if(qualityFilter)qualityFilter.onchange=()=>{
  const visible=domesticPreview.rows.filter(r=>domesticQualityMatches(r,qualityFilter.value));
  const numbers=new Set(visible.map(r=>String(r.row)));
  document.querySelectorAll('[data-domestic-quality-row]').forEach(e=>e.hidden=!numbers.has(e.dataset.domesticQualityRow));
  document.getElementById('domestic-quality-visible').textContent=`显示 ${visible.length} / ${domesticPreview.rows.length} 条；筛选只影响显示，确认仍处理完整资料包。`;
 };
 on('domestic-quality-export',()=>{
  if(domesticCorrectionsDirty)throw Error('校对草稿已变化，请重新预检后导出诊断');
  const rows=domesticPreview.rows.filter(r=>domesticQualityMatches(r,qualityFilter.value));
  const url=URL.createObjectURL(new Blob([domesticDiagnosticCsv(rows)],{type:'text/csv;charset=utf-8'}));
  const link=document.createElement('a');link.href=url;link.download='domestic-capture-diagnostics.csv';link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
 });
 const account=document.getElementById('domestic-account'),file=document.getElementById('domestic-file');
 if(account)account.onchange=reset;if(file)file.onchange=reset;
 on('domestic-preview',async()=>{const generation=++domesticCaptureGeneration;const selected=(state.domestic_capture.accounts||[]).find(a=>a.id===account.value),f=file.files[0];if(!selected)throw Error('请选择国内来源名称');if(!f||!f.size||f.size>8*1024*1024)throw Error('请选择8MB以内的扩展JSON包');let parsed;try{parsed=JSON.parse(await f.text());}catch{throw Error('JSON包格式无效');}const body={account_id:selected.id,account_revision:selected.revision,package:parsed};const preview=await api('/api/domestic-capture/preview',body);if(generation!==domesticCaptureGeneration)throw Error('来源或文件已变化，请重新预检');domesticPackage=body;domesticPreview=preview;domesticCorrections=[];domesticCorrectionsDirty=false;domesticCaptureRequest=null;domesticProductRequest=null;domesticResult=null;domesticProductPreview=null;render();toast('预检完成，未创建商品');});
 document.querySelectorAll('[data-domestic-correction]').forEach(input=>input.oninput=()=>{
  const number=Number(input.dataset.domesticRow),row=domesticPreview.rows.find(x=>x.row===number),original=row.capture.original_raw||row.raw;
  const fields={row:number},selector=`[data-domestic-row="${number}"]`;
  document.querySelectorAll(selector).forEach(e=>{const key=e.dataset.domesticCorrection,baseline=key==='evidence'?'':original[key==='sku'?'source_sku':key]||'';if(key==='evidence'||e.value!==baseline)fields[key]=e.value;});
  domesticCorrections=domesticCorrections.filter(x=>x.row!==number);
  if(['sku','supplier','facts'].some(key=>key in fields))domesticCorrections.push(fields);
  document.getElementById('domestic-quality-export')?.setAttribute('disabled','');domesticCaptureGeneration++;domesticCorrectionsDirty=true;domesticCaptureRequest=null;domesticProductRequest=null;domesticResult=null;domesticProductPreview=null;document.getElementById('domestic-result-panel')?.remove();
  const confirm=document.getElementById('domestic-confirm');if(confirm)confirm.checked=false;
  const apply=document.getElementById('domestic-apply');if(apply)apply.disabled=true;
  document.getElementById('domestic-corrections-notice').textContent='校对草稿已变化，请按人工校对重新预检；旧确认已撤销。';
 });
 on('domestic-corrections-preview',async()=>{
  if(!domesticPackage)throw Error('请先上传采集包');const generation=++domesticCaptureGeneration;
  const body={...domesticPackage,corrections:domesticCorrections.map(x=>({...x}))};
  const preview=await api('/api/domestic-capture/preview',body);
  if(generation!==domesticCaptureGeneration)throw Error('校对草稿又发生变化，请重新预检');
  domesticPackage=body;domesticPreview=preview;domesticCorrectionsDirty=false;domesticCaptureRequest=null;domesticProductRequest=null;domesticResult=null;domesticProductPreview=null;render();toast('人工校对已预检，原始观察仍保留；请再次明确确认');
 });
 const confirmed=document.getElementById('domestic-confirm');if(confirmed)confirmed.onchange=()=>document.getElementById('domestic-apply').disabled=!confirmed.checked||domesticCorrectionsDirty;
 on('domestic-apply',async()=>{if(domesticCorrectionsDirty)throw Error('人工校对已变化，请重新预检');if(!document.getElementById('domestic-confirm').checked)throw Error('请确认只录入来源候选');domesticResult=await api('/api/domestic-capture/apply',{...domesticPackage,preview_token:domesticPreview.token,confirmed:true,request_id:domesticCaptureRequest||(domesticCaptureRequest=crypto.randomUUID())});await sync();toast('来源候选已保存，商品尚未创建');});
 on('domestic-product-preview',async()=>{domesticProductPreview=await api('/api/collection/preview',{candidate_ids:domesticResult.candidate_ids});domesticProductRequest=null;render();});
 const productConfirm=document.getElementById('domestic-product-confirm');if(productConfirm)productConfirm.onchange=()=>document.getElementById('domestic-product-apply').disabled=!productConfirm.checked||!domesticProductPreview.ready;
 on('domestic-product-apply',async()=>{if(!document.getElementById('domestic-product-confirm').checked)throw Error('请确认按预览新建商品');const result=await api('/api/collection/apply',{candidate_ids:domesticResult.candidate_ids,preview_token:domesticProductPreview.token,confirmed:true,request_id:domesticProductRequest||(domesticProductRequest=crypto.randomUUID())});domesticProductPreview=null;await sync();toast(`已按确认预览新建 ${result.created.length} 件商品，仍需补齐成本、素材和审核`);});
}
