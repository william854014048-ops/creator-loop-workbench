param([switch]$NoBrowser, [switch]$Stop)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$runtimePath = Join-Path $projectRoot 'runtime'
$serviceUrl = 'http://127.0.0.1:8780'
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'

function Get-LoopHealth {
    try { return Invoke-RestMethod -Uri ($serviceUrl + '/api/health') -TimeoutSec 2 }
    catch { return $null }
}

function Test-LoopServerCommandLine([string]$commandLine, [string]$serverPath) {
    $exactPath = [regex]::Escape($serverPath)
    return $commandLine -match ('(?:^|\s)(?:"' + $exactPath + '"|' + $exactPath + ')(?=\s|$)')
}

$health = Get-LoopHealth
if ($health -and $health.application -ne 'creator-loop') {
    throw 'Port 8780 is used by another application.'
}
if ($Stop) {
    if (-not $health) { Write-Output 'Creator Loop is already stopped.'; exit 0 }
    $connection = Get-NetTCPConnection -LocalPort 8780 -State Listen -ErrorAction Stop
    $serviceProcess = Get-CimInstance Win32_Process -Filter ('ProcessId=' + $connection.OwningProcess)
    $serverPath = Join-Path $projectRoot 'server.py'
    if (-not $serviceProcess -or -not (Test-LoopServerCommandLine $serviceProcess.CommandLine $serverPath)) {
        throw 'The running service was started elsewhere. Stop it in its original terminal.'
    }
    Stop-Process -Id $serviceProcess.ProcessId -ErrorAction Stop
    Write-Output 'Creator Loop stopped. You can now back up data.'
    exit 0
}
if (-not $health) {
    $pythonCommand = Get-Command python -ErrorAction Stop
    if (-not (Test-Path -LiteralPath $runtimePath)) {
        $null = New-Item -ItemType Directory -Path $runtimePath
    }
    $arguments = @('-B', ('"' + (Join-Path $projectRoot 'server.py') + '"'), '--port', '8780')
    $process = Start-Process -FilePath $pythonCommand.Source -ArgumentList $arguments -WorkingDirectory $projectRoot -WindowStyle Hidden -RedirectStandardOutput (Join-Path $runtimePath 'server-output.log') -RedirectStandardError (Join-Path $runtimePath 'server-error.log') -PassThru
    for ($attempt = 0; $attempt -lt 50; $attempt++) {
        Start-Sleep -Milliseconds 200
        $health = Get-LoopHealth
        if ($health -and $health.application -eq 'creator-loop') { break }
        if ($process.HasExited) { break }
    }
    if (-not $health -or $health.application -ne 'creator-loop') {
        throw ('Creator Loop did not start. See ' + (Join-Path $runtimePath 'server-error.log'))
    }
    $record = @{pid=$process.Id; url=$serviceUrl; application='creator-loop'; started_at=[DateTimeOffset]::Now.ToString('o')}
    [System.IO.File]::WriteAllText((Join-Path $runtimePath 'server.json'), ($record | ConvertTo-Json), [System.Text.UTF8Encoding]::new($false))
}
if (-not $NoBrowser) { Start-Process $serviceUrl }
Write-Output $serviceUrl
