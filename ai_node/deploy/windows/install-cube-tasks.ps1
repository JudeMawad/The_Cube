# Install Cube AI Windows scheduled tasks and desktop shortcuts.
# Run this script from an Administrator PowerShell.

$ErrorActionPreference = "Stop"

$installDir = Join-Path $env:USERPROFILE "CubeScripts"
if (-not (Test-Path (Join-Path $installDir "cube-settings.psd1"))) {
    throw "Create $installDir\cube-settings.psd1 before installing tasks."
}
$currentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name

New-Item -ItemType Directory -Force -Path $installDir | Out-Null

# Keep runnable Windows copies outside WSL so Gaming Mode can terminate
# the WSL distro without affecting the script that is currently executing.
Copy-Item `
    (Join-Path $PSScriptRoot "cube-ai-startup.ps1") `
    (Join-Path $installDir "cube-ai-startup.ps1") `
    -Force

Copy-Item `
    (Join-Path $PSScriptRoot "cube-gaming-mode.ps1") `
    (Join-Path $installDir "cube-gaming-mode.ps1") `
    -Force

$principal = New-ScheduledTaskPrincipal `
    -UserId $currentUser `
    -LogonType Interactive `
    -RunLevel Highest

$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew

# ---------------------------------------------------------------------------
# Cube AI Startup
# ---------------------------------------------------------------------------

$startupScript = Join-Path $installDir "cube-ai-startup.ps1"

$startupAction = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$startupScript`""

$startupTrigger = New-ScheduledTaskTrigger `
    -AtLogOn `
    -User $currentUser

Register-ScheduledTask `
    -TaskName "Cube AI Startup" `
    -Action $startupAction `
    -Trigger $startupTrigger `
    -Principal $principal `
    -Settings $settings `
    -Force | Out-Null

# ---------------------------------------------------------------------------
# Gaming Mode
# ---------------------------------------------------------------------------

$gamingScript = Join-Path $installDir "cube-gaming-mode.ps1"

$gamingAction = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$gamingScript`""

Register-ScheduledTask `
    -TaskName "Cube Gaming Mode" `
    -Action $gamingAction `
    -Principal $principal `
    -Settings $settings `
    -Force | Out-Null

# ---------------------------------------------------------------------------
# Desktop shortcuts
# ---------------------------------------------------------------------------

$desktop = [Environment]::GetFolderPath("Desktop")
$shell = New-Object -ComObject WScript.Shell

$shortcut = $shell.CreateShortcut((Join-Path $desktop "Cube AI On.lnk"))
$shortcut.TargetPath = "$env:SystemRoot\System32\schtasks.exe"
$shortcut.Arguments = '/run /tn "Cube AI Startup"'
$shortcut.WorkingDirectory = "$env:SystemRoot\System32"
$shortcut.Save()

$shortcut = $shell.CreateShortcut((Join-Path $desktop "Gaming Mode.lnk"))
$shortcut.TargetPath = "$env:SystemRoot\System32\schtasks.exe"
$shortcut.Arguments = '/run /tn "Cube Gaming Mode"'
$shortcut.WorkingDirectory = "$env:SystemRoot\System32"
$shortcut.Save()

Write-Host "Cube AI Windows integration installed."
Write-Host "Installed scripts: $installDir"
Write-Host "Created tasks: Cube AI Startup, Cube Gaming Mode"
Write-Host "Created desktop shortcuts: Cube AI On, Gaming Mode"
