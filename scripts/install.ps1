<#
  Universal installer (PowerShell) for ssh-remote-desktop.

  Default mode: download a prebuilt client binary (.zip) from the latest GitHub
  Release, verify its SHA256 checksum, install it and put it on the user PATH.
  Falls back to a from-source install (git clone + venv + PyInstaller) when no
  matching release asset exists or when -FromSource is passed.

  Flags mirror the bash installer in scripts/install.sh.

  Usage:
    irm https://raw.githubusercontent.com/hirokyserega-web/ssh-remote-desktop/main/scripts/install.ps1 | iex
    # with args (when run as a file):
    .\install.ps1 -Version 1.1.0
    .\install.ps1 -FromSource
    .\install.ps1 -Uninstall
#>

param(
    [switch]$Dev,
    [switch]$Run,
    [switch]$Both,
    [switch]$Build,
    [switch]$NoBuild,
    [switch]$Force,
    [switch]$FromSource,
    [switch]$Uninstall,
    [string]$Version = "",
    [string]$Component = "",
    [string]$Dir = "",
    [string]$Python = ""
)

$ErrorActionPreference = 'Stop'

$Repo = "hirokyserega-web/ssh-remote-desktop"
$RepoUrl = "https://github.com/$Repo"
$ApiUrl = "https://api.github.com/repos/$Repo"

function Log($m) { Write-Host "[+] $m" }
function Warn($m) { Write-Host "[!] $m" -ForegroundColor Yellow }
function Fail($m) { Write-Host "[x] $m" -ForegroundColor Red; exit 1 }

# ---- resolve install dir ---------------------------------------------------
if (-not $Dir) {
    $Dir = if ($env:SSH_REMOTE_DESKTOP_DIR) { $env:SSH_REMOTE_DESKTOP_DIR }
           elseif ($env:LOCALAPPDATA) { Join-Path $env:LOCALAPPDATA 'ssh-remote-desktop' }
           else { Join-Path $PWD 'ssh-remote-desktop' }
}
$RepoDir = $Dir
$VenvDir = Join-Path $RepoDir '.venv'
$BinDir  = Join-Path $RepoDir 'bin'

$Mode = if ($Both) { 'both' } elseif ($Dev) { 'dev' } elseif ($Run) { 'run' } else { 'run' }
$WantBuild = ($Build -and -not $NoBuild) -or ($Mode -eq 'both')

# ---- uninstall -------------------------------------------------------------
if ($Uninstall) {
    Log "Uninstalling ssh-remote-desktop from $RepoDir"
    if (Test-Path $RepoDir) { Remove-Item -Recurse -Force $RepoDir }
    # Remove PATH entry for BinDir if present.
    $curPath = [Environment]::GetEnvironmentVariable('Path', 'User')
    if ($curPath -and ($curPath -split ';') -contains $BinDir) {
        $newPath = ($curPath -split ';' | Where-Object { $_ -ne $BinDir }) -join ';'
        [Environment]::SetEnvironmentVariable('Path', $newPath, 'User')
        Log "Removed $BinDir from user PATH"
    }
    Log "Uninstall complete."
    exit 0
}

# ---- helpers ---------------------------------------------------------------
function Get-AssetUrl($asset, $version) {
    $direct = if ($version) { "$RepoUrl/releases/download/v$version/$asset" }
              else { "$RepoUrl/releases/latest/download/$asset" }
    try {
        $req = [System.Net.HttpWebRequest]::Create($direct)
        $req.Method = 'HEAD'
        $req.AllowAutoRedirect = $true
        $req.Timeout = 8000
        $resp = $req.GetResponse()
        $resp.Close()
        return $direct
    } catch {}
    # API fallback: list assets for the latest (or tagged) release.
    $apiPath = if ($version) { "releases/tags/v$version" } else { "releases/latest" }
    try {
        $rel = Invoke-RestMethod -UseBasicParsing -TimeoutSec 10 "$ApiUrl/$apiPath"
        $found = $rel.assets | Where-Object { $_.name -eq $asset } | Select-Object -First 1
        if ($found) { return $found.browser_download_url }
    } catch {}
    return $null
}

function Get-ExpectedSha256($asset, $version) {
    $sumsUrl = if ($version) { "$RepoUrl/releases/download/v$version/SHA256SUMS" }
               else { "$RepoUrl/releases/latest/download/SHA256SUMS" }
    try {
        $sums = (Invoke-WebRequest -UseBasicParsing -TimeoutSec 10 $sumsUrl).Content
        foreach ($line in ($sums -split "`n")) {
            $parts = $line -split '\s+', 2
            if ($parts.Count -eq 2 -and $parts[1].Trim() -eq $asset) {
                return $parts[0].Trim().ToLower()
            }
        }
    } catch {}
    return $null
}

