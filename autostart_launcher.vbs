Option Explicit

Dim shell, fso, project, launcher
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

project = fso.GetParentFolderName(WScript.ScriptFullName)
launcher = fso.BuildPath(project, "launcher.ps1")

shell.CurrentDirectory = project
shell.Run "powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File """ & launcher & """ -AutoStart", 1, False
