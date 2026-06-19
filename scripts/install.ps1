<#
  Universal installer (PowerShell) for ssh-remote-desktop.

  Detects whether the current directory already is a checkout, or downloads a
  fresh tarball / clone from GitHub, sets up a Python venv, installs the
  project, optionally builds a standalone executable with PyInstaller (Nuitka
  is best-effort on Windows), and links `rd-server` / `rd-client` onto the user
  PATH.

  Flags mirror the bash installer in scripts/install.sh.
#>

param(
    [switch]$Dev,
    [switch]$Run,
    [switch]$Both,
    [switch]$Build,
    [switch]$NoBuild,
    [switch]$Force,
    [string]$Component = "",
    [string]$Dir = "",
    [string]$Python = ""
)

$ErrorActionPreference = 'Stop'

function Log($m) { Write-Host "[+] $m" }
function Warn($m) { Write-Host "[!] $m" -ForegroundColor Yellow }
function Fail($m) { Write-Host "[x] $m" -ForegroundColor Red; exit 1 }

# ---- resolve install dir ---------------------------------------------------
if (-not $Dir) {
    $Dir = if ($env:SSH_REMOTE_DESKTOP_DIR) { $env:SSH_REMOTE_DESKTOP_DIR }
           elseif ($env:LOCALAPPDATA) { Join-Path $env:LOCALAPPDATA 'ssh-remote-desktop' }
           else { Join-Path $PWD 'ssh-remote-desktop' }
}
$RepoDir = $Dir                  # the project tree (flat layout: common/, client/, server/, crypto/)
$VenvDir = Join-Path $RepoDir '.venv'
$BinDir  = Join-Path $RepoDir 'bin'

# ---- mode -------------------------------------------------------------------
$Mode = if ($Both) { 'both' } elseif ($Dev) { 'dev' } elseif ($Run) { 'run' } else { 'run' }
$WantBuild = ($Build -and -not $NoBuild) -or ($Mode -eq 'both')

# ---- fetch / clone ----------------------------------------------------------
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
            git clone https://github.com/hirokyserega-web/ssh-remote-desktop.git $RepoDir
        }
    } else {
        # Try the tagged release first; if the tag does not exist (404) or the
        # download fails for any reason, fall back to the main branch tarball so
        # the installer never aborts on a missing/pending release.
        $tag = ''
        try {
            $tag = (Invoke-WebRequest -UseBasicParsing -TimeoutSec 5 `
                    'https://raw.githubusercontent.com/hirokyserega-web/ssh-remote-desktop/main/VERSION').Content.Trim()
        } catch { $tag = '' }
        $tagUrl = if ($tag) {
            "https://codeload.github.com/hirokyserega-web/ssh-remote-desktop/tar.gz/refs/tags/v$tag"
        } else { '' }
        $mainUrl = 'https://codeload.github.com/hirokyserega-web/ssh-remote-desktop/tar.gz/refs/heads/main'
        $archive = $mainUrl
        $tmp = Join-Path $env:TEMP ("srd-" + [Guid]::NewGuid().ToString('N') + '.tar.gz')
        if ($tagUrl) {
            try {
                Log "Downloading release tarball v$tag: $tagUrl"
                Invoke-WebRequest -UseBasicParsing -OutFile $tmp $tagUrl
                $archive = $tagUrl
            } catch {
                Warn "Tag v$tag not found (404?) or download failed; falling back to main branch."
                Remove-Item $tmp -ErrorAction SilentlyContinue
                Invoke-WebRequest -UseBasicParsing -OutFile $tmp $mainUrl
                $archive = $mainUrl
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

    # Add to user PATH.
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
