"""Read-only, period-aware operating reports; no inferred revenue or profit."""
import csv
import io
import json
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP

from core import Problem, now
from finance import day, apportioned, fingerprint


class Analytics:
    PAGE_SIZE = 50

    def __init__(self, app):
        self.store = app.store

    def filters(self, body=None, page=0):
        if body is None:
            body = {}
        if not isinstance(body, dict):
            raise Problem('报表筛选格式无效')
        today = date.fromisoformat(now()[:10])
        start = day(body.get('from') or (today-timedelta(days=29)).isoformat(), '开始日期')
        end = day(body.get('to') or today.isoformat(), '结束日期')
        span = (date.fromisoformat(end)-date.fromisoformat(start)).days
        if not 0 <= span < 366:
            raise Problem('报表范围须为1至366日，开始日期不能晚于结束日期')
        page = body.get('page', page)
        if isinstance(page, bool) or not str(page).isdecimal() or not 0 <= int(page) <= 1000000:
            raise Problem('报表页码无效')
        result = {'from': start, 'to': end, 'page': int(page)}
        for key in ('shop_id', 'warehouse_id'):
            value = body.get(key) or ''
            if not isinstance(value, str) or len(value) > 100:
                raise Problem('报表店铺或仓库筛选无效')
            result[key] = value
        return result

    def state(self, body=None, page=0):
        f = self.filters(body, page)
        with self.store.connect() as c:
            c.execute('BEGIN')
            entities = [dict(r) for r in c.execute("SELECT id,kind,name FROM ops_entities WHERE kind IN ('shop','warehouse') ORDER BY kind,name")]
            for key, kind in (('shop_id', 'shop'), ('warehouse_id', 'warehouse')):
                if f[key] and not any(e['id'] == f[key] and e['kind'] == kind for e in entities):
                    raise Problem('筛选店铺或仓库不存在', 404)
            where = "d.kind='order'"
            params = []
            for key in ('shop_id', 'warehouse_id'):
                if f[key]:
                    where += " AND json_extract(d.data,'$.%s')=?" % key
                    params.append(f[key])
            # Dates are inclusive UTC calendar days. Orders use creation, shipments
            # their own event date, returns their own recorded event date.
            cte = """WITH scoped AS (SELECT d.* FROM ops_documents d WHERE %s),
            period AS (SELECT * FROM scoped WHERE substr(json_extract(data,'$.created_at'),1,10) BETWEEN ? AND ?),
            sold AS (SELECT json_extract(l.value,'$.product_id') pid,
                sum(json_extract(l.value,'$.quantity')) ordered_units FROM period d,json_each(d.data,'$.lines') l
                WHERE json_extract(d.data,'$.status')!='cancelled' GROUP BY pid),
            shipped AS (SELECT json_extract(l.value,'$.product_id') pid,
                sum(json_extract(l.value,'$.quantity')) shipped_units FROM scoped d,json_each(d.data,'$.lines') l
                WHERE json_extract(d.data,'$.status') IN ('shipped','delivered')
                AND substr(json_extract(d.data,'$.shipped_at'),1,10) BETWEEN ? AND ? GROUP BY pid),
            returned AS (SELECT json_extract(l.value,'$.product_id') pid,
                sum(json_extract(l.value,'$.quantity')) returned_units FROM scoped d,
                json_each(d.data,'$.returns') r,json_each(r.value,'$.lines') l
                WHERE substr(json_extract(r.value,'$.at'),1,10) BETWEEN ? AND ? GROUP BY pid)
            """ % where
            period_params = params + [f['from'], f['to']]*3
            statuses = {s: 0 for s in ('new','reserved','shipped','delivered','cancelled')}
            for r in c.execute(cte+"SELECT json_extract(data,'$.status') status,count(*) n FROM period GROUP BY status", period_params):
                statuses[r['status']] = r['n']
            currencies = [dict(r) for r in c.execute(cte+"""SELECT json_extract(data,'$.currency') currency,count(*) orders,
                sum(json_extract(data,'$.total_cents')) amount_cents FROM period
                WHERE json_extract(data,'$.status')!='cancelled' GROUP BY currency ORDER BY currency""", period_params)]
            returned_orders = c.execute(cte+"""SELECT count(DISTINCT d.id) FROM scoped d,json_each(d.data,'$.returns') r
                WHERE substr(json_extract(r.value,'$.at'),1,10) BETWEEN ? AND ?""", period_params+[f['from'],f['to']]).fetchone()[0]
            totals = c.execute(cte+"SELECT (SELECT coalesce(sum(ordered_units),0) FROM sold) ordered_units,(SELECT coalesce(sum(shipped_units),0) FROM shipped) shipped_units,(SELECT coalesce(sum(returned_units),0) FROM returned) returned_units", period_params).fetchone()
            orders = {'total': sum(statuses.values()), 'statuses': statuses, 'returned_orders': returned_orders, **dict(totals)}
            purchase_where = "d.kind='purchase' AND substr(json_extract(d.data,'$.created_at'),1,10) BETWEEN ? AND ?"
            pp = [f['from'], f['to']]
            if f['warehouse_id']:
                purchase_where += " AND json_extract(d.data,'$.warehouse_id')=?"; pp.append(f['warehouse_id'])
            if f['shop_id']:
                purchase_where += ' AND 0'  # Purchases have no shop ownership.
            purchases = dict(c.execute("""SELECT count(DISTINCT d.id) documents,
                coalesce(sum(json_extract(l.value,'$.quantity')),0) ordered_units,
                coalesce(sum(json_extract(l.value,'$.received')),0) received_units,
                coalesce(sum(CASE WHEN json_extract(d.data,'$.status') IN ('open','partial') THEN
                json_extract(l.value,'$.quantity')-json_extract(l.value,'$.received') ELSE 0 END),0) in_transit_units
                FROM ops_documents d,json_each(d.data,'$.lines') l WHERE """+purchase_where, pp).fetchone())
            purchases['shop_attributable'] = not bool(f['shop_id'])
            sw = '1'; sp = []
            if f['warehouse_id']:
                sw += ' AND s.warehouse_id=?'; sp.append(f['warehouse_id'])
            if f['shop_id']:
                sw += " AND EXISTS(SELECT 1 FROM scoped d,json_each(d.data,'$.lines') l WHERE json_extract(l.value,'$.product_id')=s.product_id)"
            stock_cte = cte+""", stock AS (SELECT s.product_id pid,sum(s.on_hand) on_hand,sum(s.reserved) reserved,
                sum(s.on_hand-s.reserved) available FROM ops_stock s WHERE """+sw+""" GROUP BY s.product_id), ids AS (SELECT pid FROM stock UNION SELECT pid FROM sold UNION SELECT pid FROM shipped UNION SELECT pid FROM returned), report AS (SELECT ids.pid product_id,
                coalesce(json_extract(p.data,'$.partner_sku'),ids.pid) sku,coalesce(json_extract(p.data,'$.title_zh'),'商品档案缺失') title,
                json_extract(p.data,'$.cost_cny') cost_cny,coalesce(stock.on_hand,0) on_hand,coalesce(stock.reserved,0) reserved,
                coalesce(stock.available,0) available,coalesce(sold.ordered_units,0) ordered_units,
                coalesce(shipped.shipped_units,0) shipped_units,coalesce(returned.returned_units,0) returned_units
                FROM ids LEFT JOIN products p ON p.id=ids.pid LEFT JOIN stock ON stock.pid=ids.pid
                LEFT JOIN sold ON sold.pid=ids.pid LEFT JOIN shipped ON shipped.pid=ids.pid LEFT JOIN returned ON returned.pid=ids.pid) """
            sku_params = period_params+sp
            inventory = dict(c.execute(stock_cte+"""SELECT coalesce(sum(on_hand),0) on_hand,coalesce(sum(reserved),0) reserved,
                coalesce(sum(available),0) available,coalesce(sum(CASE WHEN on_hand>0 AND cost_cny IS NULL THEN 1 ELSE 0 END),0) unknown_cost_skus,
                coalesce(sum(CASE WHEN cost_cny IS NULL THEN on_hand ELSE 0 END),0) unknown_cost_units,
                coalesce(sum(CASE WHEN cost_cny IS NOT NULL THEN round(cost_cny*100)*on_hand ELSE 0 END),0) estimated_value_cents,
                coalesce(sum(CASE WHEN available<=0 AND (on_hand>0 OR ordered_units>0 OR shipped_units>0) THEN 1 ELSE 0 END),0) stockout_skus FROM report""", sku_params).fetchone())
            total = c.execute(stock_cte+'SELECT count(*) FROM report', sku_params).fetchone()[0]
            pages = max(1,(total+49)//50); f['page'] = min(f['page'],pages-1)
            items = [dict(r) for r in c.execute(stock_cte+'SELECT * FROM report ORDER BY shipped_units DESC,sku,product_id LIMIT 50 OFFSET ?', sku_params+[f['page']*50])]
            for item in items:
                item['stock_warning'] = '缺货 / 无可用库存' if item['available']<=0 else ('可用库存低于本期发货数' if item['available']<item['shipped_units'] else '')
                item['risk_flags'] = self._sku_risks(item, f)
                item['risk_evidence'] = f"UTC {f['from']} 至 {f['to']}；库存为当前快照，非库龄或历史周转"
                item['estimated_value_cents'] = None if item['cost_cny'] is None else int((Decimal(str(item['cost_cny']))*100).quantize(Decimal('1'),rounding=ROUND_HALF_UP))*item['on_hand']
            finance = self._finance(c, f, where, params)
            trends = self._trends(c, f, where, params)
            return {'filters':f,'entities':entities,'orders':orders,'order_currencies':currencies,'purchases':purchases,
                    'inventory':inventory,'finance':finance,'trends':trends,'sku_page':{'page':f['page'],'pages':pages,'total':total,'page_size':50,'items':items},
                    'generated_at':now(),'empty':not (orders['total'] or orders['shipped_units'] or orders['returned_units'] or purchases['documents'] or inventory['on_hand'] or finance['active_entries']),
                    'basis':[
                        '时间为UTC日历日，起止日期均包含；订单与采购按创建日，发货按发货日，退货按登记日。状态计数是这些订单当前状态，退货计数独立，允许重叠。',
                        '趋势连续补零；周从周一开始，首尾周按筛选范围截断。上期紧接本期前、天数相同；零基期不推断增长百分比。凭证原币逐币种，不相加；店仓筛选仅含匹配订单分摊原币份额。',
                        'SKU无发货风险仅在至少30日且当前有库存时提示，不证明入库时间、完整销售历史或滞销事实；退货与发货可能不是同批订单，不计算退货率。',
                        '订单原币金额排除取消单；销售件数为未取消订单订购件数。发货为出库毛件数，退货单列，不推断退款或签收收入。',
                        '采购到货和在途为本期创建采购单的当前累计进度，取消单已到货仍保留，未收量不再算在途。采购没有店铺归属，选择店铺时采购不统计。',
                        '库存为当前快照，不是期末历史库存。店铺筛选仅显示该店铺历史订单关联SKU，不代表库存专属于此店铺。',
                        '库存估值按商品档案参考采购成本CNY估算，不是库存成本结转；未知成本单列，不计为零成本。库存预警按可用库存与本期发货比较，不是补货承诺。',
                        '财务按凭证确认日期及登记汇率折为CNY；收付款不会再次确认收入费用。订单售价、采购计划、发货金额不自动成为收入费用。',
                        '筛选店铺或仓库时财务仅包含分摊到匹配订单的金额；全局未分摊金额另列，不能归属到筛选店铺。贡献按本期凭证金额汇总，人工核对状态以完整现有记录指纹为准。',
                        '已核贡献和待核差额均不是净利润；没有录入完整成本、税费、广告与期间费用时不能推断经营盈利。真实noon结算、银行和平台可售尚未由本报告验证。']}

    @staticmethod
    def _sku_risks(item, f):
        flags = []
        span = (date.fromisoformat(f['to'])-date.fromisoformat(f['from'])).days+1
        if item['on_hand'] > 0 and not item['shipped_units'] and span >= 30:
            flags.append('当前有库存，观测期内无发货：滞销风险待核实')
        if item['returned_units']:
            flags.append('存在退货：核对原因与不可售库存')
        if item['on_hand'] > 0 and item['cost_cny'] is None:
            flags.append('参考成本缺失')
        if item['available'] <= 0 and (item['ordered_units'] or item['on_hand']):
            flags.append('无可用库存：核对占用与补货')
        return flags

    def _trends(self, c, f, where, params):
        start, end = date.fromisoformat(f['from']), date.fromisoformat(f['to'])
        length = (end-start).days+1
        try:
            previous_end = start-timedelta(days=1)
            previous_start = start-timedelta(days=length)
        except OverflowError:
            raise Problem('开始日期过早，无法表示同长度上期')
        low, high = previous_start.isoformat(), end.isoformat()
        keys = ('created_orders', 'ordered_units', 'shipped_units', 'returned_units')
        daily = {}
        for offset in range(length*2):
            dated = (previous_start+timedelta(days=offset)).isoformat()
            daily[dated] = {'from':dated, 'to':dated, **dict.fromkeys(keys, 0), 'finance_currencies':{}}
        scope = 'WITH scoped AS (SELECT d.* FROM ops_documents d WHERE '+where+') '
        queries = [
            ("SELECT substr(json_extract(data,'$.created_at'),1,10) dated,count(*) value FROM scoped WHERE dated BETWEEN ? AND ? GROUP BY dated", 'created_orders'),
            ("SELECT substr(json_extract(d.data,'$.created_at'),1,10) dated,sum(json_extract(l.value,'$.quantity')) value FROM scoped d,json_each(d.data,'$.lines') l WHERE json_extract(d.data,'$.status')!='cancelled' AND dated BETWEEN ? AND ? GROUP BY dated", 'ordered_units'),
            ("SELECT substr(json_extract(d.data,'$.shipped_at'),1,10) dated,sum(json_extract(l.value,'$.quantity')) value FROM scoped d,json_each(d.data,'$.lines') l WHERE json_extract(d.data,'$.status') IN ('shipped','delivered') AND dated BETWEEN ? AND ? GROUP BY dated", 'shipped_units'),
            ("SELECT substr(json_extract(r.value,'$.at'),1,10) dated,sum(json_extract(l.value,'$.quantity')) value FROM scoped d,json_each(d.data,'$.returns') r,json_each(r.value,'$.lines') l WHERE dated BETWEEN ? AND ? GROUP BY dated", 'returned_units')]
        for sql, key in queries:
            for row in c.execute(scope+sql, params+[low,high]):
                daily[row['dated']][key] = row['value']
        filtered = bool(f['shop_id'] or f['warehouse_id'])
        # Original allocation amounts are already integer cents; no conversion or
        # rounding is needed. Unallocated and purchase entries remain global.
        if filtered:
            sql = scope+"""SELECT json_extract(e.data,'$.date') dated,json_extract(e.data,'$.currency') currency,
                json_extract(e.data,'$.kind') kind,sum(json_extract(a.value,'$.amount_cents')) amount
                FROM finance_entries e,json_each(e.data,'$.allocations') a JOIN scoped d ON d.id=json_extract(a.value,'$.order_id')
                WHERE json_extract(e.data,'$.status')='active' AND json_extract(e.data,'$.date') BETWEEN ? AND ?
                GROUP BY dated,currency,kind"""
            args = params+[low,high]
        else:
            sql = """SELECT json_extract(data,'$.date') dated,json_extract(data,'$.currency') currency,
                json_extract(data,'$.kind') kind,sum(json_extract(data,'$.amount_cents')) amount FROM finance_entries
                WHERE json_extract(data,'$.status')='active' AND json_extract(data,'$.date') BETWEEN ? AND ? GROUP BY dated,currency,kind"""
            args = [low,high]
        for row in c.execute(sql, args):
            group = daily[row['dated']]['finance_currencies'].setdefault(row['currency'], {'income_cents':0,'expense_cents':0})
            group[row['kind']+'_cents'] += row['amount']

        def aggregate(rows, first, last):
            result = {'from':first,'to':last, **{key:sum(r[key] for r in rows) for key in keys}, 'finance_currencies':{}}
            for row in rows:
                for currency, values in row['finance_currencies'].items():
                    group = result['finance_currencies'].setdefault(currency, {'income_cents':0,'expense_cents':0})
                    for key, value in values.items():group[key] += value
            return result

        current_rows = [r for d,r in daily.items() if d >= f['from']]
        previous_rows = [r for d,r in daily.items() if d < f['from']]
        current = aggregate(current_rows, f['from'], f['to'])
        previous = aggregate(previous_rows, previous_start.isoformat(), previous_end.isoformat())
        def change(a, b):
            return {'current':a,'previous':b,'delta':a-b,'percent':None if not b else float((Decimal(a-b)*100/Decimal(b)).quantize(Decimal('.01')))}
        comparison = {key:change(current[key], previous[key]) for key in keys}
        currency_comparison = {}
        for currency in sorted(current['finance_currencies'].keys() | previous['finance_currencies'].keys()):
            currency_comparison[currency] = {key:change(current['finance_currencies'].get(currency,{}).get(key,0), previous['finance_currencies'].get(currency,{}).get(key,0)) for key in ('income_cents','expense_cents')}
        weeks = []
        batch = []
        for row in current_rows:
            if batch and date.fromisoformat(row['from']).weekday() == 0:
                weeks.append(aggregate(batch, batch[0]['from'], batch[-1]['to']));batch = []
            batch.append(row)
        if batch:weeks.append(aggregate(batch, batch[0]['from'], batch[-1]['to']))
        return {'daily':current_rows,'weekly':weeks,'current':current,'previous':previous,
                'comparison':comparison,'currency_comparison':currency_comparison,'finance_scope':'allocated_orders' if filtered else 'global'}

    def _finance(self, c, f, where, params):
        # Exact Finance largest-remainder allocation, including unallocated cents.
        relevant = """ AND (substr(json_extract(d.data,'$.created_at'),1,10) BETWEEN ? AND ? OR EXISTS(
            SELECT 1 FROM finance_entries e,json_each(e.data,'$.allocations') a
            WHERE json_extract(e.data,'$.status')='active' AND json_extract(e.data,'$.date') BETWEEN ? AND ?
            AND json_extract(a.value,'$.order_id')=d.id))"""
        selected = {r['id']:json.loads(r['data']) for r in c.execute('SELECT d.id,d.data FROM ops_documents d WHERE '+where+relevant, params+[f['from'],f['to']]*2)}
        summary = c.execute("""SELECT count(*) active_entries,
            coalesce(sum(CASE WHEN json_extract(data,'$.kind')='income' THEN json_extract(data,'$.base_cents') ELSE 0 END),0) income,
            coalesce(sum(CASE WHEN json_extract(data,'$.kind')='expense' THEN json_extract(data,'$.base_cents') ELSE 0 END),0) expense
            FROM finance_entries WHERE json_extract(data,'$.status')='active' AND json_extract(data,'$.date') BETWEEN ? AND ?""",(f['from'],f['to'])).fetchone()
        period = {}; all_lines = {}; unallocated_income = unallocated_expense = 0
        # Scan only period entries or older entries affecting these orders' review
        # fingerprints, rather than loading unrelated historical ledger rows.
        entry_sql = 'WITH targets AS (SELECT d.id FROM ops_documents d WHERE '+where+relevant+') '+"""SELECT e.data,(SELECT coalesce(sum(json_extract(p.data,'$.amount_cents')),0)
            FROM finance_payments p WHERE p.entry_id=e.id AND json_extract(p.data,'$.status')='active') paid
            FROM finance_entries e WHERE json_extract(e.data,'$.status')='active' AND
            (json_extract(e.data,'$.date') BETWEEN ? AND ? OR EXISTS(SELECT 1 FROM json_each(e.data,'$.allocations') a
            JOIN targets t ON t.id=json_extract(a.value,'$.order_id'))) ORDER BY e.rowid DESC"""
        for row in c.execute(entry_sql,params+[f['from'],f['to']]*3):
            e = json.loads(row['data']); in_period = f['from']<=e['date']<=f['to']
            weights = [a['amount_cents'] for a in e['allocations']]
            shares = apportioned(e['base_cents'],weights+[e['amount_cents']-sum(weights)])
            if in_period:
                if e['kind']=='income': unallocated_income += shares[-1]
                else: unallocated_expense += shares[-1]
            matching = [(a,share) for a,share in zip(e['allocations'],shares) if a['order_id'] in selected]
            if not matching: continue
            paid = row['paid']
            for a,share in matching:
                line = {'entry_id':e['id'],'entry_revision':e['revision'],'kind':e['kind'],'category':e['category'],
                        'amount_cents':a['amount_cents'],'currency':e['currency'],'base_cents':share,'remaining_entry_cents':e['amount_cents']-paid}
                all_lines.setdefault(a['order_id'],[]).append(line)
                if in_period:period.setdefault(a['order_id'],[]).append(line)
        reviewed = {r['order_id']:json.loads(r['data']) for r in c.execute('SELECT order_id,data FROM finance_reviews')}
        verified = pending = inc = exp = 0; verified_count = pending_count = missing_cost = missing_revenue = 0
        for oid,d in selected.items():
            lines = period.get(oid,[])
            income = sum(x['base_cents'] for x in lines if x['kind']=='income'); expense = sum(x['base_cents'] for x in lines if x['kind']=='expense')
            inc += income; exp += expense
            full = all_lines.get(oid,[])
            reconciled = bool(oid in reviewed and reviewed[oid]['fingerprint']==fingerprint({'document':d,'lines':full}))
            if lines:
                if reconciled: verified += income-expense; verified_count += 1
                else: pending += income-expense; pending_count += 1
            if f['from']<=d['created_at'][:10]<=f['to'] and d['status']!='cancelled':
                if not any(x['kind']=='expense' for x in full):missing_cost += 1
                if not any(x['category']=='sale' for x in full):missing_revenue += 1
        filtered = bool(f['shop_id'] or f['warehouse_id'])
        return {'currency':'CNY','income_cents':inc if filtered else summary['income'],'expense_cents':exp if filtered else summary['expense'],
                'allocated_income_cents':inc,'allocated_expense_cents':exp,'verified_contribution_cents':verified,'verified_orders':verified_count,
                'pending_difference_cents':pending,'pending_orders':pending_count,'missing_cost_orders':missing_cost,'missing_revenue_orders':missing_revenue,
                'global_unallocated_income_cents':unallocated_income,'global_unallocated_expense_cents':unallocated_expense,'active_entries':summary['active_entries'],
                'unallocated_scope':'global','filtered':filtered}

    def export(self, body=None):
        s = self.state(body); out = io.StringIO(); writer = csv.writer(out)
        def write(values):
            cells = []
            for value in values:
                value = '' if value is None else str(value)
                if value.lstrip().startswith(('=','+','-','@')) or value.startswith(('\t','\r','\n')):value = "'"+value
                cells.append(value)
            writer.writerow(cells)
        write(['运营分析报告 · 非净利润','生成时间',s['generated_at']])
        write(['期间',s['filters']['from'],s['filters']['to'],'店铺',s['filters']['shop_id'],'仓库',s['filters']['warehouse_id']])
        for basis in s['basis']:write(['口径',basis])
        for currency in s['order_currencies']:write(['订单原币金额',currency['currency'],format(Decimal(currency['amount_cents'])/100,'.2f'),'订单数',currency['orders']])
        for group in ('orders','purchases','inventory','finance'):
            for key,value in s[group].items():
                if isinstance(value,dict):
                    for k,v in value.items():write([group,key,k,v])
                else:write([group,key,value])
        t = s['trends']
        write(['上期范围',t['previous']['from'],t['previous']['to'],'本期范围',t['current']['from'],t['current']['to']])
        write(['上期对比','指标','本期','上期','差额','变化百分比（零基期留空）'])
        for key, values in t['comparison'].items():write(['上期对比',key,*[values[k] for k in ('current','previous','delta','percent')]])
        for currency, groups in t['currency_comparison'].items():
            for key, values in groups.items():write(['原币上期对比',currency,key,*[values[k] for k in ('current','previous','delta','percent')]])
        for period in ('daily','weekly'):
            write([period,'开始UTC','结束UTC','创建订单','订购件数','发货件数','退货件数'])
            for row in t[period]:
                write([period,row['from'],row['to'],*[row[k] for k in ('created_orders','ordered_units','shipped_units','returned_units')]])
                for currency, values in sorted(row['finance_currencies'].items()):write([period+'凭证原币',row['from'],row['to'],currency,values['income_cents'],values['expense_cents'],'金额单位：分'])
        write(['SKU页',s['sku_page']['page']+1,'总页数',s['sku_page']['pages'],'每页至多50行'])
        keys=['sku','title','ordered_units','shipped_units','returned_units','on_hand','reserved','available','cost_cny','estimated_value_cents','stock_warning','risk_flags','risk_evidence']
        write(['SKU','商品','未取消订单件数','发货件数','退货件数','在库','占用','可用','参考成本CNY','估算库存价值CNY分','库存提醒','风险诊断','风险证据'])
        for item in s['sku_page']['items']:write(['；'.join(item[k]) if isinstance(item[k],list) else item[k] for k in keys])
        return ('\ufeff'+out.getvalue()).encode('utf-8')
