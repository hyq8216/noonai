"""Read-only local exception observations and explicit acknowledgement history.

Acknowledging never edits, retries, pays or resolves the source business record.
"""
import csv
import io
import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from core import Problem, now
from fulfillment import csv_cell

SCHEMA_SQL='''
CREATE TABLE IF NOT EXISTS alert_marks(
 key TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, mode TEXT NOT NULL,
 until TEXT, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS alert_events(
 id INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT NOT NULL, fingerprint TEXT NOT NULL,
 mode TEXT NOT NULL, until TEXT, request_id TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS alert_requests(
 request_id TEXT PRIMARY KEY, digest TEXT NOT NULL, result TEXT NOT NULL);
'''
GROUPS=('source','automation','models','orders','purchasing','inventory','finance','backup')
PAGE_SIZE=50
ACCOUNT_DIAGNOSTIC_LIMIT=200
EXPORT_LIMIT=10000
SEVERITIES=('critical','warning','info')
MODES=('open','ack','snooze')
SORTS={
    'severity':"CASE severity WHEN 'critical' THEN 0 WHEN 'warning' THEN 1 ELSE 2 END,occurred_at DESC,key",
    'latest':'occurred_at DESC,key', 'oldest':'occurred_at ASC,key',
    'category':"category,CASE severity WHEN 'critical' THEN 0 WHEN 'warning' THEN 1 ELSE 2 END,occurred_at DESC,key",
    'overdue':"CASE WHEN deadline_at IS NULL THEN 1 ELSE 0 END,deadline_at ASC,key",
}
GUIDANCE={
    'account':('来源账号诊断发现配置或凭据问题','在来源账号页核对配置及凭据；凭据修复后重新诊断'),
    'collection':('来源采集记录失败或需要人工核对','查看采集运行证据及来源账号；核对后在来源模块决定后续处理'),
    'automation':('自动流程尚有失败、待核对或人工批准步骤','查看当前步骤与商品版本；核对事实后在自动化中心处理'),
    'model-call':('外部调用回执或费用不确定，重复发送可能重复计费','核对提供方回执及费用证据；在模型服务中对账后决定处理，不自动重发'),
    'model-quota':('今日本地次数或估算费用达到已配置上限','核对UTC日调用账本和预算配置；估算费用不等于提供方结算'),
    'order-new':('超过创建时间加24小时的本地提示阈值','核对订单有效性、采购与可用库存后，在订单模块人工安排处理'),
    'order-shipped':('超过本地发运时间加7日的提示阈值，未登记签收','核对承运商运单和签收证据；有证据后在订单模块登记'),
    'purchase':('采购单仍待收货或部分到货；未记录承诺到货日期','核对采购行待收数量与供应商交付凭证；实际到货后在采购模块登记'),
    'replenish':('按本地库存、预占、在途与待处理需求计算的预计库存低于规则','核对仓库实物、在途数量和需求；在补货建议页人工评估，不自动采购'),
    'finance-due':('已记录到期日期早于当前UTC日期，仍有未收付金额','核对凭证、实际收付款和银行证据；在财务模块登记或更正'),
    'finance-allocation':('凭证金额尚未完全分摊到订单','核对费用归属并在财务模块分摊；当前差额不能判定净利润'),
    'backup-failure':('最近完成的周期备份失败、中断或清理异常，恢复保障需核对','查看备份运行错误、磁盘空间及成功副本；人工决定重试或保留处理'),
    'backup-overdue':('超过配置备份间隔仍未登记成功副本，恢复保障需核对','保持应用打开，核对调度、磁盘及最近成功副本；未创建异地副本'),
}

def _hash(value):return hashlib.sha256(value.encode()).hexdigest()

