"""Local SPU organization of independent SKUs; no Noon variation publishing."""
import csv
import io
import itertools
import math
import hashlib
import json
import re
from core import Problem, ident, now

SCHEMA_SQL = '''
CREATE TABLE IF NOT EXISTS catalog_groups(id TEXT PRIMARY KEY, name TEXT NOT NULL, data TEXT NOT NULL, revision INTEGER NOT NULL, updated_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS catalog_groups_updated ON catalog_groups(updated_at DESC,id);
CREATE TABLE IF NOT EXISTS catalog_group_members(product_id TEXT PRIMARY KEY, group_id TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS catalog_group_members_group ON catalog_group_members(group_id);
CREATE TABLE IF NOT EXISTS catalog_group_requests(key TEXT PRIMARY KEY, digest TEXT NOT NULL, result TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS catalog_group_audit(id INTEGER PRIMARY KEY AUTOINCREMENT, group_id TEXT NOT NULL, action TEXT NOT NULL, revision INTEGER NOT NULL, data TEXT NOT NULL, created_at TEXT NOT NULL);
'''


def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()


def text(value,label,optional=False,limit=200):
    if not isinstance(value,str) or len(value.strip())>limit or (not optional and not value.strip()):raise Problem(label+'格式无效')
    return value.strip()


