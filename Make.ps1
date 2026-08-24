param($target = "help")

function Run-Command($cmd, $desc) {
    Write-Host "Running: $desc..." -ForegroundColor Cyan -NoNewline
    try {
        Invoke-Expression $cmd | Out-Null
        Write-Host " OK" -ForegroundColor Green
    } catch {
        Write-Host " FAILED" -ForegroundColor Red
        Write-Host "Error: $_"
        exit 1
    }
}

switch ($target) {
    "run" {
        Write-Host "`nStarting bot..." -ForegroundColor Magenta
        if (-not (Test-Path "venv")) {
            Write-Host "Creating virtual environment..." -ForegroundColor Yellow -NoNewline
            py -m venv venv
            Write-Host " OK" -ForegroundColor Green
        }
        & .\venv\Scripts\python.exe bot.py
    }

    "install" {
        Write-Host "`nInstalling dependencies..." -ForegroundColor Magenta
        if (-not (Test-Path "venv")) { py -m venv venv }
        & .\venv\Scripts\python.exe -m pip install -q -r requirements.txt
        Write-Host "Done!" -ForegroundColor Green
    }

    "db" {
        Write-Host "`nChecking database..." -ForegroundColor Magenta
        if (-not (Test-Path "venv")) { py -m venv venv }
        & .\venv\Scripts\python.exe -c "import asyncio; from database import init_db; asyncio.run(init_db())"
    }

    "env" {
        if (-not (Test-Path ".env")) {
            @"
DISCORD_TOKEN=your_token_here
OSU_CLIENT_ID=
OSU_CLIENT_SECRET=
MONGODB_URI=mongodb://127.0.0.1:27017/osu_tourney_dev
"@ | Out-File -FilePath ".env" -Encoding ascii
            Write-Host "`nCreated .env file - FILL IN YOUR TOKEN!" -ForegroundColor Yellow
        } else {
            Write-Host "`n.env file already exists" -ForegroundColor Green
        }
    }

    "help" {
        Write-Host "`nOsuTourneyBot - Commands" -ForegroundColor Magenta
        Write-Host "------------------------" -ForegroundColor DarkGray
        Write-Host "  .\make.ps1 run      Start bot" -ForegroundColor White
        Write-Host "  .\make.ps1 install  Install dependencies" -ForegroundColor White
        Write-Host "  .\make.ps1 db       Check database" -ForegroundColor White
        Write-Host "  .\make.ps1 env      Create .env file" -ForegroundColor White
        Write-Host "  .\make.ps1 help     Show this help" -ForegroundColor White
        Write-Host ""
    }

    default {
        Write-Host "`nUnknown command: $target" -ForegroundColor Red
        .\make.ps1 help
        exit 1
    }
}
