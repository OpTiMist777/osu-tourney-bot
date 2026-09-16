$startup = [Environment]::GetFolderPath('Startup')
$shortcutPath = Join-Path $startup 'OsuTourneyBot.lnk'
if (Test-Path -LiteralPath $shortcutPath) {
    Remove-Item -LiteralPath $shortcutPath -Force
    Write-Host 'Autostart removed.' -ForegroundColor Green
} else {
    Write-Host 'Autostart shortcut was not found.' -ForegroundColor Yellow
}
