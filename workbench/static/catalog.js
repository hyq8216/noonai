'use strict';
let catalogPage=0;
const CATALOG_PAGE_SIZE=50;
function mergeCatalog(next,previous){
 if(Array.isArray(next.products))return next;
 if(next.catalog_unchanged){next.products=previous.products;return next}
 if(Array.isArray(next.product_changes)){
  const items=new Map(previous.products.map(p=>[p.id,p]));
  for(const id of next.removed_product_ids||[])items.delete(id);
  for(const p of next.product_changes)items.set(p.id,p);
  next.products=[...items.values()].sort((a,b)=>b.created_at.localeCompare(a.created_at)||a.id.localeCompare(b.id));
  delete next.product_changes;delete next.removed_product_ids;return next;
 }
 throw Error('商品资料响应不完整，请刷新重试');
}
function catalogRows(products,filter,query){return products.filter(p=>(filter==='all'||(filter==='missing'&&p.issues.length)||(filter==='review'&&!p.issues.length&&!p.reviewed)||(filter==='approved'&&p.reviewed))&&[p.title_zh,p.supplier,p.partner_sku,p.source_sku].join(' ').toLowerCase().includes(query.toLowerCase()))}
function selectCatalog(ids,checked){
 const next=new Set(chosen);
 for(const id of ids)checked?next.add(id):next.delete(id);
 if(next.size>500){toast('一次最多选择500件商品，请先处理或清空当前选择');return false}
 chosen=next;return true;
}
