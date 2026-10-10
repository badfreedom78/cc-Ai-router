' ============================================================
'  AI Router 控制台 - 静默启动器
'
'  双击本文件即可启动可视化控制台，且不会残留任何命令行窗口。
'  原理：用 WScript.Shell 以「隐藏窗口」方式调用 pythonw.exe，
'        pythonw.exe 本身不带控制台，所以只会剩下可视化窗口。
'
'  首次运行（依赖尚未安装）时，会自动转为可见窗口运行
'  「第一次运行.bat」来完成安装。
'
'  注意：本文件请保持 ANSI/GBK 或 ASCII 编码保存。
' ============================================================

Option Explicit

Dim fso, sh, root, gui, marker, pyw, py, bat

Set fso = CreateObject("Scripting.FileSystemObject")
Set sh  = CreateObject("WScript.Shell")

' 本脚本所在的根目录
root = fso.GetParentFolderName(WScript.ScriptFullName)
gui  = root & "\gui"

marker = gui & "\.venv\.deps-ok"
pyw    = gui & "\.venv\Scripts\pythonw.exe"
py     = gui & "\.venv\Scripts\python.exe"
bat    = root & "\第一次运行.bat"

sh.CurrentDirectory = gui

If Not fso.FileExists(marker) Then
    ' 首次运行：走可见窗口的安装脚本
    sh.Run """" & bat & """", 1, False
ElseIf fso.FileExists(pyw) Then
    ' 隐藏窗口（0），不等待（False）
    sh.Run """" & pyw & """ """ & gui & "\main.py""", 0, False
ElseIf fso.FileExists(py) Then
    sh.Run """" & py & """ """ & gui & "\main.py""", 0, False
Else
    MsgBox "未找到运行环境，请先双击「第一次运行.bat」完成安装。", 16, "AI Router"
End If
