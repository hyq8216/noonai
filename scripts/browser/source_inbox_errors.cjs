const assert=require('node:assert/strict');
const fs=require('node:fs');
const {runWorkflow}=require('./harness.cjs');

runWorkflow('large supplier intake error-row CSV download',async({page,navigate,settle})=>{
 await navigate('import');
 await page.waitForFunction(()=>Array.isArray(state.source_inbox?.listing?.files),null,{timeout:30000});
 const fixture={name:'synthetic-5000-row.csv',digest:'synthetic-digest',status:'done',message:'500 rows imported; one row skipped',updated_at:'2026-10-04T00:00:00Z',
  result:{cataloged:500,skipped:1,error_row_count:1,created_rows:[],mapping_complete:true,
   error_rows:[{row:501,title:'Synthetic duplicate',source_sku:'SYN-DUP-1',source_url:'https://supplier.invalid/item/1',stock:0,cost_cny:null,status:'duplicate',reason:'與本文件前面的商品重複，僅保留第一條'}]}};
 await page.evaluate(record=>{state.source_inbox.listing={files:[{...record,row_count:0}],page:0,pages:1,total:1};state.source_inbox.detail=record;render()},fixture);await settle();
 const action=page.locator('#source-inbox-errors-export');assert.equal(await action.textContent(),'下载 1 条错误行');
 assert.equal(await action.evaluate(button=>typeof button.onclick),'function','export control must be bound after render');
 const [file]=await Promise.all([page.waitForEvent('download',{timeout:30000}),
  action.evaluate(button=>button.onclick())]);
 assert.match(file.suggestedFilename(),/货源导入错误行-.*\.csv/);
 const downloaded=fs.readFileSync(await file.path(),'utf8');
 assert.ok(downloaded.startsWith('\uFEFF'));assert.ok(downloaded.includes('501'));
 assert.ok(downloaded.includes('SYN-DUP-1'));assert.ok(downloaded.includes('重复'));
 assert.ok(downloaded.includes('"0"'));assert.ok(downloaded.includes('库存'));
 assert.equal((downloaded.match(/\r\n/g)||[]).length,1,'header plus exactly one error row');
 console.log('Verified error-row action, UTF-8 CSV fields, zero stock, exact row and skip reason in real Chromium.');
});
