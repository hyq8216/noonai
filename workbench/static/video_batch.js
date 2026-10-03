'use strict';
let videoBatchOpen=false,videoBatchPreview=null,videoBatchRequest=null;
let videoBatchSettings={aspect:'square',seconds:3,motion:'alternate',transition:'fade',background:'#ffffff'};
function resetVideoBatch(){videoBatchPreview=null;videoBatchRequest=null}
function videoBatchPanel(){
 if(!videoBatchOpen)return '';
 if(videoBatchRequest&&JSON.stringify([...chosen].sort())!==JSON.stringify(videoBatchRequest.product_ids))resetVideoBatch();
 const p=videoBatchPreview;
 return `<section class="panel section-break"><h2>每件商品制作一个视频</h2><p>使用商品图片栏中已验收的图片，按现有顺序分别制作。每批最多50件，不同商品不会混图；相同图片与配方已有视频时保留成品。照片运动视频不生成商品动作。</p><form id="video-batch-form"><div class="grid3"><div class="field"><label for="vb-aspect">画幅</label><select id="vb-aspect" name="aspect"><option value="square">方形 1080×1080</option><option value="portrait">竖版 1080×1920</option><option value="landscape">横版 1920×1080</option></select></div><div class="field"><label for="vb-motion">镜头</label><select id="vb-motion" name="motion"><option value="none">静止</option><option value="push">推进</option><option value="pull">拉远</option><option value="alternate">交替推进与拉远</option></select></div><div class="field"><label for="vb-transition">转场</label><select id="vb-transition" name="transition"><option value="cut">直接切换</option><option value="fade">叠化0.4秒</option></select></div>${field('seconds','每张展示秒数',videoBatchSettings.seconds,{number:true})}<div class="field"><label for="vb-background">背景</label><input type="color" name="background" id="vb-background" value="${esc(videoBatchSettings.background)}"></div></div><div class="actions"><button class="btn primary" ${!chosen.size?'disabled':''}>预检 ${chosen.size} 件商品</button><button type="button" class="btn" id="vb-close">收起</button></div></form>${p?`<p class="section-break">可新建 ${p.ready} 个视频 · 队列 ${p.queued} / ${p.queue_limit}。尚未开始编码。</p><div class="table-scroll"><table><thead><tr><th>商品</th><th>图片 / 预计时长</th><th>处理安排</th></tr></thead><tbody>${p.rows.map(r=>`<tr><td><button class="product-name" data-open="${r.id}">${esc(r.title)}</button><p>${esc(r.sku)}</p></td><td>${r.images} 张 · ${r.seconds} 秒</td><td>${esc({ready:'可制作',kept:'保留已有视频',active:'已在队列',blocked:'待补资料',capacity:'等待队列空位'}[r.status])}<p>${r.reasons.map(esc).join('；')}</p></td></tr>`).join('')}</tbody></table></div><label class="check section-break"><input type="checkbox" id="vb-confirm"><span>确认使用这些已验收图片分别制作视频，成片仍需检查后使用</span></label><button class="btn primary section-break" id="vb-start" disabled>制作 ${p.ready} 个视频</button>`:''}</section>`;
}
function bindVideoBatch(){
 if(view!=='batch')return;
 const open=document.getElementById('video-batch-open');if(open)open.onclick=()=>{videoBatchOpen=!videoBatchOpen;render()};
 const form=document.getElementById('video-batch-form');if(!form)return;
 restoreMediaForm(form,videoBatchSettings);form.onchange=()=>{if(busy){restoreMediaForm(form,videoBatchSettings);return}videoBatchSettings=Object.fromEntries(new FormData(form));resetVideoBatch();render()};
 document.getElementById('vb-close').onclick=()=>{videoBatchOpen=false;render()};
 form.onsubmit=e=>{e.preventDefault();perform(async()=>{videoBatchSettings=Object.fromEntries(new FormData(form));const b={product_ids:[...chosen].sort(),...videoBatchSettings};videoBatchPreview=await api('/api/media/video-batch-preview',b);videoBatchRequest={...b,preview_token:videoBatchPreview.token,request_id:crypto.randomUUID()};render()})};
 const confirm=document.getElementById('vb-confirm');if(confirm)confirm.onchange=()=>document.getElementById('vb-start').disabled=!confirm.checked||!videoBatchPreview.ready;
 const start=document.getElementById('vb-start');if(start)start.onclick=()=>perform(async()=>{const r=await api('/api/media/video-batch-apply',{...videoBatchRequest,confirmed:true});resetVideoBatch();await sync();toast(`已安排 ${r.task_ids.length} 个视频，可在图片与视频查看进度`)});
}
