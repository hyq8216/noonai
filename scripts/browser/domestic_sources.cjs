const assert=require('node:assert/strict');
const {runWorkflow}=require('./harness.cjs');

runWorkflow('domestic source registration without foreign connectors or credentials',async({page,call,navigate,settle,mobile})=>{
 await navigate('channels');
 for(const [index,provider] of ['1688','taobao','pinduoduo'].entries()){
  // Exercise the first-render race: the add action must fetch provider data before rendering its form.
  await page.evaluate(()=>{state.channels=undefined});
  await page.locator('#channel-new').click();
  await page.locator('#channel-provider').waitFor();await settle();
  assert.deepEqual(await page.locator('#channel-provider option').evaluateAll(nodes=>nodes.map(n=>n.value)),
    ['1688','taobao','pinduoduo','supplier_file']);
  assert.equal(await page.locator('#channel-account-form details').getAttribute('open'),null);
  await page.locator('#channel-provider').selectOption(provider);
  await page.locator('#channel-name').fill('合成国内来源-'+provider);
  await page.locator('#channel-account-form button[type="submit"]').click();
  await page.locator('#channel-account-form').waitFor({state:'detached'});await settle();
  const accounts=(await call('/api/channels/state')).accounts;
  assert.equal(accounts.length,index+1);
  const account=accounts.find(a=>a.provider===provider);
  assert.equal(account.base_url,'');assert.equal(account.has_token,false);
  assert.equal(account.enabled,true);
 }
 await mobile();
 await page.locator('#nav-mobile-toggle').click(); // Mobile navigation now opens on demand.
 await page.locator('#nav-search').fill('1688');
 assert.equal(await page.locator('.nav [data-nav]').count(),2);
 await page.locator('#nav-search').fill('');
 await page.screenshot({path:'/tmp/noonai-domestic-sources-mobile.png',fullPage:true});
 await navigate('domestic-capture');
 assert.equal((await call('/api/domestic-capture/state')).accounts.length,3);
 assert.equal((await call('/api/collection/state')).total,0);
 assert.equal((await call('/api/state')).products.length,0);
});
