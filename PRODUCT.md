# Product
<!-- impeccable:product-schema 1 -->
## Platform
macOS desktop first; embedded local web interface.
## Users
面向中国运营者的 noon 沙特跨境运营平台，用户当前尚未开店。
## Product Purpose
建立类似店小秘、赛盒的 noon 专用平台，覆盖选品采集、商品与多媒体自动加工、刊登、采购、仓储、履约、售后、财务和分析。
## Operating Context
用户需要功能完整的 macOS 平台，当前最高优先级是大量铺货：选品→改图/翻译→批量刊登。默认从批次开始，减少重复选择和生成，只把异常交给用户处理。其他运营模块保留，当前不优先扩展。
## Capabilities and Constraints
完整范围见 PLATFORM_SCOPE.md。当前有本地商品、批量加工、媒体与运营台账；Codex 订阅文字已在当前账号做过实测。真实店铺、货源采集接口和实际商品视觉质量未验证，经营模式待确认。不将本地记录、模拟数据、API提交或应用启动称为真实经营成功。
## Stack
Python/SQLite本地服务、原生网页界面、Swift/AppKit/WKWebView桌面客户端；内置运行依赖。当前Apple Silicon本地开发构建。
## Product Principles
来源可追溯；未知字段保留待确认；修改后重新审核；账务与库存事务不可重复计入；平台反馈、本地记录和实际可售分开。
