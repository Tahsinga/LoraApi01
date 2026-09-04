$ErrorActionPreference = 'Stop'

$project = Join-Path $PSScriptRoot 'POSViewer.csproj'
$publish = Join-Path $PSScriptRoot 'publish'
$iscc = $env:INNO_SETUP_COMPILER
if ([string]::IsNullOrWhiteSpace($iscc)) {
    $iscc = 'C:\Program Files (x86)\Inno Setup 6\ISCC.exe'
}
if (-not (Test-Path $iscc)) {
    $iscc = Join-Path $env:LOCALAPPDATA 'Programs\Inno Setup 6\ISCC.exe'
}

if (Test-Path $publish) {
    Remove-Item $publish -Recurse -Force
}

dotnet publish $project --configuration Release --runtime win-x64 --self-contained true --output $publish /p:PublishSingleFile=true /p:IncludeNativeLibrariesForSelfExtract=true

if (-not (Test-Path $iscc)) {
    Write-Error "Inno Setup compiler not found. Install Inno Setup 6 or set INNO_SETUP_COMPILER."
}

& $iscc (Join-Path $PSScriptRoot 'installer\POSViewer.iss')
Write-Output "Installer created in $PSScriptRoot\..\installer-output"