class Alerts:
    def __init__(self,app,clock=None):
        self.app=app;self.store=app.store;self.clock=clock or now
        with self.store.connect() as c:c.executescript(SCHEMA_SQL)

    def _time(self):
        value=self.clock()
        if isinstance(value,datetime):return value.astimezone(timezone.utc)
        return datetime.fromisoformat(value.replace('Z','+00:00')).astimezone(timezone.utc)

    def _query(self,c,target=None):
        """All sources normalize in SQL; only the selected page enters Python."""
        c.create_function('alert_hash',1,_hash,deterministic=True)
        tables={r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        parts=[]
        def add(table,key,group,severity,nav,target_id,message,evidence,stamp,where='1',source=None):
            if table not in tables:return
            parts.append(f"SELECT {key} key,'{group}' category,'{severity}' severity,'{nav}' nav,{target_id} target_id,{message} message,{evidence} evidence,{stamp} occurred_at FROM {source or table} WHERE {where}")
        c.execute('CREATE TEMP TABLE IF NOT EXISTS alert_account_diagnostics(id TEXT,revision INTEGER,message TEXT,stamp TEXT)')
        c.execute('DELETE FROM alert_account_diagnostics')
        diagnosed=0;account_total=0
        if 'source_channel_accounts' in tables and getattr(self.app,'channel_accounts',None):
            account_total=c.execute('SELECT count(*) FROM source_channel_accounts WHERE enabled=1').fetchone()[0]
            if target and target.startswith('account:'):
                accounts=c.execute('SELECT id,revision,updated_at FROM source_channel_accounts WHERE id=? AND enabled=1',(target.split(':',1)[1],)).fetchall()
            else:accounts=c.execute('SELECT id,revision,updated_at FROM source_channel_accounts WHERE enabled=1 ORDER BY updated_at DESC,id LIMIT ?',(ACCOUNT_DIAGNOSTIC_LIMIT,)).fetchall()
            for row in accounts:
                diagnosed+=1
                try:
                    account=self.app.channel_accounts.get(row['id'],connection=c)
                    if account.get('status')=='attention':
                        c.execute('INSERT INTO alert_account_diagnostics VALUES(?,?,?,?)',(row['id'],row['revision'],account.get('status_detail','来源账号需要核对'),row['updated_at']))
                except Problem as exc:
                    c.execute('INSERT INTO alert_account_diagnostics VALUES(?,?,?,?)',(row['id'],row['revision'],str(exc),row['updated_at']))
        parts.append("SELECT 'account:'||id key,'source' category,'warning' severity,'channels' nav,id target_id,message,json_object('revision',revision,'diagnostic',message) evidence,stamp occurred_at FROM alert_account_diagnostics")
        add('source_collection_runs',"'collection:'||id",'source','warning','channels','id',"coalesce(nullif(message,''),'来源采集需要核对')","json_object('status',status,'account_id',account_id,'account_revision',account_revision,'message',message,'updated_at',updated_at)",'updated_at',"status IN ('failed','attention')")
        add('automation_items',"'automation:'||id",'automation','warning','automation','id',"CASE WHEN status='approval' THEN '自动流程等待人工批准' ELSE coalesce(nullif(message,''),'自动流程需要核对') END","json_object('status',status,'revision',revision,'step',step,'attempt',attempt,'run_id',run_id,'product_id',product_id,'message',message,'updated_at',updated_at)",'updated_at',"status IN ('attention','failed','approval')")
        add('model_calls',"'model-call:'||id",'models','critical','models','id',"coalesce(nullif(message,''),'模型调用结果或费用待核对；不会自动重发')","json_object('status',status,'profile_id',profile_id,'product_id',product_id,'charged_micro',charged_micro,'message',message,'updated_at',updated_at)",'updated_at',"status='uncertain'")
        if {'model_profiles','model_calls'}.issubset(tables):
            source="(SELECT p.id,p.revision,p.data,count(m.id) calls,coalesce(sum(m.charged_micro),0) spent FROM model_profiles p LEFT JOIN model_calls m ON m.profile_id=p.id AND m.created_at>=:day GROUP BY p.id)"
            add('model_profiles',"'model-quota:'||id",'models','warning','models','id',"'模型今日本地调用次数或估算预算已达上限（UTC日）'","json_object('revision',revision,'day',:day,'calls',calls,'spent_micro',spent,'daily_calls',json_extract(data,'$.daily_calls'),'daily_usd',json_extract(data,'$.daily_usd'))",':day',"json_extract(data,'$.enabled')=1 AND (calls>=json_extract(data,'$.daily_calls') OR (json_extract(data,'$.provider')!='codex-subscription' AND spent>=CAST(json_extract(data,'$.daily_usd') AS REAL)*1000000 AND calls>0))",source)
        add('ops_documents',"'order-new:'||id",'orders','warning','orders','id',"'本地待处理订单超过24小时；请核对订单处理情况（本地提示阈值）'","json_object('revision',revision,'status',json_extract(data,'$.status'),'created_at',json_extract(data,'$.created_at'),'updated_at',updated_at,'threshold_hours',24)","json_extract(data,'$.created_at')","kind='order' AND json_extract(data,'$.status')='new' AND julianday(:now)-julianday(json_extract(data,'$.created_at'))>1")
        add('ops_documents',"'order-shipped:'||id",'orders','warning','orders','id',"'本地记录发运超过7日仍未登记签收；请核对物流（本地提示阈值）'","json_object('revision',revision,'status',json_extract(data,'$.status'),'shipped_at',json_extract(data,'$.shipped_at'),'tracking',json_extract(data,'$.tracking'),'threshold_days',7)","json_extract(data,'$.shipped_at')","kind='order' AND json_extract(data,'$.status')='shipped' AND julianday(:now)-julianday(json_extract(data,'$.shipped_at'))>7")
        add('ops_documents',"'purchase:'||id",'purchasing','info','purchases','id',"'本地采购单待收货或部分到货；请核对供应商交付'","json_object('revision',revision,'status',json_extract(data,'$.status'),'warehouse_id',json_extract(data,'$.warehouse_id'),'updated_at',updated_at)",'updated_at',"kind='purchase' AND json_extract(data,'$.status') IN ('open','partial')")
        if {'ops_replenishment','ops_stock','ops_documents'}.issubset(tables):
            source='''(WITH flows AS (SELECT json_extract(d.data,'$.warehouse_id') wid,json_extract(l.value,'$.product_id') pid,
                sum(CASE WHEN d.kind='purchase' THEN json_extract(l.value,'$.quantity')-json_extract(l.value,'$.received') WHEN d.kind='transfer' THEN json_extract(l.value,'$.shipped')-json_extract(l.value,'$.received') ELSE -json_extract(l.value,'$.quantity') END) delta
                FROM ops_documents d,json_each(d.data,'$.lines') l WHERE (d.kind='purchase' AND json_extract(d.data,'$.status') IN ('open','partial')) OR (d.kind='transfer' AND json_extract(d.data,'$.status') IN ('in_transit','partial')) OR (d.kind='order' AND json_extract(d.data,'$.status')='new') GROUP BY wid,pid)
                SELECT p.product_id,p.warehouse_id,p.data,coalesce(s.on_hand,0) on_hand,coalesce(s.reserved,0) reserved,coalesce(f.delta,0) incoming_minus_demand,
                coalesce(s.on_hand-s.reserved,0)+coalesce(f.delta,0) projected FROM ops_replenishment p LEFT JOIN ops_stock s ON s.product_id=p.product_id AND s.warehouse_id=p.warehouse_id LEFT JOIN flows f ON f.pid=p.product_id AND f.wid=p.warehouse_id)'''
            add('ops_replenishment',"'replenish:'||product_id||':'||warehouse_id",'inventory','warning','warehouse','product_id',"'本地补货规则出现缺口；采购与调拨在途已纳入预估，不代表可售库存'","json_object('warehouse_id',warehouse_id,'revision',json_extract(data,'$.revision'),'on_hand',on_hand,'reserved',reserved,'incoming_minus_demand',incoming_minus_demand,'projected',projected,'minimum',json_extract(data,'$.minimum'),'target',json_extract(data,'$.target'))","json_extract(data,'$.updated_at')","json_extract(data,'$.enabled')=1 AND projected<json_extract(data,'$.minimum') AND projected<json_extract(data,'$.target')",source)
        if {'finance_entries','finance_payments'}.issubset(tables):
            source="(SELECT e.id,e.data,json_extract(e.data,'$.amount_cents')-coalesce((SELECT sum(json_extract(p.data,'$.amount_cents')) FROM finance_payments p WHERE p.entry_id=e.id AND json_extract(p.data,'$.status')='active'),0) remaining FROM finance_entries e)"
            add('finance_entries',"'finance-due:'||id",'finance','warning','finance','id',"'本地财务凭证到期仍有未收付金额；请核对收付款证据'","json_object('revision',json_extract(data,'$.revision'),'kind',json_extract(data,'$.kind'),'due_date',json_extract(data,'$.due_date'),'currency',json_extract(data,'$.currency'),'remaining_cents',remaining)","json_extract(data,'$.updated_at')","json_extract(data,'$.status')='active' AND strftime('%Y-%m-%d',json_extract(data,'$.due_date'))=json_extract(data,'$.due_date') AND json_extract(data,'$.due_date')<:day AND remaining>0",source)
            add('finance_entries',"'finance-allocation:'||id",'finance','info','finance','id',"'本地财务凭证尚未完全分摊到订单；不能据此判断利润'","json_object('revision',json_extract(data,'$.revision'),'currency',json_extract(data,'$.currency'),'amount_cents',json_extract(data,'$.amount_cents'),'allocated_cents',coalesce((SELECT sum(json_extract(value,'$.amount_cents')) FROM json_each(data,'$.allocations')),0))","json_extract(data,'$.updated_at')","json_extract(data,'$.status')='active' AND json_extract(data,'$.amount_cents')>coalesce((SELECT sum(json_extract(value,'$.amount_cents')) FROM json_each(data,'$.allocations')),0)")
        add('backup_schedule_runs',"'backup-failure:'||id",'backup','critical','backup-schedules','id',"coalesce(nullif(error,''),nullif(cleanup_error,''),'周期备份需要核对')","json_object('status',status,'error',error,'cleanup_error',cleanup_error,'finished_at',finished_at)","coalesce(finished_at,started_at)","rowid=(SELECT max(rowid) FROM backup_schedule_runs WHERE status!='running') AND (status IN ('failed','interrupted') OR cleanup_error!='')")
        if {'backup_schedule_config','backup_schedule_runs'}.issubset(tables):
            source="(SELECT c.*,coalesce((SELECT max(finished_at) FROM backup_schedule_runs WHERE status='success'),nullif(c.updated_at,'')) last_success FROM backup_schedule_config c)"
            add('backup_schedule_config',"'backup-overdue:local'",'backup','critical','backup-schedules',"'1'","'已启用周期备份，但超过配置间隔未登记成功副本；需保持应用打开并核对磁盘'","json_object('version',version,'interval_minutes',interval_minutes,'last_success',last_success)",'last_success',"enabled=1 AND last_success IS NOT NULL AND (julianday(:now)-julianday(last_success))*1440>interval_minutes",source)
        sql='WITH observations AS ('+' UNION ALL '.join(parts)+'''), observed AS (SELECT *,alert_hash(key||'|'||evidence) fingerprint,
            CASE WHEN key LIKE 'order-new:%' THEN strftime('%Y-%m-%dT%H:%M:%SZ',json_extract(evidence,'$.created_at'),'+24 hours')
            WHEN key LIKE 'order-shipped:%' THEN strftime('%Y-%m-%dT%H:%M:%SZ',json_extract(evidence,'$.shipped_at'),'+7 days')
            WHEN key LIKE 'finance-due:%' THEN strftime('%Y-%m-%dT%H:%M:%SZ',json_extract(evidence,'$.due_date'),'+1 day')
            WHEN key LIKE 'backup-overdue:%' THEN strftime('%Y-%m-%dT%H:%M:%SZ',json_extract(evidence,'$.last_success'),'+'||json_extract(evidence,'$.interval_minutes')||' minutes') END deadline_at
            FROM observations), current_alerts AS (
            SELECT o.*,CASE WHEN m.fingerprint=o.fingerprint AND m.mode='ack' THEN 'ack'
            WHEN m.fingerprint=o.fingerprint AND m.mode='snooze' AND julianday(m.until)>julianday(:now) THEN 'snooze' ELSE 'open' END mode,
            CASE WHEN m.fingerprint=o.fingerprint THEN m.until END until FROM observed o LEFT JOIN alert_marks m ON m.key=o.key)
            '''
        timestamp=self._time().isoformat()
        return sql,{'now':timestamp,'day':timestamp[:10]},{'checked':diagnosed,'total_enabled':account_total,'limit':ACCOUNT_DIAGNOSTIC_LIMIT,'limited':account_total>diagnosed}

    def _filters(self,page=0,group='all',severity='all',sort='severity',mode='all'):
        if isinstance(page,bool) or not str(page).isdecimal() or int(page)>100000:raise Problem('异常中心页码无效')
        if group not in ('all',)+GROUPS:raise Problem('异常分类无效')
        if severity not in ('all',)+SEVERITIES:raise Problem('异常严重度无效')
        if mode not in ('all',)+MODES:raise Problem('异常提醒状态无效')
        if sort not in SORTS:raise Problem('异常排序无效')
        return {'page':int(page),'group':group,'severity':severity,'sort':sort,'mode':mode}

    def _record(self,row,timestamp):
        record=dict(row);record['group']=record.pop('category');record['evidence']=json.loads(record['evidence'])
        reason,action=GUIDANCE[record['key'].split(':',1)[0]]
        record['priority_reason']=reason;record['suggested_action']=action
        record['deadline_basis']='recorded_due_date' if record['key'].startswith('finance-due:') else 'local_threshold' if record['deadline_at'] else None
        record['overdue_seconds']=None
        if record['deadline_at']:
            deadline=datetime.fromisoformat(record['deadline_at'].replace('Z','+00:00'))
            record['overdue_seconds']=max(0,int((datetime.fromisoformat(timestamp)-deadline).total_seconds()))
        return record

    def state(self,page=0,group='all',severity='all',sort='severity',mode='all'):
        filters=self._filters(page,group,severity,sort,mode);page=filters['page']
        with self.store.connect() as c:
            c.execute('BEGIN');sql,params,coverage=self._query(c);params.update(filters)
            counts={g:{'total':0,'open':0,'ack':0,'snooze':0} for g in GROUPS}
            for r in c.execute(sql+'SELECT category,mode,count(*) n FROM current_alerts GROUP BY category,mode',params):
                counts[r['category']][r['mode']]=r['n'];counts[r['category']]['total']+=r['n']
            severity_counts={s:0 for s in SEVERITIES};mode_counts={m:0 for m in MODES}
            for r in c.execute(sql+"SELECT severity,mode,count(*) n FROM current_alerts WHERE (:group='all' OR category=:group) GROUP BY severity,mode",params):
                severity_counts[r['severity']]+=r['n'];mode_counts[r['mode']]+=r['n']
            where=" WHERE (:group='all' OR category=:group) AND (:severity='all' OR severity=:severity) AND (:mode='all' OR mode=:mode)"
            total=c.execute(sql+'SELECT count(*) FROM current_alerts'+where,params).fetchone()[0]
            page=min(page,max(0,(total-1)//PAGE_SIZE));filters['page']=page
            rows=c.execute(sql+'SELECT * FROM current_alerts'+where+' ORDER BY '+SORTS[sort]+' LIMIT :limit OFFSET :offset',{**params,'limit':PAGE_SIZE,'offset':page*PAGE_SIZE}).fetchall()
            result=[self._record(row,params['now']) for row in rows]
            events=[dict(r) for r in c.execute('SELECT * FROM alert_events ORDER BY id DESC LIMIT 20')]
        return {'rows':result,**filters,'page_size':PAGE_SIZE,'total':total,'pages':max(1,(total+PAGE_SIZE-1)//PAGE_SIZE),'counts':counts,'severity_counts':severity_counts,'mode_counts':mode_counts,'facet_scope':'current_group_before_severity_and_mode','account_diagnostics':coverage,'events':events,'observed_at':params['now'],'local_only':True,
                'notice':'本地异常提示；24小时待处理、7日未登记签收为本地提示阈值，并非平台SLA。财务到期按UTC日期核对。确认已阅或稍后提醒不会解决业务异常、重试请求或发送外部消息。'}

    def export(self,params=None):
        params=params or {};filters=self._filters(0,params.get('group','all'),params.get('severity','all'),params.get('sort','severity'),params.get('mode','all'))
        with self.store.connect() as c:
            c.execute('BEGIN');sql,query,_=self._query(c);query.update(filters)
            where=" WHERE (:group='all' OR category=:group) AND (:severity='all' OR severity=:severity) AND (:mode='all' OR mode=:mode)"
            total=c.execute(sql+'SELECT count(*) FROM current_alerts'+where,query).fetchone()[0]
            if total>EXPORT_LIMIT:raise Problem('异常导出最多10000条，请缩小分类、严重度或提醒状态筛选；未导出部分结果')
            stream=io.StringIO(newline='');writer=csv.writer(stream)
            writer.writerow(['异常编号','分类','严重度','提醒状态','来源编号','异常说明','优先级依据','建议动作','本地阈值或到期核对时间UTC','逾期秒数','发生时间','本地依据JSON','观察时间UTC','范围说明'])
            for row in c.execute(sql+'SELECT * FROM current_alerts'+where+' ORDER BY '+SORTS[filters['sort']],query):
                r=self._record(row,query['now'])
                writer.writerow([csv_cell(v) for v in [r['key'],r['group'],r['severity'],r['mode'],r['target_id'],r['message'],r['priority_reason'],r['suggested_action'],r['deadline_at'],r['overdue_seconds'],r['occurred_at'],json.dumps(r['evidence'],ensure_ascii=False),query['now'],'仅本地异常观察；已阅不代表解决；账号诊断最多200个；不发送外部消息']])
        return ('\ufeff'+stream.getvalue()).encode('utf-8')

    def action(self,body):
        if not isinstance(body,dict):raise Problem('异常操作格式无效')
        request=body.get('request_id');key=body.get('key');fp=body.get('fingerprint');mode=body.get('mode')
        if not isinstance(request,str) or not re.fullmatch(r'[A-Za-z0-9_-]{8,100}',request):raise Problem('异常操作请求编号无效')
        if not isinstance(key,str) or not 1<=len(key)<=250 or not isinstance(fp,str) or not re.fullmatch('[a-f0-9]{64}',fp):raise Problem('请刷新后选择当前异常')
        digest=_hash(json.dumps(body,sort_keys=True,ensure_ascii=False,separators=(',',':')))
        with self.app.write_lock,self.store.connect() as c:
            c.execute('BEGIN IMMEDIATE');old=c.execute('SELECT * FROM alert_requests WHERE request_id=?',(request,)).fetchone()
            if old:
                if old['digest']!=digest:raise Problem('同一请求编号不能用于不同异常操作',409)
                return {**json.loads(old['result']),'replayed':True}
            if body.get('confirmed') is not True:raise Problem('请明确确认仅修改异常提醒状态，不执行业务操作')
            if mode not in ('ack','snooze','reopen'):raise Problem('异常提醒操作无效')
            current=self._time();until=None
            if mode=='snooze':
                try:
                    raw=body.get('until');parsed=datetime.fromisoformat(raw.replace('Z','+00:00'))
                    if parsed.tzinfo is None:raise ValueError()
                    parsed=parsed.astimezone(timezone.utc)
                except (AttributeError,TypeError,ValueError):raise Problem('稍后提醒时间须含时区')
                if not current<parsed<=current+timedelta(days=7):raise Problem('稍后提醒须在未来7天以内')
                until=parsed.isoformat()
            sql,params,_=self._query(c,key)
            row=c.execute(sql+'SELECT fingerprint FROM current_alerts WHERE key=:key',{**params,'key':key}).fetchone()
            if not row or row['fingerprint']!=fp:raise Problem('异常来源已变化或已消失，请刷新后核对',409)
            stored='open' if mode=='reopen' else mode;stamp=current.isoformat()
            c.execute('INSERT INTO alert_marks VALUES(?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET fingerprint=excluded.fingerprint,mode=excluded.mode,until=excluded.until,updated_at=excluded.updated_at',(key,fp,stored,until,stamp))
            c.execute('INSERT INTO alert_events(key,fingerprint,mode,until,request_id,created_at) VALUES(?,?,?,?,?,?)',(key,fp,mode,until,request,stamp))
            result={'key':key,'fingerprint':fp,'mode':stored,'until':until,'replayed':False,'message':'仅更新本地提醒状态；业务异常仍需在来源模块核对'}
            c.execute('INSERT INTO alert_requests VALUES(?,?,?)',(request,digest,json.dumps(result,ensure_ascii=False)))
            return result
