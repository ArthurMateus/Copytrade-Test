# Runs the bot forever: it restarts 10 s after any exit (a /restart, a crash, a fresh `git pull`).
# Started by the Windows task "copybot" (tools/install_autostart.ps1), or by hand:
#   powershell -ExecutionPolicy Bypass -File tools\run_bot.ps1
# Only ONE copy may run (data\bot.lock refuses a second one): close the old PowerShell loop window first.
$ErrorActionPreference = "Continue"
Set-Location -Path (Split-Path -Parent $PSScriptRoot)

# The secrets saved with setx (a task started at boot does not always see them as normal variables)
foreach ($n in "DISCORD_BOT_TOKEN", "DISCORD_CHANNEL_ID", "DISCORD_OWNER_ID", "COPYBOT_PIN", "INVO_TOKEN_FILE",
               "HL_ACCOUNT_ADDRESS", "HL_AGENT_KEY", "HEALTHCHECK_URL") {
    $v = [Environment]::GetEnvironmentVariable($n, "User")
    if ($v) { Set-Item -Path "Env:$n" -Value $v }
}

$uv = (Get-Command uv -ErrorAction SilentlyContinue).Source
if (-not $uv) { $uv = Join-Path $env:USERPROFILE ".local\bin\uv.exe" }
New-Item -ItemType Directory -Force -Path "logs" | Out-Null

while ($true) {
    Add-Content -Path "logs\console.log" -Value "=== $(Get-Date -Format s) starting copybot"
    & $uv run copybot *>> "logs\console.log"
    Start-Sleep -Seconds 10
}
