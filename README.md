# Noon Studio / noonai

面向国内货源与 noon 沙特站的运营平台。现有 Python/SQLite 后端和中文网页工作台，
包含多渠道授权采集与货源导入、英阿内容、图像/视频加工、批量铺货流程、人工审核、平台回读、
订单、库存、采购、财务、完整备份恢复以及 macOS 桌面源码。

## 云端开发环境

Python 3.12 + Node.js 22，仓库根目录执行：

```bash
bash scripts/setup.sh
bash scripts/check.sh
.venv/bin/python workbench/server.py --data /tmp/noonai-preview --port 8791
```

`setup.sh` 创建独立虚拟环境，安装锁定的运行依赖和 Chromium；
`maintenance.sh` 更新缓存环境；`doctor.py` 检查依赖与 FFmpeg。
服务只监听 127.0.0.1，写操作使用会话令牌。

Codex Cloud 设置、能力边界与启动提示词见 [云端环境说明](docs/CODEX_CLOUD.md)。
GitHub Actions 在 push、PR、手动启动和每日定时执行全量测试及独立浏览器检查。

## 验证状态

2026-10-03 最新 `bash scripts/check.sh` 通过：**776项测试全部成功**，实际服务器
启动/重启、JS语法以及 Chromium 全部40页在1440/390宽度的检查通过。合成浏览器
流程覆盖采集、订单导入、结算、银行核销、拣货发货、退货隔离、采购关联、批量编辑、周期规则与异常核对，以及盘点、物流交接、价格、广告、字段模板和国内MV3扩展采集。20个浏览器脚本通过。
另追加供应商询价比较、汇率档案和库存补货建议。本轮新增21个模块，涵盖国内网页采集和本地ERP，见 [交付说明](docs/ERP_DELIVERY.md)。1万SKU容量报告见
[合成容量实测](docs/benchmarks/erp-10000.json)，初始目录已按每页500件同步。

采集重点按用户要求改为1688、淘宝、拼多多；原有渠道目录与只读适配器保留兼容。
此前实现的账号凭证隔离和候选导入，
接入现有商品与铺货流程。其他平台官方适配器待实现，真实账号连通性尚未验证。
配置见 [多渠道接入说明](docs/MULTICHANNEL.md)。完整平台与真实店铺经营仍未完成；
Noon店铺、真实商品生图、价格库存写入、商用部署及新macOS打包仍待验收。
1688/淘宝/拼多多使用步骤见[国内网页采集说明](docs/DOMESTIC_CAPTURE.md)。
当前证据与下一步见 [验证记录](docs/VERIFICATION.md) 和 [自动化路线图](docs/AUTOMATION_ROADMAP.md)。

## macOS 安装包

新版0.43.0构建入口：在Mac仓库目录运行 `./desktop/build_dmg.command`，产物为DMG、备用ZIP和SHA256校验文件。完整步骤见[桌面构建说明](desktop/README.md)。当前Linux环境没有生成新版DMG，GitHub macOS构建也未执行；不把构建脚本当作已交付的安装包。

## 资料

- [工作台功能与配置](workbench/README.md)
- [完整平台范围](PLATFORM_SCOPE.md)
- [产品定义](PRODUCT.md)
- [沙特站开发规格](noon沙特铺货系统开发规格.md)
- [工程与自动化验收要求](AGENTS.md)

仓库仅保存源码、合成样例和测试。经营数据、原图、登录信息、凭证与安装包留在本机。
