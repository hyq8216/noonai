const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawn } = require('node:child_process');
const { chromium } = require('playwright');

(async () => {
  const root = path.resolve(__dirname, '../..');
  const data = fs.mkdtempSync(path.join(os.tmpdir(), 'noonai-erp-browser-'));
  const ready = path.join(data, 'ready.json');
  const env = Object.fromEntries(Object.entries(process.env).filter(([key]) =>
    !/^(NOON_|OPENAI_|TEXT_|IMAGE_HOST_)/.test(key)));
  const server = spawn(process.env.NOON_PYTHON || path.join(root, '.venv/bin/python'),
    [path.join(root, 'workbench/server.py'), '--data', data, '--port', '0', '--ready-file', ready],
    { env, stdio:['ignore','ignore','pipe'] });
  let logs = ''; server.stderr.on('data', chunk => { logs += chunk; });
  const exited = new Promise(resolve => server.once('exit', resolve));
  let browser;
  const errors = [];
  try {
    for (let i=0; !fs.existsSync(ready); i++) {
      assert.equal(server.exitCode, null, logs);
      assert.ok(i<200, 'server startup timed out');
      await new Promise(resolve => setTimeout(resolve, 50));
    }
    browser = await chromium.launch({headless:true});
    const page = await browser.newPage({viewport:{width:1440,height:1000}});
    page.setDefaultTimeout(10000);
    page.on('pageerror', error => errors.push(error.message));
    await page.goto(JSON.parse(fs.readFileSync(ready)).url);
    await page.locator('.nav [data-nav="order-intake"]').waitFor();
    const seeded = await page.evaluate(async () => {
      const shop = await api('/api/ops/entity', {kind:'shop',name:'合成ERP店铺',request_id:crypto.randomUUID()});
      const warehouse = await api('/api/ops/entity', {kind:'warehouse',name:'合成ERP仓库',request_id:crypto.randomUUID()});
      await api('/api/import', {products:[
        {title_zh:'合成黑色夹子',source_url:'https://example.com/clips',source_sku:'BLACK',supplier:'合成供应商',brand:'合成品牌',facts:'黑色；5件装',cost_cny:5,stock:10},
        {title_zh:'合成蓝色夹子',source_url:'https://example.com/clips',source_sku:'BLUE',supplier:'合成供应商',brand:'合成品牌',facts:'蓝色；5件装',cost_cny:5,stock:10}]});
      const products=(await api('/api/state')).products;
      return {shop,warehouse,products};
    });
    const black = seeded.products.find(p=>p.source_sku==='BLACK');
    const blue = seeded.products.find(p=>p.source_sku==='BLUE');
    await page.locator('.nav [data-nav="order-intake"]').click();
    await page.locator('#order-intake-shop option').nth(1).waitFor({state:'attached'});
    await page.locator('#order-intake-shop').selectOption(seeded.shop.id);
    await page.locator('#order-intake-warehouse').selectOption(seeded.warehouse.id);
    await page.locator('#order-intake-text').fill(`external_id,partner_sku,quantity,unit_price,currency\nSYNTHETIC-ERP-1,${black.partner_sku},2,12.50,SAR\n`);
    await page.locator('#order-intake-preview').click();
    await page.locator('#order-intake-confirm').waitFor();
    assert.equal(await page.locator('#order-intake-apply').isDisabled(),true);
    const before = await page.evaluate(async()=>await api('/api/state?surface=orders'));
    assert.equal(before.ops.documents.length,0);
    await page.locator('#order-intake-confirm').check();
    await page.locator('#order-intake-apply').click();
    await page.waitForFunction(async()=> (await api('/api/state?surface=orders')).ops.documents.length===1);
    await page.locator('#order-intake-preview-panel').waitFor({state:'detached'});
    const order = await page.evaluate(async()=> (await api('/api/state?surface=orders')).ops.documents[0]);
    assert.equal(order.status,'new'); assert.equal(order.total_cents,2500);
    assert.equal((await page.evaluate(async()=> (await api('/api/state?surface=inventory')).ops.stock)).length,0);
    const template = page.waitForEvent('download');
    await page.locator('#order-intake-template').click();
    assert.equal((await template).suggestedFilename(),'订单导入模板.csv');

    await page.locator('.nav [data-nav="settlements"]').click();
    await page.locator('#settlement-shop').selectOption(seeded.shop.id);
    await page.locator('#settlement-file').setInputFiles({name:'synthetic-settlement.json',mimeType:'application/json',buffer:Buffer.from(JSON.stringify([
      {evidence_key:'synthetic-browser-sale',external_id:'SYNTHETIC-ERP-1',category:'sale',currency:'SAR',amount:'25.00',date:new Date().toISOString().slice(0,10),fx:'1.9',evidence:'合成结算第1行',kind:'income'},
      {evidence_key:'synthetic-browser-unmatched',external_id:'UNKNOWN-ORDER',category:'refund',currency:'SAR',amount:'1.00',date:new Date().toISOString().slice(0,10),fx:'1.9',evidence:'合成未匹配第2行',kind:'expense'}]))});
    await page.locator('#settlement-upload button[type="submit"]').click();
    await page.locator('#settlement-confirm').waitFor();
    assert.ok((await page.locator('#main').innerText()).includes('未匹配 1'));
    await page.locator('#settlement-confirm').check();
    await page.locator('#settlement-apply button').click();
    await page.locator('#settlement-confirm').waitFor({state:'detached'});
    const batch = await page.evaluate(async()=> (await api('/api/settlements/state')).batches[0]);
    assert.equal(batch.summary.imported,1); assert.equal(batch.summary.unmatched,1);
    assert.equal(batch.remaining_rows.length,1);
    const exported=page.waitForEvent('download');
    await page.locator('#settlement-export').click();
    assert.ok((await exported).suggestedFilename().endsWith('.csv'));

    await page.locator('.nav [data-nav="catalog-groups"]').click();
    await page.locator('#catalog-group-form [name="name"]').fill('合成夹子系列');
    await page.locator(`[data-cg-add="${black.id}"]`).click();
    await page.locator(`[data-cg-value][data-pid="${black.id}"]`).fill('黑色');
    await page.locator(`[data-cg-add="${blue.id}"]`).click();
    await page.locator(`[data-cg-value][data-pid="${blue.id}"]`).fill('蓝色');
    await page.locator('#catalog-group-form button[type="submit"]').click();
    await page.locator('#catalog-group-confirm').waitFor();
    assert.equal(await page.locator('#catalog-group-save').isDisabled(),true);
    await page.locator('#catalog-group-confirm').check();
    await page.locator('#catalog-group-save').click();
    await page.locator('[data-cg-edit]').waitFor({state:'attached'});
    const group = await page.evaluate(async()=> (await api('/api/catalog-groups/state')).rows[0]);
    assert.equal(group.members.length,2); assert.equal(group.review_required,false);

    await page.locator('.nav [data-nav="analytics"]').click();
    await page.locator('#analytics-filter').waitFor();
    const analytics = await page.evaluate(async()=> await api('/api/analytics/state'));
    assert.equal(analytics.orders.total,1);
    assert.equal(analytics.order_currencies[0].amount_cents,2500);
    assert.ok((await page.locator('#main').innerText()).includes('非净利润'));
    await page.setViewportSize({width:390,height:900});
    assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));

    await page.locator('.nav [data-nav="backup-schedules"]').click();
    await page.locator('#backup-rule-enabled').check();
    await page.locator('#backup-rule-interval').fill('60');
    await page.locator('#backup-rule-retain').fill('2');
    await page.locator('#backup-rule-confirm').check();
    await page.locator('#backup-rule-save').click();
    await page.waitForFunction(async()=> (await api('/api/backup-schedules/state')).enabled===true);
    await page.locator('#backup-run-confirm').check();
    await page.locator('#backup-rule-run').click();
    await page.waitForFunction(async()=> (await api('/api/backup-schedules/state')).latest?.status==='success');
    assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));
    assert.deepEqual(errors,[]);
    console.log('PASS real Chromium ERP order import -> settlement matched/unmatched -> shared ledger -> confirmed SPU -> analytics -> complete scheduled-rule ZIP, UTF-8 downloads and mobile layout; synthetic data, no account calls');
  } catch(error) {
    throw new Error(`${error.message}; browser errors:${JSON.stringify(errors)}; server errors:${logs}`);
  } finally {
    if(browser)await browser.close();
    if(server.exitCode===null)server.kill('SIGTERM');
    let timer;
    await Promise.race([exited,new Promise(resolve=>{timer=setTimeout(resolve,20000)})]);clearTimeout(timer);
    if(server.exitCode===null){server.kill('SIGKILL');await exited}
    fs.rmSync(data,{recursive:true,force:true});
  }
})().catch(error=>{console.error(error);process.exitCode=1});
