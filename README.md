# TeachingGateway

Windows 11 教学机辅助网关源码。

- `p7000`：课程资料与截图整理、平板文件传输及图片全屏投放。
- `p7050`：平板触控板、鼠标定位与教学机辅助控制。
- `p9090`：Claude Code 任务网关。
- `p9091`：Windows 程序与窗口控制网关。
- `release-tools`：x64 发布包构建、初始化、自启动及修复脚本。

## 部署

正式教学机请使用本地构建生成的发布 ZIP，完整解压后运行 `initialize.cmd`。初始化会创建登录配置、数据目录、防火墙规则和当前用户自启动项，并启动四项服务。

仓库不包含账号、API Key、本机状态、日志、截图、构建缓存或第三方二进制文件。

## 运维文档

- [`docs/BUILD_UPDATE_REPLACE.md`](docs/BUILD_UPDATE_REPLACE.md)：面向本地 AI/技术人员的拉取、测试、构建、打包、替换与回滚步骤。
- [`docs/RUNTIME_OPERATIONS.md`](docs/RUNTIME_OPERATIONS.md)：自启动监督、局域网通道、防火墙、诊断及日常操作说明。
