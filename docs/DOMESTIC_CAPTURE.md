# 国内平台本机网页采集

当前重点为1688、淘宝（含天猫商品地址）、拼多多。用户当前尚无平台账号；本次完成软件内的来源观察闭环，**没有验证真实平台账号、当前线上页面兼容性或官方API**。保留旧渠道数据与API兼容，但不需要配置Shopify、eBay或授权JSON渠道来使用本功能。

## 使用步骤

1. 在“国内来源与候选”登记1688、淘宝或拼多多来源名称并启用。无需输入API令牌或保存浏览器登录信息。
2. 在“国内网页采集”下载本地扩展ZIP、解压。在Chrome或Edge打开扩展程序管理页，开启开发者模式，选择“加载已解压的扩展程序”，选中含`manifest.json`的目录。
3. 用户自己在浏览器打开明确商品详情页。如果需要登录、验证码或访问权限，由用户自行完成。扩展不会登录、绕过验证码、读取cookie、登录token、浏览器存储、页面全局状态或私有网络响应。
4. 点击扩展“采集当前商品并下载JSON”。观察包保存到用户选择的位置，不自动上传。扩展仅在此点击之后读取当前页面；不会后台分页、自动跳页或自动下载商品素材。
5. 回到Noon Studio“国内网页采集”，选择对应平台来源名称、上传JSON，先点击预检。核对商品编号、真实规格货号、网页售价、数量与公开图片引用，然后明确确认录入来源候选。
6. 预检发现规格货号、供应商或事实缺失时，可展开每条“人工校对规格货号 / 供应商 / 事实”。填写有依据核对的文字和必填校对依据，点击“按人工校对重新预检”，再明确确认。只能校对这三个文字字段，不能以此填写或改变库存、售价、币种或成本；未有依据的字段继续留空。原JSON包保持原样，原始观察、人工填写值与依据分别留证，不能把手填值冒充网页捕获。草稿变化立即撤销旧确认，手机折叠/展开后仍保留草稿。
7. 点击“预览这些候选的商品入库”，再次明确确认，复用已有来源候选导入流程新建商品。冲突保持待核对；现有商品不会被采集包覆盖。采购成本、素材权利、英阿内容、价格与上架审核仍需后续核对。

## 当前提取范围与限制

接受HTTPS明确商品地址：1688的`/offer/<id>.html`，淘宝/天猫的`/item.htm?id=<id>`或`/item.html?id=<id>`，拼多多/yangkeduo的`/goods.html`、`/goods1.html`、`/goods2.html`或`/goods/detail`且有`goods_id=<id>`。仅匹配平台域及其子域，拒绝相似恶意域、非商品页、嵌入凭据的地址。

解析当前DOM中的公开JSON-LD Product，以及明确标记`data-product-json`的公开JSON。JSON商品必须通过其`url`、`@id`或明确商品编号字段绑定当前URL商品编号，未绑定或身份矛盾的记录不采用；随后只尝试当前可见DOM字段。可见提取器读取商品标题；可见SKU、事实、供应商以及非元数据图片须有当前商品的明确节点或祖先身份绑定（`data-product-id`、`data-item-id`、`data-goods-id`或`data-product-url`），推荐区或任何祖先商品ID不一致的节点被排除并提示，无法绑定的规格保留缺失，不猜测其属于当前商品。通过绑定后读取明确`data-sku-id`规格、`data-price`/`data-currency`/`data-stock`属性、公开价格币种元数据、`data-product-fact`事实和公开商品图片引用。**各网站实际字段可能不同，当前没有适配与验证所有页面模板。**网页未提供稳定规格货号时不会用商品编号伪造规格；此类观察仅保留为blocked候选，不能新建商品。

价格区间不会自动变成单一价格；`availability=InStock`不会变成猜测库存。零价格与零数量保留；缺价格与缺数量保留为未知。实际`priceCurrency`保留，未知或非CNY阻止后台录入，不擅自换算为人民币。网页售价始终保留为`source_price`，不映射`cost_cny`。

图片仅保存公开HTTPS引用，包含会话、令牌或其他凭据类query参数的图片在扩展下载前过滤并提示；后台再次拒绝敏感地址。不会采集cookie、请求header、登录session或网页脚本源码。JSON解析使用`JSON.parse`，不执行页面代码、不使用`eval`。扩展仅有`scripting`与`downloads`权限和五类国内域名host权限，没有cookie、webRequest、storage权限、自动content script或后台service worker。

