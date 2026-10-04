const assert=require('node:assert/strict');
const {runWorkflow}=require('./harness.cjs');

runWorkflow('paid model retry requires an explicit duplicate-usage confirmation',async({page,navigate})=>{
 await navigate('automation');
 const cancelled=await page.evaluate(async()=>{
  window.__retryCalls=[];window.__retryConfirmCount=0;
  window.confirm=()=>{window.__retryConfirmCount++;return false};
  api=async(path,body)=>{window.__retryCalls.push({path,body});throw Error('模型步骤已有调用记录；再次重试可能再次消耗订阅额度或产生API费用。请明确确认后再重试。')};
  return retryAutomationItem('synthetic-item','retry','1');
 });
 assert.equal(cancelled,false);assert.equal(await page.evaluate(()=>window.__retryConfirmCount),1);
 assert.equal(await page.evaluate(()=>window.__retryCalls.length),1,'declining must not send a second retry request');
 const confirmed=await page.evaluate(async()=>{
  window.__retryConfirmCount=0;window.confirm=()=>{window.__retryConfirmCount++;return true};
  api=async(path,body)=>{window.__retryCalls.push({path,body});if(!body.confirm_model_retry_after_prior_call)throw Error('模型步骤已有调用记录；再次重试可能再次消耗订阅额度或产生API费用。请明确确认后再重试。');return {id:'synthetic-item'}};
  return retryAutomationItem('synthetic-item','retry_fallback','1');
 });
 assert.equal(confirmed,true);assert.equal(await page.evaluate(()=>window.__retryConfirmCount),1);
 const call=await page.evaluate(()=>window.__retryCalls.at(-1));assert.equal(call.path,'/api/automation/control');
 assert.equal(call.body.action,'retry_fallback');assert.equal(call.body.confirm_model_retry_after_prior_call,true);
 const declinedTranslation=await page.evaluate(async()=>{
  window.__translateCalls=[];window.confirm=()=>false;
  api=async(path,body)=>{window.__translateCalls.push({path,body});throw Error('模型步骤已有调用记录；再次重试可能再次消耗订阅额度或产生API费用。')};
  return queueProductTranslation({id:'synthetic-product',revision:7});
 });
 assert.equal(declinedTranslation,false);assert.equal(await page.evaluate(()=>window.__translateCalls.length),1,'declining product translation retry must not send another request');
 const acceptedTranslation=await page.evaluate(async()=>{
  window.__translateCalls=[];window.confirm=()=>true;let attempted=0;
  api=async(path,body)=>{window.__translateCalls.push({path,body});attempted++;if(attempted===1)throw Error('模型步骤已有调用记录；再次重试可能再次消耗订阅额度或产生API费用。');return {job_id:'synthetic-retry-job'}};
  return queueProductTranslation({id:'synthetic-product',revision:7});
 });
 assert.equal(acceptedTranslation,true);const translated=await page.evaluate(()=>window.__translateCalls.at(-1));
 assert.equal(translated.path,'/api/products/synthetic-product/translate');assert.equal(translated.body.confirm_model_retry_after_prior_call,true);
 console.log('Verified retry cancellation sends no duplicate request and explicit confirmation is included for the accepted retry.');
});
