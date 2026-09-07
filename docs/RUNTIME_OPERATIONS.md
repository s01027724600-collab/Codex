# TeachingGateway 运行、自启动和局域网说明

## 运行模型

- 四项程序只在上课用户登录后运行，不是未登录时运行的 Windows 服务。
- `initialize.cmd` 和 `repair.cmd` 会立即启动四项程序，并启动隐藏监督器。
- 登录自启动有两条相同入口：启动文件夹 `TeachingGateway.lnk` 和 `HKCU\Software\Microsoft\Windows\CurrentVersion\Run`。
- 两条入口同时触发是安全的：监督器通过独占锁保证只有一个实例工作。
- 监督器每 8 秒检查四项 `/health` 与首页；监听消失时立即重启，仍在监听的程序连续三次检查失败才重启，避免一次瞬时超时造成误杀；它只停止并重启本发布目录对应的服务。
- `stop.cmd` 写入停止标记后再结束程序，防止监督器立即拉起；`start.cmd`、`repair.cmd` 或下次登录会清除标记。

## 局域网与“隧道”边界

本项目没有公网隧道，也不应把 7000/7050/9090/9091 映射到互联网。它提供的是平板到教学机的局域网通道：

- 服务监听 `0.0.0.0`；
- Windows 防火墙规则适用于所有网络配置文件，以兼容学校网被识别为“未识别/公用网络”；
- 防火墙远端范围限制为 `LocalSubnet`，只允许当前本地子网；
- 网段变化后应重新读取初始化/status 窗口列出的 IPv4 地址；
- 访客 Wi-Fi、AP 隔离、跨 VLAN 或学校入站组策略仍可能阻断访问，软件不会绕过校方策略。

## 快速诊断顺序

1. 教学机运行 `status.cmd`。四项都应显示 `OK`。
2. 在教学机浏览器打开 `http://127.0.0.1:7000/`。失败表示服务/程序问题，不是网络问题。
3. 运行 `ipconfig`，确认平板访问的是当前 IPv4，而不是旧地址。
4. 平板与教学机必须位于可互访的同一网段；移动网络、访客 Wi-Fi 通常不可用。
5. 管理员 PowerShell 检查：

```powershell
Get-NetTCPConnection -State Listen | Where-Object LocalPort -in 7000,7050,9090,9091
Get-NetFirewallRule -Name 'TeachingGateway-TCP-*' |
  Format-Table Name,Enabled,Profile,Direction,Action
Get-NetFirewallRule -Name 'TeachingGateway-TCP-*' |
  Get-NetFirewallAddressFilter | Format-Table InstanceID,RemoteAddress
```

6. 规则缺失或指向旧包时运行 `repair.cmd` 并同意 UAC。
7. 查看 `%LOCALAPPDATA%\TeachingGateway\logs` 中时间最新的 `supervise-*`、服务 stderr 和 repair 日志。

## ERR_EMPTY_RESPONSE

该错误表示浏览器建立连接后没有得到完整 HTTP 响应，可能是服务刚退出/重启、访问了旧 IP、安全软件中断连接或网络隔离。它不能单独证明是防火墙问题。

先在教学机测试 `127.0.0.1`，再检查当前 IP、监听端口和防火墙。若几秒后自动恢复，查看 supervisor 日志确认是否发生服务重启。

## 自启动验收

1. 运行 `repair.cmd`，全部项目必须 `OK`。
2. 重启教学机并登录平时上课的同一账户。
3. 30 秒后运行 `status.cmd`。
4. 检查启动目录快捷方式和 Run 项：

```powershell
Test-Path "$env:APPDATA\Microsoft\Windows\Start Menu\Programs\Startup\TeachingGateway.lnk"
Get-ItemProperty 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run' -Name TeachingGateway
```

5. 在任务管理器手动结束一个网关进程，等待最多 20 秒；监听消失会立即触发重启，随后 `status.cmd` 应再次显示该服务 `OK`。
6. 运行 `stop.cmd`，等待 15 秒；服务不应被重新拉起。
7. 运行 `start.cmd`，服务和监督器应恢复。

## 日常操作

- `status.cmd`：只诊断，不修改配置。
- `repair.cmd`：修复认证、目录、依赖、防火墙、自启动并启动监督器。
- `stop.cmd`：停止当前发布目录的服务和监督器，保留数据。
- `start.cmd`：清除停止标记、启动服务和监督器。
- `disable-autostart.cmd`：仅移除登录自启动；也可调用 `gateway_runtime.ps1 -Command disable-autostart`。

7000 的“教学控制”栏提供经过登录保护的常用按键：音量、媒体播放、PowerPoint
放映/翻页/退出、显示桌面和任务视图。演示按键会发送给教学机当前窗口，使用前应先
让 PowerPoint 成为前台窗口。服务端只接受固定白名单动作，不接受任意按键、程序或命令。

7050 设置菜单提供 1×、2×、3×、4×、5× 五档相对触控速度，默认 3×，选择会保存在
当前平板浏览器。位置映射模式仍按触控位置对应屏幕位置，不应用速度倍率。

7050 没有设置端口带宽上限。鼠标附近视野为最高 960×540；操作时最高约 6 帧/秒，
短暂停顿时降为约 2.4 帧/秒，空闲时约 0.7 帧/秒。全屏概览约每 1.2 秒更新一次，操作中的
其他帧只传鼠标附近图，避免扩大视野后反而挤占触控请求。HTTP/1.1 会复用局域网连接并关闭小包延迟。实际速度仍受教学机抓屏/JPEG 编码性能、
平板解码、Wi-Fi 信号、AP 隔离和拥塞影响，不能通过“给端口分配网速”绕过网络瓶颈。

## 触控板应急恢复

- PowerPoint 结束放映或全屏窗口关闭后，若 7050 仍无法移动，先回到 7000 页面点击右上角“重启触控板”。
- 该按钮只写入当前用户运行目录中的一次性请求；监督器会停止并重启当前发布包的 7050，不会结束其他目录或其他程序的进程。
- 请求通常在 10 秒内执行。等待后刷新 7050 页面；如果仍未恢复，再运行 `status.cmd` 并查看最新的 `supervise-*` 和 `7050-*.err.log`。

不要公开日志和 `auth.json`。向 AI 求助时可提供错误行、端口状态和脱敏后的日志，但必须删除密码、Cookie、令牌、学生信息和文件内容。
