// Optional MV3 installation smoke test: synthetic documents, real extension APIs.
// Run with the bundled full Chromium, not chromium-headless-shell.
const assert=require('node:assert/strict');
const fs=require('node:fs');
const os=require('node:os');
const path=require('node:path');
const {createHash}=require('node:crypto');
const {chromium}=require('playwright');

const extension=fs.realpathSync(path.resolve(__dirname,'../../browser-extension/domestic-capture'));
const extensionId=createHash('sha256').update(extension).digest('hex').slice(0,32).replace(/[0-9a-f]/g,c=>String.fromCharCode(97+parseInt(c,16)));
const cases=[
 {provider:'1688',url:'https://detail.1688.com/offer/123456.html',sku:'BLACK-5',price:2.5,stock:0,html:'<h1 data-product-title="合成1688黑色夹子">合成1688黑色夹子</h1><div data-product-id="123456" data-sku-id="BLACK-5" data-price="2.50" data-currency="CNY" data-stock="0">黑色5件装</div><p data-product-id="123456" data-product-fact>材质：合成塑料</p>'},
 {provider:'taobao',url:'https://item.taobao.com/item.htm?id=234567&spm=tracking',sku:'BLUE-5',price:0,stock:5,html:'<h1>合成淘宝蓝色夹子</h1><script type="application/ld+json">{"@type":"Product","url":"https://item.taobao.com/item.htm?id=234567","name":"合成淘宝蓝色夹子","offers":{"@type":"Offer","sku":"BLUE-5","price":"0","priceCurrency":"CNY","inventoryLevel":{"value":5}},"cookie":"SYNTHETIC-COOKIE-NEVER-EXPORT","token":"SYNTHETIC-TOKEN-NEVER-EXPORT"}</script>'},
 {provider:'pinduoduo',url:'https://mobile.yangkeduo.com/goods.html?goods_id=345678',sku:'',price:null,stock:null,html:'<h1>合成拼多多缺规格商品</h1><script type="application/json" data-product-json>{"product":{"productID":"345678","name":"合成拼多多缺规格商品","credentials":{"password":"SYNTHETIC-PASSWORD-NEVER-EXPORT"}}}</script>'},
];

