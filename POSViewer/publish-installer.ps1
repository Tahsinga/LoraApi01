param(
    [string]$Version = '1.0.30'
)

$ErrorActionPreference = 'Stop'

$project = Join-Path $PSScriptRoot 'POSViewer.csproj'
$publish = Join-Path $PSScriptRoot "publish-$Version"
$iscc = $env:INNO_SETUP_COMPILER
if ([string]::IsNullOrWhiteSpace($iscc)) {
    $iscc = 'C:\Program Files (x86)\Inno Setup 6\ISCC.exe'
}
if (-not (Test-Path $iscc)) {
    $iscc = Join-Path $env:LOCALAPPDATA 'Programs\Inno Setup 6\ISCC.exe'
}

if (Test-Path $publish) {
    throw "Versioned publish directory already exists: $publish"
}

dotnet publish $project --configuration Release --runtime win-x64 --self-contained true --output $publish /p:PublishSingleFile=true /p:IncludeNativeLibrariesForSelfExtract=true
if ($LASTEXITCODE -ne 0) {
    throw "dotnet publish failed with exit code $LASTEXITCODE."
}

if (-not (Test-Path $iscc)) {
    Write-Error "Inno Setup compiler not found. Install Inno Setup 6 or set INNO_SETUP_COMPILER."
}

& $iscc "/DMyAppVersion=$Version" "/DMyAppPublishDir=..\publish-$Version" (Join-Path $PSScriptRoot 'installer\POSViewer.iss')
if ($LASTEXITCODE -ne 0) {
    throw "Inno Setup compilation failed with exit code $LASTEXITCODE."
}
Write-Output "Installer created in $PSScriptRoot\..\installer-output as LoraPOSReturns-Setup-$Version.exe"