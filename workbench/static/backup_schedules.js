'use strict';
function backupSchedulesQuery(){return new URLSearchParams({});}
function backupSchedulesPage(){
 const s=state.backup_schedules||{}, pending=!!state.recovery?.pending, latest=s.latest;
 const status={success:'已完成并校验',failed:'备份失败',interrupted:'执行中断',running:'正在备份'};
 return `<div class="pagehead"><div><h1>周期备份</h1><p>应用打开时定期保存完整经营资料。仅保存到本机，不上传到远端。</p></div></div>
 ${s.scheduler_error?`<section class="panel"><p role="alert">周期调度未完成：${esc(s.scheduler_error)}。系统将在一分钟后再次检查，请核对本机资料和磁盘。</p></section>`:''}
 ${pending?'<section class="panel"><h2>恢复等待中</h2><p>周期备份暂不执行。恢复后规则停用，核对资料后再明确启用。</p></section>':''}
 <section class="panel"><h2>本地备份规则 · ${s.enabled?'已启用':'已停用'}</h2><p>包含数据库和引用素材，不包含登录令牌、API密钥与日志。应用关闭期间不执行，重启后顺延一个周期，不补跑积压备份。</p>
 <label class="check"><input id="backup-rule-enabled" type="checkbox" ${s.enabled?'checked':''} ${pending?'disabled':''}>启用周期备份</label>
 <label class="field">间隔（分钟，至少60）<input id="backup-rule-interval" type="number" min="60" max="525600" value="${esc(s.interval_minutes||1440)}" ${pending?'disabled':''}></label>
 <label class="field">保留周期副本（1–30份）<input id="backup-rule-retain" type="number" min="1" max="30" value="${esc(s.retain_count||7)}" ${pending?'disabled':''}></label>
 <p>下次运行：${s.next_run_at?esc(date(s.next_run_at)):'未安排'}。每次成功后，只清理由本规则登记且校验一致的旧周期ZIP；手动备份、恢复前回退副本与未登记文件均保留。</p>
 <label class="check"><input id="backup-rule-confirm" type="checkbox" ${pending?'disabled':''}>我确认启用本地周期备份，并按保留数量清理旧周期副本</label><button class="btn primary" id="backup-rule-save" ${pending?'disabled':''}>保存规则</button>
 <p class="sub backup-path">本机目录：${esc(s.directory||'')}。本机故障可能同时丢失资料和备份，请在“备份与恢复”下载并自行保存到其他磁盘。</p></section>
 <section class="panel"><h2>立即执行本规则</h2><p>即使规则停用，也可手动创建一份规则副本，成功后按当前保留数量清理旧规则副本。</p><label class="check"><input id="backup-run-confirm" type="checkbox" ${pending?'disabled':''}>我确认创建完整本地备份，并清理超出保留数量的旧周期副本</label><button class="btn" id="backup-rule-run" ${pending||s.running?'disabled':''}>${s.running?'正在备份…':'立即备份并校验'}</button>
 ${latest?`<p>最近执行：${esc(date(latest.started_at))} · ${esc(status[latest.status]||latest.status)}</p>${latest.error?`<p role="alert">${esc(latest.error)}。下个周期才会再次执行，可核对问题后手动重试。</p>`:''}${latest.cleanup_error?`<p role="alert">备份已保存，但旧副本清理未完成：${esc(latest.cleanup_error)}</p>`:''}`:'<p>还没有周期执行记录。</p>'}</section>
 <section class="panel"><h2>执行历史（最近50次）</h2><div class="table-scroll"><table><thead><tr><th>时间 / 来源</th><th>结果</th><th>副本</th></tr></thead><tbody>${(s.history||[]).map(r=>`<tr><td>${esc(date(r.started_at))}<div class="sub">${r.trigger==='scheduled'?'周期触发':'手动触发规则'}</div></td><td>${esc(status[r.status]||r.status)}${r.error?`<div class="sub">${esc(r.error)}</div>`:''}${r.cleanup_error?`<div class="sub">${esc(r.cleanup_error)}</div>`:''}</td><td>${r.deleted_at?'已按保留规则清理ZIP，历史保留':r.archive_id?`<a class="btn small" href="/api/backup/download/${esc(r.archive_id)}" download>下载 ZIP</a>`:'未登记完成副本'}</td></tr>`).join('')||'<tr><td colspan="3">暂无记录</td></tr>'}</tbody></table></div></section>`;
}
function bindBackupSchedules(){
 const on=(id,fn)=>{const e=document.getElementById(id);if(e)e.onclick=()=>perform(fn);};
 const requestId=()=>crypto.randomUUID();
 on('backup-rule-save',async()=>{const enabled=document.getElementById('backup-rule-enabled').checked, confirmed=document.getElementById('backup-rule-confirm').checked;if(enabled&&!confirmed)throw Error('请先明确确认启用及保留清理规则');await api('/api/backup-schedules/save',{request_id:requestId(),version:state.backup_schedules.version,enabled,interval_minutes:Number(document.getElementById('backup-rule-interval').value),retain_count:Number(document.getElementById('backup-rule-retain').value),confirmed});await sync();toast('本地周期备份规则已保存');});
 on('backup-rule-run',async()=>{if(!document.getElementById('backup-run-confirm').checked)throw Error('请先确认备份及旧周期副本清理');toast('正在创建并校验本地备份…');const result=await api('/api/backup-schedules/run',{request_id:requestId(),version:state.backup_schedules.version,confirmed:true});await sync();if(result.status!=='success')throw Error(result.error||'备份未完成，请查看执行历史');toast(result.cleanup_error?'备份已保存，旧副本清理需要核对':'完整本地备份已完成并校验');});
}
