const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawn } = require('node:child_process');

(async () => {
  const root = path.resolve(__dirname, '../..');
  const preparedBrowsers = path.resolve(root, '../.noonai-assets/playwright');
  const browserCache = fs.existsSync(preparedBrowsers) ? preparedBrowsers : path.join(root, '.cloud-runtime/cache/playwright');
  if (!process.env.PLAYWRIGHT_BROWSERS_PATH && fs.existsSync(browserCache)) {
    process.env.PLAYWRIGHT_BROWSERS_PATH = browserCache;
  }
  const { chromium } = require('playwright');
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
    for (const view of ['products', 'import', 'automation', 'models', 'recovery', 'batch']) {
      await page.locator(`[data-nav="${view}"]`).first().click();
      await page.locator(`[data-nav="${view}"][aria-current="page"]`).waitFor();
      await page.waitForTimeout(150);
    }
    assert.deepEqual(errors, [], 'uncaught browser JavaScript errors');
    console.log('PASS real Chromium startup and six navigation surfaces');
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