(async()=>{
 const temporary=fs.mkdtempSync(path.join(os.tmpdir(),'noonai-mv3-install-'));
 const profile=path.join(temporary,'profile'),downloads=path.join(temporary,'downloads');
 fs.mkdirSync(path.join(profile,'Default'),{recursive:true});fs.mkdirSync(downloads);
 fs.writeFileSync(path.join(profile,'Default','Preferences'),JSON.stringify({download:{default_directory:downloads,prompt_for_download:false,directory_upgrade:true}}));
 let context;const blocked=[],errors=[],results=[];
 try{
  context=await chromium.launchPersistentContext(profile,{headless:true,channel:'chromium',acceptDownloads:true,args:[
   '--disable-extensions-except='+extension,'--load-extension='+extension,'--disable-background-networking','--disable-component-update','--disable-sync','--no-first-run','--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE localhost'
  ]});
  // DNS resolution is disabled too: all external-page content is supplied locally.
  await context.route(/^https?:\/\//,async route=>{
   const fixture=cases.find(c=>c.url===route.request().url());
   if(fixture&&route.request().resourceType()==='document')return route.fulfill({status:200,contentType:'text/html;charset=utf-8',body:'<!doctype html><html><meta charset="utf-8"><title>合成扩展安装测试</title>'+fixture.html+'</html>'});
   blocked.push(route.request().url());return route.abort();
  });
  const popup=await context.newPage();popup.setDefaultTimeout(10000);popup.on('pageerror',e=>errors.push(e.message));
  await popup.goto('chrome-extension://'+extensionId+'/popup.html');
  await popup.locator('#capture').waitFor();
  const installed=await popup.evaluate(()=>({id:chrome.runtime.id,manifest:chrome.runtime.getManifest(),apis:[typeof chrome.tabs.query,typeof chrome.scripting.executeScript,typeof chrome.downloads.download]}));
  assert.equal(installed.id,extensionId);assert.equal(installed.manifest.manifest_version,3);assert.equal(installed.manifest.action.default_popup,'popup.html');assert.deepEqual(installed.apis,['function','function','function']);assert.deepEqual(installed.manifest.permissions,['scripting','downloads']);
  const cdp=await context.newCDPSession(popup);
  await cdp.send('Browser.setDownloadBehavior',{behavior:'allow',downloadPath:downloads,eventsEnabled:true});
  for(const fixture of cases){
   const platformTab=await context.newPage();await platformTab.goto(fixture.url);
   await context.addCookies([{name:'synthetic-private-session',value:'SYNTHETIC-SESSION-NEVER-EXPORT',url:fixture.url}]);
   // popup is a genuine extension page; activate the platform tab through real tabs API.
   const tabId=await popup.evaluate(async url=>{const tabs=await chrome.tabs.query({});const tab=tabs.find(t=>t.url===url);if(!tab)throw Error('Synthetic platform tab not found');await chrome.tabs.update(tab.id,{active:true});return tab.id},fixture.url);
   const before=await popup.evaluate(async()=> (await chrome.downloads.search({})).map(d=>d.id));
   await popup.evaluate(()=>document.getElementById('capture').click());
   await popup.waitForFunction(()=>!document.getElementById('capture').disabled,undefined,{timeout:15000});
   const status=await popup.locator('#status').innerText();
   assert.match(status,/已提取/,'Real scripting/download invocation failed: '+status);
  // Chrome can report `complete` just before exposing the final local path in
  // downloads.search(). Wait for both fields; otherwise the immediate second
  // query can race the receipt update on Linux CI.
  await popup.waitForFunction(async ids=>(await chrome.downloads.search({})).some(d=>!ids.includes(d.id)&&d.state==='complete'&&typeof d.filename==='string'&&d.filename.length>0),before,{timeout:15000});
  const receipt=await popup.evaluate(async ids=>(await chrome.downloads.search({})).find(d=>!ids.includes(d.id)&&d.state==='complete'&&typeof d.filename==='string'&&d.filename.length>0),before);
   assert.ok(receipt.filename&&fs.existsSync(receipt.filename),'Real chrome.downloads receipt has no saved file');
   assert.ok(receipt.filename.startsWith(downloads+path.sep),'Download escaped the temporary test directory');
   assert.equal(receipt.byExtensionId,extensionId);assert.equal(receipt.error,undefined);
   const source=fs.readFileSync(receipt.filename,'utf8'),data=JSON.parse(source);
   assert.equal(data.provider,fixture.provider);assert.equal(data.items[0].sku,fixture.sku);assert.equal(data.items[0].source_price,fixture.price);assert.equal(data.items[0].stock,fixture.stock);
   assert.equal('cost_cny' in data.items[0],false);assert.ok(!source.includes('NEVER-EXPORT'),'Private session/token/password leaked into downloaded capture');
   assert.ok(data.items[0].source_url.startsWith(new URL(fixture.url).origin));
   if(!fixture.sku)assert.match(status,/缺真实规格货号/);
   results.push({provider:fixture.provider,platform_tab_id:tabId,items:data.items.length,download_api_receipt:true,bytes:Buffer.byteLength(source),missing_sku:!fixture.sku});
   await platformTab.close();
  }
  assert.deepEqual(errors,[]);
  console.log(JSON.stringify({status:'passed',mv3_installed:true,real_popup:true,real_tabs_scripting_downloads:true,synthetic_documents_only:true,blocked_other_http_requests:blocked.length,cases:results,limitations:'Proves unpacked installation and extension execution against local synthetic platform DOMs. Does not verify live marketplace compatibility or real account access.'},null,2));
 }finally{
  try{if(context)await context.close()}finally{fs.rmSync(temporary,{recursive:true,force:true})}
 }
})().catch(error=>{console.error('MV3 installation/execution test failed; do not treat parser-only tests as extension installation evidence. '+(error.stack||error));process.exitCode=1});
