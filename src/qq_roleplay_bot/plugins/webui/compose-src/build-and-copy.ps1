# Build the wasm bundle and drop it into the panel's static tree (`webui/v2/`).
#
# One command:  .\build-and-copy.ps1
# Options:      -SkipBuild   copy whatever is already in build\dist (fast re-copy)
#               -Clean       empty v2\ first (default: replace files, keep anything else)
#
# Guard rails, because this script writes inside the *shipped* panel package:
#   * it only ever touches `webui/v2/` — the four legacy files (`index.html`, `app.js`,
#     `app.css`, `__init__.py`) are hashed before and after the copy and the script FAILS if any of
#     them changed;
#   * it copies, never moves, and never deletes outside `v2/`;
#   * `compose-src/build/` is already in `compose-src/.gitignore`, so build output stays out of git.
#     The files this script lands in `v2/` are deployment artifacts — they are untracked on purpose.
#
# Not done here (needs a backend change, deliberately out of scope this round): the panel only
# serves four whitelisted static names, so `/v2/` is not routable yet. See NOTES.md §部署.

[CmdletBinding()]
param(
    [switch]$SkipBuild,
    [switch]$Clean
)

$ErrorActionPreference = 'Stop'

$compose = Split-Path -Parent $MyInvocation.MyCommand.Path   # .../plugins/webui/compose-src
# 2026-10-20：源码从仓库根的 `webui-compose/` 搬进了插件包，所以离仓库根远了五级
# （compose-src -> webui -> plugins -> qq_roleplay_bot -> src -> yunru-stage4）。
$repo = [System.IO.Path]::GetFullPath((Join-Path $compose '..\..\..\..\..'))
$dist = Join-Path $compose 'build\dist\wasmJs\productionExecutable'
$target = Join-Path $repo 'src\qq_roleplay_bot\plugins\webui\webui\v2'
$legacy = Join-Path $repo 'src\qq_roleplay_bot\plugins\webui\webui'

# The four files the panel actually serves today. `v2/` must coexist with them untouched.
$legacyNames = @('index.html', 'app.js', 'app.css', '__init__.py')

Write-Host "==> repo:   $repo"
Write-Host "==> dist:   $dist"
Write-Host "==> target: $target"

if (-not $SkipBuild) {
    Write-Host '==> gradlew wasmJsBrowserDistribution'
    Push-Location $compose
    try {
        & .\gradlew.bat --console=plain wasmJsBrowserDistribution
        if ($LASTEXITCODE -ne 0) { throw "gradle build failed with exit code $LASTEXITCODE" }
    } finally {
        Pop-Location
    }
}

if (-not (Test-Path $dist)) { throw "distribution not found: $dist" }

$before = @{}
foreach ($name in $legacyNames) {
    $path = Join-Path $legacy $name
    if (Test-Path $path) { $before[$name] = (Get-FileHash $path -Algorithm SHA256).Hash }
}

if ($Clean -and (Test-Path $target)) {
    Write-Host "==> cleaning $target"
    Get-ChildItem $target -Force | Remove-Item -Recurse -Force
}
New-Item -ItemType Directory -Force -Path $target | Out-Null

# Copy the tree (not just *.wasm): index.html, composeApp.js, both .wasm files and fonts/.
# `*.map` is skipped on purpose: it is a debugging artifact that is never requested by the browser
# (838 KB of dead weight inside a shipped Python package).
$copied = 0
Get-ChildItem $dist -Recurse -File | Where-Object { $_.Extension -ne '.map' } | ForEach-Object {
    $rel = $_.FullName.Substring($dist.Length + 1)
    $dest = Join-Path $target $rel
    $destDir = Split-Path -Parent $dest
    if (-not (Test-Path $destDir)) { New-Item -ItemType Directory -Force -Path $destDir | Out-Null }
    Copy-Item -LiteralPath $_.FullName -Destination $dest -Force
    $copied++
}

$after = @{}
foreach ($name in $legacyNames) {
    $path = Join-Path $legacy $name
    if (Test-Path $path) { $after[$name] = (Get-FileHash $path -Algorithm SHA256).Hash }
}
foreach ($name in $before.Keys) {
    if ($before[$name] -ne $after[$name]) {
        throw "legacy file changed, which must never happen: $name"
    }
}

$total = (Get-ChildItem $target -Recurse -File | Measure-Object Length -Sum).Sum
Write-Host "==> copied $copied files, $([math]::Round($total / 1MB, 2)) MiB into v2/"
Write-Host '==> legacy files verified unchanged: ' + ($legacyNames -join ', ')
Write-Host '==> NOTE: the panel does not route /v2/ yet (backend change pending). See NOTES.md.'
