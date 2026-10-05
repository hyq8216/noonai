const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const {randomUUID}=require('node:crypto');
const {runWorkflow}=require('./harness.cjs');

runWorkflow('domestic browser capture and candidates',async({page,call,navigate,settle,mobile})=>{
 const parser=fs.readFileSync(path.join(__dirname,'../../browser-extension/domestic-capture/parser.js'),'utf8');
 const browser=page.context().browser();const fixture=await browser.newPage();const external=[];
 fixture.on('request',request=>external.push(request.url()));
 const cases=[
  {provider:'1688',url:'https://detail.1688.com/offer/123456.html',html:'<h1 data-product-title="合成1688黑色夹子">合成1688黑色夹子</h1><div data-product-id="123456" data-sku-id="BLACK-5" data-price="2.50" data-currency="CNY" data-stock="0">黑色5件装</div><p data-product-id="123456" data-product-fact>材质：合成塑料</p>',sku:'BLACK-5',price:2.5,stock:0},
  {provider:'taobao',url:'https://item.taobao.com/item.htm?id=234567&spm=tracking',html:'<h1>合成淘宝蓝色夹子</h1><script type="application/ld+json">{"@type":"Product","url":"https://item.taobao.com/item.htm?id=234567","name":"合成淘宝蓝色夹子","offers":{"@type":"Offer","sku":"BLUE-5","price":"0","priceCurrency":"CNY","inventoryLevel":{"value":5}},"cookie":"do-not-export-cookie","token":"do-not-export-token","brand":{"name":"合成品牌"}}</script>',sku:'BLUE-5',price:0,stock:5},
  {provider:'pinduoduo',url:'https://mobile.yangkeduo.com/goods.html?goods_id=345678',html:'<h1>合成拼多多夹子</h1><script type="application/json" data-product-json>{"product":{"productID":"345678","name":"合成拼多多夹子","sku":"PDD-RED","offers":{"price":"9.90","priceCurrency":"CNY"},"credentials":{"password":"do-not-export-password"}}}</script>',sku:'PDD-RED',price:9.9,stock:null},
 ];
 const captures=[];
 for(const test of cases){
  await fixture.setContent(test.html);
  await fixture.evaluate(()=>{
   Object.defineProperty(document,'cookie',{get(){throw Error('parser attempted cookie read');},configurable:true});
   window.fetch=()=>{throw Error('parser attempted network');};window.XMLHttpRequest=function(){throw Error('parser attempted private request');};
   Object.defineProperty(window,'localStorage',{get(){throw Error('parser attempted browser storage');},configurable:true});
   Object.defineProperty(window,'__INITIAL_STATE__',{get(){throw Error('parser attempted page globals');},configurable:true});
  });
  await fixture.addScriptTag({content:parser});
  const capture=await fixture.evaluate(url=>domesticCaptureParse(document,url,'2026-10-03T12:00:00+00:00'),test.url);
  assert.equal(capture.provider,test.provider);assert.equal(capture.items[0].sku,test.sku);assert.equal(capture.items[0].source_price,test.price);assert.equal(capture.items[0].stock,test.stock,'DOM fixture stock: '+test.provider);
  assert.ok(!JSON.stringify(capture).includes('do-not-export'));assert.equal('cost_cny' in capture.items[0],false);captures.push(capture);
 }
 await fixture.setContent('<h1>合成缺规格商品</h1><span data-price-currency="CNY">人民币</span><div data-price="2-5">¥2–5</div>');await fixture.addScriptTag({content:parser});
 const missing=await fixture.evaluate(()=>domesticCaptureParse(document,'https://detail.1688.com/offer/888888.html','2026-10-03T12:00:00+00:00'));
 assert.equal(missing.items[0].sku,'');assert.equal(missing.items[0].source_price,null,'missing-SKU DOM must keep price unknown');assert.equal(missing.items[0].stock,null,'missing-SKU DOM must keep stock unknown');
 await fixture.setContent('<h1>请登录</h1><input type="password">');await fixture.addScriptTag({content:parser});
 const login=await fixture.evaluate(()=>{try{domesticCaptureParse(document,'https://item.taobao.com/item.htm?id=234567');return null;}catch(e){return e.message;}});
 assert.ok(login.includes('登录'));
 const unsupported=await fixture.evaluate(()=>{try{domesticCaptureParse(document,'https://detail.1688.com.evil.com/offer/123456.html');return null;}catch(e){return e.message;}});assert.ok(unsupported.includes('不支持'));
 // Regression: a different/unbound JSON-LD product never supplies the current page SKU/title.
 await fixture.setContent('<h1>当前商品标题</h1><span data-price-currency="CNY">人民币</span><script type="application/ld+json">{"@type":"Product","url":"https://detail.1688.com/offer/999999.html","name":"其他商品标题","sku":"OTHER","offers":{"price":"7","priceCurrency":"CNY"}}</script>');await fixture.addScriptTag({content:parser});
 const wrongProduct=await fixture.evaluate(()=>domesticCaptureParse(document,'https://detail.1688.com/offer/123456.html','2026-10-03T12:00:00+00:00'));
 assert.equal(wrongProduct.items[0].sku,'');assert.equal(wrongProduct.items[0].title,'当前商品标题');assert.equal(wrongProduct.items[0].source_price,null);assert.ok(wrongProduct.warnings.some(w=>w.includes('绑定')));
 await fixture.setContent('<h1>当前商品标题</h1><span data-price-currency="CNY">人民币</span><script type="application/ld+json">{"@type":"Product","name":"未绑定商品标题","sku":"UNBOUND"}</script>');await fixture.addScriptTag({content:parser});
 assert.equal((await fixture.evaluate(()=>domesticCaptureParse(document,'https://detail.1688.com/offer/123456.html'))).items[0].sku,'');
 await fixture.setContent('<h1>当前商品标题</h1><span data-price-currency="CNY">人民币</span><script type="application/ld+json">{"@type":"Product","url":"https://detail.1688.com/offer/999999.html","productID":"123456","name":"身份矛盾商品","sku":"CONTRADICTED"}</script>');await fixture.addScriptTag({content:parser});
 assert.equal((await fixture.evaluate(()=>domesticCaptureParse(document,'https://detail.1688.com/offer/123456.html'))).items[0].sku,'');

 // Regression: credential-like image URL query keys are stripped before a downloadable package exists.
 // Regression: recommended SKU nodes must not be reassigned to the current URL product.
 await fixture.setContent('<h1>当前商品标题</h1><span data-price-currency="CNY">人民币</span><aside data-product-id="999999"><button data-sku-id="RECOMMEND-SKU" data-price="9" data-currency="CNY" data-stock="5">推荐</button><span data-product-fact>推荐商品事实</span><meta itemprop="image" content="https://example.com/recommend.png"></aside>');await fixture.addScriptTag({content:parser});
 const recommended=await fixture.evaluate(()=>domesticCaptureParse(document,'https://detail.1688.com/offer/123456.html'));
 assert.equal(recommended.items[0].sku,'');assert.equal(recommended.items[0].source_price,null);assert.equal(recommended.items[0].facts,'');assert.deepEqual(recommended.items[0].images,[]);assert.ok(recommended.warnings.some(w=>w.includes('其他商品')));
 await fixture.setContent('<h1>当前商品标题</h1><span data-price-currency="CNY">人民币</span><button data-sku-id="UNBOUND-SKU" data-price="9" data-stock="5">未绑定可见规格</button>');await fixture.addScriptTag({content:parser});
 assert.equal((await fixture.evaluate(()=>domesticCaptureParse(document,'https://detail.1688.com/offer/123456.html'))).items[0].sku,'');
 const sensitive=['session_id','api_key','refresh_token','csrf','csrf_token','Cookie','authorization','access-token','x-amz-signature'];
 const secretImages=sensitive.map(k=>'https://example.com/image.png?'+k+'=EXPORT-SECRET');
 await fixture.setContent('<h1>当前商品标题</h1><script type="application/ld+json">'+JSON.stringify({'@type':'Product',productID:'123456',name:'安全商品',sku:'SAFE',offers:{price:'7',priceCurrency:'CNY'},image:secretImages})+'</script>');await fixture.addScriptTag({content:parser});
 const noSecrets=await fixture.evaluate(()=>domesticCaptureParse(document,'https://detail.1688.com/offer/123456.html'));
 assert.deepEqual(noSecrets.items[0].images,[]);assert.ok(!JSON.stringify(noSecrets).includes('EXPORT-SECRET'));assert.ok(noSecrets.warnings.some(w=>w.includes('图片')));
 // Regression: actual USD remains USD; unknown currency remains missing, never silently CNY.
 await fixture.setContent('<h1>当前商品标题</h1><script type="application/ld+json">'+JSON.stringify({'@type':'Product',productID:'123456',name:'美元售价',sku:'USD-SKU',offers:{price:'7',priceCurrency:'USD'}})+'</script>');await fixture.addScriptTag({content:parser});
 const usd=await fixture.evaluate(()=>domesticCaptureParse(document,'https://detail.1688.com/offer/123456.html'));
 assert.equal(usd.items[0].source_currency,'USD');assert.ok(usd.warnings.some(w=>w.includes('币种')));
 await fixture.setContent('<h1>当前商品标题</h1><div data-product-id="123456" data-sku-id="UNKNOWN" data-price="7">可见规格</div>');await fixture.addScriptTag({content:parser});
 const unknownCurrency=await fixture.evaluate(()=>domesticCaptureParse(document,'https://detail.1688.com/offer/123456.html'));
 assert.equal(unknownCurrency.items[0].source_currency,'');
 assert.deepEqual(external,[]);await fixture.close();
 const manifest=JSON.parse(fs.readFileSync(path.join(__dirname,'../../browser-extension/domestic-capture/manifest.json')));
 assert.deepEqual(manifest.permissions,['scripting','downloads']);assert.equal(!!manifest.content_scripts,false);assert.equal(!!manifest.background,false);
 const accounts=[];for(const provider of ['1688','taobao','pinduoduo'])accounts.push(await call('/api/channels/save',{provider,name:'合成'+provider+'来源',base_url:'',config:{},enabled:true,revision:0}));
 for(const denied of [usd,unknownCurrency]){const response=await page.evaluate(async args=>{const r=await fetch('/api/domestic-capture/preview',{method:'POST',headers:{'Content-Type':'application/json','X-Workbench-Token':state.token},body:JSON.stringify(args)});return {status:r.status,body:await r.json()};},{account_id:accounts[0].id,account_revision:accounts[0].revision,package:denied});assert.equal(response.status,400);assert.ok(response.body.error.includes('人民币'));}
 await navigate('domestic-capture');
 // Download the actual extension bundle exposed by the local server.
 const downloading=page.waitForEvent('download');await page.locator('a[href="/api/domestic-capture/extension"]').click();assert.ok((await downloading).suggestedFilename().endsWith('.zip'));
 const upload=async(index,data)=>{
  await page.locator('#domestic-account').selectOption(accounts[index].id);
  await page.locator('#domestic-file').setInputFiles({name:'synthetic-domestic.json',mimeType:'application/json',buffer:Buffer.from(JSON.stringify(data))});
  await page.locator('#domestic-preview').click();await settle();await page.locator('#domestic-confirm').waitFor();
 };
 await upload(0,captures[0]);assert.equal(await page.locator('#domestic-apply').isDisabled(),true);assert.equal((await call('/api/collection/state')).total,0);assert.equal((await call('/api/state')).products.length,0);
 // Changing the uploaded file must remove a stale confirmation, even before a new preview.
 await page.locator('#domestic-file').setInputFiles({name:'synthetic-domestic-again.json',mimeType:'application/json',buffer:Buffer.from(JSON.stringify(captures[0]))});
 assert.equal(await page.locator('#domestic-confirm').count(),0);await page.locator('#domestic-account').selectOption(accounts[0].id);await page.locator('#domestic-preview').click();await settle();
 await mobile();await page.locator('#domestic-preview-panel details summary').first().click();await mobile();
 await page.locator('#domestic-confirm').check();await page.locator('#domestic-apply').click();await settle();await page.locator('#domestic-product-preview').waitFor();
 assert.equal((await call('/api/state')).products.length,0);assert.equal((await call('/api/collection/state')).total,1);
 await page.locator('#domestic-product-preview').click();await settle();await page.locator('#domestic-product-confirm').waitFor();assert.equal(await page.locator('#domestic-product-apply').isDisabled(),true);
 await page.locator('#domestic-product-confirm').check();await page.locator('#domestic-product-apply').click();await settle();
 await page.waitForFunction(async()=> (await api('/api/state')).products.length===1);
 let products=(await call('/api/state')).products;assert.equal(products[0].source_sku,'BLACK-5');assert.equal(products[0].cost_cny,null);assert.equal(products[0].stock,0);assert.equal(products[0].reviewed,false);
 for(let i=1;i<3;i++){
  await upload(i,captures[i]);await page.locator('#domestic-confirm').check();await page.locator('#domestic-apply').click();await settle();
  await page.locator('#domestic-product-preview').click();await settle();await page.locator('#domestic-product-confirm').check();await page.locator('#domestic-product-apply').click();await settle();
 }
 assert.equal((await call('/api/state')).products.length,3);
 await upload(0,missing);await page.locator('#domestic-confirm').check();await page.locator('#domestic-apply').click();await settle();await page.locator('#domestic-product-preview').click();await settle();
 await page.locator('#domestic-product-confirm').check();assert.equal(await page.locator('#domestic-product-apply').isDisabled(),true);assert.equal((await call('/api/state')).products.length,3);
 const candidates=(await call('/api/collection/state')).candidates;assert.equal(candidates.find(c=>c.raw.external_product_id==='888888').status,'blocked');
 // Human correction works inside the app without changing the downloaded original package.
 await page.locator('[data-domestic-correction-details="1"] summary').click();
 await page.locator('[data-domestic-correction="sku"]').fill('MANUAL-VERIFIED-SKU');
 await page.locator('[data-domestic-correction="supplier"]').fill('人工核对合成供应商');
 await page.locator('[data-domestic-correction="facts"]').fill('合成规格单：黑色5件装');
 await page.locator('[data-domestic-correction="evidence"]').fill('合成供应商规格单第8行；浏览器测试依据');
 await page.locator('[data-domestic-correction-details="1"] summary').click();
 await page.locator('[data-domestic-correction-details="1"] summary').click();
 assert.equal(await page.locator('[data-domestic-correction="sku"]').inputValue(),'MANUAL-VERIFIED-SKU');
 assert.equal(await page.locator('[data-domestic-correction="evidence"]').inputValue(),'合成供应商规格单第8行；浏览器测试依据');
 await mobile();await page.locator('#domestic-corrections-preview').click();await settle();await page.locator('#domestic-confirm').waitFor();
 assert.equal(await page.locator('#domestic-apply').isDisabled(),true);
 assert.equal(await page.locator('[data-domestic-correction="sku"]').inputValue(),'MANUAL-VERIFIED-SKU');
 await page.locator('#domestic-confirm').check();
 await page.locator('[data-domestic-correction="evidence"]').fill('合成供应商规格单第9行；改动需重新预检');
 assert.equal(await page.locator('#domestic-confirm').isChecked(),false);assert.equal(await page.locator('#domestic-apply').isDisabled(),true);
 await page.locator('#domestic-corrections-preview').click();await settle();
 await page.locator('#domestic-confirm').check();await page.locator('#domestic-apply').click();await settle();
 const correctedCandidates=(await call('/api/collection/state')).candidates;
 const originalUnknown=correctedCandidates.find(c=>c.raw.external_product_id==='888888'&&c.raw.source_sku==='');
 const correctedCandidate=correctedCandidates.find(c=>c.raw.source_sku==='MANUAL-VERIFIED-SKU');
 assert.equal(originalUnknown.status,'blocked');assert.equal(correctedCandidate.status,'ready');
 const correctionEvidence=correctedCandidate.snapshots[0].capture;
 assert.equal(correctionEvidence.original_raw.source_sku,'');assert.equal(correctionEvidence.sku,'');assert.equal(correctionEvidence.correction.manual,true);
 assert.ok(correctionEvidence.correction.evidence.includes('第9行'));
 assert.equal(correctedCandidate.raw.external_product_id,'888888','manual correction must bind the chosen missing-SKU product');assert.equal(correctedCandidate.raw.source_price,null,'manual correction must preserve original unknown price');assert.equal(correctedCandidate.normalized.stock,null,'manual correction must preserve original unknown stock');assert.equal(correctedCandidate.normalized.cost_cny,null,'manual correction must not create procurement cost');
 assert.equal((await call('/api/state')).products.length,3);
 await page.locator('#domestic-product-preview').click();await settle();await page.locator('#domestic-product-confirm').check();await page.locator('#domestic-product-apply').click();await settle();
 assert.equal((await call('/api/state')).products.length,4);await mobile();
 const before=(await call('/api/state')).products;const changed=structuredClone(captures[0]);changed.items[0].source_price=99;
 await upload(0,changed);assert.ok((await page.locator('#domestic-preview-panel').innerText()).includes('事实冲突 1'));
 await page.locator('#domestic-confirm').check();await page.locator('#domestic-apply').click();await settle();assert.deepEqual((await call('/api/state')).products,before);
 const after=(await call('/api/collection/state')).candidates.find(c=>c.raw.external_product_id==='123456');assert.equal(after.status,'conflict');assert.equal(after.snapshots.length,2);await mobile();
 console.log('Verified synthetic 1688/Taobao/Pinduoduo DOM parsers: cookie/storage/global/network traps not touched, secrets omitted, login/unsupported blocked, zero/missing values preserved; wrong/unbound JSON products ignored, secret image query references omitted before export, USD/unknown currency preserved and backend-blocked. Real local extension ZIP + upload/preflight/confirmed candidates + separate product confirmation work at 390px; stale file confirmation removed, missing SKU blocked, changed source price conflicts without overwriting products; manual SKU/supplier/fact correction requires evidence, preserves original unknown candidate/snapshot, revokes old confirmation, keeps collapsed/mobile drafts and needs separate product approval. No real platform account or live domestic website was used.');
}).catch(error=>{console.error(error.stack||error);process.exitCode=1;});
