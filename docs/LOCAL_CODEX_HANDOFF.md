# 本次改动简要备注（交接本地 Codex）

更新日期：2026-09-08。本文汇总本次对话中已经落入源码的改动、现场现象和仍需在
Windows 教学机完成的工作。构建和换包的详细命令以 `BUILD_UPDATE_REPLACE.md` 为准。

## 1. 本地 Codex 先做什么

1. 只拉取 `origin/main`，确认工作区干净；不要从聊天内容手工重写源码。
2. 核对版本：p7000 `0.4.0`、p7050 `0.3.2`、p9090 `0.1.5`、p9091 `0.1.1`；
   `release-tools/build_release.ps1` 必须显示相同版本。
3. 检查未提交到 GitHub 的构建输入：Claude Code、WebView2、Portable Git、CC Switch；
   不要把 API Key、`auth.json`、日志、截图或任务记录提交或打进包。
4. 运行四组单元测试和 `compileall`；任一失败立即停止，不得继续打包。
5. 使用全新时间戳输出目录构建，执行 `finalize_release.ps1`，核对 ZIP 与 `.sha256`。
6. 保留旧版回滚包；旧目录运行 `stop.cmd`，新版解压到新目录，禁止覆盖安装。
7. 新目录运行 `initialize.cmd`，全部为 `OK` 后运行 `status.cmd`；再重启、登录同一
   上课账户、等待 30 秒并复测。任何 `FAILED` 都不能报告完成。

## 2. p7000 文件、投放与认证

- 四个网页共用 `auth.json` 登录；远程 API 使用签名会话 Cookie，本机回环可排障。
- 7000 已保护课件、截图、剪贴板、上传、投放、扫描、教学控制和重启触控板接口。
- 平板可流式上传单个不超过 512 MB 的文件到“平板传输”，断线清理 `.partial`，同名
  自动加序号，不覆盖原文件。
- 平板图片支持 PNG/JPG/JPEG/GIF/BMP，上传后可等比例黑底全屏投放；教学机右上角、
  Esc 和平板“结束投放”都可关闭。
- “教学控制”栏提供固定白名单操作：音量、媒体播放、PowerPoint 放映/翻页/退出、
  显示桌面和任务视图；不接受任意按键、程序路径或命令。

## 3. PPT/PPTX/PDF 扫描

- 已补齐 `.ppt`、`.pptx`、`.pdf`；旧机器保留旧配置时也会自动补齐内置扩展名。
- U 盘递归扫描遇到无权限/损坏目录会跳过并继续，不再因一个目录终止整次扫描。
- 自动扫描占用中再点手动扫描，会合并排队一次后续扫描，不再静默丢请求。
- 归档页显示运行状态、检查数、归档数和错误；日志记录开始、完成及异常。
- 只扫描检测到的可移动盘和配置的下载目录，不应擅自递归扫描整个系统盘。

## 4. 7050 现场故障与处理

现场反复出现：远程点击浏览器最小化/关闭或 PowerPoint 结束放映后，平板滑动不再移动；
触摸一下教学机实体屏幕后恢复，之后可能再次出现。

已做的处理：

- 点击/双击/右键改为成批 `SendInput`；移动使用 `SetCursorPos`，每次移动前释放可能由
  全屏程序遗留的 `ClipCursor` 限制；拖动有超时安全松键。
- 修复浏览器输入队列在错误清理与新事件同时发生时可能永久睡眠的竞态。
- 7000 右上角有“重启触控板”应急按钮。它写入一次性请求，由监督器只重启当前包的
  7050；通常等待约 10 秒再刷新 7050。
- 相对触控速度提供 1× 至 5× 五档，默认 3×并保存在平板浏览器；位置映射不乘倍率。

这些修改在 Linux 中有单元测试，但 Windows 真实触摸、PowerPoint 和畅言桌面行为尚需
实机确认。验收必须重复至少 20 次“放映—结束放映—立即滑动”，期间不要触摸教学机。

## 5. 鼠标附近画面与局域网

- 服务未给端口限速；HTTP/1.1 持久连接和 `TCP_NODELAY` 用于降低重复握手和小包延迟。
- 局部视野最高 960×540；操作时最高约 6 fps，短暂停顿约 2.4 fps，空闲约 0.7 fps。
- 全屏概览约每 1.2 秒更新，其余活跃帧只传局部图，防止概览重复占用 Wi-Fi 和解码。
- 防火墙覆盖 Domain/Private/Public，但远端限制 `LocalSubnet`；不存在公网隧道，也不要
  做互联网端口映射。访客 Wi-Fi、AP 隔离、跨 VLAN 和校方策略仍可能阻断连接。

## 6. 畅言 Windows 10 教育版自启动

现场出现重启后四端口未就绪及 `ERR_EMPTY_RESPONSE`。单张截图不能证明唯一原因；该错误
只表示连接后未收到完整 HTTP 响应。畅言可能替换或限制 Explorer，使启动文件夹及 HKCU
Run 项存在却不执行，因此新增不依赖 Explorer 的第三入口：

- 启动文件夹 `TeachingGateway.lnk`；
- `HKCU\Software\Microsoft\Windows\CurrentVersion\Run`；
- 登录后延迟 15 秒的当前用户计划任务 `TeachingGateway-Logon`。

三条入口由 `supervisor.lock` 合并成一个监督器。监督器写
`%LOCALAPPDATA%\TeachingGateway\last-supervisor-start.json`：重启后时间未更新表示入口未
执行；时间更新表示监督器启动过，应查最新 `supervise-*` 与端口 `.err.log`。计划任务
`LastTaskResult=0` 才是成功；`0x80070005` 通常需要管理员或校方策略放行。

## 7. 上线验收清单

- `status.cmd` 显示上述四个准确版本，四项 health/UI 均为 `OK`。
- 平板登录四端口；7000 上传普通文件、横/竖图片投放及三种关闭方式均正常。
- 含 `.ppt`/`.pptx` 的 U 盘能扫描；扫描进度和完成日志可见。
- 7050 分别验证 1×、3×、5×，单击、双击、拖动、滚动、PPT 结束放映和应急重启。
- 7000 教学控制逐项验证音量、媒体、F5、Shift+F5、Esc、翻页、Win+D、Win+Tab。
- 重启后检查计划任务、`last-supervisor-start.json`，并手动结束一个网关确认自动恢复。
- `stop.cmd` 后等待 15 秒不得被拉起；`start.cmd` 后应全部恢复。

## 8. 可直接给本地 Codex 的任务文本

```text
阅读 docs/LOCAL_CODEX_HANDOFF.md、docs/BUILD_UPDATE_REPLACE.md 和 docs/RUNTIME_OPERATIONS.md。
从干净的 origin/main 构建全新 Windows x64 发布包，核对版本 7000=0.4.0、7050=0.3.2、
9090=0.1.5、9091=0.1.1。先运行全部测试，生成 ZIP 和 SHA-256，不覆盖旧目录、不携带
账号/密钥/auth.json/日志/截图/任务数据。停止旧包后解压到新目录，运行 initialize.cmd
和 status.cmd；重启登录后检查 TeachingGateway-Logon、last-supervisor-start.json 和四端口。
按交接清单实测上传、投放、扫描、教学控制、触控档位、PPT 结束放映、7050 应急重启和
监督器恢复。任何 FAILED 或版本不符立即停止，保留旧包并报告原始错误与脱敏日志。
```
