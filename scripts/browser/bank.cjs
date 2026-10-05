const assert = require('node:assert/strict');
const { randomUUID } = require('node:crypto');
const { runWorkflow } = require('./harness.cjs');

runWorkflow('银行流水人工核对', async ({page,call,navigate,settle,mobile}) => {
  const today = new Date().toISOString().slice(0,10);
  const expense = await call('/api/finance/entry', {
    request_id:randomUUID(),kind:'expense',category:'other',currency:'SAR',amount:'25.00',fx:'1.9',
    date:today,evidence:'合成银行浏览器测试费用凭证',evidence_key:'synthetic-bank-expense-'+randomUUID()
  });
  const income = await call('/api/finance/entry', {
    request_id:randomUUID(),kind:'income',category:'other',currency:'SAR',amount:'25.00',fx:'1.9',
    date:today,evidence:'合成银行浏览器测试收入凭证',evidence_key:'synthetic-bank-income-'+randomUUID()
  });
  const finance = async () => (await call('/api/state?surface=finance')).finance;
  assert.equal((await finance()).payments.length,0,'seeded obligations must not imply money movement');

  await navigate('bank-reconciliation');
  assert.match(await page.locator('main').innerText(),/不连接银行、不发起转账/);
  await mobile();
  const source = {
    account_name:'合成SAR银行账户',bank_reference:'SYNTHETIC-BANK-'+randomUUID(),currency:'SAR',
    direction:'out',amount:'25.00',date:today,fx:'1.9',evidence:'合成银行文件第1行，仅用于本地软件验收'
  };
  await page.locator('#bank-file').setInputFiles({
    name:'synthetic-bank.json',mimeType:'application/json',buffer:Buffer.from(JSON.stringify([source]))
  });
  await page.locator('#bank-upload button[type="submit"]').click();
  await page.locator('#bank-import-confirm').waitFor();
  await settle();
  assert.equal((await finance()).payments.length,0,'preview must not record payments');
  assert.equal(await page.locator('#bank-import-confirm').isChecked(),false,'import needs explicit confirmation');
  let batches = (await call('/api/bank-reconciliation/state')).batches;
  assert.equal(batches[0].status,'preview');
  assert.equal(batches[0].summary.ready,1);
  assert.equal(batches[0].rows[0].amount_cents,2500);
  await mobile();

  await page.locator('#bank-import-confirm').check();
  await page.locator('#bank-import button').click();
  await page.locator('[data-bank-row]').waitFor();
  await settle();
  assert.equal((await finance()).payments.length,0,'importing a bank row must not match or pay automatically');
  batches = (await call('/api/bank-reconciliation/state')).batches;
  const bankRow = batches[0].rows[0];
  assert.equal(batches[0].status,'imported');
  assert.equal(batches[0].summary.unmatched,1);
  assert.equal(bankRow.payment_id,'');
  assert.deepEqual(bankRow.suggestions.map(item=>item.id),[expense.id],'inflow must not be suggested for an outflow row');

  await page.locator('[data-bank-row]').click();
  await page.locator('#bank-match-panel').waitFor();
  await mobile();
  assert.equal(await page.locator('#bank-entry').inputValue(),'','no candidate may be selected automatically');
  assert.equal(await page.locator('#bank-match-confirm').isChecked(),false);
  assert.equal(await page.locator(`#bank-entry option[value="${income.id}"]`).count(),0,'wrong-direction income is not selectable');
  // Native required fields must stop a match before a candidate and explicit confirmation.
  await page.locator('#bank-match button.btn.primary').click();
  await settle();
  assert.equal((await finance()).payments.length,0,'unconfirmed match must not post any payment');
  await page.locator('#bank-entry').selectOption(expense.id);
  assert.match(await page.locator('#bank-entry-detail').innerText(),/合成银行浏览器测试费用凭证/);
  assert.match(await page.locator('#bank-match-panel').innerText(),/不发起实际转账/);
  await page.locator('#bank-match-confirm').check();
  await page.locator('#bank-match button.btn.primary').click();
  await page.locator('[data-bank-row]').waitFor({state:'detached'});
  await settle();
  await mobile();

  const matched = (await call('/api/bank-reconciliation/state')).batches[0];
  assert.equal(matched.summary.matched,1);
  assert.equal(matched.summary.unmatched,0);
  assert.equal(matched.rows[0].amount_cents,2500);
  assert.equal(matched.rows[0].entry_id,expense.id);
  const ledger = await finance();
  assert.equal(ledger.payments.length,1,'a single selected bank row posts exactly one local payment');
  assert.equal(ledger.payments[0].amount_cents,2500);
  assert.equal(ledger.payments[0].evidence_key,'bank-row:'+bankRow.id);
  assert.equal(ledger.payments[0].account,source.account_name);
  assert.equal(ledger.entries.find(entry=>entry.id===expense.id).remaining_cents,0);
  assert.equal(ledger.entries.find(entry=>entry.id===income.id).remaining_cents,2500);
  assert.match(await page.locator('main').innerText(),/未发起银行转账/);

  const downloading = page.waitForEvent('download');
  await page.locator('#bank-export').click();
  const downloaded = await downloading;
  assert.equal(downloaded.suggestedFilename(),'bank-reconciliation.csv');
  const stream = await downloaded.createReadStream();
  const chunks = [];
  for await (const chunk of stream) chunks.push(chunk);
  const csv = Buffer.concat(chunks).toString('utf8');
  assert.ok(csv.startsWith('\ufeff'),'export keeps UTF-8 BOM');
  assert.ok(csv.includes(source.bank_reference),'export preserves the bank reference');
  assert.ok(csv.includes(matched.rows[0].payment_id),'export includes durable payment receipt');
  assert.ok(csv.includes('matched'),'export reflects confirmed matching');
  console.log('Bank workflow verified: SAR 25.00 expense settled once; separate income remains unpaid; 390px preview/import/expanded match/results fit; CSV receipt downloaded.');
}).catch(error => { console.error(error); process.exitCode=1; });
