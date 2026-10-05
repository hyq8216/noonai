'use strict';

// Routes stay unchanged. Disclosure only changes which navigation choices are visible.
const navigationSections = [
 {id:'catalog',label:'商品铺货',items:[
  ['batch','批量铺货'],['products','商品库','products'],['import','导入货源'],['platform','批量刊登与结果'],
  {id:'sources',label:'采集与来源',items:[['channels','国内来源与候选'],['domestic-capture','国内网页采集'],['collection-schedules','周期采集'],['import-profiles','导入字段模板']]},
  {id:'content',label:'商品与素材',items:[['catalog-groups','SPU 与规格组'],['batch-editor','商品批量编辑'],['media','图片与视频'],['visuals','电商视觉制作']]}
 ]},
 {id:'orders',label:'订单履约',items:[['orders','订单履约'],['order-intake','订单批量导入'],['fulfillment','批量拣货发货'],['shipping-manifests','物流包装与交接'],['after-sales','售后与退款']]},
 {id:'supply',label:'采购库存',items:[
  {id:'purchasing',label:'采购管理',items:[['supplier-quotes','供应商询价比较'],['purchases','采购收货'],['procurement','采购订单关联']]},
  {id:'stock',label:'库存管理',items:[['inventory','库存台账'],['replenishment','库存补货建议'],['inventory-counts','批量库存盘点'],['warehouse','调拨与补货']]}
 ]},
 {id:'finance',label:'财务经营',items:[['analytics','经营分析'],['pricing-plans','价格方案预检'],['ad-analytics','广告归因分析'],
  {id:'accounts',label:'财务对账',items:[['finance','财务与对账'],['settlements','结算文件核对'],['bank-reconciliation','银行流水核对'],['fx-registry','汇率档案']]}
 ]},
 {id:'automation',label:'自动化',items:[['automation','自动化中心'],['alerts','异常中心'],['jobs','任务与记录','jobs']]},
 {id:'system',label:'系统设置',items:[['partners','业务档案'],['models','模型服务'],['settings','连接设置'],
  {id:'backup',label:'资料备份',items:[['recovery','备份与恢复'],['backup-schedules','周期备份']]}
 ]}
];
const navigationAliases = {
 channels:'1688 淘宝 拼多多 采集 来源', 'domestic-capture':'1688 淘宝 拼多多 浏览器',
 'pricing-plans':'利润 保本价 定价', 'after-sales':'退货 退款 赔付',
 'bank-reconciliation':'银行 收款 付款 对账', visuals:'生图 模特照 主图',
 media:'视频 素材', models:'GPT Codex Luna Sol MiniMax 模型 订阅',
 recovery:'恢复 备份', 'backup-schedules':'定时 备份'
};
let navSearch='',navExpandedSection='catalog',navExpandedSubsection=null,navMobileOpen=false;

