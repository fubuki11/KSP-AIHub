#requires -Version 5.1
[CmdletBinding()]
param(
    [string] $KspRoot = 'D:\steam\steamapps\common\Kerbal Space Program',
    [string] $CkanPath
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$root = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$KspRoot = [IO.Path]::GetFullPath($KspRoot)
if (-not $CkanPath) { $CkanPath = Join-Path $KspRoot 'CKAN\ckan-windows.exe' }
$versionData = [IO.File]::ReadAllText((Join-Path $root 'GameData\KSPAIHub\KSPAIHub.version')) | ConvertFrom-Json
$version = '{0}.{1}.{2}' -f $versionData.VERSION.MAJOR, $versionData.VERSION.MINOR, $versionData.VERSION.PATCH
$archivePath = Join-Path $root "dist\KSPAIHub-$version.zip"
$metadataPath = Join-Path $root "dist\KSPAIHub-$version.ckan"
$repositoryPath = Join-Path $root 'dist\KSPAIHub-local-repository.zip'
$registryPath = Join-Path $KspRoot 'CKAN\registry.json'
$lockPath = Join-Path $KspRoot 'CKAN\registry.locked'
foreach ($path in @($CkanPath, $archivePath, $metadataPath, $repositoryPath)) {
    if (-not [IO.File]::Exists($path)) { throw "Required file not found: $path. Build the release before installing." }
}
if (-not [IO.Directory]::Exists((Join-Path $KspRoot 'GameData'))) { throw 'KspRoot must contain GameData.' }
if ([IO.File]::Exists($lockPath)) {
    try {
        $probe = [IO.File]::Open($lockPath, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::None)
        $probe.Dispose()
    }
    catch { throw 'CKAN is running for this instance. Exit its GUI normally, then retry; do not delete registry.locked.' }
}
foreach ($process in @(Get-Process KSP_x64 -ErrorAction SilentlyContinue)) {
    if ($process.Path -and $process.Path.StartsWith($KspRoot.TrimEnd('\', '/') + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
        throw 'Save and exit KSP before installing or upgrading AI Hub.'
    }
}

function Property-Value($Object, [string] $Name) {
    if ($null -eq $Object) { return $null }
    $property = $Object.PSObject.Properties[$Name]
    if ($null -eq $property) { return $null }
    return $property.Value
}
function Read-Registry {
    if ([IO.File]::Exists($registryPath)) { return ([IO.File]::ReadAllText($registryPath) | ConvertFrom-Json) }
    return $null
}
function Invoke-Ckan([string[]] $Arguments) {
    & $CkanPath @Arguments --gamedir $KspRoot --headless
    if ($LASTEXITCODE -ne 0) { throw "CKAN command '$($Arguments[0])' failed with exit code $LASTEXITCODE." }
}
function Hash-Stream([IO.Stream] $Stream) {
    $algorithm = [Security.Cryptography.SHA256]::Create()
    try { return [BitConverter]::ToString($algorithm.ComputeHash($Stream)).Replace('-', '') }
    finally { $algorithm.Dispose() }
}

$metadata = [IO.File]::ReadAllText($metadataPath) | ConvertFrom-Json
if ($metadata.identifier -ne 'KSPAIHub' -or $metadata.version -ne $version -or
    @($metadata.install).Count -ne 1 -or $metadata.install[0].file -ne 'GameData/KSPAIHub' -or $metadata.install[0].install_to -ne 'GameData') {
    throw 'Release metadata does not match KSPAIHub and its intended install directory.'
}
if ([string] $metadata.download -ne [Uri]::new($archivePath).AbsoluteUri) {
    throw 'Release path changed or metadata points elsewhere. Rebuild this local release at its current location.'
}
Add-Type -AssemblyName System.IO.Compression.FileSystem
$hashes = [Collections.Generic.Dictionary[string,string]]::new([StringComparer]::OrdinalIgnoreCase)
$zip = [IO.Compression.ZipFile]::OpenRead($archivePath)
try {
    foreach ($entry in $zip.Entries) {
        if ($entry.FullName -eq 'KSPAIHub.ckan') { continue }
        $name = $entry.FullName
        if (-not $name.StartsWith('GameData/KSPAIHub/', [StringComparison]::Ordinal) -or $name -match '(^|/)\.\.(/|$)|\\|:|/PluginData/') {
            throw 'Release contains an unexpected or unsafe install path.'
        }
        if ($name.EndsWith('/')) { continue }
        if ($hashes.ContainsKey($name)) { throw 'Duplicate install path in release archive.' }
        $stream = $entry.Open()
        try { $hashes.Add($name, (Hash-Stream $stream)) } finally { $stream.Dispose() }
    }
    foreach ($required in @('Plugins/KSPAIHub.dll', 'KSPAIHub.version', 'Service/ksp_aihub/__main__.py', 'hub.example.json')) {
        if (-not $hashes.ContainsKey('GameData/KSPAIHub/' + $required)) { throw "Required Hub payload is missing: $required" }
    }
}
finally { $zip.Dispose() }
$repository = [IO.Compression.ZipFile]::OpenRead($repositoryPath)
try {
    $entry = $repository.GetEntry("KSPAIHub/KSPAIHub-$version.ckan")
    if ($null -eq $entry -or $repository.Entries.Count -ne 1) { throw 'Local repository does not contain exactly the requested Hub release.' }
    $reader = [IO.StreamReader]::new($entry.Open())
    try { $repoModule = $reader.ReadToEnd() | ConvertFrom-Json } finally { $reader.Dispose() }
    if ($repoModule.identifier -ne $metadata.identifier -or $repoModule.version -ne $version -or $repoModule.download -ne $metadata.download) {
        throw 'Local repository and release metadata differ. Rebuild before installing.'
    }
}
finally { $repository.Dispose() }

function Assert-Installation {
    $registry = Read-Registry
    $installed = Property-Value (Property-Value $registry 'installed_modules') 'KSPAIHub'
    $source = Property-Value $installed 'source_module'
    if ($null -eq $installed -or (Property-Value $source 'version') -ne $version) {
        throw 'CKAN did not register the requested KSPAIHub version. A cached ZIP alone is not an installation.'
    }
    $owners = Property-Value $registry 'installed_files'
    $moduleFiles = Property-Value $installed 'installed_files'
    foreach ($relative in $hashes.Keys) {
        $path = Join-Path $KspRoot $relative
        if ((Property-Value $owners $relative) -ne 'KSPAIHub' -or $null -eq (Property-Value $moduleFiles $relative) -or -not [IO.File]::Exists($path)) {
            throw "Hub file missing or not registered to this module: $relative"
        }
        $stream = [IO.File]::OpenRead($path)
        try { $actual = Hash-Stream $stream } finally { $stream.Dispose() }
        if ($actual -ne $hashes[$relative]) { throw "Hub payload differs from the requested release: $relative. Use CKAN to repair/reinstall it." }
    }
    "Verified KSPAIHub ${version}: $($hashes.Count) registered payload files, all SHA-256 hashes match."
}

$registry = Read-Registry
$installed = Property-Value (Property-Value $registry 'installed_modules') 'KSPAIHub'
$installedVersion = Property-Value (Property-Value $installed 'source_module') 'version'
if ($installedVersion -and [version]$installedVersion -gt [version]$version) { throw 'This script will not downgrade a newer installed Hub.' }
if ($installedVersion -eq $version) { Assert-Installation }

# Ensure the module is indexed, so it appears in CKAN and can be upgraded later.
$repositoryUri = [Uri]::new($repositoryPath).AbsoluteUri
$repositories = Property-Value $registry 'sorted_repositories'
if ($null -eq $repositories) { $repositories = Property-Value $registry 'repositories' }
$existing = Property-Value $repositories 'KSPAIHub-local'
if ($existing -and (Property-Value $existing 'uri') -ne $repositoryUri) {
    throw 'KSPAIHub-local points to another location. Adjust that repository in CKAN first; it was not overwritten.'
}
if ($null -eq $existing) { Invoke-Ckan @('repo', 'add', 'KSPAIHub-local', $repositoryUri) }
Invoke-Ckan @('update', '--urls', $repositoryUri, '--game', 'KSP', '--force')
if (-not $installedVersion) {
    Invoke-Ckan @('install', "KSPAIHub=$version", '--no-recommends')
}
elseif ($installedVersion -ne $version) {
    Invoke-Ckan @('upgrade', "KSPAIHub=$version", '--no-recommends')
}
Assert-Installation
& (Join-Path $PSScriptRoot 'configure.ps1') -KspRoot $KspRoot
'AI Hub is installed and indexed. Reopen CKAN, select Installed, and search KSPAIHub.'
