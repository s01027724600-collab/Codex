教学机四服务部署包（Windows 10/11，Intel/AMD 64 位）

第一次使用
1. 完整解压 ZIP 到教学机可写的固定目录，例如 C:\TeachingGateway。
   不要在压缩软件内直接运行，不要只复制 EXE，不要解压后只移走部分文件。
2. 使用平时上课的 Windows 账户双击 initialize.cmd。
   输入两次网页登录密码；遇到防火墙管理员授权时同意。
   仅防火墙步骤提权，服务和自启动仍属于当前上课账户。
3. 必须看到所有步骤 OK，四个服务通过健康和网页检查；FAILED 或 NOT COMPLETE 都不是成功。
   修正失败原因后再次运行 initialize.cmd 或 repair.cmd，不需要重新配置已有密码。
4. 平板与教学机连接同一局域网，用窗口列出的教学机 IPv4 地址访问：
   7000：课件、截图、下载、投放、剪贴板。
   7050：触控板、倒 T 辅助、参考线、鼠标定位。
   9091：教学机窗口/桌面控制。
   9090：Claude Code 任务网关。
5. 双击 cc_switch.cmd，在教学机上配置 Claude Code 的账号或 API Key。
   包内不携带原机器的账号、密码、API Key、个人配置或历史任务。
   四服务就绪不等于 AI 已联网可用；AI 任务还需要可用凭据和网络。
6. 正式上课前重启一次，用平板再次访问四个端口。
   测试截图、图片投放和结束、鼠标单击/拖动、断网后的安全松键。

无需安装 Python。随包包含 Claude Code x64、CC Switch、Portable Git/Bash，
以及微软 WebView2 离线安装程序。初始化无需临时下载这些依赖。
服务默认在用户登录后隐藏启动；不是锁屏/未登录桌面的 Windows 系统服务。
不要删除 _internal 文件夹。不要在初始化后移动程序目录；若移动，重新初始化。

数据、配置和日志
%LOCALAPPDATA%\TeachingGateway
  auth.json：三服务共用的网页登录密码哈希。
  p7000.json：截图/课件目录等；默认使用用户本地目录，不依赖 D 盘。
  data\courses：归档课件；data\screenshots：截图，默认 60 秒一次、保留 7 天。
  state9090：任务记录；work：Claude Code 默认工作目录。
  logs：逐次初始化及各服务日志；last-initialize.json：最近初始化结果。
7000 当前是全天定时截图，尚未接入课表。

失败时
窗口不会自动消失。优先看 FAILED 行与对应日志；status.cmd 可重新诊断。
端口被占用：初始化不会强杀其他程序，请关闭旧服务/旧包，再重试。
本机可访问、平板不能：确认已允许防火墙、不是访客 Wi-Fi/AP 隔离、IP 没变。
如果学校策略禁止入站、程序执行或自启动，需要管理员解除限制，软件不能保证绕过策略。
WebView2 失败：手动运行 dependencies\WebView2-x64.exe 后重试。
文件损坏/缺失：从原 ZIP 解压到新目录，不要关闭杀毒；检查隔离记录或联系管理员。
自动启动异常：用同一个上课账户重新 initialize.cmd；自启动快捷方式是 TeachingGateway.lnk。
停止：stop.cmd，仅停止本目录服务，保留数据；下次登录仍会自启动。

打包验证边界
本机进行了独立端口、独立状态目录的服务部署演练；
这不替代真实教学机的 UAC、防火墙策略、登录自启动、无线网络及大屏验收。

依赖来源
Claude Code：https://claude.ai/install.ps1
Git for Windows：https://git-scm.com/install/windows
CC Switch：https://github.com/farion1231/cc-switch
WebView2：https://developer.microsoft.com/en-us/microsoft-edge/webview2/
