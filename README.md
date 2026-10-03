# Noon Studio / noonai

面向国内货源与 noon 沙特站的运营平台。现有 Python/SQLite 后端和中文网页工作台，
包含货源导入、英阿内容、图像/视频加工、批量铺货流程、人工审核、平台回读、
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

2026-10-03 首次接入：本地原始基线 373 项测试，25 failures、17 errors。
该结果来自迁移前已有代码；CI 保留这些失败，不能宣称完整平台通过验收。
本轮修复后复测为24 failures、16 errors；Linux安装/诊断/服务器重启/Chromium检查通过，
全量回归继续失败。当前验证与下一步见 [验证记录](docs/VERIFICATION.md) 和 [自动化路线图](docs/AUTOMATION_ROADMAP.md)。
真实 noon 店铺、1688 授权、真实商品生图、价格库存写入与商用云部署仍待验证。

## 资料

- [工作台功能与配置](workbench/README.md)
- [完整平台范围](PLATFORM_SCOPE.md)
- [产品定义](PRODUCT.md)
- [沙特站开发规格](noon沙特铺货系统开发规格.md)
- [工程与自动化验收要求](AGENTS.md)

仓库仅保存源码、合成样例和测试。经营数据、原图、登录信息、凭证与安装包留在本机。
