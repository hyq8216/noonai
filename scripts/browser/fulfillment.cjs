const assert=require('node:assert/strict');
const fs=require('node:fs');
const {runWorkflow}=require('./harness.cjs');

runWorkflow('批量占用 -> 拣货CSV -> 修改运单撤销旧预检 -> 批量发货库存守恒',async({page,call,navigate,settle,mobile})=>{
  const key=()=>crypto.randomUUID();
  const shop=await call('/api/ops/entity',{kind:'shop',name:'合成拣货店铺',request_id:key()});
  const warehouse=await call('/api/ops/entity',{kind:'warehouse',name:'合成拣货仓库',request_id:key()});
  const imported=await call('/api/import',{products:[{title_zh:'合成拣货夹子',source_url:'https://example.com/picking',source_sku:'PICK-1',supplier:'合成供应商',facts:'黑色；5件装',cost_cny:5,stock:10}]});
  const product=(await call('/api/state')).products.find(p=>p.id===imported.created[0]);
  await call('/api/ops/adjust',{product_id:product.id,warehouse_id:warehouse.id,direction:'in',quantity:10,reason:'合成盘点10件',request_id:key()});
  const orders=[];
  for(const quantity of [1,2])orders.push(await call('/api/ops/order',{shop_id:shop.id,warehouse_id:warehouse.id,external_id:'SYNTHETIC-PICK-'+quantity,currency:'SAR',lines:[{product_id:product.id,quantity,unit_price:'12.50'}],request_id:key()}));
  await navigate('fulfillment');
  await page.locator('#fulfillment-select-page').click();
  await page.locator('#fulfillment-preview').click();
  await page.locator('#fulfillment-confirm').waitFor();
  assert.equal(await page.locator('#fulfillment-apply').isDisabled(),true);
  assert.equal((await call('/api/state?surface=inventory')).ops.stock[0].reserved,0);
  await page.locator('#fulfillment-confirm').check();await page.locator('#fulfillment-apply').click();await settle();
  let stock=(await call('/api/state?surface=inventory')).ops.stock[0];
  assert.equal(stock.on_hand,10);assert.equal(stock.reserved,3);assert.equal(stock.available,7);
  const downloadPromise=page.waitForEvent('download');
  await page.locator('[data-fulfillment-export]').first().click();
  const download=await downloadPromise;
  const csv=fs.readFileSync(await download.path(),'utf8');
  assert.ok(csv.startsWith('\ufeff'));assert.ok(csv.includes(product.partner_sku));
  await page.locator('#fulfillment-action').selectOption('ship');
  await page.locator('#fulfillment-select-page').click();
  for(const [index,order]of orders.entries()){
    await page.locator(`[data-fulfillment-carrier="${order.id}"]`).fill('合成承运商');
    await page.locator(`[data-fulfillment-tracking="${order.id}"]`).fill('SYNTHETIC-PARCEL-'+index);
  }
  await page.locator('#fulfillment-preview').click();await page.locator('#fulfillment-confirm').check();
  await page.locator(`[data-fulfillment-tracking="${orders[0].id}"]`).fill('SYNTHETIC-PARCEL-UPDATED');
  assert.equal(await page.locator('#fulfillment-preview-panel').isVisible(),false);
  stock=(await call('/api/state?surface=inventory')).ops.stock[0];assert.equal(stock.on_hand,10);assert.equal(stock.reserved,3);
  await page.locator('#fulfillment-preview').click();await page.locator('#fulfillment-confirm').waitFor();
  await mobile();assert.equal(await page.locator('#fulfillment-apply').isDisabled(),true);
  await page.locator('#fulfillment-confirm').check();await page.locator('#fulfillment-apply').click();await settle();
  stock=(await call('/api/state?surface=inventory')).ops.stock[0];
  assert.equal(stock.on_hand,7);assert.equal(stock.reserved,0);assert.equal(stock.available,7);
  const state=await call('/api/fulfillment/state');assert.equal(state.orders.length,0);assert.equal(state.wave_total,2);
  const saved=(await call('/api/state?surface=orders')).ops.documents;
  assert.ok(saved.every(o=>o.status==='shipped'));assert.ok(saved.some(o=>o.tracking==='SYNTHETIC-PARCEL-UPDATED'));
  await mobile();
}).catch(error=>{console.error(error);process.exitCode=1});
