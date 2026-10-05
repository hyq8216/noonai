const {clickNavigation}=require('./navigation_helpers.cjs');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawn } = require('node:child_process');
const { chromium } = require('playwright');

async function runWorkflow(name, workflow) {
  const root = path.resolve(__dirname, '../..');
  const data = fs.mkdtempSync(path.join(os.tmpdir(), 'noonai-local-workflow-'));
  const ready = path.join(data, 'ready.json');
  const env = Object.fromEntries(Object.entries(process.env).filter(([key]) =>
    !/^(NOON_|OPENAI_|TEXT_|IMAGE_HOST_)/.test(key)));
  const server = spawn(process.env.NOON_PYTHON || path.join(root, '.venv/bin/python'),
    [path.join(root, 'workbench/server.py'),'--data',data,'--port','0','--ready-file',ready],
    {env,stdio:['ignore','ignore','pipe']});
  let logs=''; server.stderr.on('data',chunk=>{logs+=chunk});
  const exited=new Promise(resolve=>server.once('exit',resolve));
  let browser; const errors=[];
  try {
    for(let i=0;!fs.existsSync(ready);i++){
      assert.equal(server.exitCode,null,logs);assert.ok(i<200,'server startup timed out');
      await new Promise(resolve=>setTimeout(resolve,50));
    }
    browser=await chromium.launch({headless:true});
    const page=await browser.newPage({viewport:{width:1440,height:1000}});
    page.setDefaultTimeout(10000);page.on('pageerror',error=>errors.push(error.message));
    await page.goto(JSON.parse(fs.readFileSync(ready)).url);
    await page.locator('.nav [data-nav]').first().waitFor();
    const settle=()=>page.waitForFunction(()=>!busy&&!!document.querySelector('.nav'));
    const call=(route,body)=>page.evaluate(async args=>api(args.route,args.body),{route,body});
    const navigate=async view=>{
      await settle();await clickNavigation(page,view);
      await page.locator(`.nav [data-nav="${view}"][aria-current="page"]`).waitFor({state:'attached'});
      await page.locator('main h1').waitFor();await settle();
    };
    const mobile=async()=>{
      await page.setViewportSize({width:390,height:900});
      assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),'mobile document overflow');
    };
    await workflow({page,call,navigate,settle,mobile});
    assert.deepEqual(errors,[],'uncaught JavaScript errors');
    console.log('PASS '+name+'; real local HTTP and Chromium, synthetic data, no seller or bank calls');
  }catch(error){error.message+=`; browser errors:${JSON.stringify(errors)}; server errors:${logs}`;throw error}
  finally{
    if(browser)await browser.close();if(server.exitCode===null)server.kill('SIGTERM');
    let timer;await Promise.race([exited,new Promise(resolve=>{timer=setTimeout(resolve,20000)})]);clearTimeout(timer);
    if(server.exitCode===null){server.kill('SIGKILL');await exited}
    fs.rmSync(data,{recursive:true,force:true});
  }
}
module.exports={runWorkflow};
