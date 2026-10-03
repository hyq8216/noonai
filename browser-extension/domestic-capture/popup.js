'use strict';
document.getElementById('capture').onclick=async()=>{
 const button=document.getElementById('capture'),status=document.getElementById('status');button.disabled=true;
 try{
  const [tab]=await chrome.tabs.query({active:true,currentWindow:true});
  if(!tab?.url||!/^https:\/\/([^/]+\.)?(1688\.com|taobao\.com|tmall\.com|yangkeduo\.com|pinduoduo\.com)\//i.test(tab.url))throw Error('不支持此网页；请打开国内平台商品详情页');
  await chrome.scripting.executeScript({target:{tabId:tab.id},files:['parser.js']});
  const results=await chrome.scripting.executeScript({target:{tabId:tab.id},func:()=>{try{return {package:globalThis.domesticCaptureParse(document,location.href)}}catch(error){return {error:error.message}}}});
  const result=results[0]?.result;if(result?.error)throw Error(result.error);if(!result?.package)throw Error('页面不允许采集，请自行检查浏览器权限');
  const data=result.package,blob=new Blob([JSON.stringify(data,null,2)],{type:'application/json'}),url=URL.createObjectURL(blob);
  try{await chrome.downloads.download({url,filename:`noon-${data.provider}-${data.items[0].product_id}.json`,saveAs:true});}finally{setTimeout(()=>URL.revokeObjectURL(url),60000);}
  const missing=data.items.filter(i=>!i.sku).length;status.textContent=`已提取 ${data.items.length} 条当前观察并请求保存。${missing?missing+' 条缺真实规格货号，导入后保持待补。':''}${data.warnings?.length?' 提示：'+data.warnings.join('；'):''}请回到Noon Studio“国内网页采集”上传、预检并确认；真实平台兼容性尚未验证。`;
 }catch(error){status.textContent=error.message||'采集未完成；未下载猜测资料';}finally{button.disabled=false;}
};