# ---- try prebuilt binary ---------------------------------------------------
function Try-InstallBinary {
    $comp = if ($Component) { $Component }
            elseif ($env:SSH_REMOTE_DESKTOP_COMPONENT) { $env:SSH_REMOTE_DESKTOP_COMPONENT }
            else { 'client' }
    $arch = 'x86_64'
    $asset = "ssh-remote-desktop-$comp-windows-$arch.zip"
    Log "Looking for release asset: $asset"
    $url = Get-AssetUrl $asset $Version
    if (-not $url) { Warn "No release asset $asset; will install from source."; return $false }

    $tmp = Join-Path $env:TEMP ("srd-" + [Guid]::NewGuid().ToString('N') + '.zip')
    Log "Downloading: $url"
    try {
        Invoke-WebRequest -UseBasicParsing -OutFile $tmp $url
    } catch { Warn "Download failed for $asset; will install from source."; return $false }

    # Verify SHA256.
    $expected = Get-ExpectedSha256 $asset $Version
    if ($expected) {
        $actual = (Get-FileHash -Algorithm SHA256 $tmp).Hash.ToLower()
        if ($actual -ne $expected) {
            Fail "SHA256 mismatch for $asset (expected $expected, got $actual)"
        }
        Log "Checksum OK: $asset"
    } else {
        Warn "No SHA256 entry for $asset; skipping verification."
    }

    New-Item -ItemType Directory -Path $BinDir -Force | Out-Null
    Expand-Archive -Path $tmp -DestinationPath $BinDir -Force
    Remove-Item $tmp -ErrorAction SilentlyContinue

    # Put BinDir on the user PATH.
    $curPath = [Environment]::GetEnvironmentVariable('Path', 'User')
    if (-not ($curPath -and ($curPath -split ';') -contains $BinDir)) {
        Log "Adding $BinDir to user PATH"
        [Environment]::SetEnvironmentVariable('Path', "$BinDir;$curPath", 'User')
        $env:Path = "$BinDir;$env:Path"
    }
    Log "Installed prebuilt binary: rd-$comp.exe"
    return $true
}

# ---- main flow -------------------------------------------------------------
if ($Mode -eq 'run' -and -not $FromSource) {
    if (Try-InstallBinary) {
        Log "Done. Run 'rd-client' (open a new PowerShell so PATH reloads)."
        exit 0
    }
    Warn "Falling back to from-source install."
}

