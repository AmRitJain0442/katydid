param(
    [Parameter(Mandatory = $true)][string]$Fleet,
    [Parameter(Mandatory = $true)][string]$Logs,
    [string]$CredentialFile,
    [string]$SecurityManifest,
    [ValidatePattern('^$|^[a-z][a-z0-9-]{0,63}$')][string]$SweepNamespace,
    [ValidateRange(1, 65535)][int]$Port = 8765,
    [ValidateRange(1, 86400)][int]$Interval = 60,
    [ValidateRange(0, 2147483647)][int]$ScheduleSeconds = 86400,
    [ValidatePattern('^Vultron-[A-Za-z0-9-]+$')][string]$TaskName = 'Vultron-Worker'
)
$ErrorActionPreference = 'Stop'
$workspaceRoot = Split-Path -Parent $PSScriptRoot
$pythonExecutable = Join-Path $workspaceRoot '.venv\Scripts\python.exe'
$hostExecutable = Join-Path $workspaceRoot '.venv\Scripts\pythonw.exe'
$launcher = Join-Path $PSScriptRoot 'run_service.py'
$fleetPath = (Resolve-Path -LiteralPath $Fleet).Path
$logsPath = [System.IO.Path]::GetFullPath($Logs)
if (-not (Test-Path -LiteralPath $pythonExecutable)) { throw 'Run python scripts/dev.py sync first.' }
if (-not (Test-Path -LiteralPath $hostExecutable)) { throw 'The Windows background Python launcher is unavailable.' }
if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    throw 'A task with this name already exists; inspect or remove that specific task before reinstalling.'
}
$refreshTaskName = "$TaskName-SecurityDB"
if ($SecurityManifest -and (Get-ScheduledTask -TaskName $refreshTaskName -ErrorAction SilentlyContinue)) {
    throw 'The security refresh task already exists; inspect that specific task before reinstalling.'
}
& $pythonExecutable -m katydid fleet $fleetPath | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Fleet validation failed.' }
$argumentList = @($launcher, '--fleet', $fleetPath, '--logs', $logsPath,
    '--port', [string]$Port, '--interval', [string]$Interval,
    '--schedule-seconds', [string]$ScheduleSeconds)
if ($CredentialFile) {
    $credentialPath = (Resolve-Path -LiteralPath $CredentialFile).Path
    $argumentList += @('--credential-file', $credentialPath)
}
if ($SecurityManifest) {
    $manifestPath = (Resolve-Path -LiteralPath $SecurityManifest).Path
    if ((Split-Path -Leaf $manifestPath) -ne 'setup.json') { throw 'SecurityManifest must be the installed setup.json.' }
    $argumentList += @('--security-manifest', $manifestPath)
}
if ($SweepNamespace) { $argumentList += @('--sweep-namespace', $SweepNamespace) }
foreach ($argument in $argumentList) {
    if ($argument.Contains('"') -or $argument.Contains("`n") -or $argument.Contains("`r")) {
        throw 'Task arguments cannot contain quotes or newlines.'
    }
}
$taskArguments = ($argumentList | ForEach-Object { '"' + $_ + '"' }) -join ' '
$identity = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$action = New-ScheduledTaskAction -Execute $hostExecutable -Argument $taskArguments -WorkingDirectory $workspaceRoot
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $identity
$principal = New-ScheduledTaskPrincipal -UserId $identity -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable `
    -RestartCount 10 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Principal $principal -Settings $settings | Out-Null
if ($SecurityManifest) {
    $refreshScript = Join-Path $workspaceRoot 'scripts\refresh_security.py'
    $refreshArguments = @($refreshScript, '--manifest', $manifestPath, '--logs', $logsPath)
    $refreshCommand = ($refreshArguments | ForEach-Object { '"' + $_ + '"' }) -join ' '
    $refreshAction = New-ScheduledTaskAction -Execute $hostExecutable -Argument $refreshCommand -WorkingDirectory $workspaceRoot
    $refreshTrigger = New-ScheduledTaskTrigger -Daily -At '03:00'
    $refreshSettings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable `
        -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 15) `
        -ExecutionTimeLimit (New-TimeSpan -Minutes 10) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
    Register-ScheduledTask -TaskName $refreshTaskName -Action $refreshAction -Trigger $refreshTrigger `
        -Principal $principal -Settings $refreshSettings | Out-Null
    Write-Output "Installed $refreshTaskName for daily vulnerability database refresh."
}
Write-Output "Installed $TaskName for the current user's logon. Start it with Start-ScheduledTask -TaskName $TaskName."
