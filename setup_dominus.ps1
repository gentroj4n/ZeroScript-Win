$ErrorActionPreference = "Stop"

Write-Host ""
Write-Host "ZeroScript-Win - Dominus / romcp setup" -ForegroundColor Cyan
Write-Host "Requires Node.js 20+ and npm." -ForegroundColor DarkGray
Write-Host ""

$node = Get-Command node -ErrorAction SilentlyContinue
$npm = Get-Command npm -ErrorAction SilentlyContinue
if (-not $node -or -not $npm) {
    Write-Error "Node.js/npm not found. Install Node.js 20 or newer from https://nodejs.org/ and rerun this script."
    exit 1
}

$nodeVersion = (& node --version).Trim()
Write-Host "Detected Node.js $nodeVersion"
$major = 0
if ($nodeVersion -match '^v?(\d+)') { $major = [int]$Matches[1] }
if ($major -lt 20) {
    Write-Error "Dominus/romcp requires Node.js 20 or newer. Detected $nodeVersion."
    exit 1
}

Write-Host "Installing the official dominus-cli package..." -ForegroundColor Cyan
& npm install --global dominus-cli@latest
if ($LASTEXITCODE -ne 0) {
    Write-Error "npm install failed with exit code $LASTEXITCODE."
    exit $LASTEXITCODE
}

$romcp = Get-Command romcp -ErrorAction SilentlyContinue
if (-not $romcp) {
    Write-Error "The romcp command was not found after installation. Close and reopen PowerShell to refresh PATH, then run 'romcp setup'."
    exit 1
}

Write-Host ""
Write-Host "Starting the interactive Studio/plugin setup. Follow its prompts." -ForegroundColor Yellow
& romcp setup
if ($LASTEXITCODE -ne 0) {
    Write-Error "romcp setup did not complete successfully. Resolve the displayed issue, then rerun 'romcp setup'."
    exit $LASTEXITCODE
}

Write-Host ""
Write-Host "Checking the local Studio connection (up to 30 seconds)..." -ForegroundColor Cyan
& romcp doctor --wait 30
$doctorExit = $LASTEXITCODE

Write-Host ""
Write-Host "Next: open/reload Roblox Studio, ensure the Dominus/romcp plugin is enabled, then restart the ZeroScript bridge." -ForegroundColor Green
Write-Host "The ZeroScript config already registers the Dominus MCP process. See docs/DOMINUS_MCP.md."
if ($doctorExit -ne 0) {
    Write-Warning "The doctor check did not pass. Read its output and resolve Studio/plugin connectivity before relying on the tools."
}
exit $doctorExit
