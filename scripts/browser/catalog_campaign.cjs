const assert=require('node:assert/strict');
const {runWorkflow}=require('./harness.cjs');

runWorkflow('bulk catalog campaign preview, isolation, replay and stale guard',async({page,call,navigate,settle})=>{
 const products=[
  {title_zh:'合成可安排商品',source_url:'https://detail.1688.com/offer/901001.html',source_sku:'CAMPAIGN-READY',supplier:'合成供应商',facts:'黑色，5件装'},
  {title_zh:'合成缺事实商品',source_url:'https://detail.1688.com/offer/901002.html',source_sku:'CAMPAIGN-BLOCKED',supplier:'合成供应商',facts:''},
  {title_zh:'合成已有任务商品',source_url:'https://detail.1688.com/offer/901003.html',source_sku:'CAMPAIGN-ACTIVE',supplier:'合成供应商',facts:'蓝色，2件装'}
 ];
 const imported=await call('/api/import',{products});assert.equal(imported.created.length,3);
 const [ready,blocked,active]=imported.created;
 await call('/api/automation/create',{name:'合成既有流程',request_id:'campaign-active-fixture',product_ids:[active],plan:{translate:false}});
 await page.evaluate(()=>sync(false));await settle();await navigate('batch');
 const translate=page.locator('#batch-options input[name="translate"]');if(await translate.isChecked())await translate.uncheck();await settle();
 await page.locator('#campaign-preview').click();await page.locator('#campaign-apply').waitFor();await settle();
 const preview=await page.locator('main').innerText();
 assert.match(preview,/当前筛选 3 件/);assert.match(preview,/可安排 1 件/);assert.match(preview,/待补资料或配置 1 件/);assert.match(preview,/已有任务 1 件/);
 assert.equal(await page.locator('#campaign-apply').textContent(),'确认安排 1 件');
 await page.locator('#campaign-apply').click();await page.locator('main h1').filter({hasText:'自动化中心'}).waitFor();await settle();
 const history=await call('/api/catalog-campaign/history?page=0');assert.equal(history.total,1);
 const campaign=history.rows[0];assert.equal(campaign.totals.ready,1);assert.equal(campaign.totals.blocked,1);assert.equal(campaign.totals.active,1);
 assert.equal(campaign.runs.length,1);assert.equal(campaign.runs[0].ready,1);
 const exceptions=await call('/api/catalog-campaign/exceptions?request_id='+encodeURIComponent(campaign.request_id));
 assert.equal(exceptions.rows.length,2);assert.deepEqual(new Set(exceptions.rows.map(row=>row.id)),new Set([blocked,active]));
 // A stale preview must be rejected after product facts change and cannot create a second campaign.
 const stale=await call('/api/catalog-campaign/preview',{product_ids:[ready],plan:{translate:false}});
 const current=await page.evaluate(id=>state.products.find(product=>product.id===id),ready);
 assert.ok(current);
 await call('/api/products/'+ready+'/save',{revision:current.revision,data:{facts:'修订后的合成事实'}});
 await assert.rejects(()=>call('/api/catalog-campaign/apply',{name:'陈旧预检',request_id:'campaign-stale-fixture',product_ids:[ready],plan:{translate:false},preview_token:stale.token,confirmed:true}),/变化|重新预览/);
 assert.equal((await call('/api/catalog-campaign/history?page=0')).total,1);
 console.log('Verified browser-operated preview -> confirmed local campaign, ready/blocked/active isolation, exception snapshot and idempotent stale-preview barrier; no model or seller request.');
});