## 数据与安全契约

`DomesticCapture`使用现有`source_collection_runs`、`source_collection_candidates`和候选预览/导入规则，仅增加`domestic_capture_requests`防重回执表。采集预检不写商品或候选；确认录入事务校验预览token与当前账号版本、候选版本，绑定provider/account_id/product_id/真实sku/规范商品URL。恢复等待期间拒绝录入。

人工校对以独立`corrections:[{row,sku?,supplier?,facts?,evidence}]`字段传入；校对内容与依据一起绑定预检token，价格、数量和成本字段拒绝。原始观察保留在快照`capture.original_raw`，变化和依据保存在`capture.correction`及操作审计；补SKU新候选不会覆盖原缺SKU观察或已存在商品。凭据/登录会话文字不得粘贴到校对值或依据中。

上传包最多8MB、500条观察，单条最多128KB，JSON结构和字段受白名单限制，拒绝嵌入凭据字段。相同身份与相同观察跳过而不增加候选版本；相同身份事实、售价或图片变化保存首个与最新快照并标记conflict，保留旧normalized事实和现有商品。请求编号重复返回原回执，不同内容复用编号409。包内同一身份不同事实拒绝整包，避免静默选取其中一条。

接口：

- `GET /api/domestic-capture/state?page=0`：国内来源名称与每页20条录入历史。
- `GET /api/domestic-capture/extension`：固定五个源码文件的本地扩展ZIP。
- `POST /api/domestic-capture/preview`：`account_id`、`account_revision`、`package`。
- `POST /api/domestic-capture/apply`：上述字段加`preview_token`、`confirmed:true`、`request_id`，只录入候选。
- 后续`POST /api/collection/preview`与`POST /api/collection/apply`：独立的商品预览及确认导入。

包格式示例（合成资料）：

```json
{
  "format": "noon-domestic-capture-v1",
  "provider": "1688",
  "captured_at": "2026-10-03T12:00:00+00:00",
  "warnings": [],
  "items": [{
    "product_id": "123456",
    "sku": "VISIBLE-BLACK-5",
    "title": "合成黑色夹子",
    "source_url": "https://detail.1688.com/offer/123456.html",
    "source_price": 2.5,
    "source_currency": "CNY",
    "stock": null,
    "images": [],
    "facts": "颜色：黑色",
    "supplier": "",
    "brand": "",
    "captured_at": "2026-10-03T12:00:00+00:00",
    "method": "visible-dom"
  }]
}
```

## 验证证据

`.venv/bin/python -m unittest discover -s workbench/tests -p test_domestic_capture.py -v`：11项测试通过，覆盖两次确认、缺SKU禁止建商品、来源域与商品ID、敏感字段/URL、账号版本绑定、8MB/500条、原始币种/零数量、重复请求/观察、价格图片冲突与商品不覆盖、恢复等待锁定、人工校对必须有依据/禁止秘值/禁价格库存成本、校对变化使旧token失效、原始观察/已有商品不被覆盖与独立审计。

`node scripts/browser/domestic_capture.cjs`：真实本地HTTP与Chromium、临时数据目录，通过1688/淘宝/拼多多三个合成DOM parser fixture。cookie/storage/page global/network getter与调用陷阱未触发；秘密字段未导出；错误或未绑定JSON商品被忽略；推荐区DOM SKU/事实/图片与无身份绑定SKU被排除；敏感图片query在下载包前过滤；USD与未知币种保留并后台400。实际扩展ZIP下载、JSON上传、预检、候选确认、独立商品确认、390px展开与操作均通过。文件更改撤销旧确认；缺SKU候选blocked；变价冲突保留两个快照且商品不变。人工校对SKU/供应商/事实与依据在390px实际填写、折叠后草稿保持，依据变化撤销旧确认，重新预检再确认；缺SKU原观察继续blocked，校对的新观察保留原值与人工依据，仍需独立商品确认。

测试证明本地软件契约，未使用真实国内平台账号或线上页面；实际页面适配与账号条件具备后的合法小样采集仍待验证。
