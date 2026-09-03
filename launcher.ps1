Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

$project = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = Join-Path $project 'venv\Scripts\python.exe'
$bot = Join-Path $project 'bot.py'
$script:process = $null

$form = New-Object System.Windows.Forms.Form
$form.Text = 'OsuTourneyBot — Launcher'
$form.Size = New-Object System.Drawing.Size(820, 560)
$form.MinimumSize = New-Object System.Drawing.Size(680, 420)
$form.StartPosition = 'CenterScreen'

$status = New-Object System.Windows.Forms.Label
$status.Text = 'Статус: остановлен'
$status.Location = New-Object System.Drawing.Point(20, 18)
$status.AutoSize = $true
$status.Font = New-Object System.Drawing.Font('Segoe UI', 11, [System.Drawing.FontStyle]::Bold)
$form.Controls.Add($status)

$start = New-Object System.Windows.Forms.Button
$start.Text = 'Запустить'
$start.Location = New-Object System.Drawing.Point(20, 52)
$start.Size = New-Object System.Drawing.Size(115, 34)
$form.Controls.Add($start)

$restart = New-Object System.Windows.Forms.Button
$restart.Text = 'Перезапустить'
$restart.Location = New-Object System.Drawing.Point(145, 52)
$restart.Size = New-Object System.Drawing.Size(125, 34)
$form.Controls.Add($restart)

$stop = New-Object System.Windows.Forms.Button
$stop.Text = 'Остановить'
$stop.Location = New-Object System.Drawing.Point(280, 52)
$stop.Size = New-Object System.Drawing.Size(115, 34)
$stop.Enabled = $false
$form.Controls.Add($stop)

$log = New-Object System.Windows.Forms.TextBox
$log.Multiline = $true
$log.ReadOnly = $true
$log.ScrollBars = 'Vertical'
$log.BackColor = [System.Drawing.Color]::FromArgb( twenty=25, 25, 25 )
$log.ForeColor = [System.Drawing.Color]::White
$log.Font = New-Object System.Drawing.Font('Consolas', 9)
$log.Location = New-Object System.Drawing.Point(20, 105)
$log.Size = New-Object System.Drawing.Size(765, 390)
$form.Controls.Add($log)

function Add-Log([string]$line) {
    $form.BeginInvoke([Action]{ $log.AppendText($line + [Environment]::NewLine) }) | Out-Null
}

function Stop-Bot {
    if ($script:process -and -not $script:process.HasExited) {
        $script:process.Kill($true)
        $script:process.WaitForExit(2000)
    }
    $script:process = $null
    $status.Text = 'Статус: остановлен'
    $start.Enabled = $true
    $restart.Enabled = $true
    $stop.Enabled = $false
}

function Start-Bot {
    if (-not (Test-Path -LiteralPath $python)) {
        [System.Windows.Forms.MessageBox]::Show('Не найдено venv. Сначала выполни .\Make.ps1 install.', 'Ошибка')
        return
    }
    if ($script:process -and -not $script:process.HasExited) { return }
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $python
    $psi.Arguments = '"' + $bot + '"'
    $psi.WorkingDirectory = $project
    $psi.UseShellExecute = $false
    $psi.CreateNoWindow = $true
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $psi.StandardOutputEncoding = [System.Text.Encoding]::UTF8
    $psi.StandardErrorEncoding = [System.Text.Encoding]::UTF8
    $script:process = New-Object System.Diagnostics.Process
    $script:process.StartInfo = $psi
    $script:process.EnableRaisingEvents = $true
    $script:process.add_OutputDataReceived({ param($s,$e) if ($e.Data) { Add-Log $e.Data } })
    $script:process.add_ErrorDataReceived({ param($s,$e) if ($e.Data) { Add-Log $e.Data } })
    $script:process.add_Exited({ $form.BeginInvoke([Action]{ Stop-Bot }) | Out-Null })
    [void]$script:process.Start()
    $script:process.BeginOutputReadLine()
    $script:process.BeginErrorReadLine()
    $status.Text = 'Статус: запущен'
    $start.Enabled = $false
    $restart.Enabled = $false
    $stop.Enabled = $true
    Add-Log ('[' + (Get-Date -Format 'HH:mm:ss') + '] Бот запущен через launcher')
}

$start.Add_Click({ Start-Bot })
$stop.Add_Click({ Stop-Bot })
$restart.Add_Click({ Stop-Bot; Start-Bot })
$form.Add_FormClosing({ Stop-Bot })
[void]$form.ShowDialog()
