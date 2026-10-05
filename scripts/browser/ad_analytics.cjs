const assert=require('node:assert/strict');
const fs=require('node:fs');
const {randomUUID}=require('node:crypto');
const {runWorkflow}=require('./harness.cjs');

runWorkflow('advertising GUI import/duplicate/version replacement -> original currencies/zero metrics -> mobile CSV',async({page,call,navigate,settle,mobile})=>{
  const shop=await call('/api/ops/entity',{kind:'shop',name:'合成广告报告店',request_id:randomUUID()});
  const today=new Date().toISOString().slice(0,10);
  const base={date:today,campaign:'合成SAR活动',channel:'noon-search',currency:'SAR',spend:'10',impressions:100,clicks:10,orders:2,attributed_sales:'40',attribution_basis:'点击7天'};
  const rows=[base,{...base,campaign:'合成USD活动',currency:'USD',spend:'5',attributed_sales:'10'},
    {...base,campaign:'合成零分母活动',channel:'noon-zero',spend:'0',impressions:0,clicks:0,orders:0,attributed_sales:'0'},
    {...base,campaign:'未知SKU活动',sku:'SYNTHETIC-UNKNOWN-AD-SKU',spend:'2',attributed_sales:'8'}];
  await navigate('ad-analytics');await page.locator('#ad-upload').waitFor();
  const upload=async(payload,filename='synthetic-ads.json')=>{
    await page.locator('#ad-shop').selectOption(shop.id);
    await page.locator('#ad-file').setInputFiles({name:filename,mimeType:filename.endsWith('.json')?'application/json':'text/csv',buffer:Buffer.from(payload)});
    await page.locator('#ad-upload button').click();
    await page.locator('#ad-confirm').waitFor();await settle();
  };
  await upload(JSON.stringify(rows));
  assert.equal((await call('/api/ad-analytics/state')).summary.records,0,'preview must not persist measured rows');
  assert.match(await page.locator('#ad-batch-panel').innerText(),/未知SKU1/);
  await mobile();
  await page.locator('#ad-confirm').check();
  assert.equal(await page.locator('#ad-confirm').isChecked(),true,'checking confirmation must not rerender/uncheck');
  await page.locator('#ad-apply button').click();
  await page.waitForFunction(()=>state.ad_analytics.batch?.status==='applied');await settle();
  let state=await call('/api/ad-analytics/state');assert.equal(state.summary.records,4);
  const sar=state.summary.groups.find(g=>g.currency==='SAR'&&g.channel==='noon-search'&&g.report_level==='campaign');
  const usd=state.summary.groups.find(g=>g.currency==='USD');
  assert.equal(sar.spend_cents,1000);assert.equal(usd.spend_cents,500);assert.equal(sar.roas,4);
  const zero=state.summary.groups.find(g=>g.channel==='noon-zero');
  for(const field of ['ctr','cpc_cents','acos','roas'])assert.equal(zero[field],null);
  assert.match(await page.locator('#main').innerText(),/未知/);
  assert.match(await page.locator('#main').innerText(),/SKU未匹配，非真实SKU销量/);
  assert.deepEqual((await call('/api/state?surface=finance')).finance.entries,[],'ad upload must not book income or expense');
  assert.deepEqual((await call('/api/state?surface=inventory')).ops.stock,[]);

  // Reupload via CSV to verify semantic deduplication across the two formats.
  const fields=['date','campaign','channel','currency','spend','impressions','clicks','orders','attributed_sales','sku','attribution_basis'];
  const csv=fields.join(',')+'\r\n'+rows.map(r=>fields.map(k=>r[k]??'').join(',')).join('\r\n')+'\r\n';
  await upload(csv,'synthetic-ads-duplicate.csv');
  assert.match(await page.locator('#ad-batch-panel').innerText(),/完全重复4/);
  await page.locator('#ad-confirm').check();await page.locator('#ad-apply button').click();
  await page.waitForFunction(()=>state.ad_analytics.batch?.status==='applied');
  assert.equal(await page.evaluate(()=>state.ad_analytics.batch.receipt.duplicates),4);
  assert.equal((await call('/api/ad-analytics/state')).summary.records,4);

  await upload(JSON.stringify([{...base,spend:'20'}]),'synthetic-ads-replacement.json');
  assert.match(await page.locator('#ad-batch-panel').innerText(),/现有冲突1/);
  assert.equal(await page.locator('#ad-replace').isChecked(),false,'version replacement must be explicit');
  assert.equal((await call('/api/ad-analytics/state')).summary.groups.find(g=>g.currency==='SAR'&&g.channel==='noon-search'&&g.report_level==='campaign').spend_cents,1000,'preview may not replace old version');
  await page.locator('#ad-replace').check();await page.locator('#ad-confirm').check();
  assert.equal(await page.locator('#ad-replace').isChecked(),true);
  await page.locator('#ad-apply button').click();
  await page.waitForFunction(()=>state.ad_analytics.batch?.receipt?.replaced===1);
  state=await call('/api/ad-analytics/state');
  assert.equal(state.summary.records,4);
  assert.equal(state.record_page.items.find(r=>r.campaign===base.campaign).revision,2);
  assert.equal(state.summary.groups.find(g=>g.currency==='SAR'&&g.channel==='noon-search'&&g.report_level==='campaign').spend_cents,2000,'replacement must not add old spend');

  await page.locator('#ad-from').fill(today);await page.locator('#ad-to').fill(today);
  await page.locator('#ad-filter-shop').selectOption(shop.id);await page.locator('#ad-currency').selectOption('USD');
  await page.locator('#ad-filter button').click();await page.waitForFunction(()=>state.ad_analytics.filters.currency==='USD');
  assert.equal(await page.evaluate(()=>state.ad_analytics.summary.records),1);
  assert.equal(await page.evaluate(()=>state.ad_analytics.summary.groups[0].currency),'USD');
  await mobile();
  const exportedPromise=page.waitForEvent('download');await page.locator('#ad-export').click();const exported=await exportedPromise;
  assert.equal(exported.suggestedFilename(),'广告归因分析.csv');
  const bytes=fs.readFileSync(await exported.path());assert.deepEqual([...bytes.subarray(0,3)],[239,187,191]);
  assert.match(bytes.toString('utf8'),/合成USD活动/);assert.doesNotMatch(bytes.toString('utf8'),/合成SAR活动/);
  const templatePromise=page.waitForEvent('download');await page.locator('#ad-template').click();const template=await templatePromise;
  assert.equal(template.suggestedFilename(),'广告归因空白模板.csv');assert.match(fs.readFileSync(await template.path(),'utf8'),/attributed_sales/);
  assert.deepEqual((await call('/api/state?surface=finance')).finance.entries,[]);
}).catch(error=>{console.error(error);process.exitCode=1});
