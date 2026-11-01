[CmdletBinding()]
param(
    [switch]$Chat,
    [switch]$NoVoice,
    [switch]$Voice,
    [switch]$Smoke,
    [double]$ExitAfter = 0,
    [string]$Screenshot,
    [switch]$Console
)

$ErrorActionPreference = 'Stop'
$projectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$localPython = Join-Path $projectRoot '.venv/Scripts/python.exe'
$pythonPath = if (Test-Path -LiteralPath $localPython) { $localPython } else { (Get-Command python -ErrorAction Stop).Source }
if (-not $Console) {
    $pythonWindowlessPath = Join-Path (Split-Path -Parent $pythonPath) 'pythonw.exe'
    if (Test-Path -LiteralPath $pythonWindowlessPath) {
        $pythonPath = $pythonWindowlessPath
    }
}

$petArguments = @('-m', 'desktop_pet')
if ($Chat) { $petArguments += '--chat' }
if ($NoVoice -or -not $Voice) { $petArguments += '--no-voice' }
if ($Smoke) { $petArguments += '--smoke' }
if ($ExitAfter -gt 0) { $petArguments += @('--exit-after', $ExitAfter.ToString([Globalization.CultureInfo]::InvariantCulture)) }
if ($Screenshot) {
    $screenshotPath = [IO.Path]::GetFullPath($Screenshot)
    $petArguments += @('--screenshot', $screenshotPath)
}

# Start-Process on Windows joins ArgumentList; preserve paths that contain spaces.
if ($Console) {
    Push-Location -LiteralPath $projectRoot
    try { & $pythonPath @petArguments; exit $LASTEXITCODE }
    finally { Pop-Location }
}
$quotedArguments = $petArguments | ForEach-Object { '"' + $_.Replace('"', '\"') + '"' }
$process = Start-Process -FilePath $pythonPath -ArgumentList $quotedArguments -WorkingDirectory $projectRoot -WindowStyle Hidden -PassThru
Write-Output ('Nino desktop pet started (PID {0}). Click Nino to chat; Ctrl+Alt+N restores mouse input.' -f $process.Id)