function navigationPath(route){
 if(route==='overview')return {label:'运营总览',section:null,subsection:null,titles:['运营总览']};
 for(const section of navigationSections){
  for(const item of section.items){
   if(Array.isArray(item)&&item[0]===route)return {label:item[1],section:section.id,subsection:null,titles:[section.label,item[1]]};
   if(!Array.isArray(item))for(const leaf of item.items)if(leaf[0]===route)return {label:leaf[1],section:section.id,subsection:item.id,titles:[section.label,item.label,leaf[1]]};
  }
 }
 return null;
}
function revealNavigation(route){
 const path=navigationPath(route);if(!path)return;
 navExpandedSection=path.section;navExpandedSubsection=path.subsection;navSearch='';navMobileOpen=false;
}
function navButton(id,label,count=''){
 const value=count==='products'?state.products.length:count==='jobs'?(state.active_jobs||0):count;
 return `<button type="button" class="nav-link ${view===id?'active':''}" data-nav="${id}" ${view===id?'aria-current="page"':''}><span>${esc(label)}</span>${value!==''?`<span class="count">${esc(value)}</span>`:''}</button>`;
}
function navigationChevron(){return '<svg class="nav-chevron" viewBox="0 0 16 16" width="16" height="16" aria-hidden="true"><path d="m6 3 5 5-5 5" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>'}
function platformNavigation(){
 const query=navSearch.trim().toLowerCase(),active=navigationPath(view);
 const matches=(id,label)=>!query||`${label} ${id} ${navigationAliases[id]||''}`.toLowerCase().includes(query);
 let html=matches('overview','运营总览')?navButton('overview','运营总览'):'';
 for(const section of navigationSections){
  const sectionMatch=!!query&&section.label.includes(query);
  const items=section.items.map(item=>{
   if(Array.isArray(item))return sectionMatch||matches(item[0],item[1])?item:null;
   const leaves=item.items.filter(leaf=>sectionMatch||item.label.includes(query)||matches(leaf[0],leaf[1]));
   return leaves.length?{...item,items:leaves}:null;
  }).filter(Boolean);
  if(!items.length)continue;
  const open=!!query||navExpandedSection===section.id,selected=active?.section===section.id;
  html+=`<section class="nav-section ${selected?'has-current':''}"><h2><button type="button" class="nav-disclosure" data-nav-group="${section.id}" aria-expanded="${open}" aria-controls="nav-panel-${section.id}"><span>${esc(section.label)}</span>${navigationChevron()}</button></h2><div id="nav-panel-${section.id}" class="nav-children" ${open?'':'hidden'}>`;
  for(const item of items){
   if(Array.isArray(item)){html+=navButton(...item);continue}
   const subOpen=!!query||navExpandedSubsection===item.id;
   html+=`<div class="nav-subsection ${active?.subsection===item.id?'has-current':''}"><button type="button" class="nav-disclosure nav-subheading" data-nav-subgroup="${item.id}" aria-expanded="${subOpen}" aria-controls="nav-panel-${item.id}"><span>${esc(item.label)}</span>${navigationChevron()}</button><div id="nav-panel-${item.id}" class="nav-grandchildren" ${subOpen?'':'hidden'}>${item.items.map(leaf=>navButton(...leaf)).join('')}</div></div>`;
  }
  html+='</div></section>';
 }
 return html||'<p class="nav-empty" role="status">未找到功能，请换个关键词或清空搜索。</p>';
}
function navigationBreadcrumb(){const titles=navigationPath(view)?.titles||[];return `<nav class="nav-breadcrumb" aria-label="当前位置">${titles.map((title,index)=>`<span ${index===titles.length-1?'aria-current="page"':''}>${esc(title)}</span>`).join('')}</nav>`}
function navigationSidebar(){return `<aside class="sidebar ${navMobileOpen?'menu-open':''}"><div class="sidebar-heading"><div><div class="brand">Noon Studio</div><div class="brand-sub">国内货源 · noon 沙特</div></div><button type="button" class="nav-mobile-toggle" id="nav-mobile-toggle" aria-expanded="${navMobileOpen}" aria-controls="sidebar-menu">${navMobileOpen?'收起菜单':'打开菜单'}</button></div><div class="sidebar-menu" id="sidebar-menu"><div class="nav-search"><label for="nav-search">查找功能</label><div class="nav-search-control"><input id="nav-search" type="search" placeholder="采集、订单、库存…" value="${esc(navSearch)}" autocomplete="off"><button type="button" id="nav-search-clear" aria-label="清空功能搜索" ${navSearch?'':'hidden'}>清空</button></div></div><nav class="nav" aria-label="主导航">${platformNavigation()}</nav></div><div class="sidebar-foot"><strong>本地工作空间</strong>资料保存在本机 · 经营模式待确认</div></aside>`}
function redrawNavigation(focusSelector){
 const nav=document.querySelector('.nav');if(!nav)return;
 const scroll=nav.scrollTop;nav.innerHTML=platformNavigation();bindNavigationDisclosure();nav.scrollTop=scroll;
 const clear=document.getElementById('nav-search-clear');if(clear)clear.hidden=!navSearch;
 if(focusSelector)nav.querySelector(focusSelector)?.focus({preventScroll:true});
}
function bindNavigationDisclosure(){
 document.querySelectorAll('.nav [data-nav]').forEach(button=>button.onclick=()=>navigate(button.dataset.nav));
 document.querySelectorAll('[data-nav-group]').forEach(button=>button.onclick=()=>{
  if(navSearch.trim()){toast('请先清空搜索，再收起菜单');return}
  navExpandedSection=navExpandedSection===button.dataset.navGroup?null:button.dataset.navGroup;navExpandedSubsection=null;
  redrawNavigation(`[data-nav-group="${button.dataset.navGroup}"]`);
 });
 document.querySelectorAll('[data-nav-subgroup]').forEach(button=>button.onclick=()=>{
  if(navSearch.trim()){toast('请先清空搜索，再收起菜单');return}
  navExpandedSubsection=navExpandedSubsection===button.dataset.navSubgroup?null:button.dataset.navSubgroup;
  redrawNavigation(`[data-nav-subgroup="${button.dataset.navSubgroup}"]`);
 });
}
function bindNavigation(){
 bindNavigationDisclosure();
 const input=document.getElementById('nav-search'),clear=document.getElementById('nav-search-clear');
 const clearSearch=()=>{navSearch='';input.value='';redrawNavigation();input.focus()};
 if(input){input.oninput=()=>{navSearch=input.value;redrawNavigation()};input.onkeydown=event=>{
  if(event.key==='Escape'&&navSearch){event.preventDefault();clearSearch()}
  if(event.key==='Enter'&&navSearch.trim()){
   const results=document.querySelectorAll('.nav [data-nav]');if(results.length===1){event.preventDefault();navigate(results[0].dataset.nav)}
  }
 }}
 if(clear)clear.onclick=clearSearch;
 const toggle=document.getElementById('nav-mobile-toggle');if(toggle)toggle.onclick=()=>{
  navMobileOpen=!navMobileOpen;document.querySelector('.sidebar').classList.toggle('menu-open',navMobileOpen);
  toggle.setAttribute('aria-expanded',String(navMobileOpen));toggle.textContent=navMobileOpen?'收起菜单':'打开菜单';
 };
}
