# 0.42.0 构建交付记录 · 2026-10-03

应用：desktop/dist-v42/Noon Studio.app
DMG：desktop/dist/Noon-Studio-0.42.0-macOS-arm64.dmg
ZIP：desktop/dist/Noon-Studio-0.42.0-macOS-arm64.zip
构建日志：desktop/build-v42.log
版本、文件大小和 SHA-256：desktop/dist/Noon-Studio-0.42.0-manifest.json

PyInstaller 与 Swift arm64 构建成功；codesign --verify --deep --strict 由构建脚本执行并成功。DMG hdiutil verify 返回 checksum VALID。ZIP 中 Info.plist 版本为0.42.0。未运行应用、浏览器、业务功能测试或真实平台测试。未做 Developer ID 签名、公证或公开发行许可包验收。

新增直接二进制投递端点受现有本机 Host、token 和恢复状态保护。文件扩展名、编码、记录数与体积检查后写入暂存文件，flush/fsync 后原子链接到投递箱；扫描器不会发现半写文件。接收回执与导入处理回执分开。所有源代码运行行为仍待后续验证。
