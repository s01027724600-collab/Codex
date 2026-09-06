Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
appData = sh.ExpandEnvironmentStrings("%APPDATA%")
localAppData = sh.ExpandEnvironmentStrings("%LOCALAPPDATA%")

Function Q(value)
  Q = Chr(34) & Replace(value, Chr(34), Chr(34) & Chr(34)) & Chr(34)
End Function

Set processes = GetObject("winmgmts:\\.\root\cimv2").ExecQuery( _
  "SELECT CommandLine FROM Win32_Process WHERE Name='python.exe' OR Name='pythonw.exe'")
For Each process In processes
  If Not IsNull(process.CommandLine) Then
    If InStr(1, process.CommandLine, "touchpad_gateway.py", vbTextCompare) > 0 And _
       InStr(1, process.CommandLine, scriptDir, vbTextCompare) > 0 Then
      WScript.Quit 0
    End If
  End If
Next

pythonExe = localAppData & "\Programs\Python\Python312\pythonw.exe"
If Not fso.FileExists(pythonExe) Then
  pythonExe = "pythonw.exe"
End If

cmd = Q(pythonExe) & " " & _
  Q(scriptDir & "\touchpad_gateway.py") & " " & _
  "--config " & Q(scriptDir & "\config.json") & " " & _
  "--auth-file " & Q(appData & "\ClaudeCodeGateway\auth.json")

sh.CurrentDirectory = fso.GetParentFolderName(scriptDir)
sh.Run cmd, 0, False
