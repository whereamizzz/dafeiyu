' 静默启动后端并打开浏览器（可以在桌面放个快捷方式）
Set fso = CreateObject("Scripting.FileSystemObject")
Set sh = CreateObject("WScript.Shell")
root = fso.GetParentFolderName(fso.GetParentFolderName(WScript.ScriptFullName))
sh.CurrentDirectory = root
sh.Run """" & root & "\tools\start-bot.bat""", 0, False
WScript.Sleep 4000
sh.Run "http://127.0.0.1:8010", 1, False