# ---- from-source path ------------------------------------------------------
if (Test-Path (Join-Path $PWD 'pyproject.toml')) {
    $RepoDir = $PWD
    Log "Using current directory as source: $RepoDir"
} else {
    if ($Mode -eq 'dev' -or $Mode -eq 'both') {
        if (Test-Path (Join-Path $RepoDir '.git')) {
            Log "Pulling latest in $RepoDir"
            git -C $RepoDir pull --ff-only
        } else {
            Log "Cloning repo into $RepoDir"
            New-Item -ItemType Directory -Path $RepoDir -Force | Out-Null
            git clone $RepoUrl.git $RepoDir
        }
    } else {
        $tag = ''
        if ($Version) { $tag = $Version } else {
            try {
                $tag = (Invoke-WebRequest -UseBasicParsing -TimeoutSec 5 `
                        "$RepoUrl/raw/main/VERSION").Content.Trim()
            } catch { $tag = '' }
        }
        $tagUrl = if ($tag) { "$RepoUrl/archive/refs/tags/v$tag.tar.gz" } else { '' }
        $mainUrl = "$RepoUrl/archive/refs/heads/main.tar.gz"
        $tmp = Join-Path $env:TEMP ("srd-" + [Guid]::NewGuid().ToString('N') + '.tar.gz')
        if ($tagUrl) {
            try {
                Log "Downloading source tarball v$tag"
                Invoke-WebRequest -UseBasicParsing -OutFile $tmp $tagUrl
            } catch {
                Warn "Tag v$tag not found; falling back to main branch."
                Remove-Item $tmp -ErrorAction SilentlyContinue
                Invoke-WebRequest -UseBasicParsing -OutFile $tmp $mainUrl
            }
        } else {
            Log "No VERSION tag found; downloading main branch tarball."
            Invoke-WebRequest -UseBasicParsing -OutFile $tmp $mainUrl
        }
        New-Item -ItemType Directory -Path $RepoDir -Force | Out-Null
        tar -xzf $tmp -C $RepoDir --strip-components=1
        Remove-Item $tmp
    }
}

Set-Location $RepoDir

# ---- Python -----------------------------------------------------------------
$py = if ($Python) { Get-Command $Python -ErrorAction SilentlyContinue }
      elseif (Get-Command python.exe -ErrorAction SilentlyContinue) { Get-Command python.exe }
      elseif (Get-Command py.exe -ErrorAction SilentlyContinue)     { Get-Command py.exe }
      else { $null }
if (-not $py) {
    Log "Python not found on PATH; downloading official embeddable 3.12 into $RepoDir\python"
    New-Item -ItemType Directory -Path (Join-Path $RepoDir 'python') -Force | Out-Null
    $zip = Join-Path $RepoDir 'python-embed.zip'
    Invoke-WebRequest -UseBasicParsing -OutFile $zip `
        'https://www.python.org/ftp/python/3.12.7/python-3.12.7-embed-amd64.zip'
    Expand-Archive -Path $zip -DestinationPath (Join-Path $RepoDir 'python') -Force
    Remove-Item $zip
    $env:PATH = "$(Join-Path $RepoDir 'python');$env:PATH"
    $py = Join-Path $RepoDir 'python\python.exe'
}
Log "Using Python: $(& $py --version 2>&1) ($py)"

# ---- venv + deps ------------------------------------------------------------
if (-not (Test-Path $VenvDir)) {
    Log "Creating venv at $VenvDir"
    & $py -m venv $VenvDir
}
$venvPy = Join-Path $VenvDir 'Scripts\python.exe'
& $venvPy -m pip install --upgrade pip wheel setuptools | Out-Null
Log "Installing Python requirements"
& $venvPy -m pip install -r (Join-Path $RepoDir 'requirements.txt') | Out-Null
if ($Mode -eq 'dev' -or $Mode -eq 'both') {
    Log "Editable install"
    & $venvPy -m pip install -e $RepoDir | Out-Null
} else {
    & $venvPy -m pip install $RepoDir | Out-Null
}

# ---- optional binary build --------------------------------------------------
if ($WantBuild) {
    Log "Installing PyInstaller"
    & $venvPy -m pip install --upgrade pyinstaller | Out-Null
    New-Item -ItemType Directory -Path $BinDir -Force | Out-Null
    $entry = if ($Component) { $Component }
             elseif ($env:SSH_REMOTE_DESKTOP_COMPONENT) { $env:SSH_REMOTE_DESKTOP_COMPONENT }
             else { 'client' }
    $entryPy = if ($entry -eq 'server') { 'server\__main__.py' } else { 'client\__main__.py' }
    $exeName = "rd-$entry"
    Log "Building $exeName.exe with PyInstaller"
    & $venvPy -m PyInstaller --noconfirm --onefile --name $exeName $entryPy
    Move-Item -Force "dist\$exeName.exe" (Join-Path $BinDir "$exeName.exe")
    Get-ChildItem -Recurse -Filter '__pycache__' -ErrorAction SilentlyContinue | Remove-Item -Recurse -Force
    Get-ChildItem -Recurse -Filter '*.spec' -ErrorAction SilentlyContinue | Remove-Item -Force
    Remove-Item -Recurse -Force build,dist -ErrorAction SilentlyContinue

    $curPath = [Environment]::GetEnvironmentVariable('Path', 'User')
    if ($curPath -notlike '*' + [regex]::Escape($BinDir) + '*') {
        Log "Adding $BinDir to user PATH"
        [Environment]::SetEnvironmentVariable('Path', "$BinDir;$curPath", 'User')
        $env:Path = "$BinDir;$env:Path"
    }
}

# ---- verify -----------------------------------------------------------------
Log "Verifying installation"
if ($WantBuild) {
    $entry = if ($Component) { $Component }
             elseif ($env:SSH_REMOTE_DESKTOP_COMPONENT) { $env:SSH_REMOTE_DESKTOP_COMPONENT }
             else { 'client' }
    & (Join-Path $BinDir "rd-$entry.exe") --help
} else {
    & $venvPy -c "import client, server, common, crypto; print('imports ok')"
}

Log "Done."
Log "Run 'rd-client' or 'rd-server' (open a new PowerShell first so PATH is reloaded)."
