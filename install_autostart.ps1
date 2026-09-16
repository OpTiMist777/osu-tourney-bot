$ErrorActionPreference = 'Stop'
$project = Split-Path -Parent $MyInvocation.MyCommand.Path
$startup = [Environment]::GetFolderPath('Startup')
$shortcutPath = Join-Path $startup 'OsuTourneyBot.lnk'
$vbsPath = Join-Path $project 'autostart_launcher.vbs'

$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($shortcutPath)
$shortcut.TargetPath = Join-Path $env:WINDIR 'System32\wscript.exe'
$shortcut.Arguments = '"' + $vbsPath + '"'
$shortcut.WorkingDirectory = $project
$shortcut.Description = 'Start OsuTourneyBot automatically at Windows sign-in'
$shortcut.Save()

Write-Host "Autostart installed: $shortcutPath" -ForegroundColor Green
