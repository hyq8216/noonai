const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawn } = require('node:child_process');
const { chromium } = require('playwright');

(async () => {
  const root = path.resolve(__dirname, '../..');
  const data = fs.mkdtempSync(path.join(os.tmpdir(), 'noonai-browser-'));
  const ready = path.join(data, 'ready.json');
  const env = Object.fromEntries(Object.entries(process.env).filter(([key]) =>
    !/^(NOON_|OPENAI_|TEXT_|IMAGE_HOST_)/.test(key)));
  const server = spawn(process.env.NOON_PYTHON || path.join(root, '.venv/bin/python'),
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
    browser = await chromium.launch({ headless: true });
    const page = await browser.newPage();
    page.setDefaultTimeout(10000);
    page.on('pageerror', error => errors.push(error.message));
    await page.goto(url);
    await page.locator('[data-nav="automation"]').first().waitFor();
    await page.locator('#nav-search').fill('1688');
    assert.equal(await page.locator('.nav [data-nav]').count(),2,'domestic collection must be searchable by supplier name');
    await page.locator('#nav-search').fill('');
    const views = await page.locator('.nav [data-nav]').evaluateAll(nodes =>
      nodes.map(node => node.dataset.nav));
    assert.equal(views.length, 40, 'all platform navigation surfaces must be covered');
    for (const width of [1440, 390]) {
      await page.setViewportSize({ width, height: 900 });
      for (const view of views) {
        await page.locator(`.nav [data-nav="${view}"]`).click();
        await page.locator(`.nav [data-nav="${view}"][aria-current="page"]`).waitFor();
        await page.locator('main h1').waitFor();
        await page.waitForTimeout(150);
        const overflow = await page.evaluate(() =>
          document.documentElement.scrollWidth - window.innerWidth);
        assert.ok(overflow <= 1, `${view} overflows the ${width}px viewport by ${overflow}px`);
      }
    }
    assert.deepEqual(errors, [], 'uncaught browser JavaScript errors');
    console.log('PASS real Chromium startup, all 40 navigation surfaces at 1440px and 390px, no page overflow or uncaught JavaScript errors');
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
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
