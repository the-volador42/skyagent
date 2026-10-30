# Запуск и остановка локального Neo4j для SkyAgent (без службы Windows и без прав админа).
# Использование: powershell -File scripts\neo4j.ps1 start|stop|status
param([ValidateSet("start", "stop", "status")] [string]$Action = "status")

$Neo4jHome = "C:\Users\maxim\Tools\neo4j-community-5.26.32"
$env:JAVA_HOME = "C:\Users\maxim\Tools\jdk-21.0.12.1+1"
$env:NEO4J_HOME = $Neo4jHome

function Get-Neo4jProcess {
    Get-CimInstance Win32_Process -Filter "Name='java.exe'" |
        Where-Object { $_.CommandLine -match [regex]::Escape($Neo4jHome) }
}

function Test-Neo4j {
    try { Invoke-WebRequest http://127.0.0.1:7474 -UseBasicParsing -TimeoutSec 2 | Out-Null; $true } catch { $false }
}

switch ($Action) {
    "start" {
        if (Get-Neo4jProcess) { "Neo4j уже запущен"; break }
        Start-Process -FilePath "$Neo4jHome\bin\neo4j.bat" -ArgumentList "console" -WindowStyle Hidden `
            -RedirectStandardOutput "$Neo4jHome\logs\console.out" -RedirectStandardError "$Neo4jHome\logs\console.err"
        for ($i = 0; $i -lt 60; $i++) { if (Test-Neo4j) { "Neo4j запущен: http://localhost:7474"; return }; Start-Sleep 2 }
        "Neo4j не ответил за 2 минуты, смотри $Neo4jHome\logs\console.out"
    }
    "stop" {
        $p = Get-Neo4jProcess
        if (-not $p) { "Neo4j не запущен"; break }
        $p | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -Confirm:$false }
        "Neo4j остановлен"
    }
    "status" {
        if (Test-Neo4j) { "Neo4j работает" } else { "Neo4j не отвечает" }
    }
}
