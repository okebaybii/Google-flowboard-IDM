$ErrorActionPreference = "Stop"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$repoRoot = Split-Path -Parent $PSScriptRoot
$toolsDir = Join-Path $repoRoot "tools"
$ffmpegDir = Join-Path $toolsDir "ffmpeg"
$zipPath = Join-Path $env:TEMP "ffmpeg-release-essentials.zip"
$url = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"

New-Item -ItemType Directory -Force -Path $toolsDir | Out-Null

Write-Host "Downloading FFmpeg..."
Invoke-WebRequest -Uri $url -OutFile $zipPath

if (Test-Path $ffmpegDir) {
    Remove-Item -Recurse -Force $ffmpegDir
}

Write-Host "Extracting FFmpeg..."
Expand-Archive -Path $zipPath -DestinationPath $toolsDir -Force

$extracted = Get-ChildItem $toolsDir -Directory |
    Where-Object { $_.Name -like "ffmpeg-*essentials_build*" } |
    Select-Object -First 1

if ($null -eq $extracted) {
    throw "Extracted FFmpeg folder not found"
}

Rename-Item -Path $extracted.FullName -NewName "ffmpeg"

$ffmpegExe = Join-Path $ffmpegDir "bin\ffmpeg.exe"
if (!(Test-Path $ffmpegExe)) {
    throw "ffmpeg.exe not found after extraction: $ffmpegExe"
}

Write-Host "FFmpeg installed: $ffmpegExe"
