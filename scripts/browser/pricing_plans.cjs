const assert=require('node:assert/strict');
const fs=require('node:fs');
const {runWorkflow}=require('./harness.cjs');

runWorkflow('pricing plans: decimal tax calculation, missing-cost draft, invalidated previews, revisions and CSV',async({page,call,navigate,settle,mobile})=>{
 const imported=await call('/api/import',{products:[{title_zh:'合成定价有成本商品',cost_cny:19,facts:'合成测试采购成本19CNY'},{title_zh:'合成定价未知成本商品',facts:'采购成本待确认'}]});
 const [known,unknown]=imported.created;assert.equal(imported.created.length,2);
 const factsBefore=(await call('/api/state')).products;
 const state=()=>call('/api/pricing-plans/state');
 const get=id=>call('/api/pricing-plans/get?id='+encodeURIComponent(id));
 await navigate('pricing-plans');
 const select=async pid=>{await page.locator(`[data-price-add="${pid}"]`).click();await page.locator(`[data-member="${pid}"][data-field="target_price"]`).waitFor()};
 const common=async name=>{
  await page.locator('#pricing-plan-form [name="name"]').fill(name);
  await page.locator('#pricing-plan-form [name="currency"]').selectOption('SAR');
  for(const [key,value] of Object.entries({fx:'1.9',fx_date:new Date().toISOString().slice(0,10),fx_evidence:'合成日期汇率依据',platform_fee_rate:'0.10',logistics:'2',packing:'1',advertising:'1',tax_rate:'0.15',max_drop_rate:'0.20'}))await page.locator(`[data-param="${key}"]`).fill(value);
  await page.locator('[data-param="tax_basis"]').selectOption('inclusive');
 };
 const row=async(pid,values)=>{for(const [key,value] of Object.entries(values))await page.locator(`[data-member="${pid}"][data-field="${key}"]`).fill(value)};
 const preview=async()=>{await page.locator('#pricing-plan-form button[type="submit"]').click();await page.locator('#pricing-plan-preview').waitFor();assert.equal(await page.locator('#pricing-plan-save').isDisabled(),true)};
 const savePreview=async()=>{await page.locator('#pricing-plan-confirm').check();assert.equal(await page.locator('#pricing-plan-save').isDisabled(),false);await page.locator('#pricing-plan-save').click();await page.locator('#pricing-plan-preview').waitFor({state:'detached'});await settle()};
 await select(known);await common('合成本地预检方案');
 await row(known,{target_price:'25.00',cost_cny:'19.00',cost_evidence:'合成成本凭证19CNY',min_price:'20.00',max_price:'30.00',reference_price:'25.00'});
 await page.locator('#pricing-plan-form [name="status"]').selectOption('checked');
 await preview();
 assert.equal((await state()).total,0,'preview cannot save a price plan');
 let text=await page.locator('#pricing-plan-preview').innerText();assert.match(text,/5\.24/);assert.match(text,/18\.20/);assert.match(text,/本地预检通过/);
 // A changed parameter must remove the old checkbox and its confirmation token.
 await page.locator('#pricing-plan-confirm').check();
 await page.locator('[data-param="fx"]').fill('2');
 await page.locator('#pricing-plan-preview').waitFor({state:'detached'});
 assert.equal((await state()).total,0);
 await page.locator('[data-param="fx"]').fill('1.9');await preview();
 assert.equal(await page.locator('#pricing-plan-confirm').isChecked(),false);
 await mobile();await savePreview();
 const initialSummary=(await state()).rows.find(p=>p.name==='合成本地预检方案');assert.equal(initialSummary.member_count,1);assert.equal('members' in initialSummary,false);assert.equal('parameters' in initialSummary,false);
 let saved=await get(initialSummary.id);assert.equal(saved.status,'checked');assert.equal(saved.revision,1);assert.equal(saved.precheck_current,true);assert.equal(saved.sendable,false);assert.equal(saved.members[0].calculation.contribution,'5.24');assert.equal(saved.members[0].calculation.break_even_price,'18.20');
 assert.deepEqual((await call('/api/state')).products,factsBefore,'local pricing plans must not change products, costs or approval facts');
 // Product facts are changed through the actual product editor, not an API shortcut.
 await page.setViewportSize({width:1440,height:1000});await navigate('products');
 await page.locator(`[data-open="${known}"]`).first().click();await page.locator('#edit-form [name="facts"]').waitFor();
 await page.locator('#edit-form [name="facts"]').fill('合成商品新增事实，原价格方案必须复核');
 await page.locator('#edit-form button[type="submit"]').click();await settle();
 await page.waitForFunction(()=>!dirty);
 await navigate('pricing-plans');
 saved=await get(saved.id);assert.equal(saved.review_required,true);assert.equal(saved.precheck_current,false);assert.equal(saved.members[0].current_revision,2);
 await page.locator(`[data-price-edit="${saved.id}"]`).locator('xpath=ancestor::details').locator('summary').click();
 await page.locator(`[data-price-edit="${saved.id}"]`).click();await settle();
 await page.locator('#pricing-plan-form [name="status"]').selectOption('checked');await preview();await mobile();await savePreview();
 saved=await get(saved.id);assert.equal(saved.revision,2);assert.equal(saved.review_required,false);assert.equal(saved.members[0].revision,2);
 // Unknown procurement cost remains blank; the result cannot become a checked plan.
 await page.setViewportSize({width:1440,height:1000});await page.locator('#pricing-plan-new').click();await select(unknown);await common('=合成缺失成本草稿');
 assert.equal(await page.locator(`[data-member="${unknown}"][data-field="cost_cny"]`).inputValue(),'');
 await row(unknown,{target_price:'25',min_price:'20',max_price:'30',reference_price:'25',cost_evidence:'采购成本尚待供应商确认'});
 await preview();text=await page.locator('#pricing-plan-preview').innerText();assert.match(text,/参考采购成本待确认/);assert.match(text,/资料不完整，无法计算/);assert.match(text,/只可保存草稿/);
 await mobile();await savePreview();
 const missing=await get((await state()).rows.find(p=>p.name==='=合成缺失成本草稿').id);assert.equal(missing.status,'draft');assert.equal(missing.precheck_current,false);assert.equal(missing.members[0].cost_cny,null);assert.equal(missing.members[0].calculation.contribution,null);
 await page.locator('details summary').first().click();await page.locator('[data-price-details]').first().locator('table').waitFor();await mobile();
 const downloaded=page.waitForEvent('download');await page.locator('#pricing-plans-export').click();const file=await downloaded;
 assert.equal(file.suggestedFilename(),'pricing-plans.csv');const csv=fs.readFileSync(await file.path(),'utf8');assert.ok(csv.startsWith('\uFEFF'));assert.match(csv,/预计贡献非利润/);assert.match(csv,/5\.24/);assert.match(csv,/18\.20/);assert.ok(csv.includes("'=合成缺失成本草稿"),'formula-like plan names must be safe in CSV');assert.match(csv,/参考采购成本待确认/);
 assert.equal((await state()).total,2);
 // Cancellation is bound to the visible revision; changing its reason clears consent.
 const cancelForm=page.locator(`[data-price-cancel="${saved.id}"]`),cancelDetails=cancelForm.locator('xpath=ancestor::details');
 if(!await cancelDetails.evaluate(e=>e.open))await cancelDetails.locator('summary').click();
 await cancelForm.locator('[name="reason"]').fill('合成撤销原因');await cancelForm.locator('[name="confirmed"]').check();
 assert.equal(await cancelForm.locator('button').isDisabled(),false);
 await cancelForm.locator('[name="reason"]').fill('合成修订后的撤销原因');assert.equal(await cancelForm.locator('[name="confirmed"]').isChecked(),false);assert.equal(await cancelForm.locator('button').isDisabled(),true);
 await mobile();await cancelForm.locator('[name="confirmed"]').check();await cancelForm.locator('button').click();await settle();
 const cancelled=(await state()).rows.find(p=>p.id===saved.id);assert.equal(cancelled.status,'cancelled');assert.equal(cancelled.revision,3);assert.equal(cancelled.precheck_current,false);assert.equal(cancelled.sendable,false);
 assert.equal((await call('/api/state')).products.find(p=>p.id===known).revision,2,'cancellation cannot mutate a product');
}).catch(e=>{console.error(e);process.exitCode=1});
