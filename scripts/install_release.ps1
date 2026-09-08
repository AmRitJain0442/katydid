param(
    [Parameter(Mandatory = $true)][string]$State,
    [Parameter(Mandatory = $true)][string]$Spec,
    [Parameter(Mandatory = $true)][string]$Logs,
    [ValidatePattern('^Vultron-[A-Za-z0-9-]+$')][string]$TaskName = 'Vultron-Release'
)
$ErrorActionPreference = 'Stop'
$workspaceRoot = Split-Path -Parent $PSScriptRoot
$hostExecutable = Join-Path $workspaceRoot '.venv\Scripts\pythonw.exe'
$launcher = Join-Path $PSScriptRoot 'maintain_release.py'
$statePath = (Resolve-Path -LiteralPath $State).Path
$specPath = (Resolve-Path -LiteralPath $Spec).Path
$logsPath = [System.IO.Path]::GetFullPath($Logs)
if (-not (Test-Path -LiteralPath $hostExecutable)) { throw 'Install the locked platform environment first.' }
if (-not (Test-Path -LiteralPath (Join-Path $statePath 'managed-release.json'))) {
    throw 'Deploy the first verified release before installing recovery.'
}
if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    throw 'A task with this name already exists; inspect that specific task before reinstalling.'
}
$argumentList = @($launcher, '--state', $statePath, '--spec', $specPath, '--logs', $logsPath)
foreach ($argument in $argumentList) {
    if ($argument.Contains('"') -or $argument.Contains("`n") -or $argument.Contains("`r")) {
        throw 'Task arguments cannot contain quotes or newlines.'
    }
}
$taskArguments = ($argumentList | ForEach-Object { '"' + $_ + '"' }) -join ' '
$identity = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$action = New-ScheduledTaskAction -Execute $hostExecutable -Argument $taskArguments -WorkingDirectory $workspaceRoot
$logon = New-ScheduledTaskTrigger -AtLogOn -User $identity
$periodic = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 1)
$principal = New-ScheduledTaskPrincipal -UserId $identity -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 3) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger @($logon, $periodic) `
    -Principal $principal -Settings $settings | Out-Null
Write-Output "Installed $TaskName for logon and one-minute health/recovery checks. Explicitly stopped releases remain stopped."
