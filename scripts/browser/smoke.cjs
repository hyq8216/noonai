const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawn, spawnSync } = require('node:child_process');

(async () => {
  const root = path.resolve(__dirname, '../..');
  const pythonExe = process.env.NOON_PYTHON || path.join(root, '.venv/bin/python');
  const fakeCodexDir = fs.mkdtempSync(path.join(os.tmpdir(), 'noonai-fake-codex-'));
  const fakeCodex = path.join(fakeCodexDir, 'codex');
  fs.writeFileSync(fakeCodex, `#!${pythonExe}\n${String.raw`
import json,sys,time
content={'title_en':'Synthetic black storage clips','description_en':'Black plastic storage clips.','title_ar':'مشابك تخزين سوداء','description_ar':'مشابك تخزين بلاستيكية سوداء','warnings':[]}
def send(value): print(json.dumps(value),flush=True)
for line in sys.stdin:
 d=json.loads(line);method=d.get('method');params=d.get('params',{});rid=d.get('id')
 if rid is None: continue
 if method=='config/read': result={'config':{'mcp_servers':{},'model_provider':'openai'}}
 elif method=='account/read': result={'account':{'type':'chatgpt','planType':'fixture'}}
 elif method=='modelProvider/capabilities/read': result={'imageGeneration':False}
 elif method=='model/list': result={'data':[{'model':'gpt-6-luna','displayName':'Fixture Luna'}]}
 elif method=='account/rateLimits/read': result={'rateLimitsByLimitId':{'codex':{'primary':{'usedPercent':1,'resetsAt':int(time.time())+600,'windowDurationMins':300}}}}
 elif method=='thread/start': result={'thread':{'id':'browser-fixture-thread'},'model':params['model']}
 elif method=='turn/start':
  send({'id':rid,'result':{'turn':{'id':'browser-fixture-turn','status':'inProgress'}}})
  schema=params['outputSchema']['properties']
  output={'passed':True,'warnings':[]} if 'passed' in schema else content
  send({'method':'thread/tokenUsage/updated','params':{'threadId':'browser-fixture-thread','turnId':'browser-fixture-turn','tokenUsage':{'last':{'inputTokens':120,'outputTokens':60}}}})
  send({'method':'item/completed','params':{'threadId':'browser-fixture-thread','turnId':'browser-fixture-turn','item':{'type':'agentMessage','phase':'final_answer','text':json.dumps(output,ensure_ascii=False)}}})
  send({'method':'turn/completed','params':{'threadId':'browser-fixture-thread','turn':{'id':'browser-fixture-turn','status':'completed'}}})
  continue
 else: result={}
 send({'id':rid,'result':result})
`}`, {mode:0o700});
  fs.chmodSync(fakeCodex,0o700);
  process.env.PATH = `${fakeCodexDir}:${process.env.PATH || ''}`;
  const preparedBrowsers = path.resolve(root, '../.noonai-assets/playwright');
  const browserCache = fs.existsSync(preparedBrowsers) ? preparedBrowsers : path.join(root, '.cloud-runtime/cache/playwright');
  if (!process.env.PLAYWRIGHT_BROWSERS_PATH && fs.existsSync(browserCache)) {
    process.env.PLAYWRIGHT_BROWSERS_PATH = browserCache;
  }
  const { chromium } = require('playwright');
  const data = fs.mkdtempSync(path.join(os.tmpdir(), 'noonai-browser-'));
  const ready = path.join(data, 'ready.json');
  const seededBudget = spawnSync(pythonExe, ['-c', `
import json,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1]+'/workbench')
from core import Store,ident,now
from models import Models
store=Store(Path(sys.argv[2]));models=Models(store)
pid=models.save({'name':'QA budget alert','provider':'openai','model':'gpt-6-luna','enabled':True,'api_key':'synthetic-browser-only','input_price':'.10','output_price':'.50','daily_usd':'1','daily_calls':5,'rpm':10})['id']
profile=models.get(pid);stamp=now()
with store.connect() as c:
 for index,status in enumerate(('done','done','done','uncertain')):
  c.execute('INSERT INTO model_calls VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',(ident(),'browser-budget-'+str(index),'synthetic-digest-'+str(index),pid,json.dumps(profile),None,status,35,35,None,None,'Synthetic browser alert fixture',stamp,stamp))
`, root, data], {encoding:'utf8'});
  assert.equal(seededBudget.status, 0, seededBudget.stderr);
  const env = Object.fromEntries(Object.entries(process.env).filter(([key]) =>
    !/^(NOON_|OPENAI_|TEXT_|IMAGE_HOST_)/.test(key)));
  env.PATH = `${fakeCodexDir}:${env.PATH || ''}`;
  const server = spawn(pythonExe,
    [path.join(root, 'workbench/server.py'), '--data', data, '--port', '0', '--ready-file', ready],
    { env, stdio: ['ignore', 'ignore', 'pipe'] });
  let logs = '';
  server.stderr.on('data', chunk => { logs += chunk; });
  const exited = new Promise(resolve => server.once('exit', resolve));
  let browser;
  const errors = [];
  try {
    for (let i = 0; !fs.existsSync(ready); i++) {
      assert.equal(server.exitCode, null, logs);
      assert.ok(i < 200, 'server startup timed out');
      await new Promise(resolve => setTimeout(resolve, 50));
    }
    const { url } = JSON.parse(fs.readFileSync(ready));
    const seededCatalog = spawnSync(pythonExe, ['-c', `
import json,sys,time,tracemalloc
from pathlib import Path
sys.path.insert(0,sys.argv[1]+'/workbench')
from core import Store
from operations import Operations
store=Store(Path(sys.argv[2]));started=time.perf_counter()
for start in range(0,10000,500):
 store.import_rows([{'title_zh':f'性能样本商品 {n:05d}','supplier':'固定性能夹具','source_url':f'https://supplier.example/perf/{n}','source_sku':f'PERF-{n:05d}','facts':'合成规格，不是真实供应商数据','cost_cny':12.5,'stock':20} for n in range(start,start+500)])
seed_seconds=time.perf_counter()-started
trace=[];connect=store.connect
def traced_connect():
 connection=connect();connection.set_trace_callback(trace.append);return connection
store.connect=traced_connect
tracemalloc.start();tracemalloc.reset_peak();started=time.perf_counter();snapshot=store.catalog_snapshot()
full_seconds=time.perf_counter()-started;full_bytes=len(json.dumps(snapshot,ensure_ascii=False,separators=(',',':')).encode())
_,peak_bytes=tracemalloc.get_traced_memory();full_sql_count=len(trace);token=snapshot['catalog_token']
trace.clear();started=time.perf_counter();unchanged=store.catalog_snapshot(token);unchanged_seconds=time.perf_counter()-started
unchanged_sql_count=len(trace);trace.clear();product=store.list()[5000];store.update(product['id'],{'stock':21},product['revision']);trace.clear()
started=time.perf_counter();delta=store.catalog_snapshot(token);delta_seconds=time.perf_counter()-started
delta_bytes=len(json.dumps(delta,ensure_ascii=False,separators=(',',':')).encode())
product=next(p for p in store.list() if p['source_sku']=='PERF-00001')
store.record_offer(product['id'],product['partner_sku'],{'partner_sku':product['partner_sku'],'sku':'NOON-QA','offers':[{'offer_code':'OFFER-QA','country_code':'sa','business_model':'noon','price':{'amount':0,'currency':'SAR'},'is_active':True,'active_net_stock':3,'live_status':True,'offer_issues':[]}]},product['revision'])
stale=next(p for p in store.list() if p['source_sku']=='PERF-00002')
store.record_offer(stale['id'],stale['partner_sku'],{'partner_sku':stale['partner_sku'],'sku':'NOON-QA-2','offers':[{'offer_code':'OFFER-QA-2','country_code':'sa','business_model':'noon','price':{'amount':10,'currency':'SAR'},'is_active':True,'active_net_stock':3,'live_status':True,'offer_issues':[]}]},stale['revision'])
store.update(stale['id'],{'note':'合成浏览器用例：商品变更后报价回读已过期'},stale['revision'])
with store.connect() as c:latest=c.execute('SELECT coalesce(max(id),0) FROM events').fetchone()[0]
trace.clear();started=time.perf_counter();boundary=store.catalog_snapshot(f'{store.catalog_session}:{latest}:{int(time.time()//15)-1}')
boundary_seconds=time.perf_counter()-started;boundary_sql_count=len(trace)
assert boundary.get('catalog_unchanged'),boundary
ops=Operations(store)
warehouse=ops.transact('entity',{'request_id':'browser-stock-warehouse','kind':'warehouse','name':'浏览器测试库存仓'})
shop=ops.transact('entity',{'request_id':'browser-stock-shop','kind':'shop','name':'浏览器测试店铺'})
stock_product=next(p for p in store.list() if p['source_sku']=='PERF-00003')
stock_product=store.update(stock_product['id'],{'mode':'LOCAL'},stock_product['revision'])
ops.transact('adjust',{'request_id':'browser-stock-adjust','product_id':stock_product['id'],'warehouse_id':warehouse['id'],'direction':'in','quantity':20,'reason':'合成浏览器期初库存'})
ops.transact('order',{'request_id':'browser-stock-order','shop_id':shop['id'],'warehouse_id':warehouse['id'],'external_id':'BROWSER-STOCK-ORDER','currency':'SAR','lines':[{'product_id':stock_product['id'],'quantity':3,'unit_price':'10'}]})
print(json.dumps({'rows':len(snapshot['products']),'seed_seconds':round(seed_seconds,3),
 'full_snapshot_seconds':round(full_seconds,3),'full_payload_bytes':full_bytes,'snapshot_peak_tracemalloc_bytes':peak_bytes,
 'full_snapshot_sql_statements':full_sql_count,'unchanged_seconds':round(unchanged_seconds,6),
 'unchanged_payload_keys':sorted(unchanged),'unchanged_sql_statements':unchanged_sql_count,
 'time_bucket_boundary_seconds':round(boundary_seconds,6),'time_bucket_boundary_sql_statements':boundary_sql_count,
 'one_product_delta_seconds':round(delta_seconds,6),'one_product_delta_count':len(delta.get('product_changes',[])),
 'one_product_delta_bytes':delta_bytes},separators=(',',':')))
`, root, data], {encoding:'utf8'});
    assert.equal(seededCatalog.status, 0, seededCatalog.stderr);
    const backendMetrics = JSON.parse(seededCatalog.stdout);
    assert.equal(backendMetrics.rows, 10000);
    assert.equal(backendMetrics.unchanged_payload_keys.includes('products'), false);
    assert.equal(backendMetrics.one_product_delta_count, 1);
    const photoBytes = spawnSync(pythonExe, ['-c',
      "from PIL import Image;import io,base64;f=io.BytesIO();Image.new('RGB',(400,400),'#4080c0').save(f,format='PNG');print(base64.b64encode(f.getvalue()).decode())"
    ], {encoding:'utf8'});
    assert.equal(photoBytes.status, 0, photoBytes.stderr);
    browser = await chromium.launch({ headless: true });
    const page = await browser.newPage();
    page.setDefaultTimeout(10000);
    page.on('pageerror', error => errors.push(error.message));
    page.on('dialog', dialog => dialog.accept());
    const firstLoadStarted = Date.now();
    await page.goto(url);
    await page.locator('[data-nav="batch"][aria-current="page"]').waitFor();
    const navigateTo = async view => {
      const item = page.locator(`[data-nav="${view}"]`).first();
      if (!(await item.isVisible())) {
        await item.locator('xpath=ancestor::section[contains(@class,"nav-group")]//button[contains(@class,"nav-group-toggle")]').click();
      }
      await item.click();
      try {
        await page.locator(`[data-nav="${view}"][aria-current="page"]`).waitFor();
      } catch (error) {
        const state = await page.evaluate(() => ({active:document.querySelector('.nav-item[aria-current="page"]')?.dataset.nav,
          expanded:[...document.querySelectorAll('.nav-group-toggle')].filter(el=>el.getAttribute('aria-expanded')==='true').map(el=>el.dataset.navGroup),
          dirty:window.dirty, heading:document.querySelector('h1')?.textContent}));
        throw new Error(`${view} navigation failed; state=${JSON.stringify(state)}; ${error.message}`);
      }
      assert.equal(await item.isVisible(),true,`${view} should be visible in its expanded group`);
      assert.equal(await page.locator('.nav-group-toggle[aria-expanded="true"]').count(),1,`${view} should leave one group expanded`);
    };
    const navGroups = page.locator('.nav-group-toggle');
    assert.equal(await navGroups.count(), 6, 'sidebar should group the 18 destinations into six sections');
    const navIds = await page.locator('.nav-item').evaluateAll(items => items.map(item => item.dataset.nav));
    assert.equal(navIds.length,18,'all 18 destinations should be present');
    assert.equal(new Set(navIds).size,18,'each destination should appear exactly once');
    assert.equal(await page.locator('[data-nav-group="listing"]').getAttribute('aria-expanded'), 'true', 'bulk listing group should open first');
    assert.equal(await page.locator('.nav-group-toggle[aria-expanded="true"]').count(), 1, 'only one navigation group may be open');
    assert.equal(await page.locator('[data-nav="products"]').isVisible(), true);
    assert.equal(await page.locator('[data-nav="orders"]').isVisible(), false);
    await page.locator('[data-nav-group="fulfillment"]').click();
    assert.equal(await page.locator('[data-nav-group="fulfillment"]').getAttribute('aria-expanded'), 'true');
    assert.equal(await page.locator('[data-nav-group="listing"]').getAttribute('aria-expanded'), 'false');
    await page.locator('[data-nav="orders"]').click();
    await page.locator('[data-nav="orders"][aria-current="page"]').waitFor();
    assert.equal(await page.locator('[data-nav-group="fulfillment"]').getAttribute('aria-expanded'), 'true', 'navigation should expand the active destination group');
    assert.equal(await page.locator('.nav-group-toggle[aria-expanded="true"]').count(), 1);
    await page.setViewportSize({width:390,height:844});
    const mobileNav = await page.locator('.nav').evaluate(el => ({height:el.clientHeight,scrollHeight:el.scrollHeight,activeVisible:(()=>{const item=el.querySelector('[aria-current="page"]');const box=item.getBoundingClientRect(),nav=el.getBoundingClientRect();return box.top>=nav.top&&box.bottom<=nav.bottom})()}));
    assert.ok(mobileNav.height<=300, JSON.stringify(mobileNav));
    assert.ok(mobileNav.scrollHeight>=mobileNav.height, JSON.stringify(mobileNav));
    assert.equal(mobileNav.activeVisible,true,JSON.stringify(mobileNav));
    await page.setViewportSize({width:1280,height:900});
    const startupReadyMs = Date.now() - firstLoadStarted;
    const initialStateTiming = await page.evaluate(() => {
      const rows = performance.getEntriesByType('resource').filter(item => item.name.includes('/api/state?'));
      const item = rows[rows.length - 1];
      return item ? {duration_ms: Math.round(item.duration), encoded_bytes: item.encodedBodySize || 0} : null;
    });
    let catalogPageMs = null, catalogSearchMs = null, catalogPaginationPassed = false;
    const navigatedViews=['batch','overview','products','import','media','visuals','models','platform','automation','jobs','orders','purchases','inventory','warehouse','partners','finance','recovery','settings'];
    for (const view of navigatedViews) {
      const viewStarted = Date.now();
      await navigateTo(view);
      if (view === 'settings') {
        const sourcing = page.locator('.settings-row').filter({hasText:'1688 货源'});
        assert.match(await sourcing.innerText(), /申请开发者与应用/);
        assert.match(await sourcing.innerText(), /商品搜索\/详情\/规格库存读取权限/);
        assert.equal(await sourcing.locator('a[href="https://aop.alibaba.com/"]').count(),1);
        assert.equal(await sourcing.locator('a[href="https://aop.alibaba.com/doc/notice.htm"]').count(),1);
      }
      if (view === 'products') {
        await page.getByText('共 10000 件 · 第 1 / 200 页 · 每页50件').waitFor();
        catalogPageMs = Date.now() - viewStarted;
        const searchStarted = Date.now();
        await page.locator('#search').fill('PERF-09999');
        await page.getByRole('button', {name: '性能样本商品 09999'}).waitFor();
        catalogSearchMs = Date.now() - searchStarted;
        await page.locator('#search').fill('');
        await page.getByText('共 10000 件 · 第 1 / 200 页 · 每页50件').waitFor();
        await page.locator('#catalog-next').click();
        await page.getByText('共 10000 件 · 第 2 / 200 页 · 每页50件').waitFor();
        assert.equal(await page.locator('[data-select]').count(), 50);
        await page.locator('#catalog-prev').click();
        await page.getByText('共 10000 件 · 第 1 / 200 页 · 每页50件').waitFor();
        catalogPaginationPassed = true;
      }
      if (view === 'batch') {
        const before = spawnSync(pythonExe, ['-c', `
import sqlite3,sys
db=sqlite3.connect(sys.argv[1]+'/workbench.sqlite3')
print('%s|%s'%(db.execute('SELECT count(*) FROM automation_runs').fetchone()[0],db.execute('SELECT count(*) FROM model_calls').fetchone()[0]))
`, data], {encoding:'utf8'});
        assert.equal(before.status, 0, before.stderr);
        await page.locator('#batch-search').fill('PERF-09999');
        await page.getByText('共 1 件 · 第 1 / 1 页 · 每页 50 件').waitFor();
        assert.equal(await page.locator('#campaign-preview').isEnabled(), true,
          'the filtered result should be eligible for read-only campaign preview');
        await page.locator('#campaign-preview').click();
        await page.getByText(/当前筛选 1 件，分为 1 批；可安排 0 件，待补资料或配置 1 件，已有任务 0 件。/).waitFor();
        await page.getByText(/预计文字调用 0 次.*预览没有调用模型/).waitFor();
        assert.equal(await page.locator('#campaign-apply').isDisabled(), true,
          'a campaign with no eligible items must not be schedulable');
        const unchanged = spawnSync(pythonExe, ['-c', `
import sqlite3,sys
db=sqlite3.connect(sys.argv[1]+'/workbench.sqlite3')
print('%s|%s'%(db.execute('SELECT count(*) FROM automation_runs').fetchone()[0],db.execute('SELECT count(*) FROM model_calls').fetchone()[0]))
`, data], {encoding:'utf8'});
        assert.equal(unchanged.status, 0, unchanged.stderr);
        assert.equal(unchanged.stdout.trim(), before.stdout.trim(),
          'preview with missing model route must not create a workflow or call a model');
        await page.locator('#campaign-clear').click();
        await page.locator('#batch-search').fill('');
      }
      if (view === 'media') {
        await page.locator('#media-bulk-import').click();
        const png = Buffer.from(photoBytes.stdout.trim(), 'base64');
        await page.locator('#photo-files').setInputFiles([
          {name:'PERF-09999__main.png', mimeType:'image/png', buffer:png},
          {name:'NO-SUCH-SKU.png', mimeType:'image/png', buffer:png}
        ]);
        await page.locator('#photo-rights').fill('合成浏览器测试素材使用依据');
        assert.equal(await page.locator('#photo-preview').isDisabled(), false, 'selected photos should enable matching preview');
        const photoPreviewResponse = page.waitForResponse(response => response.url().includes('/api/media/import-preview'), {timeout:5000});
        await page.locator('#photo-preview').click();
        const photoPreviewHttp = await photoPreviewResponse;
        assert.equal(photoPreviewHttp.status(), 200, await photoPreviewHttp.text());
        await page.getByText('匹配结果').waitFor();
        const photoPreviewText = await page.locator('body').innerText();
        assert.match(photoPreviewText, /1 件商品 · 可导入 1 张 · 需处理 1 张 · 已完成 0 张/, photoPreviewText.slice(-1800));
        await page.getByText('NO-SUCH-SKU.png', {exact:true}).waitFor();
        assert.equal(await page.locator('#photo-import').isEnabled(), true,
          'a valid row may be imported while an unmatched file remains isolated');
        await page.locator('#photo-import').click();
        await page.getByText('已保存原图并关联商品').waitFor({timeout:10000});
        await page.getByText('NO-SUCH-SKU.png', {exact:true}).waitFor();
        const photoReadback = spawnSync(pythonExe, ['-c', `
import json,sqlite3,sys
db=sqlite3.connect(sys.argv[1]+'/workbench.sqlite3')
rows=[json.loads(r[0]) for r in db.execute("SELECT data FROM media_assets WHERE json_extract(data,'$.kind')='image'")]
assets=[r for r in rows if r.get('name')=='PERF-09999__main.png']
assert len(assets)==1, assets
asset=assets[0]
product=db.execute('SELECT data FROM products WHERE id=?',(asset['product_id'],)).fetchone()
assert json.loads(product[0])['source_sku']=='PERF-09999'
assert asset['rights']=='合成浏览器测试素材使用依据'
print('linked|rights-recorded|one-asset')
`, data], {encoding:'utf8'});
        assert.equal(photoReadback.status, 0, photoReadback.stderr);
        assert.equal(photoReadback.stdout.trim(), 'linked|rights-recorded|one-asset');
        await page.evaluate(() => {dirty=false;resetPhotoImport();render()});
      }
      if (view === 'models') {
        const budgetRow = page.locator('.settings-row').filter({hasText:'QA budget alert'});
        await budgetRow.getByText('即将达到本机预算上限').waitFor();
        await budgetRow.getByText('1 次调用结果待核对，仍计入本机上限').waitFor();
      }
      if (view === 'platform') {
        await page.locator('#platform-query').fill('PERF-00001');
        await page.locator('#offer-group').selectOption('unknown');
        await page.locator('#platform-search button').click();
        const offer = page.locator('summary').filter({hasText:'沙特报价与前台状态：部分报价需核对'});
        await offer.waitFor();
        await offer.click();
        await page.getByText('返回信息矛盾，待核对').waitFor();
        await page.locator('#platform-query').fill('PERF-00002');
        await page.locator('#platform-search button').click();
        const staleOffer = page.locator('summary').filter({hasText:'沙特报价与前台状态：本地资料已变化，报价回读过期'});
        await staleOffer.waitFor();
        await staleOffer.click();
        await page.getByText('报价回读已过期').waitFor();
      }
      if (view === 'inventory') {
        await page.locator('#stock-plan-warehouse').selectOption({label:'浏览器测试库存仓'});
        await page.locator('#f-buffer').fill('2');
        await page.locator('#f-cap').fill('8');
        await page.locator('#stock-plan-form button').click();
        const stockRow=page.locator('#stock-plan-result tbody tr').filter({hasText:'性能样本商品 00003'});
        await stockRow.waitFor();
        const stockText=await stockRow.innerText();
        assert.match(stockText,/20\s*\/\s*0/);
        assert.match(stockText,/3\s*\/\s*2/);
        assert.match(stockText,/8/);
        assert.match(stockText,/供应商可供：20（未计入）/);
      }
      if (view === 'automation') {
        await page.locator('[data-scheduler-health]').getByText('到期待执行 0 项 · 执行中 0 项 · 当前并发 1 项').waitFor();
        await page.locator('[data-scheduler-health]').getByText('当前没有因额度或服务状态等待的流程项。').waitFor();
        const scheduler=page.locator('#scheduler-settings');
        await scheduler.locator('select[name="concurrency_limit"]').selectOption('2');
        await scheduler.locator('select[name="scan_interval_ms"]').selectOption('2000');
        await scheduler.locator('button').click();
        await page.locator('[data-scheduler-health]').getByText('到期待执行 0 项 · 执行中 0 项 · 当前并发 2 项').waitFor();
        const parallelState=await page.evaluate(async()=>(await(await fetch('/api/state?surface=automation')).json()).automation.scheduler);
        assert.equal(parallelState.concurrency_limit,2);
        assert.equal(parallelState.scan_interval_ms,2000);
        await scheduler.locator('select[name="concurrency_limit"]').selectOption('1');
        await scheduler.locator('select[name="scan_interval_ms"]').selectOption('400');
        await scheduler.locator('button').click();
        await page.locator('[data-scheduler-health]').getByText('到期待执行 0 项 · 执行中 0 项 · 当前并发 1 项').waitFor();
        const defaultState=await page.evaluate(async()=>(await(await fetch('/api/state?surface=automation')).json()).automation.scheduler);
        assert.equal(defaultState.scan_interval_ms,400);
      }
      if (view === 'recovery') {
        await page.getByText(/备份空间与手动清理预览/).waitFor();
        await page.locator('#create-backup').click();
        await page.getByText('备份已创建并校验，可下载到其他位置').waitFor();
        await page.getByText(/磁盘剩余/).waitFor();
        const backupHealth = await page.evaluate(async () => (await (await fetch('/api/state')).json()).recovery);
        assert.equal(backupHealth.archive_count, 1);
        assert.ok(backupHealth.archives[0].expanded_bytes > 0);
        assert.equal(backupHealth.disk.estimated_required_bytes, 2 * backupHealth.archives[0].expanded_bytes + 64 * 1024 * 1024);
        assert.ok(Number.isFinite(backupHealth.newest_archive_age_seconds));
        await page.locator('#retention-preview-form input[name="keep"]').fill('1');
        await page.locator('#retention-preview-form button[type="submit"]').click();
        await page.getByText(/预览结果：保留 1 份，候选 0 份/).waitFor();
        await page.getByText('此操作没有删除文件。').waitFor();
        const schedule = page.locator('#backup-schedule-form');
        await schedule.locator('input[name="enabled"]').check();
        await schedule.locator('select[name="interval_hours"]').selectOption('6');
        await schedule.locator('input[name="keep_count"]').fill('2');
        await schedule.locator('button[type="submit"]').click();
        await page.getByText(/状态：scheduled/).waitFor();
        const scheduledState = await page.evaluate(async () => (await (await fetch('/api/state')).json()).recovery.schedule);
        assert.equal(scheduledState.enabled, true);
        assert.equal(scheduledState.interval_hours, 6);
        assert.equal(scheduledState.keep_count, 2);
        assert.equal(scheduledState.background_agent, 'application_only');
        await page.getByText(/需保持应用运行，到期后自动执行/).waitFor();
        await schedule.locator('input[name="enabled"]').uncheck();
        await schedule.locator('button[type="submit"]').click();
        await page.getByText(/状态：disabled/).waitFor();
      }
      await page.waitForTimeout(150);
    }
    console.log('10k catalog metrics:', JSON.stringify({...backendMetrics,startup_ready_ms:startupReadyMs,
      initial_state_resource:initialStateTiming,product_list_ready_ms:catalogPageMs,exact_sku_search_ms:catalogSearchMs,
      fifty_item_pagination_passed:catalogPaginationPassed}));
    const token = await page.evaluate(async () => (await (await fetch('/api/state')).json()).token);
    const imported = await page.evaluate(async token => {
      const response = await fetch('/api/import', {
        method: 'POST', headers: {'Content-Type':'application/json','X-Workbench-Token':token},
        body: JSON.stringify({products:[{title_zh:'Smoke uncertain submit',source_url:'https://supplier.example/smoke-'+crypto.randomUUID(),supplier:'Smoke supplier',facts:'One sample item'}]})
      });
      if (!response.ok) throw new Error(`fixture import failed: ${response.status}`);
      return response.json();
    }, token);
    const productId = imported.created[0];
    const seeded = spawnSync(pythonExe, ['-c',
      "import sys;sys.path.insert(0,sys.argv[1]+'/workbench');from core import Store;s=Store(sys.argv[2]);p=s.get(sys.argv[3]);j=s.add_job(p['id'],'submit',p['revision']);s.job_result(j,'uncertain','synthetic browser smoke receipt');print(j)",
      root, data, productId], {encoding:'utf8'});
    assert.equal(seeded.status, 0, seeded.stderr);
    await navigateTo('jobs');
    const reconcile = page.locator('form[data-submit-reconcile]').first();
    await reconcile.locator('xpath=ancestor::details/summary').click();
    await reconcile.waitFor();
    await reconcile.locator('textarea[name="evidence"]').fill('Seller Lab 按 SKU 搜索，确认没有对应刊登');
    await reconcile.locator('input[name="confirmed"]').check();
    await reconcile.locator('button[type="submit"]').click();
    await page.getByText('已记录人工核对结果；重试仍需操作员重新安排').waitFor();
    await page.getByText('人工核对已记录 · 确认未找到').waitFor();
    assert.equal(await page.locator('form[data-submit-reconcile]').count(), 0, 'resolved row must not offer duplicate reconciliation');
    const verified = spawnSync(pythonExe, ['-c',
      "import sys;sys.path.insert(0,sys.argv[1]+'/workbench');from core import Store;s=Store(sys.argv[2]);c=s.connect();r=c.execute('SELECT outcome FROM submit_reconciliations').fetchone();j=c.execute(\"SELECT status FROM jobs WHERE id=(SELECT job_id FROM submit_reconciliations)\").fetchone();print(r[0]+'|'+j[0]);c.close()",
      root, data], {encoding:'utf8'});
    assert.equal(verified.status, 0, verified.stderr);
    assert.equal(verified.stdout.trim(), 'absent|uncertain', 'reconciliation must be durable and must not rewrite the external job outcome');
    const catalog = spawnSync(pythonExe, ['-c', `
import csv,hashlib,io,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1]+'/workbench')
from server import App
app=App(Path(sys.argv[2]));box=app.source_inbox
box.configure({'enabled':True,'translate':False,'review':False})
out=io.StringIO(newline='');writer=csv.writer(out)
writer.writerow(['商品名称','货源链接','规格货号','供应商','规格事实','采购成本（人民币元）'])
rows=[[f'商品{i}',f'https://detail.1688.com/offer/{i}.html',f'S{i}','工厂','款式A','12'] for i in range(1,503)]
rows[0]=['重复商品','https://detail.1688.com/offer/1.html','S1','工厂','款式A','12'];rows[499]=list(rows[0])
rows[500]=['冲突甲','https://detail.1688.com/offer/9000.html','CONFLICT','工厂','款式A','12']
rows[501]=['冲突乙','https://detail.1688.com/offer/9000.html','CONFLICT','工厂','款式B','12'];rows[9][5]='不是价格'
writer.writerows(rows);content=out.getvalue();digest=hashlib.sha256(content.encode()).hexdigest()
box.process_large_catalog('browser-bulk.csv',digest,'.csv',content,{'translate':False,'review':False})
` , root, data], {encoding:'utf8'});
    assert.equal(catalog.status, 0, catalog.stderr);
    await navigateTo('import');
    await page.locator('#source-inbox-query').fill('browser-bulk.csv');
    await page.locator('#source-inbox-group').selectOption('all');
    await page.locator('#source-inbox-query').fill('browser-bulk.csv');
    const inboxListingResponse = page.waitForResponse(response => {
      const url = new URL(response.url());
      return url.pathname === '/api/source-inbox/list' && url.searchParams.get('query') === 'browser-bulk.csv';
    }, {timeout:45000});
    await page.locator('#source-inbox-history button').click();
    const inboxListingHttp = await inboxListingResponse;
    assert.equal(inboxListingHttp.status(),200,await inboxListingHttp.text());
    const filteredInbox = await inboxListingHttp.json();
    assert.equal(filteredInbox.files.length,1,JSON.stringify(filteredInbox));
    assert.equal(filteredInbox.files[0].name,'browser-bulk.csv');
    const inboxReadback = await page.evaluate(async () => (await (await fetch('/api/source-inbox/list?page=0&group=all&kind=all&query=browser-bulk.csv')).json()).files);
    assert.equal(inboxReadback.length, 1, JSON.stringify(inboxReadback));
    await page.locator('[data-source-inbox-detail="browser-bulk.csv"]').waitFor({timeout:20000});
    await page.locator('[data-source-inbox-detail="browser-bulk.csv"]').click();
    const issueExport = page.locator('#source-inbox-issues-export');
    await issueExport.waitFor();
    assert.equal(await issueExport.textContent(), '下载全部 4 条异常行');
    const [issueDownload] = await Promise.all([page.waitForEvent('download'), issueExport.click()]);
    const issuePath = await issueDownload.path();
    const issueCsv = fs.readFileSync(issuePath, 'utf8');
    assert.match(issueCsv, /"10","需修正"/);
    assert.match(issueCsv, /"500","跳过重复"/);
    assert.match(issueCsv, /"501","需修正"/);
    assert.match(issueCsv, /"502","需修正"/);
    const leadUrls = [
      `https://detail.1688.com/offer/lead-${Date.now()}.html?track=one`,
      `https://supplier.example/browser-lead-${Date.now()}`
    ];
    await page.locator('#source-lead-links').fill(leadUrls.join('\n'));
    await page.locator('#source-lead-preview').click();
    await page.getByText('可登记 2 · 已入库 0 · 候选池已有 0 · 本清单重复 0 · 链接无效 0').waitFor();
    await page.locator('#source-lead-add').click();
    await page.getByText(/已登记 2 条候选链接，跳过 0 条/).waitFor();
    assert.equal(await page.locator('.section-break').getByText(leadUrls[0], {exact:false}).count(), 1);
    const [leadDownload] = await Promise.all([
      page.waitForEvent('download'), page.locator('#source-lead-export').click()
    ]);
    const leadCsv = fs.readFileSync(await leadDownload.path(), 'utf8');
    assert.match(leadCsv, /商品名称/);
    assert.ok(leadUrls.every(url => leadCsv.includes(url)), 'candidate export should include both saved URLs');
    await page.getByText('已导出 2 条尚未入库的链接').waitFor();
    assert.equal(await page.locator('#source-lead-export').textContent(), '下载待补资料 CSV · 第 1 份');
    await page.setViewportSize({width:390,height:844});
    for (const view of navIds) {
      await navigateTo(view);
      const layout=await page.evaluate(()=>{const active=document.querySelector('.nav-item[aria-current="page"]'),nav=document.querySelector('.nav'),item=active.getBoundingClientRect(),navBox=nav.getBoundingClientRect();return {viewport:innerWidth,documentWidth:document.documentElement.scrollWidth,activeVisible:item.top>=navBox.top-1&&item.bottom<=navBox.bottom+1,activeTop:item.top,activeBottom:item.bottom,navTop:navBox.top,navBottom:navBox.bottom,navScrollTop:nav.scrollTop,navClientHeight:nav.clientHeight,navScrollHeight:nav.scrollHeight}});
      assert.ok(layout.documentWidth<=layout.viewport,`${view} causes horizontal page overflow: ${JSON.stringify(layout)}`);
      assert.equal(layout.activeVisible,true,`${view} active navigation is outside the mobile menu viewport: ${JSON.stringify(layout)}`);
    }
    const campaignPage = await browser.newPage();
    campaignPage.setDefaultTimeout(10000);
    campaignPage.on('pageerror', error => errors.push(error.message));
    campaignPage.on('dialog', dialog => dialog.accept());
    await campaignPage.goto(url);
    await campaignPage.locator('[data-nav="batch"][aria-current="page"]').waitFor();
    const configureModel = spawnSync(pythonExe, ['-c', `
import sys
from pathlib import Path
sys.path.insert(0,sys.argv[1]+'/workbench')
from core import Store
from models import Models
store=Store(Path(sys.argv[2]));models=Models(store)
profile=models.save({'name':'浏览器离线订阅夹具','provider':'codex-subscription','model':'gpt-6-luna','enabled':True,'daily_calls':10})['id']
models.route({'role':'primary','profile_id':profile});models.route({'role':'review','profile_id':profile})
product=next(p for p in store.list() if p['source_sku']=='PERF-09999')
store.update(product['id'],{'facts':'黑色塑料收纳夹，10件装','supplier':'合成浏览器测试供应商'},product['revision'])
models.codex.close()
print('fixture-profile-ready')
`, root, data], {encoding:'utf8'});
    assert.equal(configureModel.status,0,`${configureModel.stderr} stdout=${configureModel.stdout}`);
    await campaignPage.locator('#batch-options [name="review"]').check();
    await campaignPage.locator('#batch-search').fill('PERF-09999');
    await campaignPage.locator('#campaign-preview').click();
    await campaignPage.getByText(/当前筛选 1 件，分为 1 批；可安排 1 件，待补资料或配置 0 件，已有任务 0 件。/).waitFor();
    await campaignPage.getByText(/预计文字调用 2 次.*预览没有调用模型/).waitFor();
    assert.equal(await campaignPage.locator('#campaign-apply').isEnabled(),true);
    await campaignPage.locator('#campaign-apply').click();
    await campaignPage.locator('[data-nav="automation"][aria-current="page"]').waitFor();
    await campaignPage.waitForFunction(() => [...document.querySelectorAll('.auto-item')].some(item =>
      item.innerText.includes('待人工审核') && item.innerText.includes('请打开商品')),null,{timeout:30000});
    const workflowReadback = spawnSync(pythonExe, ['-c', `
import json,sqlite3,sys
db=sqlite3.connect(sys.argv[1]+'/workbench.sqlite3');db.row_factory=sqlite3.Row
saved=db.execute("SELECT id,data,approved_revision FROM products WHERE json_extract(data,'$.source_sku')='PERF-09999'").fetchone();product=json.loads(saved['data'])
items=[dict(r) for r in db.execute("SELECT status,step,data FROM automation_items WHERE product_id=?",(saved['id'],))]
calls=[dict(r) for r in db.execute("SELECT status,usage,profile FROM model_calls WHERE product_id=?",(saved['id'],))]
assert product['title_en']=='Synthetic black storage clips',product
assert 'مشابك' in product['title_ar'],product
assert saved['approved_revision'] is None, saved['approved_revision']
assert len(calls)==2 and all(c['status']=='done' for c in calls),calls
assert len(items)==1 and items[0]['status']=='approval' and items[0]['step']==4,items
assert db.execute('SELECT count(*) FROM jobs WHERE product_id=?',(saved['id'],)).fetchone()[0]==0
print('translation=done|review=done|workflow=approval|human_approval=required|noon_writes=0')
`, data], {encoding:'utf8'});
    assert.equal(workflowReadback.status,0,workflowReadback.stderr);
    assert.equal(workflowReadback.stdout.trim(),'translation=done|review=done|workflow=approval|human_approval=required|noon_writes=0');
    assert.deepEqual(errors, [], 'uncaught browser JavaScript errors');
    console.log('PASS real Chromium startup, all 18 grouped navigation destinations on desktop and 390px mobile, offline catalog campaign apply through bilingual generation and model review to required human-approval hold, no-call preview guard, stock preview, zero-price and stale-offer warnings, scheduler settings, original photo selection/matching/rights/import with unmatched-row isolation, manual submit reconciliation, cross-batch issue CSV download, and source-lead preview/add/export');
  } catch (error) {
    throw new Error(`${error.message}; browser errors: ${JSON.stringify(errors)}; server errors: ${logs}`);
  } finally {
    if (browser) await browser.close();
    if (server.exitCode === null) server.kill('SIGTERM');
    let shutdownTimer;
    await Promise.race([exited, new Promise(resolve => { shutdownTimer = setTimeout(resolve, 20000); })]);
    clearTimeout(shutdownTimer);
    if (server.exitCode === null) { server.kill('SIGKILL'); await exited; }
    fs.rmSync(data, { recursive: true, force: true });
    fs.rmSync(fakeCodexDir, { recursive: true, force: true });
    fs.rmSync(fakeCodexDir, { recursive: true, force: true });
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
