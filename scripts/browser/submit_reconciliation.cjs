const assert=require('node:assert/strict');
const {runWorkflow}=require('./harness.cjs');

runWorkflow('uncertain Noon submit manual reconciliation form',async({page,navigate,settle})=>{
 await navigate('jobs');
 await page.evaluate(()=>{
  const job={id:'a'.repeat(32),kind:'submit',product_id:'synthetic-product',revision:3,status:'uncertain',message:'合成请求超时，结果待核对',updated_at:'2026-10-04T00:00:00Z'};
  state.job_history={jobs:[job],total:1,page:0,pages:1};state.event_history={events:[],total:0,page:0,pages:1};
  window.__reconcileCalls=[];window.__realReconcileApi=api;api=async(path,body)=>{if(path==='/api/content-submit-batch/reconcile'){window.__reconcileCalls.push({path,body});return {job_status:body.outcome==='accepted'?'needs_attention':'failed'}}return window.__realReconcileApi(path,body)};render();
 });
 const details=page.locator('details:has([data-submit-reconcile])');await details.locator('summary').click();const form=details.locator('[data-submit-reconcile]');
 const submit=form.locator('button[type=submit]');assert.equal(await submit.isDisabled(),true);
 await form.locator('[name=note]').fill('卖家中心按测试SKU搜索');await form.locator('[name=confirmed]').check();
 assert.equal(await submit.isDisabled(),true,'accepted outcome requires Noon parent ID');
 await form.locator('[name=sku_parent]').fill('QA-PARENT-1');assert.equal(await submit.isDisabled(),false);
 await submit.click();await settle();
 let call=await page.evaluate(()=>window.__reconcileCalls.at(-1));assert.equal(call.path,'/api/content-submit-batch/reconcile');
 assert.equal(call.body.outcome,'accepted');assert.equal(call.body.sku_parent,'QA-PARENT-1');assert.equal(call.body.confirmed,true);
 assert.match(call.body.request_id,/^[0-9a-f-]{36}$/);assert.equal(call.body.job_id,'a'.repeat(32));
 await page.evaluate(()=>{state.job_history={jobs:[{id:'b'.repeat(32),kind:'submit',product_id:'synthetic-product',revision:4,status:'interrupted',message:'合成中断',updated_at:'2026-10-04T00:00:00Z'}],total:1,page:0,pages:1};render()});
 const details2=page.locator('details:has([data-submit-reconcile])');const form2=details2.locator('[data-submit-reconcile]');await details2.locator('summary').click();await form2.locator('[name=outcome]').selectOption('not_found');
 assert.equal(await form2.locator('[name=sku_parent]').isVisible(),false);assert.equal(await form2.locator('[name=sku_parent]').evaluate(node=>node.required),false);
 await form2.locator('[name=note]').fill('合成回查未发现商品');await form2.locator('[name=confirmed]').check();assert.equal(await form2.locator('button[type=submit]').isDisabled(),false);
 await form2.locator('button[type=submit]').click();await settle();
 call=await page.evaluate(()=>window.__reconcileCalls.at(-1));assert.equal(call.body.outcome,'not_found');assert.equal(call.body.sku_parent,'');
 assert.match(await page.locator('#toast').innerText(),/已记录未找到/);
 console.log('Verified reconciliation UI validation, accepted/not-found payloads and explicit confirmation in Chromium.');
});
