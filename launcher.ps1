param(
    [switch]$AutoStart
)

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

$project = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = Join-Path $project 'venv\Scripts\python.exe'
$bot = Join-Path $project 'bot.py'
$script:process = $null
$script:isClosing = $false
$script:logQueue = New-Object 'System.Collections.Concurrent.ConcurrentQueue[string]'
$script:stdoutTask = $null
$script:stderrTask = $null

$form = New-Object System.Windows.Forms.Form
$form.Text = 'OsuTourneyBot - Launcher'
$form.Size = New-Object System.Drawing.Size(820, 560)
$form.MinimumSize = New-Object System.Drawing.Size(680, 420)
$form.StartPosition = 'CenterScreen'

$status = New-Object System.Windows.Forms.Label
$status.Text = 'Status: stopped'
$status.Location = New-Object System.Drawing.Point(20, 18)
$status.AutoSize = $true
$status.Font = New-Object System.Drawing.Font('Segoe UI', 11, [System.Drawing.FontStyle]::Bold)
$form.Controls.Add($status)

$start = New-Object System.Windows.Forms.Button
$start.Text = 'Start'
$start.Location = New-Object System.Drawing.Point(20, 52)
$start.Size = New-Object System.Drawing.Size(115, 34)
$form.Controls.Add($start)

$restart = New-Object System.Windows.Forms.Button
$restart.Text = 'Restart'
$restart.Location = New-Object System.Drawing.Point(145, 52)
$restart.Size = New-Object System.Drawing.Size(125, 34)
$form.Controls.Add($restart)

$stop = New-Object System.Windows.Forms.Button
$stop.Text = 'Stop'
$stop.Location = New-Object System.Drawing.Point(280, 52)
$stop.Size = New-Object System.Drawing.Size(115, 34)
$stop.Enabled = $false
$form.Controls.Add($stop)

$log = New-Object System.Windows.Forms.TextBox
$log.Multiline = $true
$log.ReadOnly = $true
$log.ScrollBars = 'Vertical'
$log.BackColor = [System.Drawing.Color]::FromArgb(25, 25, 25)
$log.ForeColor = [System.Drawing.Color]::White
$log.Font = New-Object System.Drawing.Font('Consolas', 9)
$log.Location = New-Object System.Drawing.Point(20, 105)
$log.Size = New-Object System.Drawing.Size(765, 390)
$form.Controls.Add($log)

function Add-Log([string]$line) {
    if (-not [string]::IsNullOrWhiteSpace($line)) {
        [void]$script:logQueue.Enqueue($line)
    }
}

function Stop-Bot {
    try {
        if ($script:process -and -not $script:process.HasExited) {
            Stop-Process -Id $script:process.Id -Force -ErrorAction SilentlyContinue
            $script:process.WaitForExit(2000)
        }
    } catch {
        Add-Log ('Stop error: ' + $_.Exception.Message)
    }
    $script:process = $null
    $script:stdoutTask = $null
    $script:stderrTask = $null
    $status.Text = 'Status: stopped'
    $start.Enabled = $true
    $restart.Enabled = $true
    $stop.Enabled = $false
}

function Start-Bot {
    try {
        if (-not (Test-Path -LiteralPath $python)) {
            throw 'venv was not found. Run .\Make.ps1 install first.'
        }
        if (-not (Test-Path -LiteralPath $bot)) {
            throw 'bot.py was not found in the project folder.'
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
        $psi.EnvironmentVariables['PYTHONIOENCODING'] = 'utf-8'
        $psi.EnvironmentVariables['PYTHONUTF8'] = '1'
        $psi.EnvironmentVariables['PYTHONUNBUFFERED'] = '1'
        $psi.StandardOutputEncoding = [System.Text.Encoding]::UTF8
        $psi.StandardErrorEncoding = [System.Text.Encoding]::UTF8
        $script:process = New-Object System.Diagnostics.Process
        $script:process.StartInfo = $psi
        $script:process.EnableRaisingEvents = $false
        if (-not $script:process.Start()) { throw 'Windows could not start the bot process.' }
        $script:stdoutTask = $script:process.StandardOutput.ReadLineAsync()
        $script:stderrTask = $script:process.StandardError.ReadLineAsync()
        $status.Text = 'Status: running'
        $start.Enabled = $false
        $restart.Enabled = $false
        $stop.Enabled = $true
        Add-Log ('[' + (Get-Date -Format 'HH:mm:ss') + '] Bot started by launcher')
    } catch {
        $script:process = $null
        $status.Text = 'Status: error'
        $start.Enabled = $true
        $restart.Enabled = $true
        $stop.Enabled = $false
        Add-Log ('Start error: ' + $_.Exception.Message)
        [System.Windows.Forms.MessageBox]::Show($_.Exception.Message, 'Launcher error') | Out-Null
    }
}

function Read-ProcessOutput {
    if (-not $script:process) { return }
    foreach ($stream in @('stdoutTask', 'stderrTask')) {
        $task = Get-Variable -Name $stream -Scope Script -ValueOnly
        if ($task -and $task.IsCompleted) {
            if ($task.Status -eq [System.Threading.Tasks.TaskStatus]::RanToCompletion) {
                $line = $task.Result
                if ($null -ne $line) { Add-Log $line }
                if (-not $script:process.HasExited) {
                    $next = if ($stream -eq 'stdoutTask') {
                        $script:process.StandardOutput.ReadLineAsync()
                    } else {
                        $script:process.StandardError.ReadLineAsync()
                    }
                    Set-Variable -Name $stream -Scope Script -Value $next
                }
            } else {
                Set-Variable -Name $stream -Scope Script -Value $null
            }
        }
    }
}

$timer = New-Object System.Windows.Forms.Timer
$timer.Interval = 500
$timer.Add_Tick({
    Read-ProcessOutput
    $pending = $null
    while ($script:logQueue.TryDequeue([ref]$pending)) {
        if (-not $log.IsDisposed) {
            $log.AppendText($pending + [Environment]::NewLine)
            $log.SelectionStart = $log.TextLength
            $log.ScrollToCaret()
        }
        $pending = $null
    }
    if ($script:process -and $script:process.HasExited -and -not $script:isClosing) {
        $exitCode = $script:process.ExitCode
        Add-Log ("Bot process stopped with exit code $exitCode")
        Stop-Bot
    }
})
$timer.Start()

$start.Add_Click({ Start-Bot })
$stop.Add_Click({ Stop-Bot })
$restart.Add_Click({ Stop-Bot; Start-Bot })
$form.Add_FormClosing({ $script:isClosing = $true; $timer.Stop(); Stop-Bot })
$form.Add_Shown({
    if ($AutoStart) {
        Start-Bot
    }
})
[void]$form.ShowDialog()
