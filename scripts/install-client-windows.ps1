<#
  One-line install for the CLIENT on Windows (PowerShell 5.1+).

  Run in PowerShell:

    iwr -useb https://raw.githubusercontent.com/hirokyserega-web/ssh-remote-desktop/main/scripts/install-client-windows.ps1 | iex

  Or:

    Invoke-Expression (Invoke-WebRequest -UseBasicParsing `
      https://raw.githubusercontent.com/hirokyserega-web/ssh-remote-desktop/main/scripts/install-client-windows.ps1).Content
#>

$ErrorActionPreference = 'Stop'

$Dir = if ($env:SSH_REMOTE_DESKTOP_DIR) { $env:SSH_REMOTE_DESKTOP_DIR } else { "$env:LOCALAPPDATA\ssh-remote-desktop" }
$Env:SSH_REMOTE_DESKTOP_DIR = $Dir

Write-Host "[+] Installing ssh-remote-desktop client into $Dir"
Write-Host "[+] OS: $([System.Environment]::OSVersion.VersionString)"

# Bootstrap Python 3.11+ if needed.
$py = (Get-Command python.exe -ErrorAction SilentlyContinue) ?? (Get-Command py.exe -ErrorAction SilentlyContinue)
if (-not $py) {
    Write-Host "[+] Python not found; downloading the official embeddable 3.12 to $Dir\python"
    New-Item -ItemType Directory -Path $Dir -Force | Out-Null
    $zip = "$Dir\python-3.12-embed.zip"
    Invoke-WebRequest -UseBasicParsing -OutFile $zip `
        "https://www.python.org/ftp/python/3.12.7/python-3.12.7-embed-amd64.zip"
    Expand-Archive -Path $zip -DestinationPath "$Dir\python" -Force
    Remove-Item $zip
    $env:PATH = "$Dir\python;$env:PATH"
    $py = "$Dir\python\python.exe"
}

# Download the universal installer (PowerShell port of the bash one).
$installer = "$Dir\install.ps1"
$env:SSH_REMOTE_DESKTOP_COMPONENT = "client"
Invoke-WebRequest -UseBasicParsing -OutFile $installer `
    "https://raw.githubusercontent.com/hirokyserega-web/ssh-remote-desktop/main/scripts/install.ps1"

& $py -m pip install --upgrade pip
& $py $installer --dev --build --dir $Dir @args

Write-Host ""
Write-Host "[+] Done. Run 'rd-client' from a shell, or 'rd-client --keygen' to generate an SSH key."
