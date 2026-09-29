# Cube AI PC startup

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
# LM Studio
# ---------------------------------------------------------------------------

Start-Sleep -Seconds 5

# Make sure the LM Studio API server is running.
lms server start --port $LlmPort --bind 0.0.0.0 | Out-Null

Start-Sleep -Seconds 3

# Make sure the configured model is loaded.
$loadedModels = lms ps 2>&1 | Out-String

if ($loadedModels -notmatch [regex]::Escape($Model)) {
    lms load $Model --gpu=max
}

# ---------------------------------------------------------------------------
# WSL / port forwarding
# ---------------------------------------------------------------------------

# Start Ubuntu first.
wsl.exe -d $WslDistro -- true

# Give WSL/systemd time to initialize after a full terminate.
Start-Sleep -Seconds 3

$wslIpOutput = wsl.exe -d $WslDistro -- hostname -I
$wslIp = ($wslIpOutput.Trim() -split '\s+')[0]

if (-not $wslIp) {
    throw "Could not determine WSL IP."
}

# WSL IP can change after restart, so recreate the forwarding rule.
netsh interface portproxy delete v4tov4 `
    listenaddress=0.0.0.0 `
    listenport=$AiPort 2>$null | Out-Null

netsh interface portproxy add v4tov4 `
    listenaddress=0.0.0.0 `
    listenport=$AiPort `
    connectaddress=$wslIp `
    connectport=$AiPort | Out-Null

# ---------------------------------------------------------------------------
# Cube AI node
# ---------------------------------------------------------------------------

# Do not start another copy if it is already running.
$existingAiNode = wsl.exe -d $WslDistro -- pgrep -f "uvicorn ai_node.app:app" 2>$null

if (-not $existingAiNode) {
    $aiArgs = @(
        "-d", $WslDistro,
        "--cd", $WslRepoPath,
        "--",
        "env",
        "CUBE_AI_LLM_BASE_URL=http://${LlmHost}:$LlmPort/v1",
        "CUBE_AI_LLM_MODEL=$Model",
        "CUBE_AI_LLM_TIMEOUT=20",
        "./ai_node/start.sh"
    )

    Start-Process `
        -FilePath "wsl.exe" `
        -ArgumentList $aiArgs `
        -WindowStyle Hidden
}
