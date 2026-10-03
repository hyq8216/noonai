const assert = require('node:assert/strict');
const { randomUUID } = require('node:crypto');
const { runWorkflow } = require('./harness.cjs');

runWorkflow('命名导入字段模板复用', async ({page,call,navigate,settle,mobile}) => {
  const today=new Date().toISOString().slice(0,10);
  const bankColumns={account_name:'账号',bank_reference:'流水号',currency:'币种',direction:'方向',amount:'原币金额',date:'日期',fx:'汇率',evidence:'凭证'};
  const createProfile=async(type,name,columns)=>{
    await navigate('import-profiles');
    await page.locator('#import-profile-new').click();
    await page.locator('#import-profile-name').fill(name);
    await page.locator('#import-profile-type').selectOption(type);
    for(const [field,column] of Object.entries(columns))await page.locator(`[name="column_${field}"]`).fill(column);
    await page.locator('#import-profile-currency').selectOption('SAR');
    await page.locator('#import-profile-confirm').check();
    assert.equal(await page.locator('#import-profile-confirm').isChecked(),true,'confirmation must not uncheck itself');
    await page.locator('#import-profile-form button.btn.primary').click();
    await settle();
    await page.waitForFunction(name=>state.import_profiles?.profiles.some(p=>p.name===name),name);
    return (await call('/api/import-profiles/state')).profiles.find(p=>p.name===name);
  };
  const bankProfile=await createProfile('bank','合成银行字段模板',bankColumns);
  assert.equal(bankProfile.revision,1);
  await mobile();
  await navigate('bank-reconciliation');
  const rows=[
    {账号:'合成映射SAR账户',流水号:'PROFILE-BANK-1',币种:'SAR',方向:'out',原币金额:'10.00',日期:today,汇率:'1.9',凭证:'合成映射流水第1行'},
    {账号:'合成映射SAR账户',流水号:'PROFILE-BANK-2',方向:'out',原币金额:'15.00',日期:today,汇率:'1.9',凭证:'合成映射流水第2行'},
    {账号:'合成映射SAR账户',流水号:'PROFILE-BANK-3',币种:'SAR',方向:'out',原币金额:'1,000.00',日期:today,汇率:'1.9',凭证:'合成无效千位金额第3行'}
  ];
  const uploadMappedBank=async()=>{
    await page.locator('#bank-profile').selectOption(bankProfile.id);
    await page.locator('#bank-file').setInputFiles({name:'synthetic-mapped-bank.json',mimeType:'application/json',buffer:Buffer.from(JSON.stringify(rows))});
    await page.locator('#bank-upload button[type="submit"]').click();
    await page.locator('#bank-import-confirm').waitFor();await settle();
  };
  await uploadMappedBank();
  let batch=(await call('/api/bank-reconciliation/state')).batches[0];
  assert.equal(batch.summary.ready,2);assert.equal(batch.summary.invalid,1);
  assert.equal(batch.profile_fact.revision,1);
  assert.equal(batch.transformations[0].original['原币金额'],'10.00');
  assert.equal(batch.transformations[0].mapped.amount,'10.00');
  assert.equal(batch.transformations[1].mapped.currency,'SAR');
  assert.equal(batch.transformations[2].mapped.amount,'1,000.00','thousands separators must not be guessed away');
  assert.ok(batch.transformations[2].problems.length);
  await page.locator('[data-profile-preview] summary').click();
  assert.match(await page.locator('[data-profile-preview]').innerText(),/原币金额/);
  assert.match(await page.locator('[data-profile-preview]').innerText(),/amount/);
  assert.match(await page.locator('[data-profile-preview]').innerText(),/1,000.00/);
  await mobile();
  assert.equal((await call('/api/state?surface=finance')).finance.payments.length,0);

  // Edit the same template through its actual UI after file preview, then attempt stale import.
  await navigate('import-profiles');
  await page.locator(`[data-import-profile-edit="${bankProfile.id}"]`).click();
  await page.locator('#import-profile-name').fill('合成银行字段模板第二版');
  await page.locator('#import-profile-confirm').check();
  await page.locator('#import-profile-form button.btn.primary').click();await settle();
  await page.waitForFunction(id=>state.import_profiles?.profiles.find(p=>p.id===id)?.revision===2,bankProfile.id);
  await navigate('bank-reconciliation');
  await page.locator('#bank-import-confirm').check();
  await page.locator('#bank-import button').click();await settle();
  await page.waitForFunction(()=>document.getElementById('toast').textContent.includes('模板已修改'));
  assert.equal((await call('/api/bank-reconciliation/state')).batches[0].status,'preview','changed template must invalidate old confirmation');
  assert.equal((await call('/api/state?surface=finance')).finance.payments.length,0);
  await uploadMappedBank();
  batch=(await call('/api/bank-reconciliation/state')).batches[0];
  assert.equal(batch.profile_fact.revision,2);
  await page.locator('#bank-import-confirm').check();await page.locator('#bank-import button').click();
  await page.locator('[data-bank-row]').first().waitFor();await settle();
  batch=(await call('/api/bank-reconciliation/state')).batches[0];
  assert.equal(batch.summary.unmatched,2);assert.equal(batch.summary.invalid,1);
  assert.equal((await call('/api/state?surface=finance')).finance.payments.length,0,'mapped rows still require separate payment matching');
  await mobile();

  const shop=await call('/api/ops/entity',{request_id:randomUUID(),kind:'shop',name:'合成映射订单店铺'});
  const warehouse=await call('/api/ops/entity',{request_id:randomUUID(),kind:'warehouse',name:'合成映射订单仓库'});
  await call('/api/import',{products:[{title_zh:'合成模板订单商品',source_sku:'PROFILE-SKU'}]});
  const product=(await call('/api/state')).products.find(p=>p.source_sku==='PROFILE-SKU');
  const orderColumns={external_id:'订单号',partner_sku:'商品编号',quantity:'件数',unit_price:'单件金额',currency:'',order_total:'',order_date:'',note:''};
  const orderProfile=await createProfile('order','合成订单字段模板',orderColumns);
  await navigate('order-intake');
  await page.locator('#order-intake-shop').selectOption(shop.id);
  await page.locator('#order-intake-warehouse').selectOption(warehouse.id);
  await page.locator('#order-intake-profile').selectOption(orderProfile.id);
  await page.locator('#order-intake-text').fill(`订单号,商品编号,件数,单件金额\nPROFILE-GUI-ORDER,${product.partner_sku},2,12.50\n`);
  await page.locator('#order-intake-preview').click();await page.locator('#order-intake-confirm').waitFor();await settle();
  await page.locator('[data-profile-preview] summary').click();
  assert.match(await page.locator('[data-profile-preview]').innerText(),/订单号/);
  assert.match(await page.locator('[data-profile-preview]').innerText(),/external_id/);
  assert.equal(await page.locator('#order-intake-col-external_id').isDisabled(),true,'saved profile mapping cannot be changed by unrelated manual selectors');
  await mobile();
  await page.locator('#order-intake-confirm').check();await page.locator('#order-intake-apply').click();
  await page.locator('#order-intake-preview-panel').waitFor({state:'detached'});await settle();
  const orders=(await call('/api/state?surface=orders')).ops.documents;
  assert.equal(orders.length,1);assert.equal(orders[0].external_id,'PROFILE-GUI-ORDER');assert.equal(orders[0].total_cents,2500);assert.equal(orders[0].currency,'SAR');
  assert.equal((await call('/api/state?surface=inventory')).ops.stock.length,0);
  await mobile();
  console.log('Import profiles verified: GUI save/edit, two mapped bank rows, original/transformed/error details, stale-template confirmation rejected, no automatic payments; mapped CSV order imported at SAR 25.00, no stock writes; 390px fits.');
}).catch(error=>{console.error(error);process.exitCode=1;});
