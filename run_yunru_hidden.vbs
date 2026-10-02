' 云茹 · 隐藏启动器（开机自启动用）
'
' 为什么不直接用 run_stage3.bat：
'   1) 会在桌面上留一个控制台窗口；
'   2) 不重定向输出，而 `data/bot.err.log` 是所有诊断脚本（watch_restart / watch_round2 /
'      memory_audit …）唯一的日志来源，断了这条线等于把排查手段丢了。
' 所以这里用 WScript.Shell 以**隐藏窗口**跑 cmd，并把 stdout/stderr 追加到
' `data/bot.out.log` / `data/bot.err.log`——与手工启动时用的是同一对日志文件。
'
' **先查有没有实例在跑**（这一步不能省）：正在跑的实例占着 bot.err.log，
' 第二个 cmd 连日志都打不开（`The process cannot access the file …`），
' 于是它既不启动、也不报错，看起来像"自启动没生效"。查到就直接退出。
'
' 装法（启动文件夹，不需要管理员）：把指向本文件的快捷方式放进
'   %APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup
'   目标：wscript.exe   参数："<项目路径>\run_yunru_hidden.vbs"
' 手动跑一次：wscript.exe run_yunru_hidden.vbs
' 想用计划任务（需要管理员）：schtasks /Create /TN YunRuBot /SC ONLOGON /RL LIMITED /F ^
'   /TR "wscript.exe \"<项目路径>\run_yunru_hidden.vbs\""
'
' 引号一律用 Chr(34) 拼，不在字符串里嵌 """"（嵌套引号很容易写错，已踩过）。

Option Explicit

Dim shell, fso, root, q, command, wmi, running
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

' 脚本所在目录＝项目根目录（这样搬目录也不用改内容）
root = fso.GetParentFolderName(WScript.ScriptFullName)

' 已经有一个实例在跑就安静退出（同一个项目目录下）
running = 0
On Error Resume Next
Set wmi = GetObject("winmgmts:\\.\root\cimv2")
If Err.Number = 0 Then
    Dim items, item
    Set items = wmi.ExecQuery("SELECT CommandLine FROM Win32_Process WHERE Name = 'python.exe'")
    For Each item In items
        If Not IsNull(item.CommandLine) Then
            If InStr(item.CommandLine, "qq_roleplay_bot.stage3_main") > 0 Then
                running = running + 1
            End If
        End If
    Next
End If
On Error GoTo 0

If running > 0 Then
    ' 不启动、不报错：登录时已经有了，这次什么都不用做
    WScript.Quit 0
End If

q = Chr(34)
' 显式指明包在哪：共用 venv 的 .pth 指向**部署树**（run\src），
' 所以这一侧必须自己说清用我这份 src，否则会静默 import 到另一边。
command = "cmd.exe /c cd /d " & q & root & q & " && " & _
          "set " & q & "PYTHONPATH=" & root & "\src" & q & " && " & _
          q & ".venv\Scripts\python.exe" & q & " -m qq_roleplay_bot.stage3_main " & _
          "1>>" & q & "data\bot.out.log" & q & " " & _
          "2>>" & q & "data\bot.err.log" & q

' 0 = 隐藏窗口，False = 不等待它结束
shell.Run command, 0, False
