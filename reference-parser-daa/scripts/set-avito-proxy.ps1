param([switch]$NoRestart)
$ErrorActionPreference = 'Stop'
$appRoot = Split-Path -Parent $PSScriptRoot
$envPath = Join-Path $appRoot '.env'
if (-not (Test-Path -LiteralPath $envPath)) { throw 'Application .env not found' }
$proxyHost = (Read-Host 'HTTP proxy host (without protocol)').Trim()
$proxyPort = (Read-Host 'HTTP proxy port').Trim()
$proxyLogin = Read-Host 'Proxy login (empty for IP authorization)'
$portNumber = 0
if ($proxyHost -notmatch '^[a-zA-Z0-9.-]+$' -or -not [int]::TryParse($proxyPort, [ref]$portNumber) -or $portNumber -lt 1 -or $portNumber -gt 65535) { throw 'Invalid proxy host or port' }
$credentials = ''
if ($proxyLogin) {
    $securePassword = Read-Host 'Proxy password' -AsSecureString
    $passwordPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($securePassword)
    try {
        $plainPassword = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($passwordPointer)
        $credentials = [Uri]::EscapeDataString($proxyLogin) + ':' + [Uri]::EscapeDataString($plainPassword) + '@'
    } finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($passwordPointer)
        $plainPassword = $null
    }
}
$proxyUrl = 'http://' + $credentials + $proxyHost + ':' + $portNumber
$settingsText = [IO.File]::ReadAllText($envPath)
foreach ($setting in @(@('AVITO_PROXY_URL', $proxyUrl), @('AVITO_TRANSPORT', 'http'))) {
    $pattern = '(?m)^' + $setting[0] + '=.*$'
    $replacement = $setting[0] + '=' + $setting[1]
    if ([regex]::IsMatch($settingsText, $pattern)) {
        $settingsText = [regex]::Replace($settingsText, $pattern, [System.Text.RegularExpressions.MatchEvaluator]{ param($match) $replacement })
    } else { $settingsText = $settingsText.TrimEnd() + "`r`n" + $replacement + "`r`n" }
}
[IO.File]::WriteAllText($envPath, $settingsText, [Text.UTF8Encoding]::new($false))
$proxyUrl = $null
$credentials = $null
$settingsText = $null
Write-Host 'Avito HTTP proxy saved locally. Telegram and other sources unchanged.'
if (-not $NoRestart) {
    Push-Location $appRoot
    try {
        & docker compose up -d --no-deps parsers
        if ($LASTEXITCODE -ne 0) { throw 'Docker restart failed; settings are saved' }
    } finally { Pop-Location }
}
