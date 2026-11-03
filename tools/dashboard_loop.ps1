# Keeps the read-only web dashboard running (README "Web dashboard"): restarts it 10 s after it stops.
# Used by the "copybot-dashboard" scheduled task; it can also be run by hand.
Set-Location (Split-Path $PSScriptRoot -Parent)
while ($true) { uv run copybot-dashboard; Start-Sleep 10 }
