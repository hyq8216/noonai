'use strict';
let recoveryPreview=null;
const backupCounts=c=>`商品 ${c.products} · 历史版本 ${c.revisions} · 媒体 ${c.media_assets} · 业务单据 ${c.ops_documents} · 财务凭证 ${c.finance_entries}`;
const backupSize=n=>n<1048576?(n/1024).toFixed(1)+' KB':(n/1048576).toFixed(1)+' MB';
function recoveryPage(){
 const s=state.recovery||{},pending=s.pending,last=s.last_restore;
 return `<div class="pagehead"><div><h1>备份与恢复</h1><p>保存完整经营资料，并在需要时恢复到某个备份时间点。</p></div></div>
 ${pending?`<section class="panel"><h2>已安排恢复 · 等待重新打开应用</h2><p>将恢复 ${esc(date(pending.created_at))} 的资料。当前业务修改已锁定，后台队列已暂停。请先退出 Noon Studio，再重新打开。</p><button class="btn" id="cancel-restore">取消本次恢复</button><p class="sub">取消后队列仍保持暂停，需要你手动恢复。</p></section>`:''}
 ${last?`<section class="panel"><h2>${last.status==='restored'?'最近一次恢复完成':last.status==='rolled_back'?'上次恢复已回退':'上次恢复未完成'}</h2><p>${esc(last.message)}</p><p class="sub">${esc(date(last.finished_at))}</p>${last.rollback_archive_id?`<a class="btn" href="/api/backup/download/${esc(last.rollback_archive_id)}" download>下载恢复前的回退备份</a>`:''}</section>`:''}
 <section class="panel"><h2>创建完整备份</h2><p>包含商品及历史版本、引用的原图与成品、采购、订单、库存、财务、流程记录和模型配置。保存的是一致的资料快照，未完成任务的中间文件不包含在内。</p><p>登录令牌、API 密钥、本地服务配置、日志不进入备份。备份包含经营资料且未加密，请保存在你信任的位置。导出到另一块磁盘，可避免本机故障时一并丢失。</p><button class="btn primary" id="create-backup" ${pending?'disabled':''}>创建并校验备份</button><p class="sub">当前支持压缩后不超过 2GB、展开后不超过 8GB。备份完成后可在下方下载。</p></section>
 <section class="panel"><h2>已有备份</h2><div class="table-scroll"><table><thead><tr><th>创建时间 / 内容</th><th>大小 / 来源</th><th>保存到其他位置</th></tr></thead><tbody>${(s.archives||[]).map(a=>`<tr><td>${esc(date(a.created_at))}<div class="sub">${esc(backupCounts(a.counts))}</div></td><td>${backupSize(a.bytes)}<div class="sub">${a.label==='before-restore'?'恢复前自动回退副本':'手动备份'}</div></td><td><a class="btn small" href="/api/backup/download/${esc(a.id)}" download>下载 ZIP</a></td></tr>`).join('')||'<tr><td colspan="3">还没有备份。先创建一份，再下载保存。</td></tr>'}</tbody></table></div><p class="sub backup-path">本机副本：${esc(s.directory||'')}</p></section>
 <section class="panel"><h2>检查并恢复备份</h2><p>恢复会替换当前商品、媒体和所有经营台账；不会合并两份资料。系统会先创建当前资料的回退备份，检查失败则不切换。</p><label class="field">选择 Noon Studio 备份 ZIP<input id="backup-file" type="file" accept=".zip,application/zip" ${pending?'disabled':''}></label><button class="btn" id="inspect-backup" ${pending?'disabled':''}>检查文件和预览内容</button>
 ${recoveryPreview&&!pending?`<div class="backup-preview"><h3>备份检查通过</h3><p>备份时间：${esc(date(recoveryPreview.created_at))} · 版本 ${esc(recoveryPreview.app_version)}</p><p>${esc(backupCounts(recoveryPreview.counts))}</p><p>已检查文件校验值、数据库结构与完整性、素材引用。恢复只改变本地记录，不能撤销真实下单、发货、付款或平台提交。请在恢复后核对这些外部结果。</p><label class="check"><input type="checkbox" id="restore-confirm">我确认替换当前资料。恢复后队列暂停、模型停用、商品上架审核重置；连接凭证需重新核对。</label><button class="btn" id="schedule-restore">安排下次启动恢复</button></div>`:''}
 <p class="sub">仅接受与当前数据结构兼容的完整备份。处理中任务需要先结束；检查备份不会修改业务资料。</p></section>`;
}
function bindRecovery(){
 const on=(id,fn)=>{const e=document.getElementById(id);if(e)e.onclick=()=>perform(fn)};
 on('create-backup',async()=>{toast('正在创建并校验完整备份，请保持应用打开…');await api('/api/backup/create',{});await sync();toast('备份已创建并校验，可下载到其他位置')});
 on('inspect-backup',async()=>{recoveryPreview=null;const f=document.getElementById('backup-file').files[0];if(!f)throw Error('请先选择备份 ZIP 文件');if(f.size>state.recovery.max_archive_bytes)throw Error('备份文件超过2GB');toast('正在上传并检查备份，当前业务资料不会改变…');const r=await fetch('/api/backup/inspect',{method:'POST',headers:{'Content-Type':'application/octet-stream','X-Workbench-Token':state.token},body:f});const data=await r.json();if(!r.ok)throw Error(data.error||'备份检查失败');recoveryPreview=data;render();toast('备份检查通过，请核对时间和资料数量')});
 on('schedule-restore',async()=>{if(!document.getElementById('restore-confirm').checked)throw Error('请先确认替换资料和恢复后的暂停规则');if(!recoveryPreview)throw Error('请重新检查备份');await api('/api/backup/schedule',{id:recoveryPreview.id,sha256:recoveryPreview.sha256,confirmed:true});recoveryPreview=null;await sync();toast('已安排恢复。请退出并重新打开应用')});
 on('cancel-restore',async()=>{await api('/api/backup/cancel',{});await sync();toast('已取消恢复，队列保持暂停')});
 const f=document.getElementById('backup-file');if(f)f.onchange=()=>{recoveryPreview=null;document.querySelector('.backup-preview')?.remove()};
}
