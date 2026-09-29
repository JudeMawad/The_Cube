# Cube Gaming Mode

$ErrorActionPreference = "Continue"

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

$configPath = Join-Path $env:USERPROFILE "CubeScripts\cube-settings.psd1"
if (-not (Test-Path $configPath)) {
    throw "Create CubeScripts\cube-settings.psd1 from cube-settings.example.psd1 first."
}
$config = Import-PowerShellDataFile $configPath
foreach ($key in @("WslDistro", "WslRepoPath", "Model", "LlmHost", "LlmPort", "AiPort")) {
    if (-not $config[$key] -or "$($config[$key])" -match "REPLACE_ME") {
        throw "Missing Cube setting: $key"
    }
}
$WslDistro = $config.WslDistro
$WslRepoPath = $config.WslRepoPath
$Model = $config.Model
$LlmHost = $config.LlmHost
$LlmPort = $config.LlmPort
$AiPort = $config.AiPort

# ---------------------------------------------------------------------------
# Stop Cube AI
# ---------------------------------------------------------------------------

# Stop the AI node.
wsl.exe -d $WslDistro -- pkill -f "uvicorn ai_node.app:app" 2>$null

Start-Sleep -Seconds 1

# Stop WSL completely, releasing Whisper/Kokoro CUDA resources.
wsl.exe --terminate $WslDistro

# Unload the configured model from VRAM.
lms unload $Model 2>$null

# Stop the LM Studio API server.
lms server stop 2>$null

Write-Host "Cube AI stack stopped. Gaming mode active."
