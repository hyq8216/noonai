import sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from core import Store
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
 def test_expired_foreign_future_or_malformed_cursor_gives_full_snapshot(self):
  s=self.snapshot()
  with patch('core.time.time',return_value=1800086415):self.assertIn('products',self.store.catalog_snapshot(s['catalog_token']))
  for token in ['wrong',s['catalog_token'].replace(':1:',':999:'),'x'*200,self.store.catalog_session+':'+'9'*50+':120000000',Store(Path(self.tmp.name)).catalog_snapshot()['catalog_token']]:self.assertIn('products',self.store.catalog_snapshot(token))
 def test_rollback_does_not_advertise_uncommitted_changes(self):
  s=self.snapshot()
  try:
   with self.store.connect() as c:
    c.execute('BEGIN IMMEDIATE');self.store.update(self.pid,{'stock':99},1,connection=c);raise RuntimeError('rollback')
  except RuntimeError:pass
  self.assertTrue(self.store.catalog_snapshot(s['catalog_token'])['catalog_unchanged']);self.assertIsNone(self.store.get(self.pid)['stock'])
 def test_large_change_set_is_paged_without_losing_later_updates(self):
  s=self.snapshot();ids=self.store.import_rows([{'title_zh':f'Batch{i}'} for i in range(500)])['created'];self.store.update(self.pid,{'stock':2},1)
  first=self.store.catalog_snapshot(s['catalog_token']);self.assertNotIn('products',first);self.assertTrue(first['catalog_has_more']);self.assertEqual(len(first['product_changes']),500)
  # A write while the client drains the backlog must also be delivered.
  self.store.update(ids[0],{'stock':3},1);second=self.store.catalog_snapshot(first['catalog_token']);self.assertFalse(second['catalog_has_more']);self.assertNotIn('products',second)
  changes={p['id']:p for p in first['product_changes']+second['product_changes']};self.assertEqual(len(changes),501);self.assertEqual(changes[self.pid]['stock'],2);self.assertEqual(changes[ids[0]]['stock'],3)
  self.assertTrue(self.store.catalog_snapshot(second['catalog_token'])['catalog_unchanged'])
 def test_time_bucket_refresh_keeps_unchanged_catalog_compact(self):
  s=self.snapshot()
  with patch('core.time.time',return_value=1800000015):self.assertTrue(self.store.catalog_snapshot(s['catalog_token'])['catalog_unchanged'])
 def test_product_event_for_missing_id_removes_client_record(self):
  s=self.snapshot()
  with self.store.connect() as c:
   c.execute('DELETE FROM products WHERE id=?',(self.pid,));self.store.event(c,self.pid,'删除测试','')
  change=self.store.catalog_snapshot(s['catalog_token']);self.assertEqual(change['removed_product_ids'],[self.pid]);self.assertEqual(change['product_changes'],[])
if __name__=='__main__':unittest.main()
