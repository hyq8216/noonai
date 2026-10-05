# 多渠道采集：配置与验收边界

2026-10-03，集成分支 `codex/multichannel-platform`。在原工作台左侧选择“多渠道采集”。

## 最新采集方向

用户明确主要采集1688、淘宝、拼多多，当前暂无账号，先完成软件能力。
新建来源页面仅提供这三类及供应商文件，官方API参数折叠；无需Shopify/eBay/JSON接口。
“国内网页采集”已接本机浏览器扩展、资料包核验和两次确认入库；仅当前页公开字段，
未做真实平台或所有网页模板验收。具体操作与限制见[国内网页采集说明](DOMESTIC_CAPTURE.md)。
原渠道适配器只保留历史数据与API兼容。下方配置表为此前能力记录，不是当前优先接入要求。

## 此前支持


| 渠道 | 接入方式 | 当前状态 |
| --- | --- | --- |
| Shopify | 店铺 Admin GraphQL access token，商品/库存读取权限 | 已实现只读变体采集；默认 API 2026-07；真实权限和当前服务 schema 尚未验证 |
| eBay | OAuth access token，Browse API | 已实现关键词商品搜索；默认 EBAY_US；无真实账号验收 |
| 授权 JSON API | 供应商公共 HTTPS GET，可选 Bearer token | 已实现字段映射与分页；需供应商接口匹配契约 |
| 1688、Alibaba、淘宝、天猫、京东、拼多多、AliExpress、Amazon | 各平台开放接口授权；当前可用供应商 CSV/JSON | 渠道目录与账号配置已建立，官方适配器待实现 |
| 供应商文件 | 现有 CSV/JSON 导入或投递箱 | 已有本地导入流程，不代表在线连接 |

登录账号不等于开放接口权限。此轮不模拟登录、保存浏览器 cookie 或绕过平台权限。
Shopify 文档工具需要重新认证，公开文档请求被网络拒绝；协议夹具通过不等于真实 schema 通过。
eBay 是公开商品搜索读取，不是供应商询价、采购或卖家订单同步。

## 操作流程

1. 添加账号，填写平台、名称、官方/供应商接口地址和授权令牌。Shopify 地址为
   `https://店铺.myshopify.com`；eBay 为 `https://api.ebay.com`。
2. 选择账号、查询和页数上限（1–20），创建只读采集任务。每页最多50条，每次任务
   最多5000条；达到页数上限会保存游标，明确重试可继续。重启后的任务先暂停核对。
3. 候选按50项分页查看，保留来源事实、零库存、原币价格、图片 URL 和来源快照。
   标题保留来源原文，字段名 `title_zh` 不意味着已完成中文翻译。
4. 跨页选择最多500件，预检去重、事实与身份冲突，勾选确认后才导入商品。
   采集和预检本身不创建商品；重复导入请求返回原回执，不重复建档。
5. 事实变化的候选进入冲突状态。展开原始/最新快照，选择已核实记录、填写核对依据并
   明确确认，再重新预检。已绑定商品的候选核对只改变来源记录，已有商品资料保持原值。

原币报价保留为来源证据；只有明确提供的 CNY `cost_cny` 才进入人民币采购成本。
图片只保存 URL 引用，不自动下载、确认使用权或加入商品。导入不自动审核、运行模型、
生成图片或提交 Noon。后续仍走现有事实补齐、素材授权、制作验收和商品审核流程。
Shopify 以 variant ID 保持规格身份，缺 SKU 时使用来源 ID；完整 SPU/父子变体管理仍待开发。

## 通用 JSON 接口契约

接口须是公共 HTTPS 地址，不带查询参数、登录信息或片段，不接受自定义请求头。
读取请求自动添加 `limit`，可选 `q` 和 `cursor`。返回示例：

```json
{
  "items": [{
    "external_id": "supplier-variant-001",
    "title_zh": "供应商原始商品标题",
    "source_url": "https://supplier.example/products/001",
    "source_sku": "BLACK-5",
    "supplier": "供应商名称",
    "facts": "黑色；5件装",
    "stock": 0,
    "source_currency": "CNY",
    "source_price": 9.5,
    "cost_cny": 8.0,
    "images": ["https://supplier.example/images/001.jpg"]
  }],
  "next_cursor": null
}
```

`external_id` 必须稳定且代表唯一规格；`stock: 0` 保留为0，未知留空。
下一页可提供不透明游标或同域 HTTPS URL；跨域地址、内网目标和重复游标拒绝。
嵌套响应可在“高级配置”填写点分隔路径，例如：

```json
{
  "items_path": "data.products",
  "cursor_path": "data.next_cursor",
  "field_mapping": {
    "external_id": "variant.id",
    "title_zh": "product.title",
    "source_sku": "variant.sku"
  }
}
```

未映射字段读取同名路径。Shopify 配置可设置 `api_version`；eBay 可设置
`marketplace_id`。高级配置不接受 token、password、cookie 等凭证字段。

## 凭证与恢复

令牌独立保存在资料目录的 `credentials/source-channels/`，目录权限0700、文件0600，
不进入SQLite、状态响应或备份包。密码输入留空保留旧凭证，原值不回显。
修改接口地址须同时提供新令牌或明确清除旧令牌；已有文件与账号/平台/地址不匹配时
会显示异常并阻止读取凭证。并发修改须使用当前版本。

备份恢复保留候选、来源与导入回执，停用账号、暂停采集；恢复后重新核对授权和配置。
在途采集请求结束前不能安排恢复。即使任务已取消，也等待正在返回的请求结束。

## 开发验证

使用独立临时目录和合成协议，没有真实账号请求。
`bash scripts/check.sh` 包含全量Python测试、实际服务器检查、JS语法、19页桌面/手机
浏览器检查，以及完整合成采集入库/重复回执/冲突核对流程。
实际结果见 [验证记录](VERIFICATION.md)。没有验证真实供应商价格、商品质量、
Noon可售或新macOS发行包；完整ERP的其他阶段见 [持续路线图](AUTOMATION_ROADMAP.md)。
