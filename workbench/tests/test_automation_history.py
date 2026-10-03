import sys,json,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from core import Store,Problem,now
from automation import Automation

class HistoryTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.store=Store(self.tmp.name);self.auto=Automation(SimpleNamespace(store=self.store))
  self.pid=self.store.import_rows([{'title_zh':'旧夹子100%','partner_sku':'HistorySKU','source_sku':'variant_old'}])['created'][0]
  with self.store.connect() as c:
   for i in range(125):
    c.execute('INSERT INTO automation_runs VALUES(?,?,?,?,?,?,?,?,?)',(str(i),str(i),'digest','历史流程 '+str(i),json.dumps({'steps':['source']}),'paused' if i==0 else 'done',now(),now(),now()))
    c.execute('INSERT INTO automation_items VALUES(?,?,?,?,?,?,?,?,?,?)',('item'+str(i),str(i),self.pid if i==0 else 'other',1,0,'attention' if i==0 else 'done',0,'{}','说明',now()))
 def tearDown(self):self.tmp.cleanup()
 def test_all_history_pages_no_omissions(self):
  ids=[]
  for p in range(13):
   data=self.auto.state(p);self.assertEqual(data['total'],125);self.assertEqual(data['pages'],13);self.assertLessEqual(len(data['runs']),10)
   ids.extend(r['id'] for r in data['runs']);self.assertTrue(all(i['run_id'] in [r['id'] for r in data['runs']] for i in data['items']))
  self.assertEqual(len(set(ids)),125);self.assertEqual(ids[0],'124');self.assertEqual(ids[-1],'0')
 def test_old_unfinished_visible_and_global_count(self):
  self.assertEqual(self.auto.state()['active_count'],1)
  self.assertEqual(self.auto.state()['item_counts'],{'attention':1,'done':124})
  for group in ['active','attention','paused']:
   r=self.auto.state(group=group);self.assertEqual([x['id'] for x in r['runs']],['0'])
  self.assertEqual(self.auto.state(group='done')['total'],124);self.assertEqual(self.auto.state(group='approval')['total'],0)
 def test_search_names_skus_and_literal_symbols(self):
  for q in ['旧夹子','100%',self.store.get(self.pid)['partner_sku'].lower(),'variant_old']:
   self.assertEqual([r['id'] for r in self.auto.state(query=q)['runs']],['0'])
  self.assertEqual(self.auto.state(query="' OR 1=1 --")['total'],0)
  self.assertEqual(self.auto.state(query='历史流程 124')['total'],1)
  self.assertEqual(self.auto.state(group='done',query='旧夹子')['total'],0)
 def test_page_clamp_and_invalid_requests(self):
  self.assertEqual(self.auto.state(999)['page'],12)
  self.assertEqual(self.auto.state(999,query='absent')['page'],0)
  for args in [(-1,),('bad',),(0,'bad'),(0,'all','x'*201)]:
   with self.assertRaises(Problem):self.auto.state(*args)
