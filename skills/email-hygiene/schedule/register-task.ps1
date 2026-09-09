<#
.SYNOPSIS
    Register (or remove) the scheduled task that runs email hygiene.

.EXAMPLE
    .\register-task.ps1 -Time 07:30
    .\register-task.ps1 -DryRun          # schedule a non-destructive run
    .\register-task.ps1 -Unregister
#>
[CmdletBinding()]
param(
    [string]$Time = "07:30",
    [ValidateSet("Daily", "Weekly")]
    [string]$Frequency = "Daily",
    [string]$TaskName = "EmailHygiene",
    [switch]$DryRun,
    [switch]$AllowWorktree,
    [switch]$Unregister
)

$ErrorActionPreference = "Stop"

if ($Unregister) {
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "Removed scheduled task '$TaskName'."
    } else {
        Write-Host "No scheduled task named '$TaskName'."
    }
    return
}

$scriptRoot = Split-Path -Parent $PSScriptRoot

# A git worktree is temporary. Registering a task that points into one produces
# a job that works today and silently stops working when the worktree is
# removed, so resolve to the main checkout instead.
$gitDir = Join-Path $scriptRoot ".git"
$repoRoot = $scriptRoot
while ($repoRoot -and -not (Test-Path (Join-Path $repoRoot ".git"))) {
    $parent = Split-Path -Parent $repoRoot
    if ($parent -eq $repoRoot) { break }
    $repoRoot = $parent
}

if ($repoRoot -and (Test-Path (Join-Path $repoRoot ".git")) -and -not $AllowWorktree) {
    $gitPath = Join-Path $repoRoot ".git"
    # In a worktree, .git is a file pointing elsewhere; in a normal clone it's a directory.
    if (-not (Get-Item $gitPath -Force).PSIsContainer) {
        $mainCheckout = (git -C $repoRoot worktree list |
            Select-Object -First 1) -replace '\s.*$', ''
        $mainCheckout = $mainCheckout -replace '/', '\'
        $relative = $scriptRoot.Substring($repoRoot.Length).TrimStart('\')
        $candidate = Join-Path $mainCheckout $relative

        Write-Warning "This is a git worktree, which is temporary."
        if (Test-Path (Join-Path $candidate "scripts\run.py")) {
            Write-Host "Using the main checkout instead: $candidate"
            $scriptRoot = $candidate
        } else {
            Write-Warning "The main checkout doesn't have this skill yet:"
            Write-Warning "  $candidate"
            Write-Warning "Merge the branch first, then re-run this script."
            Write-Warning "To register against the worktree anyway, pass -AllowWorktree."
            throw "Refusing to register a task that will break when the worktree is removed."
        }
    }
}

$runner = Join-Path $scriptRoot "scripts\run.py"
if (-not (Test-Path $runner)) { throw "Cannot find run.py at $runner" }

$python = (Get-Command pythonw.exe -ErrorAction SilentlyContinue).Source
if (-not $python) { $python = (Get-Command python.exe -ErrorAction Stop).Source }

$flags = if ($DryRun) { "--non-interactive" } else { "--apply --non-interactive" }
$arguments = "`"$runner`" $flags"

$action = New-ScheduledTaskAction -Execute $python -Argument $arguments -WorkingDirectory $scriptRoot

$trigger = if ($Frequency -eq "Weekly") {
    New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday -At $Time
} else {
    New-ScheduledTaskTrigger -Daily -At $Time
}

$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -DontStopIfGoingOnBatteries `
    -AllowStartIfOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 30) `
    -MultipleInstances IgnoreNew

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Description "Personal Outlook.com inbox hygiene: file noise, detect repetitive senders, suggest unsubscribes." `
    -Force | Out-Null

Write-Host "Registered '$TaskName' — $Frequency at $Time"
Write-Host "  $python $arguments"
Write-Host ""
Write-Host "Run it now:      Start-ScheduledTask -TaskName $TaskName"
Write-Host "Check history:   Get-ScheduledTaskInfo -TaskName $TaskName"
Write-Host "Reports land in: $env:USERPROFILE\.email-hygiene\reports"
