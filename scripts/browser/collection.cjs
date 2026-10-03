const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawn } = require('node:child_process');
const { chromium } = require('playwright');

(async () => {
  const root = path.resolve(__dirname, '../..');
  const data = fs.mkdtempSync(path.join(os.tmpdir(), 'noonai-collection-browser-'));
  const ready = path.join(data, 'ready.json');
  const env = Object.fromEntries(Object.entries(process.env).filter(([key]) =>
    !/^(NOON_|OPENAI_|TEXT_|IMAGE_HOST_)/.test(key)));
  const server = spawn(process.env.NOON_PYTHON || path.join(root, '.venv/bin/python'),
    [path.join(__dirname, 'collection_fixture.py'), '--data', data, '--ready-file', ready],
    { env, stdio: ['ignore','ignore','pipe'] });
  let logs = ''; server.stderr.on('data', chunk => { logs += chunk; });
  const exited = new Promise(resolve => server.once('exit', resolve));
  let browser;
  const errors = [];
  try {
    for (let i = 0; !fs.existsSync(ready); i++) {
      assert.equal(server.exitCode, null, logs);
      assert.ok(i < 200, 'fixture startup timed out');
      await new Promise(resolve => setTimeout(resolve, 50));
    }
    browser = await chromium.launch({ headless:true });
    const page = await browser.newPage({ viewport:{width:1440,height:1000} });
    let importBody;
    page.on('request', request => {
      if (request.url().endsWith('/api/collection/apply')) importBody=request.postDataJSON();
    });
    page.setDefaultTimeout(10000);
    page.on('pageerror', error => errors.push(error.message));
    const { url } = JSON.parse(fs.readFileSync(ready));
    await page.goto(url);
    await page.locator('.nav [data-nav="channels"]').click();
    // Foreign connectors are retained for historic records, but are no longer
    // offered by the new-account UI. Seed that legacy record through its API.
    await page.evaluate(async()=>{
      await api('/api/channels/save',{provider:'custom_json',name:'浏览器合成供应商',
        base_url:'https://example.com/catalog',enabled:true,config:{},token:'browser-synthetic-secret'});
      await sync();
    });
    await page.locator('[data-channel-edit]').waitFor();
    await page.locator('[data-channel-edit]').click();
    assert.equal(await page.locator('#channel-token').inputValue(), '');
    await page.locator('#channel-account-form button[type="submit"]').click();
    await page.locator('#channel-account-form').waitFor({state:'detached'});
    const accounts = await page.evaluate(async () => (await (await fetch('/api/channels/state')).json()).accounts);
    assert.equal(accounts.length, 1); assert.equal(accounts[0].has_token, true);
    assert.equal(JSON.stringify(accounts).includes('browser-synthetic-secret'), false);
    await page.locator('#channel-collect-account').selectOption(accounts[0].id);
    await page.locator('#channel-collect-limit').fill('1');
    await page.locator('#channel-collect-query').fill('synthetic clips');
    await page.locator('#channel-collect-form button[type="submit"]').click();
    await page.locator('[data-channel-select]').first().waitFor();
    assert.equal(await page.locator('[data-channel-select]').count(), 2);
    let products = await page.evaluate(async () => (await (await fetch('/api/state')).json()).products);
    assert.equal(products.length, 0, 'collection must not automatically create products');
    await page.locator('#channel-select-page').check();
    await page.locator('#channel-preview').click();
    await page.locator('#channel-import-confirm').waitFor();
    assert.equal(await page.locator('#channel-apply').isDisabled(), true);
    await page.locator('#channel-import-confirm').check();
    await page.locator('#channel-apply').click();
    await page.waitForFunction(async () => (await (await fetch('/api/state')).json()).products.length === 2);
    products = await page.evaluate(async () => (await (await fetch('/api/state')).json()).products);
    const black = products.find(p => p.source_sku === 'BLACK');
    assert.equal(black.stock, 0); assert.equal(black.cost_cny, null);
    assert.ok(products.every(p => !p.reviewed && !p.images.length && p.source_collection));
    assert.equal(await page.locator('#channel-apply').isDisabled(), true);
    const replay = await page.evaluate(async body => {
      const state = await (await fetch('/api/state')).json();
      const response = await fetch('/api/collection/apply', { method:'POST',
        headers:{'Content-Type':'application/json','X-Workbench-Token':state.token}, body:JSON.stringify(body) });
      return { status:response.status, data:await response.json() };
    }, importBody);
    assert.equal(replay.status, 200); assert.equal(replay.data.replayed, true);
    assert.equal((await page.evaluate(async () => (await (await fetch('/api/state')).json()).products)).length, 2);
    await page.locator('#channel-collect-query').fill('changed synthetic clips');
    await page.locator('#channel-collect-form button[type="submit"]').click();
    await page.locator('[data-channel-resolve-form]').waitFor({state:'attached'});
    const conflictForm = page.locator('[data-channel-resolve-form]');
    await conflictForm.locator('xpath=ancestor::details/summary').click();
    assert.equal(await conflictForm.locator('button[type="submit"]').isDisabled(), true);
    await conflictForm.locator('select[name="snapshot_index"]').selectOption('1');
    await conflictForm.locator('textarea[name="note"]').fill('合成验收：已核对最新来源记录');
    assert.equal(await conflictForm.locator('button[type="submit"]').isDisabled(), true);
    await conflictForm.locator('input[name="confirmed"]').check();
    await page.setViewportSize({width:390,height:900});
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth+1));
    await conflictForm.locator('button[type="submit"]').click();
    await page.locator('[data-channel-resolve-form]').waitFor({state:'detached'});
    products = await page.evaluate(async () => (await (await fetch('/api/state')).json()).products);
    assert.equal(products.find(p => p.source_sku === 'BLACK').stock, 0);
    assert.equal(products.find(p => p.source_sku === 'BLACK').facts, black.facts);
    const resolved = await page.evaluate(async () => (await (await fetch('/api/collection/state')).json()).candidates);
    assert.ok(resolved.some(row => row.normalized.stock === 7 && row.status === 'imported'));
    await page.locator('[data-channel-edit]').click();
    assert.equal(await page.locator('#channel-provider').isDisabled(), true);
    await page.locator('#channel-clear-token').check();
    await page.locator('#channel-account-form button[type="submit"]').click();
    await page.locator('#channel-account-form').waitFor({state:'detached'});
    assert.equal((await page.evaluate(async () => (await (await fetch('/api/channels/state')).json()).accounts))[0].has_token, false);
    await page.setViewportSize({width:390,height:900});
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth+1));
    assert.deepEqual(errors, []);
    console.log('PASS synthetic Chromium account save/edit/clear, credential redaction, collection -> preview -> confirmed import, replay, conflict confirmation without product overwrite, stock/currency/provenance and mobile layout; no real account calls');
  } catch (error) {
    throw new Error(`${error.message}; page errors: ${JSON.stringify(errors)}; fixture errors: ${logs}`);
  } finally {
    if (browser) await browser.close();
    if (server.exitCode === null) server.kill('SIGTERM');
    let timer;
    await Promise.race([exited, new Promise(resolve => { timer=setTimeout(resolve,20000); })]);
    clearTimeout(timer);
    if (server.exitCode === null) { server.kill('SIGKILL'); await exited; }
    fs.rmSync(data,{recursive:true,force:true});
  }
})().catch(error => { console.error(error); process.exitCode=1; });