class CatalogGroups:
    def __init__(self,app):
        self.app=app;self.store=app.store
        with self.store.connect() as c:c.executescript(SCHEMA_SQL)

    def product(self,c,pid):
        if not isinstance(pid,str):raise Problem('商品编号无效')
        row=c.execute('SELECT * FROM products WHERE id=?',(pid,)).fetchone()
        if not row:raise Problem('商品不存在',404)
        data=json.loads(row['data'])
        return row,data

    def snapshot(self,row,data):
        # Identity and source facts are bound as well as the supported product revision.
        return digest([row['source_key'],data])

    def parent(self,data):
        provenance=data.get('source_collection') or {}
        for snapshot in reversed(provenance.get('snapshots',[])):
            parent=(snapshot.get('raw') or {}).get('external_product_id')
            if parent:return {'source_channel':provenance.get('provider',''),'account_id':provenance.get('account_id',''),'source_parent_id':parent,'requires_confirmation':True}
        return None

    def quality(self,c,group,saved=False):
        axes=group['axes'];members=group['members'];issues=[];axis_values={a:set() for a in axes};occupied={}
        def issue(code,message,pid='',severity='error'):
            issues.append(dict(code=code,message=message,product_id=pid,severity=severity))
        brands=set();categories=set()
        if not members:issue('empty_group','请选择独立SKU后核对规格')
        seen=set();ids=[m.get('product_id') for m in members if isinstance(m.get('product_id'),str)]
        products={r['id']:r for r in c.execute('SELECT * FROM products WHERE id IN ('+','.join('?' for _ in ids)+')',ids)} if ids else {}
        assignments={r['product_id']:r['group_id'] for r in c.execute('SELECT * FROM catalog_group_members WHERE product_id IN ('+','.join('?' for _ in ids)+')',ids)} if ids else {}
        for m in members:
            pid=m.get('product_id','');values=m.get('values') or {};valid=True
            if pid in seen:issue('duplicate_member','同一SKU重复出现，请移除重复成员',pid)
            seen.add(pid)
            for axis in axes:
                value=values.get(axis)
                if not isinstance(value,str) or not value.strip():issue('missing_axis_value','补充规格值：'+axis,pid);valid=False
                else:axis_values[axis].add(value.strip())
            if set(values)-set(axes):issue('extra_axis_value','存在未使用的规格字段，请重新核对规格轴',pid)
            if valid:
                combo=tuple(values[a].strip() for a in axes)
                occupied.setdefault(combo,[]).append(pid)
            product=products.get(pid)
            if not product:issue('missing_product','成员商品不存在，请移除或恢复商品',pid);continue
            data=json.loads(product['data']);brands.add(data.get('brand',''));categories.add(data.get('category',''));m.update(current_revision=product['revision'],sku=data.get('partner_sku',''),title=data.get('title_zh',''))
            if product['revision']!=m.get('revision'):issue('revision_changed','商品版本已变化，请重新复核当前事实',pid)
            if m.get('facts_digest') and self.snapshot(product,data)!=m['facts_digest']:issue('facts_changed','商品身份或事实已变化，请重新复核',pid)
            for field,label in (('brand','品牌'),('category','类目')):
                if not data.get(field):issue('missing_'+field,'商品'+label+'待补充，请在商品流程核对',pid,'warning')
                elif field in group and data.get(field)!=group[field]:issue(field+'_mismatch','当前'+label+'与分组不一致，请在商品流程核对',pid)
            assigned=assignments.get(pid)
            if assigned and assigned!=group.get('id'):issue('other_group','SKU归属其他组，请核对本地归属',pid)
            elif saved and not assigned:issue('missing_membership','成员归属索引缺失，请重新复核保存本地组',pid)
        if len(brands)>1:issue('mixed_brands','成员当前品牌不一致，请在商品流程核对')
        if len(categories)>1:issue('mixed_categories','成员当前类目不一致，请在商品流程核对')
        for combo,pids in occupied.items():
            if len(pids)>1:issue('duplicate_combination','规格组合重复：'+' / '.join(combo)+'；请调整规格或移除成员',pids[0])
        if saved:
            indexed={r[0] for r in c.execute('SELECT product_id FROM catalog_group_members WHERE group_id=?',(group['id'],))}
            for pid in sorted(indexed-set(ids)):issue('extra_membership','归属索引存在组内未记录SKU，请重新复核保存本地组',pid)
        values={a:sorted(v) for a,v in axis_values.items()};expected=math.prod(len(v) for v in values.values())
        missing=max(0,expected-len(occupied));matrix=[]
        for combo in itertools.islice(itertools.product(*(values[a] for a in axes)),200):
            pids=occupied.get(combo,[]);matrix.append(dict(values=dict(zip(axes,combo)),product_ids=pids,status='duplicate' if len(pids)>1 else 'present' if pids else 'missing'))
        if missing:issue('combination_gap',str(missing)+'个观测规格组合未有SKU；请人工确认是否有意不供货，不会自动生成SKU','', 'warning')
        return dict(issues=issues,error_count=sum(i['severity']=='error' for i in issues),warning_count=sum(i['severity']=='warning' for i in issues),axes=values,expected_combinations=expected,occupied_combinations=len(occupied),missing_combinations=missing,coverage=round(len(occupied)/expected,4) if expected else 0,matrix=matrix,matrix_limit=200,matrix_truncated=expected>200,basis='observed_axis_values',message='按本组已填写规格值推算组合；缺组合仅提示，不代表供应商承诺供货，不会自动生成SKU或发布Noon')

    def unpack(self,c,row):
        group=json.loads(row['data']);group.update(id=row['id'],revision=row['revision'],updated_at=row['updated_at'])
        group['quality']=self.quality(c,group,True)
        changed=sorted({i['product_id'] for i in group['quality']['issues'] if i['severity']=='error' and i['product_id']})
        for member in group['members']:
            if 'current_revision' not in member:member['missing']=True
        group.update(review_required=bool(group['quality']['error_count']),changed_product_ids=changed,connection='local_only',noon_variants_published=False)
        return group

    def diagnose(self,body):
        if not isinstance(body,dict):raise Problem('分组格式无效')
        allowed={'id','revision','name','source_channel','source_parent_id','axes','members'}
        if set(body)-allowed:raise Problem('分组包含未知字段')
        for field,limit in (('name',200),('source_channel',200),('source_parent_id',500)):
            if field in body:text(body[field],field,True,limit)
        if body.get('id') is not None:text(body['id'],'分组编号',limit=96)
        axes=body.get('axes');members=body.get('members')
        if not isinstance(axes,list) or not 1<=len(axes)<=3:raise Problem('需要1至3个规格轴')
        axes=[text(a,'规格轴',limit=60) for a in axes]
        if len(set(axes))!=len(axes):raise Problem('规格轴不能重复')
        if not isinstance(members,list) or len(members)>100:raise Problem('每组最多100个SKU')
        for m in members:
            if not isinstance(m,dict) or not isinstance(m.get('product_id'),str) or not isinstance(m.get('values',{}),dict):raise Problem('成员格式无效')
            if set(m)-{'product_id','revision','values','facts_digest'}:raise Problem('成员包含未知字段')
            text(m['product_id'],'商品编号',limit=96)
            if type(m.get('revision')) is not int or m['revision']<1:raise Problem('商品版本格式无效')
            if m.get('facts_digest') is not None and (not isinstance(m['facts_digest'],str) or not re.fullmatch(r'[a-f0-9]{64}',m['facts_digest'])):raise Problem('商品事实摘要格式无效')
            if len(m.get('values',{}))>3 or any(not isinstance(k,str) or len(k)>60 for k in m.get('values',{})):raise Problem('规格字段格式无效')
            if any(not isinstance(v,str) or len(v)>100 for v in m.get('values',{}).values()):raise Problem('规格值格式无效')
        group={**body,'axes':axes,'members':[dict(m) for m in members]}
        with self.store.connect() as c:
            c.execute('BEGIN');return dict(quality=self.quality(c,group),connection='local_only',noon_variants_published=False)

    def export_csv(self,group_id):
        with self.store.connect() as c:
            c.execute('BEGIN');row=c.execute('SELECT * FROM catalog_groups WHERE id=?',(text(group_id,'分组编号'),)).fetchone()
            if not row:raise Problem('分组不存在',404)
            group=self.unpack(c,row)
        out=io.StringIO();writer=csv.writer(out)
        def safe(value):
            value=str(value if value is not None else '')
            return "'"+value if value.lstrip().startswith(('=','+','-','@')) or value.startswith(('\t','\r','\n')) else value
        def write(row):writer.writerow([safe(v) for v in row])
        write(['本地规格组质检','组名',group['name'],'组编号',group['id'],'组版本',group['revision'],'未发布Noon'])
        write(['口径',group['quality']['message']]);write(['组合总数',group['quality']['expected_combinations'],'缺口',group['quality']['missing_combinations'],'矩阵最多导出',200])
        write(['类型','SKU/商品编号','绑定版本','当前版本',*group['axes'],'诊断'])
        for m in group['members']:
            reasons='；'.join(i['message'] for i in group['quality']['issues'] if i['product_id']==m['product_id'])
            write(['成员',m.get('sku') or m['product_id'],m['revision'],m.get('current_revision','已删除'),*(m['values'].get(a,'') for a in group['axes']),reasons])
        for cell in group['quality']['matrix']:
            write(['矩阵 '+cell['status'],' / '.join(cell['product_ids']),'','',*(cell['values'][a] for a in group['axes']),'缺口仅提示，不自动生成SKU' if cell['status']=='missing' else ''])
        for i in group['quality']['issues']:write(['诊断',i['product_id'],'','',*('' for _ in group['axes']),i['message']])
        return '\ufeff'+out.getvalue()

    def state(self,page=0,query='',product_page=None):
        if isinstance(page,bool) or not str(page).isdecimal() or int(page)>100000:raise Problem('页码无效')
        page=int(page);query=text(query,'搜索',True)
        if product_page is None:product_page=page
        if isinstance(product_page,bool) or not str(product_page).isdecimal() or int(product_page)>100000:raise Problem('商品页码无效')
        product_page=int(product_page)
        pattern='%'+query.replace('\\','\\\\').replace('%','\\%').replace('_','\\_')+'%'
        with self.store.connect() as c:
            c.execute('BEGIN')
            condition="name LIKE ? ESCAPE '\\' OR id IN (SELECT m.group_id FROM catalog_group_members m JOIN products p ON p.id=m.product_id WHERE json_extract(p.data,'$.partner_sku') LIKE ? ESCAPE '\\' OR json_extract(p.data,'$.title_zh') LIKE ? ESCAPE '\\')"
            total=c.execute('SELECT count(*) FROM catalog_groups WHERE '+condition,(pattern,)*3).fetchone()[0]
            pages=max(1,(total+19)//20);gp=min(page,pages-1)
            groups=[self.unpack(c,r) for r in c.execute('SELECT * FROM catalog_groups WHERE '+condition+' ORDER BY updated_at DESC,id LIMIT 20 OFFSET ?',(pattern,pattern,pattern,gp*20))]
            pc="json_extract(p.data,'$.partner_sku') LIKE ? ESCAPE '\\' OR json_extract(p.data,'$.title_zh') LIKE ? ESCAPE '\\'"
            product_total=c.execute('SELECT count(*) FROM products p WHERE '+pc,(pattern,pattern)).fetchone()[0]
            product_pages=max(1,(product_total+99)//100);pp=min(product_page,product_pages-1)
            selectable=[]
            for row in c.execute('SELECT p.*,m.group_id FROM products p LEFT JOIN catalog_group_members m ON p.id=m.product_id WHERE '+pc+' ORDER BY p.created_at DESC,p.id LIMIT 100 OFFSET ?',(pattern,pattern,pp*100)):
                data=json.loads(row['data']);selectable.append({'id':row['id'],'revision':row['revision'],'sku':data.get('partner_sku',''),'title':data.get('title_zh',''),'brand':data.get('brand',''),'category':data.get('category',''),'group_id':row['group_id'],'parent_candidate':self.parent(data)})
            return dict(rows=groups,groups=groups,total=total,page=gp,pages=pages,selectable_products=selectable,product_page=pp,product_pages=product_pages,product_total=product_total,query=query)

    def prepare(self,c,body):
        if not isinstance(body,dict):raise Problem('分组格式无效')
        gid=body.get('id');old=None
        if gid:
            old=c.execute('SELECT * FROM catalog_groups WHERE id=?',(text(gid,'分组编号'),)).fetchone()
            if not old:raise Problem('分组不存在',404)
            if type(body.get('revision')) is not int or body['revision']!=old['revision']:raise Problem('分组版本已更新，请刷新',409)
        axes=body.get('axes')
        if not isinstance(axes,list) or not 1<=len(axes)<=3:raise Problem('需要1至3个规格轴')
        axes=[text(a,'规格轴',limit=60) for a in axes]
        if len(set(axes))!=len(axes):raise Problem('规格轴不能重复')
        members=body.get('members')
        if not isinstance(members,list) or not 1<=len(members)<=100:raise Problem('每组需要1至100个SKU')
        prepared=[];seen=set();combinations=set();brands=set();categories=set();axis_values={a:set() for a in axes}
        for member in members:
            if not isinstance(member,dict):raise Problem('成员格式无效')
            row,data=self.product(c,member.get('product_id'));pid=row['id']
            if pid in seen:raise Problem('成员SKU重复')
            seen.add(pid)
            if type(member.get('revision')) is not int or member['revision']!=row['revision']:raise Problem('商品版本已变化，请重新选择并预览',409)
            assigned=c.execute('SELECT group_id FROM catalog_group_members WHERE product_id=?',(pid,)).fetchone()
            if assigned and assigned['group_id']!=gid:raise Problem('商品已属于其他分组',409)
            values=member.get('values')
            if not isinstance(values,dict) or set(values)!=set(axes):raise Problem('每个SKU必须填写所有规格轴值')
            values={a:text(values[a],'规格值',limit=100) for a in axes}
            combo=tuple(values[a] for a in axes)
            if combo in combinations:raise Problem('同组不能存在重复规格组合')
            combinations.add(combo)
            for a in axes:axis_values[a].add(values[a])
            brands.add(data.get('brand',''));categories.add(data.get('category',''))
            prepared.append(dict(product_id=pid,revision=row['revision'],values=values,facts_digest=self.snapshot(row,data)))
        if len(brands)!=1 or len(categories)!=1:raise Problem('同组商品的品牌和类目必须一致，请先核对商品事实')
        if any(len(v)>100 for v in axis_values.values()):raise Problem('每轴最多100个规格值')
        group=dict(name=text(body.get('name'),'组名称'),source_channel=text(body.get('source_channel',''),'源渠道',True),source_parent_id=text(body.get('source_parent_id',''),'源父商品',True,500),axes=axes,members=prepared,brand=next(iter(brands)),category=next(iter(categories)))
        fingerprint=digest([gid,old['revision'] if old else 0,group])
        return group,old,fingerprint

    def preview(self,body):
        with self.store.connect() as c:
            c.execute('BEGIN');group,old,fingerprint=self.prepare(c,body)
            group['quality']=self.quality(c,{**group,'id':old['id'] if old else None})
            return dict(group=group,preview_digest=fingerprint,revision=old['revision'] if old else 0,connection='local_only',message='仅建立本地SKU父子规格关系；请人工核对来源父商品和规格，不会发布Noon变体')

    def mutation(self,action,body):
        if not isinstance(body,dict):raise Problem('操作格式无效')
        key=body.get('request_id')
        if not isinstance(key,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,96}',key):raise Problem('操作编号无效')
        if body.get('confirmed') is not True:raise Problem('请人工确认本地规格分组操作')
        fingerprint=digest([action,body])
        with self.store.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            replay=c.execute('SELECT * FROM catalog_group_requests WHERE key=?',(key,)).fetchone()
            if replay:
                if replay['digest']!=fingerprint:raise Problem('操作编号已用于不同内容',409)
                return json.loads(replay['result'])
            if action=='save':
                group,old,preview=self.prepare(c,body)
                if body.get('preview_digest')!=preview:raise Problem('预览已失效，请重新预览并人工确认',409)
                gid=old['id'] if old else ident();revision=old['revision']+1 if old else 1;ts=now()
                c.execute('DELETE FROM catalog_group_members WHERE group_id=?',(gid,))
                c.execute('INSERT INTO catalog_groups VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name,data=excluded.data,revision=excluded.revision,updated_at=excluded.updated_at',(gid,group['name'],json.dumps(group,ensure_ascii=False),revision,ts))
                for member in group['members']:c.execute('INSERT INTO catalog_group_members VALUES(?,?)',(member['product_id'],gid))
                result=self.unpack(c,c.execute('SELECT * FROM catalog_groups WHERE id=?',(gid,)).fetchone())
            else:
                row=c.execute('SELECT * FROM catalog_groups WHERE id=?',(text(body.get('id'),'分组编号'),)).fetchone()
                if not row:raise Problem('分组不存在',404)
                if type(body.get('revision')) is not int or body['revision']!=row['revision']:raise Problem('分组版本已更新，请刷新',409)
                gid=row['id'];revision=row['revision']+1;group=json.loads(row['data']);pid=body.get('product_id')
                if pid is not None:
                    if not isinstance(pid,str) or pid not in {m['product_id'] for m in group['members']}:raise Problem('商品不属于此组')
                    group['members']=[m for m in group['members'] if m['product_id']!=pid]
                    if not group['members']:raise Problem('最后一个成员请使用解散分组')
                    c.execute('DELETE FROM catalog_group_members WHERE group_id=? AND product_id=?',(gid,pid))
                    c.execute('UPDATE catalog_groups SET data=?,revision=?,updated_at=? WHERE id=?',(json.dumps(group,ensure_ascii=False),revision,now(),gid))
                    result=self.unpack(c,c.execute('SELECT * FROM catalog_groups WHERE id=?',(gid,)).fetchone())
                else:
                    c.execute('DELETE FROM catalog_group_members WHERE group_id=?',(gid,));c.execute('DELETE FROM catalog_groups WHERE id=?',(gid,));result={'id':gid,'revision':revision,'dissolved':True}
            c.execute('INSERT INTO catalog_group_audit(group_id,action,revision,data,created_at) VALUES(?,?,?,?,?)',(gid,action,revision,json.dumps({'request_id':key,'group':group},ensure_ascii=False),now()))
            self.store.event(c,None,'本地规格分组 · '+action,gid+' · 版本 '+str(revision))
            c.execute('INSERT INTO catalog_group_requests VALUES(?,?,?)',(key,fingerprint,json.dumps(result,ensure_ascii=False)))
            return result

    def save(self,body):return self.mutation('save',body)
    def remove(self,body):return self.mutation('remove',body)
