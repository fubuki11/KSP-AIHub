#requires -Version 5.1
param(
    [Parameter(Mandatory = $true)][string] $KspRoot,
    [ValidateNotNullOrEmpty()][string] $DownloadUrl
)
$ErrorActionPreference = 'Stop'
if ($PSBoundParameters.ContainsKey('DownloadUrl')) {
    $publicDownload = $null
    if (-not [Uri]::IsWellFormedUriString($DownloadUrl, [UriKind]::Absolute) -or
        -not [Uri]::TryCreate($DownloadUrl, [UriKind]::Absolute, [ref] $publicDownload) -or
        $publicDownload.Scheme -ne 'https' -or -not $publicDownload.Host -or
        $publicDownload.UserInfo -or $publicDownload.Query -or $publicDownload.Fragment -or $publicDownload.Port -lt 1) {
        throw '-DownloadUrl must be an absolute HTTPS URL without credentials, query or fragment.'
    }
}
$root = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$dist = Join-Path $root 'dist'
$versionData = [IO.File]::ReadAllText((Join-Path $root 'GameData\KSPAIHub\KSPAIHub.version')) | ConvertFrom-Json
$version = '{0}.{1}.{2}' -f $versionData.VERSION.MAJOR, $versionData.VERSION.MINOR, $versionData.VERSION.PATCH
$archivePath = Join-Path $dist "KSPAIHub-$version.zip"
$metadataPath = Join-Path $dist "KSPAIHub-$version.ckan"
$repoPath = Join-Path $dist 'KSPAIHub-local-repository.zip'
& dotnet build (Join-Path $root 'src\KSPAIHub\KSPAIHub.csproj') --configuration Release "/p:KspRoot=$KspRoot"
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
Add-Type -AssemblyName System.IO.Compression
Add-Type -AssemblyName System.IO.Compression.FileSystem
[void] [IO.Directory]::CreateDirectory($dist)
$metadata = [ordered]@{
    spec_version = 1; identifier = 'KSPAIHub'; name = 'KSP AI Hub'; author = 'fubuki11st'
    abstract = 'Shared local AI gateway and model/authentication manager for compatible KSP mods.'
    license = 'MIT'; version = $version; ksp_version = '1.12.5'
    download = if ($PSBoundParameters.ContainsKey('DownloadUrl')) { $publicDownload.AbsoluteUri } else { [Uri]::new($archivePath).AbsoluteUri }
    install = @(@{ file = 'GameData/KSPAIHub'; install_to = 'GameData' })
}
$stage = Join-Path $dist ('.build-' + [Guid]::NewGuid().ToString('N'))
[void] [IO.Directory]::CreateDirectory($stage)
$stagedMetadata = Join-Path $stage 'KSPAIHub.ckan'
$stagedArchive = Join-Path $stage 'release.zip'
$stagedRepo = Join-Path $stage 'repo.zip'
function Write-Archive([string] $Path, [hashtable] $Files) {
    $zip = [IO.Compression.ZipFile]::Open($Path, [IO.Compression.ZipArchiveMode]::Create)
    try {
        [string[]] $names = @($Files.Keys); [Array]::Sort($names, [StringComparer]::Ordinal)
        foreach ($name in $names) {
            $entry = $zip.CreateEntry($name, [IO.Compression.CompressionLevel]::Optimal)
            $entry.LastWriteTime = [DateTimeOffset]::new(1980, 1, 1, 0, 0, 0, [TimeSpan]::Zero)
            $input = [IO.File]::OpenRead($Files[$name]); $output = $entry.Open()
            try { $input.CopyTo($output) } finally { $input.Dispose(); $output.Dispose() }
        }
    } finally { $zip.Dispose() }
}
try {
    [IO.File]::WriteAllText($stagedMetadata, ($metadata | ConvertTo-Json -Depth 5), [Text.UTF8Encoding]::new($false))
    $files = @{
        'KSPAIHub.ckan' = $stagedMetadata
        'GameData/KSPAIHub/Plugins/KSPAIHub.dll' = Join-Path $root 'src\KSPAIHub\bin\Release\net472\KSPAIHub.dll'
        'GameData/KSPAIHub/KSPAIHub.version' = Join-Path $root 'GameData\KSPAIHub\KSPAIHub.version'
        'GameData/KSPAIHub/README.md' = Join-Path $root 'README.md'
        'GameData/KSPAIHub/DESIGN.md' = Join-Path $root 'DESIGN.md'
        'GameData/KSPAIHub/PROTOCOL.md' = Join-Path $root 'PROTOCOL.md'
        'GameData/KSPAIHub/INSTALL.md' = Join-Path $root 'INSTALL.md'
        'GameData/KSPAIHub/MODELS.md' = Join-Path $root 'MODELS.md'
        'GameData/KSPAIHub/GENERATION.md' = Join-Path $root 'GENERATION.md'
        'GameData/KSPAIHub/LICENSE' = Join-Path $root 'LICENSE'
        'GameData/KSPAIHub/hub.example.json' = Join-Path $root 'examples\hub.example.json'
    }
    foreach ($name in @('__init__.py', '__main__.py', 'common.py', 'config.py', 'store.py', 'auth.py', 'providers.py', 'hub.py', 'server.py', 'models.py', 'presets.py')) {
        $files["GameData/KSPAIHub/Service/ksp_aihub/$name"] = Join-Path $root "service/ksp_aihub/$name"
    }
    Write-Archive $stagedArchive $files
    Write-Archive $stagedRepo @{ "KSPAIHub/KSPAIHub-$version.ckan" = $stagedMetadata }
    Move-Item -LiteralPath $stagedArchive -Destination $archivePath -Force
    Move-Item -LiteralPath $stagedMetadata -Destination $metadataPath -Force
    Move-Item -LiteralPath $stagedRepo -Destination $repoPath -Force
} finally {
    foreach ($path in @($stagedMetadata, $stagedArchive, $stagedRepo)) { [IO.File]::Delete($path) }
    [IO.Directory]::Delete($stage, $false)
}
$archivePath
$metadataPath
$repoPath
