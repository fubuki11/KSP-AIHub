#requires -Version 5.1
param([Parameter(Mandatory = $true)][string] $KspRoot)
$ErrorActionPreference = 'Stop'
$hub = Join-Path $KspRoot 'GameData\KSPAIHub'
if (-not (Test-Path -LiteralPath (Join-Path $hub 'Plugins\KSPAIHub.dll'))) { throw 'Install the AI Hub Mod before configuring its launcher.' }
$data = Join-Path $hub 'PluginData'
[void] [IO.Directory]::CreateDirectory($data)
$config = Join-Path $data 'hub.json'
if (-not [IO.File]::Exists($config)) { [IO.File]::Copy((Join-Path $hub 'hub.example.json'), $config, $false) }
$launcher = Join-Path $data 'launcher.json'
if ([IO.File]::Exists($launcher)) { 'Existing launcher settings were preserved.'; return }
$python = ''
foreach ($command in @(Get-Command python -CommandType Application -ErrorAction SilentlyContinue | Sort-Object @{ Expression = { $_.Source -like '*\Microsoft\WindowsApps\*' } })) {
    try {
        $text = & $command.Source -c "import sys,json; print(json.dumps({'executable':sys.executable,'supported':sys.version_info >= (3,10)}))"
        if ($LASTEXITCODE -eq 0) {
            $info = $text | ConvertFrom-Json
            if ($info.supported -and [IO.File]::Exists($info.executable)) { $python = [string] $info.executable; break }
        }
    } catch { }
}
$settings = @{ pythonExecutable = $python; configFile = $config; autoStart = $true }
[IO.File]::WriteAllText($launcher, ($settings | ConvertTo-Json), [Text.UTF8Encoding]::new($false))
"Launcher configured: $launcher"
'Set real model IDs and credentials in the Hub manager/configuration before generating.'
