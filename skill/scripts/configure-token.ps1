param([string]$ExpiresOn)
$ErrorActionPreference = 'Stop'
if (-not $ExpiresOn) { $ExpiresOn = Read-Host 'Actual MinerU expiry date (YYYY-MM-DD; blank if unknown)' }
if ($ExpiresOn) { $null = [datetime]::ParseExact($ExpiresOn, 'yyyy-MM-dd', [Globalization.CultureInfo]::InvariantCulture) }
$credentialFolder = Join-Path $env:LOCALAPPDATA 'CodexMinerU'
New-Item -ItemType Directory -Force -Path $credentialFolder | Out-Null
$secret = Read-Host 'MinerU precision API token' -AsSecureString
if ($secret.Length -eq 0) { throw 'Token must not be empty' }
$secret | ConvertFrom-SecureString | Set-Content -LiteralPath (Join-Path $credentialFolder 'token.dpapi') -Encoding UTF8
Write-Output 'Encrypted token saved for the current Windows user. No restart required.'
$metadata = @{configured_at=(Get-Date).ToString('o'); expires_on=$ExpiresOn; reminder_status='pending_scheduler'}
$metadataPath = Join-Path $credentialFolder 'token-metadata.json'
if (Test-Path -LiteralPath $metadataPath) {
    $previous = Get-Content -Raw -LiteralPath $metadataPath | ConvertFrom-Json
    if ($previous.automation_id) { $metadata.automation_id = $previous.automation_id }
}
$metadata | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $credentialFolder 'token-metadata.json') -Encoding UTF8
if ($ExpiresOn) {
    python (Join-Path $PSScriptRoot 'reminder.py') --expires-on $ExpiresOn --output (Join-Path $credentialFolder 'reminder-request.json')
    if ($LASTEXITCODE -ne 0) { Write-Warning 'Token saved, but reminder preparation failed.' }
}
Write-Output 'Tell Codex the token was replaced and ask it to create/update the one-shot reminder. No task has been scheduled by this script.'
