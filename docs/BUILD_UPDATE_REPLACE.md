# TeachingGateway 构建、更新与替换手册

本文供本地能力有限的 AI 或技术人员照步骤执行。不要跳过校验，不要把源码目录直接当发布包。

## 1. 不会被仓库保存的构建输入

以下二进制文件刻意受 `.gitignore` 排除，GitHub 不保存账号、密钥或第三方二进制：

- `release-tools/downloads/claude-x64.exe`
- `release-tools/downloads/WebView2-x64.exe`
- `release-tools/downloads/PortableGit-x64.7z.exe`
- `tools/cc-switch/cc-switch.exe`
- `tools/cc-switch/portable.ini`

构建机还需要 Windows 10/11 x64、Python 3.12、PyInstaller，以及系统自带的 .NET Framework x64 C# 编译器。不要提交 API Key、`auth.json`、日志、截图、任务记录或构建产物。

## 2. 拉取与确认源码

```powershell
git switch main
git pull --ff-only origin main
git status --short
git log -1 --oneline
```

`git status --short` 必须为空。检查 `release-tools/build_release.ps1` 中四项版本与源码 `VERSION` 一致。

## 3. 运行测试

```powershell
py -3.12 -m unittest discover -s p7000 -p "test_*.py"
py -3.12 -m unittest discover -s p7050 -p "test_*.py"
py -3.12 -m unittest discover -s p9090 -p "test_*.py"
py -3.12 -m unittest discover -s release-tools -p "test_*.py"
py -3.12 -m compileall -q p7000 p7050 p9090 p9091
```

失败不得继续打包。浏览器 UI 测试需要单独启动 Edge 调试端口；没有该环境时必须记录为“未执行”，不能写成通过。

## 4. 构建全新目录

始终使用新的输出目录，禁止覆盖旧构建：

```powershell
$stamp = Get-Date -Format yyyyMMdd-HHmmss
$out = Join-Path $PWD "release-tools\out\$stamp"
& .\release-tools\build_release.ps1 -OutputRoot $out
```

只接受脚本最后输出的 `PACKAGE_READY=...` 目录。构建会校验 Claude Code 固定哈希、WebView2 微软签名，编译四项服务和 UI Automation worker，并生成 `manifest.json`。

## 5. 生成 ZIP 与 SHA-256

```powershell
$package = Join-Path $out 'TeachingGateway-x64'
$zip = Join-Path $PWD "release-tools\out\TeachingGateway-x64-$stamp.zip"
& .\release-tools\finalize_release.ps1 -PackageRoot $package -ZipPath $zip
Get-FileHash $zip -Algorithm SHA256
Get-Content ($zip + '.sha256')
```

命令显示的哈希必须与 `.sha256` 文件一致。保留上一个已验证 ZIP 作为回滚包。

## 6. 替换教学机版本

程序文件和运行数据分离：程序在解压目录，密码、配置、课件、截图、任务和日志在 `%LOCALAPPDATA%\TeachingGateway`。正常换包不会删除运行数据。

1. 运行旧目录的 `stop.cmd`。
2. 不要覆盖旧目录；把新 ZIP 完整解压到新目录，例如 `C:\TeachingGateway-r6`。
3. 从新目录运行 `initialize.cmd`。已有认证和数据会复用。
4. 必须看到所有检查 `OK`，随后运行 `status.cmd`。
5. 平板分别访问 7000、7050、9090、9091，验证登录、上传、投放、鼠标和 AI 任务。
6. 重启 Windows，登录同一上课账户，等待 30 秒后再次运行 `status.cmd`。
7. 新版稳定后才删除旧程序目录；不要删除 `%LOCALAPPDATA%\TeachingGateway`。

## 7. 回滚

1. 新目录运行 `stop.cmd`。
2. 旧版 ZIP 解压到另一个新目录。
3. 旧目录运行 `repair.cmd`，让自启动入口重新指向旧目录。
4. 运行 `status.cmd` 并用平板复测。

## 8. 可直接交给本地 AI 的任务文本

```text
阅读 docs/BUILD_UPDATE_REPLACE.md 和 docs/RUNTIME_OPERATIONS.md。只从 origin/main 构建；先确认工作区干净并运行全部 Python 测试。使用全新的 release-tools/out 时间戳目录执行 build_release.ps1，再用 finalize_release.ps1 生成 ZIP 和 SHA-256。不得提交或打包本机账号、API Key、auth.json、日志、截图和旧任务数据。停止旧包后把新 ZIP 解压到新目录，运行 initialize.cmd、status.cmd，核对四项版本和健康检查。重启并复测平板四端口。任何 FAILED 都停止，不得声称完成。
```
