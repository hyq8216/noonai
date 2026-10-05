import sys,tempfile,unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from core import Store,supply_check_valid_at
class CatalogDeltaTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.store=Store(Path(self.tmp.name));self.clock=patch('core.time.time',return_value=1800000000);self.clock.start();self.pid=self.store.import_rows([{'title_zh':'Fixture'}])['created'][0]
 def tearDown(self):self.clock.stop();self.tmp.cleanup()
 def snapshot(self):return self.store.catalog_snapshot()
 def test_unchanged_omits_catalog_and_other_events_do_not_send_products(self):
  s=self.snapshot();same=self.store.catalog_snapshot(s['catalog_token']);self.assertTrue(same['catalog_unchanged']);self.assertNotIn('products',same)
  with self.store.connect() as c:self.store.event(c,None,'运营业务','不修改商品')
  same=self.store.catalog_snapshot(s['catalog_token']);self.assertTrue(same['catalog_unchanged']);self.assertNotEqual(same['catalog_token'],s['catalog_token'])
 def test_import_and_update_send_only_changed_products(self):
  s=self.snapshot();second=self.store.import_rows([{'title_zh':'Second'}])['created'][0];self.store.update(self.pid,{'stock':4},1);change=self.store.catalog_snapshot(s['catalog_token']);self.assertEqual({p['id'] for p in change['product_changes']},{self.pid,second});self.assertNotIn('products',change)
 def test_approval_and_platform_changes_do_not_require_revision_bump(self):
  s=self.snapshot()
  with patch('core.issues',return_value=[]):self.store.approve(self.pid,1)
  change=self.store.catalog_snapshot(s['catalog_token']);self.assertEqual(change['product_changes'][0]['approved_revision'],1)
  self.store.record_platform(self.pid,{'state':'pending'});change=self.store.catalog_snapshot(change['catalog_token']);self.assertEqual(change['product_changes'][0]['platform'],{'state':'pending'});self.assertEqual(change['product_changes'][0]['revision'],1)
 def test_expired_bucket_refreshes_token_and_invalid_cursor_gives_full_snapshot(self):
  s=self.snapshot()
  with patch('core.time.time',return_value=1800000015):
   refreshed=self.store.catalog_snapshot(s['catalog_token']);self.assertTrue(refreshed['catalog_unchanged']);self.assertNotEqual(refreshed['catalog_token'],s['catalog_token'])
  with self.store.connect() as c:latest=c.execute('SELECT coalesce(max(id),0) FROM events').fetchone()[0]
  nonfinite_cursor=f'{self.store.catalog_session}:{latest}:120000000:1800000000000000:e:inf:{"0"*32}'
  for token in ['wrong',s['catalog_token'].replace(':1:',':999:'),'x'*200,self.store.catalog_session+':'+'9'*50+':120000000',nonfinite_cursor,Store(Path(self.tmp.name)).catalog_snapshot()['catalog_token']]:self.assertIn('products',self.store.catalog_snapshot(token))
 def test_rollback_does_not_advertise_uncommitted_changes(self):
  s=self.snapshot()
  try:
   with self.store.connect() as c:
    c.execute('BEGIN IMMEDIATE');self.store.update(self.pid,{'stock':99},1,connection=c);raise RuntimeError('rollback')
  except RuntimeError:pass
  self.assertTrue(self.store.catalog_snapshot(s['catalog_token'])['catalog_unchanged']);self.assertIsNone(self.store.get(self.pid)['stock'])
 def test_large_change_set_pages_without_full_snapshot(self):
  s=self.snapshot();self.store.import_rows([{'title_zh':f'Batch{i}'} for i in range(500)]);self.store.update(self.pid,{'stock':2},1)
  first=self.store.catalog_snapshot(s['catalog_token']);self.assertTrue(first['catalog_has_more']);self.assertEqual(len(first['product_changes']),500)
  second=self.store.catalog_snapshot(first['catalog_token']);self.assertFalse(second['catalog_has_more']);self.assertEqual(len(second['product_changes']),1)
 def test_product_event_for_missing_id_removes_client_record(self):
  s=self.snapshot()
  with self.store.connect() as c:
   c.execute('DELETE FROM products WHERE id=?',(self.pid,));self.store.event(c,self.pid,'删除测试','')
  change=self.store.catalog_snapshot(s['catalog_token']);self.assertEqual(change['removed_product_ids'],[self.pid]);self.assertEqual(change['product_changes'],[])
 def test_time_bucket_refresh_uses_index_and_emits_only_supply_boundary_changes(self):
  wall=datetime.now(timezone.utc);bucket=int(wall.timestamp()//15);before=datetime.fromtimestamp((bucket-1)*15,timezone.utc);current=before+timedelta(seconds=20)
  samples=[
   ('expiry-crossing',before-timedelta(days=1)+timedelta(seconds=7),'UTC'),
   ('future-becomes-valid',before+timedelta(seconds=303),'+04'),
   ('expiry-exact-at-start',before-timedelta(days=1),'UTC'),
   ('expiry-last-submillisecond',current-timedelta(days=1)-timedelta(microseconds=500),'+05'),
   ('future-exact-at-end',current+timedelta(seconds=300),'-07'),
   ('just-before-expiry-range',before-timedelta(days=1)-timedelta(microseconds=500),'UTC'),
   ('already-valid-before-future-range',before+timedelta(seconds=299),timezone.utc),
   ('just-after-future-cutoff',current+timedelta(seconds=300,microseconds=500),'+04'),
   ('stable-expired',before-timedelta(days=1,seconds=5),'-05'),
   ('stable-future',current+timedelta(seconds=301),'UTC'),
  ]
  def serialized(value,offset):
   if offset=='UTC':return value.isoformat()
   if isinstance(offset,str):
    hours=int(offset);return value.astimezone(timezone(timedelta(hours=hours))).isoformat()
   return value.astimezone(offset).isoformat()
  records=[{'title_zh':name,'source_sku':name,'supply_checked_at':serialized(value,offset)} for name,value,offset in samples]
  ids=self.store.import_rows(records)['created']
  with self.store.connect() as c:
   latest=c.execute('SELECT coalesce(max(id),0) FROM events').fetchone()[0]
   plan=c.execute("""EXPLAIN QUERY PLAN
    SELECT id FROM products WHERE json_extract(data,'$.supply_checked_at') IS NOT NULL AND julianday(json_extract(data,'$.supply_checked_at')) BETWEEN julianday(?) AND julianday(?)
    UNION ALL
    SELECT id FROM products WHERE json_extract(data,'$.supply_checked_at') IS NOT NULL AND julianday(json_extract(data,'$.supply_checked_at')) BETWEEN julianday(?) AND julianday(?)
   """,((before-timedelta(days=1,seconds=1)).isoformat(),(current-timedelta(days=1)+timedelta(seconds=1)).isoformat(),(before+timedelta(seconds=299)).isoformat(),(current+timedelta(seconds=301)).isoformat())).fetchall()
  plan_text=' '.join(str(tuple(row)) for row in plan)
  self.assertEqual(plan_text.count('idx_products_supply_checked_jd_id'),2,plan_text)
  class FrozenDateTime(datetime):
   @classmethod
   def now(cls,tz=None):return current if tz else current.replace(tzinfo=None)
   @classmethod
   def fromtimestamp(cls,value,tz=None):return datetime.fromtimestamp(value,tz)
  token=f'{self.store.catalog_session}:{latest}:{bucket-1}'
  with patch('core.datetime',FrozenDateTime),patch('core.time.time',return_value=current.timestamp()):
   changed=self.store.catalog_snapshot(token)
  self.assertIn('product_changes',changed,list(changed))
  expected={pid for pid,(_,value,offset) in zip(ids,samples) if supply_check_valid_at(serialized(value,offset),before)!=supply_check_valid_at(serialized(value,offset),current)}
  actual={p['id'] for p in changed['product_changes']}
  self.assertEqual(len(expected),5)
  self.assertEqual(actual,expected)
 def test_time_bucket_changes_over_500_page_without_full_catalog(self):
  bucket=int(datetime.now(timezone.utc).timestamp()//15);before=datetime.fromtimestamp((bucket-1)*15,timezone.utc);current=before+timedelta(seconds=20)
  checked=(before-timedelta(days=1)+timedelta(seconds=7)).isoformat()
  ids=[]
  for start in range(0,501,500):
   ids.extend(self.store.import_rows([{'title_zh':f'Expiring {n}','source_sku':f'EXP-{n:03d}','supply_checked_at':checked} for n in range(start,min(start+500,501))])['created'])
  with self.store.connect() as c:latest=c.execute('SELECT coalesce(max(id),0) FROM events').fetchone()[0]
  class FrozenDateTime(datetime):
   @classmethod
   def now(cls,tz=None):return current if tz else current.replace(tzinfo=None)
   @classmethod
   def fromtimestamp(cls,value,tz=None):return datetime.fromtimestamp(value,tz)
  token=f'{self.store.catalog_session}:{latest}:{bucket-1}'
  with patch('core.datetime',FrozenDateTime),patch('core.time.time',return_value=current.timestamp()):
   first=self.store.catalog_snapshot(token)
   second=self.store.catalog_snapshot(first.get('catalog_token'))
  self.assertNotIn('products',first,list(first))
  self.assertNotIn('products',second,list(second))
  self.assertEqual(len(first.get('product_changes',[])),500)
  self.assertEqual(len(second.get('product_changes',[])),1)
  self.assertTrue(first.get('catalog_has_more'))
  self.assertFalse(second.get('catalog_has_more'))
  self.assertEqual({p['id'] for p in first['product_changes']+second['product_changes']},set(ids))
if __name__=='__main__':unittest.main()
