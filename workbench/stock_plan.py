"""Read-only FBPI stock planning. Supplier stock and incoming stock are never sellable."""
import json
import re

from core import Problem, now


def whole(value, label, optional=False):
    if optional and value in (None, ''): return None
    if type(value) is not int or value < 0 or value > 1000000:
        raise Problem(label+'需为0至1000000的整数')
    return value


class StockPlan:
    def __init__(self, app):
        self.app=app
        self.store=app.store

    def preview(self, body):
        if not isinstance(body,dict):raise Problem('库存预览参数无效')
        warehouse_id=body.get('warehouse_id')
        if not isinstance(warehouse_id,str) or not warehouse_id:raise Problem('请选择本地仓库')
        buffer=whole(body.get('buffer',0),'安全缓冲')
        cap=whole(body.get('cap'),'单SKU上限',True)
        code=body.get('warehouse_code','')
        if not isinstance(code,str) or len(code)>80 or (code and not re.fullmatch(r'[A-Za-z0-9_-]+',code)):
            raise Problem('noon集成仓库编码格式无效')
        code=code.strip()
        with self.store.connect() as c:
            warehouse=c.execute("SELECT id,name FROM ops_entities WHERE id=? AND kind='warehouse'",(warehouse_id,)).fetchone()
            if not warehouse:raise Problem('本地仓库不存在',404)
            stocks={r['product_id']:dict(r) for r in c.execute('SELECT * FROM ops_stock WHERE warehouse_id=?',(warehouse_id,))}
            demands={}
            for row in c.execute("SELECT data FROM ops_documents WHERE kind='order'"):
                d=json.loads(row['data'])
                if d.get('warehouse_id')!=warehouse_id or d.get('status')!='new':continue
                for line in d.get('lines',[]):
                    pid=line['product_id'];demands[pid]=demands.get(pid,0)+line['quantity']
            rows=[]
            for saved in c.execute('SELECT * FROM products ORDER BY created_at,id'):
                p=self.store.unpack(saved)
                if p['demo'] or p.get('mode')!='LOCAL':continue
                stock=stocks.get(p['id'])
                on_hand=stock['on_hand'] if stock else 0
                reserved=stock['reserved'] if stock else 0
                demand=demands.get(p['id'],0)
                ceiling=max(0,on_hand-reserved-demand-buffer)
                qty=min(ceiling,cap) if cap is not None else ceiling
                reasons=[]
                if not code:reasons.append('尚未填入经 Seller Lab 确认的集成仓库编码')
                if not (p.get('platform') or {}).get('sku_parent'):reasons.append('商品尚无 noon 平台编号')
                if stock is None:reasons.append('本地仓库尚无此商品的盘点记录')
                # LOCAL also includes fulfillment models other than FBPI. A seller
                # must establish that this warehouse is an FBPI integration warehouse.
                rows.append({'id':p['id'],'sku':p['partner_sku'],'title':p['title_zh'],
                             'supplier_available':p.get('stock'),'on_hand':on_hand,'reserved':reserved,
                             'unreserved_order_demand':demand,'buffer':buffer,'ceiling':ceiling,'qty':qty,
                             'status':'blocked' if reasons else 'candidate','reasons':reasons})
        return {'warehouse_id':warehouse_id,'warehouse_name':warehouse['name'],'warehouse_code':code,
                'buffer':buffer,'cap':cap,'rows':rows,'candidates':sum(r['status']=='candidate' for r in rows),
                'blocked':sum(r['status']=='blocked' for r in rows),'created_at':now(),
                'notice':'本地 FBPI 库存候选预览，尚未核对经营模式、仓库权限或 noon 实际库存；不会发送平台。供应商可供量、采购待收和调拨在途不计入可售数量。'}
