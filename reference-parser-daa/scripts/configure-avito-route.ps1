param([ValidateSet('msk','spb')][string]$Node = 'msk', [switch]$NoRestart, [Parameter(Mandatory=$true)][string]$ProxyUrl)
$ErrorActionPreference = 'Stop'
$appRoot = Split-Path -Parent $PSScriptRoot
$envPath = Join-Path $appRoot '.env'
$karingPath = Join-Path $env:APPDATA 'karing\karing\service_core.json'
if (-not (Test-Path -LiteralPath $envPath)) { throw 'Application .env not found' }
if (-not (Test-Path -LiteralPath $karingPath)) { throw 'Karing configuration not found' }
# Select only the user's existing RU node. Do not modify Karing or its active node.
$pattern = if ($Node -eq 'msk') { -join @([char]0x041c,[char]0x0421,[char]0x041a) } else { -join @([char]0x0421,[char]0x041f,[char]0x0411) }
$karing = Get-Content -LiteralPath $karingPath -Raw -Encoding UTF8 | ConvertFrom-Json
$selected = @($karing.outbounds | Where-Object { $_.tag -match $pattern -and $_.type -eq 'vless' })
if ($selected.Count -ne 1) { throw 'Expected one existing RU VLESS node in Karing' }
$outbound = $selected[0]
$parsedAddress = $null
if (-not [Net.IPAddress]::TryParse($outbound.server, [ref]$parsedAddress)) { throw 'RU node requires an explicit IP address' }
$outbound.PSObject.Properties.Remove('domain_resolver')
$outbound.tag = 'avito-ru'
$outbound | Add-Member -NotePropertyName detour -NotePropertyValue 'pc-direct' -Force
$config = @{
    log = @{ level = 'error' }
    # No published ports: this listener is accessible only inside the Docker network.
    inbounds = @(@{type='mixed'; tag='avito-local'; listen='0.0.0.0'; listen_port=3068})
    dns = @{servers=@(@{type='local'; tag='system'})}
    outbounds = @($outbound, @{type='http'; tag='pc-direct'; server='host.docker.internal'; server_port=3065})
    route = @{final='avito-ru'; default_domain_resolver='system'}
}
$sessionDir = Join-Path $appRoot 'runtime\sessions'
New-Item -ItemType Directory -Path $sessionDir -Force | Out-Null
$configFile = Join-Path $sessionDir 'avito-proxy.json'
[IO.File]::WriteAllText($configFile, ($config | ConvertTo-Json -Depth 30), [Text.UTF8Encoding]::new($false))
# This file contains the user's VPN credentials. Restrict its Windows ACL.
$identity = [Security.Principal.WindowsIdentity]::GetCurrent().Name
& icacls $configFile /grant:r "${identity}:(F)" "*S-1-5-18:(F)" | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Cannot grant private config access' }
& icacls $configFile /inheritance:r | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Cannot restrict private config access' }
$settingsText = [IO.File]::ReadAllText($envPath)
$existingProfiles = [regex]::Match($settingsText, '(?m)^COMPOSE_PROFILES=(.*)$').Groups[1].Value.Trim()
$profiles = @($existingProfiles.Split(',') | Where-Object { $_ -and $_ -ne 'web' -and $_ -ne 'avito-proxy' }) + @('avito-proxy')
foreach ($setting in @(@('AVITO_PROXY_URL',$ProxyUrl), @('AVITO_TRANSPORT','http'), @('COMPOSE_PROFILES',($profiles -join ',')))) {
    $regex = '(?m)^' + $setting[0] + '=.*$'
    $replacement = $setting[0] + '=' + $setting[1]
    if ([regex]::IsMatch($settingsText, $regex)) {
        $settingsText = [regex]::Replace($settingsText, $regex, [Text.RegularExpressions.MatchEvaluator]{param($match) $replacement})
    } else { $settingsText = $settingsText.TrimEnd() + "`r`n" + $replacement + "`r`n" }
}
[IO.File]::WriteAllText($envPath, $settingsText, [Text.UTF8Encoding]::new($false))
$config = $null; $karing = $null; $outbound = $null; $selected = $null; $settingsText = $null
Write-Host ('Own RU node configured for Avito: ' + $Node + '. Telegram route unchanged.')
if (-not $NoRestart) {
    Push-Location $appRoot
    try {
        & docker compose up -d --force-recreate avito_proxy
        if ($LASTEXITCODE -ne 0) { throw 'Avito proxy startup failed' }
        & docker compose up -d --no-deps --force-recreate parsers
        if ($LASTEXITCODE -ne 0) { throw 'Parser restart failed; settings saved' }
    } finally { Pop-Location }
}