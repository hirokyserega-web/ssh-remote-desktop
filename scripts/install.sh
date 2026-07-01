#!/usr/bin/env bash
# Universal installer for ssh-remote-desktop.
#
# Default mode (--run): download a prebuilt binary from the latest GitHub
# Release for this platform, verify its SHA256 checksum, install it and symlink
# it into ~/.local/bin. Falls back to building from source when no matching
# release asset exists (or with --from-source).
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/hirokyserega-web/ssh-remote-desktop/main/scripts/install.sh | bash
#   ... | bash -s -- --version 1.1.0
#   ... | bash -s -- --from-source
#   ... | bash -s -- --dev
set -euo pipefail

REPO="hirokyserega-web/ssh-remote-desktop"
REPO_URL="https://github.com/${REPO}"
REPO_RAW="https://raw.githubusercontent.com/${REPO}/main"
API_URL="https://api.github.com/repos/${REPO}"
TARGET_DIR="${SSH_REMOTE_DESKTOP_DIR:-$HOME/.local/share/ssh-remote-desktop}"
MODE="run"      # run | dev | both
BUILD="auto"    # auto | yes | no
PYTHON_BIN=""
UNINSTALL="no"
DIAGNOSE="no"
VERSION=""      # specific release version (X.Y.Z), empty = latest
FROM_SOURCE="no"
COMPONENT=""    # client | server | both; empty = auto (both on linux, client elsewhere)
WITH_WM="${RD_WITH_WM:-}"        # --with-wm=NAME (or RD_WITH_WM env); empty = not requested
WITH_WM_REQUESTED="no"           # becomes "yes" once --with-wm is seen in any form
if [[ -n "$WITH_WM" ]]; then WITH_WM_REQUESTED="yes"; fi
SERVER_WM_WARN="no"             # set to "yes" by install_session_defaults when server has no WM

usage() {
  cat <<USAGE
ssh-remote-desktop installer

Usage: install.sh [options]
  --dev            Development checkout (git clone, editable pip install)
  --run            Install a stable release (default)
  --both           Dev checkout AND try to build a binary
  --no-build       Skip compiling binaries, just install Python deps
  --build          Force building binaries with Nuitka
  --dir PATH       Install into a different directory
  --python BIN     Use a specific Python interpreter
  --version X.Y.Z  Install a specific release version (binary or source tag)
  --from-source    Skip release binaries; always build from source
  --component C    client | server | server-gui | both (default: both on Linux, client elsewhere; "server" on Linux = daemon + control panel)
  --with-wm=NAME   Server only (Linux): pre-install a window manager and generate /etc/ssh-remote-desktop/server.toml. NAME: openbox|plasma|xfce|xterm (or a raw package name). Bare --with-wm defaults to openbox. Existing server.toml is never overwritten.
  --uninstall      Remove the install (binary, venv, sources, symlinks, empty config)
  --diagnose       Print diagnostics (PATH, Qt platform, system libs) and exit
  --doctor         Alias for --diagnose
  -h, --help       Show this help

Environment:
  SSH_REMOTE_DESKTOP_DIR  default install directory
  RD_WITH_WM              same as --with-wm=NAME
USAGE
}

log() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33mWARN:\033[0m %s\n' "$*" >&2; }
err() { printf '\033[1;31mERR:\033[0m %s\n' "$*" >&2; }

# ---- argument parsing ------------------------------------------------------
# Resolve script dir; fall back to CWD when piped via curl|bash
# (BASH_SOURCE[0] is empty there, which under `set -u` is fatal).
if [[ -n "${BASH_SOURCE[0]:-}" ]]; then
  HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
else
  HERE="$(pwd)"
fi
while [[ $# -gt 0 ]]; do
  case "$1" in
    --dev) MODE="dev"; shift;;
    --run) MODE="run"; shift;;
    --both) MODE="both"; shift;;
    --no-build) BUILD="no"; shift;;
    --build) BUILD="yes"; shift;;
    --dir) TARGET_DIR="$2"; shift 2;;
    --python) PYTHON_BIN="$2"; shift 2;;
    --version)
      if [[ $# -ge 2 && "${2#-}" == "$2" ]]; then
        VERSION="$2"; shift 2
      else
        printf '%s\n' "$(cat "$HERE/../VERSION" 2>/dev/null || echo unknown)"
        exit 0
      fi
      ;;
    --from-source) FROM_SOURCE="yes"; shift;;
    --component) COMPONENT="$2"; shift 2;;
    --with-wm)
      WITH_WM_REQUESTED="yes"
      if [[ $# -ge 2 && "${2#-}" == "$2" ]]; then
        WITH_WM="$2"; shift 2
      else
        WITH_WM="openbox"; shift
      fi
      ;;
    --with-wm=*)
      WITH_WM_REQUESTED="yes"
      WITH_WM="${1#*=}"
      if [[ -z "$WITH_WM" ]]; then WITH_WM="openbox"; fi
      shift
      ;;
    --uninstall) UNINSTALL="yes"; shift;;
    --diagnose|--doctor) DIAGNOSE="yes"; shift;;
    -h|--help) usage; exit 0;;
    *) err "unknown argument: $1"; usage; exit 1;;
  esac
done

# ---- platform detection ----------------------------------------------------
detect_os() {
  case "$(uname -s)" in
    Linux)  OS=linux;;
    Darwin) OS=macos;;
    MINGW*|MSYS*|CYGWIN*) OS=windows;;
    *) err "Unsupported OS: $(uname -s)"; exit 1;;
  esac
}

detect_arch() {
  ARCH="$(uname -m)"
}

detect_distro() {
  DISTRO="unknown"
  DISTRO_LIKE=""
  if [[ -f /etc/os-release ]]; then
    # shellcheck disable=SC1091
    . /etc/os-release
    DISTRO="${ID:-unknown}"
    DISTRO_LIKE="${ID_LIKE:-}"
  fi
  # Map Arch/Debian/RHEL/SUSE derivatives that aren't a family we dispatch on
  # directly onto their parent family via ID_LIKE, so Garuda / ArcoLinux /
  # CachyOS / … get the Arch package set instead of falling into the
  # "install manually" branch. Known exact IDs keep their own value.
  case "$DISTRO" in
    ubuntu|debian|linuxmint|pop|\
    fedora|rhel|centos|rocky|almalinux|\
    arch|manjaro|endeavouros|garuda|arcolinux|\
    opensuse*|sles|alpine)
      ;;
    *)
      for like in $DISTRO_LIKE; do
        case "$like" in
          debian|ubuntu|linuxmint|pop) DISTRO="ubuntu"; break;;
          rhel|fedora|centos|rocky|almalinux) DISTRO="fedora"; break;;
          arch|manjaro) DISTRO="arch"; break;;
          suse|opensuse|sles) DISTRO="opensuse"; break;;
          alpine) DISTRO="alpine"; break;;
        esac
      done
      ;;
  esac
}

need_cmd() { command -v "$1" >/dev/null 2>&1; }

default_components() {
  if [[ -n "$COMPONENT" ]]; then
    echo "$COMPONENT"
    return
  fi
  case "$OS" in
    linux) echo "both";;
    *) echo "client";;
  esac
}

# Expand "both" into the concrete list for this platform.
expand_components() {
  local sel; sel="$1"
  case "$sel" in
    both)
      case "$OS" in
        linux) echo "client server server-gui";;
        *) echo "client";;
      esac
      ;;
    server)
      # "server" on Linux means the daemon AND its control panel: an operator
      # installing the server expects rd-server-gui in the menu too, not just
      # the headless rd-server binary. (server-gui ships no non-Linux asset, so
      # it only expands here.)
      case "$OS" in
        linux) echo "server server-gui";;
        *) echo "server";;
      esac
      ;;
    *) echo "$sel";;
  esac
}

asset_name() {
  # $1 = component (client|server|server-gui). Generic: any component maps to
  # ssh-remote-desktop-${comp}-${OS}-${ARCH}.${ext}, so server-gui yields
  # ssh-remote-desktop-server-gui-linux-x86_64.tar.gz with no special casing.
  local comp="$1" ext
  case "$OS" in
    windows) ext="zip";;
    *) ext="tar.gz";;
  esac
  echo "ssh-remote-desktop-${comp}-${OS}-${ARCH}.${ext}"
}

# ---- package manager helpers (source path) ---------------------------------
pkg_install() {
  local pkgs=("$@")
  case "$DISTRO" in
    ubuntu|debian|linuxmint|pop)
      sudo -n apt-get update
      sudo -n apt-get install -y "${pkgs[@]}"
      ;;
    fedora|rhel|centos|rocky|almalinux)
      sudo -n dnf install -y "${pkgs[@]}"
      ;;
    arch|manjaro|endeavouros|garuda|arcolinux)
      sudo -n pacman -S --noconfirm --needed "${pkgs[@]}"
      ;;
    opensuse*|sles)
      sudo -n zypper install -y "${pkgs[@]}"
      ;;
    alpine)
      sudo -n apk add "${pkgs[@]}"
      ;;
    *)
      err "Unknown distro ($DISTRO). Install manually: ${pkgs[*]}"
      return 1
      ;;
  esac
}

ensure_sudo() {
  if ! sudo -n true 2>/dev/null; then
    # No cached/passwordless sudo — prompt once to validate and cache
    # credentials so subsequent `sudo -n` calls in pkg_install succeed.
    if ! sudo -v 2>/dev/null; then
      err "This installer needs sudo to install system packages. Re-run as a sudoer, or install manually."
      exit 1
    fi
  fi
}

# ---- release asset download ------------------------------------------------
# Resolve the download URL for an asset. Tries the direct "latest/download"
# (or "download/vX.Y.Z") path first, then the GitHub API. Prints the URL on
# stdout (empty on failure).
resolve_asset_url() {
  local asset="$1" version="$2"
  local direct=""
  if [[ -n "$version" ]]; then
    direct="$REPO_URL/releases/download/v${version}/${asset}"
  else
    direct="$REPO_URL/releases/latest/download/${asset}"
  fi
  if curl -fsSI "$direct" -o /dev/null 2>/dev/null; then
    echo "$direct"
    return 0
  fi
  # API fallback: list assets for the latest (or tagged) release.
  local api_path="releases/latest"
  [[ -n "$version" ]] && api_path="releases/tags/v${version}"
  local url
  url=$(curl -fsSL "${API_URL}/${api_path}" 2>/dev/null \
        | grep -o "\"browser_download_url\": *\"[^\"]*${asset}\"" \
        | head -1 | sed -E 's/.*"browser_download_url": *"([^"]*)".*/\1/')
  echo "${url:-}"
}

# Download $1 (url) into $2 (dest path).
download_to() {
  local url="$1" dest="$2"
  if ! curl -fsSL "$url" -o "$dest" 2>/dev/null; then
    return 1
  fi
}

# Verify the SHA256 of $1 (archive path) against the release SHA256SUMS.
# Downloads SHA256SUMS from the same release and greps the matching line.
# $3 (optional) = the release asset name to look up in SHA256SUMS. When the
# archive was downloaded into an mktemp path, basename($1) is a random string
# (e.g. srd-1rrF5n) that never appears in SHA256SUMS, so callers should pass
# the real asset name here.
verify_sha256() {
  local archive="$1" version="$2" expected_name="${3:-}" base sums_url sums_file expected actual
  if [[ -n "$expected_name" ]]; then
    base="$expected_name"
  else
    base="$(basename "$archive")"
  fi
  if [[ -n "$version" ]]; then
    sums_url="$REPO_URL/releases/download/v${version}/SHA256SUMS"
  else
    sums_url="$REPO_URL/releases/latest/download/SHA256SUMS"
  fi
  sums_file="$(mktemp -t srd-sums-XXXXXX)"
  if ! download_to "$sums_url" "$sums_file"; then
    rm -f "$sums_file"
    warn "SHA256SUMS unavailable from release; skipping checksum verification."
    return 0
  fi
  # Match the basename at end of line, accepting either a space (bare name,
  # e.g. "<hash>  ssh-remote-desktop-...tar.gz") or a slash (prefixed path,
  # e.g. "<hash>  staging/ssh-remote-desktop-...tar.gz") before it. The Release
  # pipeline historically wrote SHA256SUMS with a "staging/" prefix; without
  # accepting the slash, verification silently no-matched and was skipped.
  expected=$(grep -E "[ /]${base}$" "$sums_file" | awk '{print $1}' | head -1)
  rm -f "$sums_file"
  if [[ -z "$expected" ]]; then
    warn "No SHA256 entry for $base in SHA256SUMS; skipping verification."
    return 0
  fi
  if ! need_cmd sha256sum; then
    if need_cmd shasum; then
      actual=$(shasum -a 256 "$archive" | awk '{print $1}')
    else
      warn "Neither sha256sum nor shasum available; skipping verification."
      return 0
    fi
  else
    actual=$(sha256sum "$archive" | awk '{print $1}')
  fi
  if [[ "$expected" != "$actual" ]]; then
    err "SHA256 mismatch for $base (expected $expected, got $actual)"
    return 1
  fi
  log "Checksum OK: $base"
  return 0
}

# Try to install prebuilt binaries for the requested components.
# Returns 0 only if ALL requested components got a binary; 1 otherwise (so the
# caller can fall back to source for the missing ones).
install_release_binaries() {
  local comps sel comp asset url archive bindir
  sel="$(default_components)"
  comps="$(expand_components "$sel")"
  bindir="$TARGET_DIR/bin"
  mkdir -p "$bindir"
  local missing=0
  for comp in $comps; do
    asset="$(asset_name "$comp")"
    log "Looking for release asset: $asset"
    url="$(resolve_asset_url "$asset" "$VERSION")"
    if [[ -z "$url" ]]; then
      warn "No release asset for $comp ($asset). Will build from source."
      missing=1
      continue
    fi
    archive="$(mktemp -t srd-XXXXXX)"
    log "Downloading: $url"
    if ! download_to "$url" "$archive"; then
      warn "Download failed for $asset. Will build from source."
      rm -f "$archive"
      missing=1
      continue
    fi
    if ! verify_sha256 "$archive" "$VERSION" "$asset"; then
      rm -f "$archive"
      missing=1
      continue
    fi
    # Extract into bindir.
    case "$OS" in
      windows)
        # Windows extraction is handled by install.ps1; this branch is a no-op
        # safety net when run under MSYS/Cygwin bash.
        (cd "$bindir" && unzip -o "$archive") >/dev/null 2>&1 || tar -C "$bindir" -xzf "$archive"
        ;;
      *)
        tar -C "$bindir" -xzf "$archive"
        ;;
    esac
    chmod +x "$bindir/rd-${comp}" 2>/dev/null || true
    rm -f "$archive"
    log "Installed prebuilt binary: rd-${comp}"
  done
  return "$missing"
}

# ---- source retrieval (fallback) -------------------------------------------
fetch_release_tarball() {
  log "Downloading ssh-remote-desktop source tarball…"
  mkdir -p "$TARGET_DIR"
  local tarball
  tarball=$(mktemp -t srd-XXXXXX.tar.gz)
  local url=""
  if [[ -n "$VERSION" ]]; then
    local tag_url="$REPO_URL/archive/refs/tags/v${VERSION}.tar.gz"
    if curl -fsSI "$tag_url" -o /dev/null 2>/dev/null; then
      url="$tag_url"
    else
      warn "Release tag v${VERSION} not found; falling back to main branch."
    fi
  else
    local version
    version=$(curl -fsSL "$REPO_RAW/VERSION" 2>/dev/null | tr -d '[:space:]' || true)
    if [[ -n "$version" ]]; then
      local tag_url="$REPO_URL/archive/refs/tags/v${version}.tar.gz"
      if curl -fsSI "$tag_url" -o /dev/null 2>/dev/null; then
        url="$tag_url"
      else
        log "Release tag v${version} not found; using main branch tarball."
      fi
    fi
  fi
  [[ -z "$url" ]] && url="$REPO_URL/archive/refs/heads/main.tar.gz"
  log "Fetching: $url"
  if ! curl -fsSL "$url" -o "$tarball"; then
    err "Failed to download sources from $url"
    rm -f "$tarball"
    exit 1
  fi
  tar -xzf "$tarball" -C "$TARGET_DIR" --strip-components=1
  rm -f "$tarball"
}

clone_dev() {
  if [[ -d "$TARGET_DIR/.git" ]]; then
    log "Existing checkout in $TARGET_DIR — pulling latest."
    git -C "$TARGET_DIR" pull --ff-only
  else
    log "Cloning $REPO_URL into $TARGET_DIR…"
    mkdir -p "$(dirname "$TARGET_DIR")"
    git clone "$REPO_URL.git" "$TARGET_DIR"
  fi
}

# ---- venv + python deps (source path) --------------------------------------
find_python() {
  if [[ -n "$PYTHON_BIN" ]]; then
    command -v "$PYTHON_BIN" || { err "Python '$PYTHON_BIN' not found"; exit 1; }
    return
  fi
  for cand in python3.13 python3.12 python3.11 python3; do
    if command -v "$cand" >/dev/null 2>&1; then
      command -v "$cand"
      return
    fi
  done
  err "Python 3.11+ is required but not found."
  exit 1
}

setup_venv() {
  local py; py=$(find_python)
  log "Using Python: $($py --version 2>&1) ($py)"
  if [[ -x "$TARGET_DIR/.venv/bin/python" ]]; then
    log "Reusing existing venv at $TARGET_DIR/.venv"
  else
    log "Creating virtual environment…"
    "$py" -m venv "$TARGET_DIR/.venv"
  fi
  # shellcheck disable=SC1091
  source "$TARGET_DIR/.venv/bin/activate"
  pip install --upgrade pip wheel setuptools
  log "Installing Python dependencies…"
  pip install -r "$TARGET_DIR/requirements.txt"
  # Linux-only runtime deps (PAM/evdev/Wayland/D-Bus). requirements-linux.txt
  # documents that "install.sh sources it on Linux hosts"; without python-pam
  # (+ its undeclared `six` dep) the source-built/installed rd-server has no
  # PAM and rejects every password. evdev/pywayland/dbus-next are lazily
  # imported, so installing them does not change Nuitka's bundling.
  if [[ "$OS" == "linux" ]]; then
    pip install -r "$TARGET_DIR/requirements-linux.txt" || warn "Some Linux-only deps failed to install; server PAM/Wayland may be limited."
  fi
  pip install -e "$TARGET_DIR"

  # Check that mss and python-xlib are importable inside the venv
  log "Verifying Python dependencies inside virtual environment..."
  if ! "$TARGET_DIR/.venv/bin/python" -c "import mss; import Xlib"; then
    err "Validation failed: mss and/or python-xlib could not be imported inside the virtual environment!"
    exit 1
  fi
  log "Python dependencies verified successfully."
}

# ---- system deps (source path) ---------------------------------------------
install_system_deps() {
  [[ "$OS" == "linux" ]] || return 0
  ensure_sudo
  case "$DISTRO" in
    ubuntu|debian|linuxmint|pop)
      pkg_install \
        python3 python3-pip python3-venv python3-dev build-essential \
        libssl-dev libffi-dev libxcb-cursor0 libxkbcommon0 \
        libxcb-shape0 libxcb-shm0 libxcb-xinerama0 libxcb-randr0 \
        libxcb-render0 libxcb-render-util0 libxcb-image0 libxcb-keysyms1 \
        libxcb-icccm4 libxcb-sync1 libxcb-xfixes0 libxcb-xkb1 \
        libqt6gui6 libqt6widgets6 libqt6network6 libqt6waylandclient6 \
        qt6-wayland qml6-module-qtquick qml6-module-qtqml-workerscript \
        libwayland-client0 libwayland-cursor0 libwayland-egl1 \
        libegl1 libxkbcommon-x11-0 libdbus-1-3 \
        pkg-config libwayland-dev \
        xvfb xauth xclip openssh-client openssh-server ffmpeg \
        xdg-utils dbus-x11 \
        x11-xserver-utils x11-utils xfce4 xfce4-terminal openbox tint2 xterm libx11-6 libxext6 libxfixes3 libxdamage1 libxtst6 \
        || true
      ;;
    fedora|rhel|centos|rocky|almalinux)
      pkg_install \
        python3 python3-pip python3-devel gcc make \
        openssl-devel libffi-devel \
        qt6-qtbase qt6-qtbase-gui qt6-qtwayland \
        wayland-devel libxkbcommon-x11 libxcb-devel \
        mesa-libEGL-devel dbus-devel \
        xorg-x11-server-Xvfb xorg-x11-xauth xclip \
        openssh-server ffmpeg xdg-utils \
        xorg-x11-xinit xorg-x11-utils xfce4-session xfce4-terminal openbox tint2 xterm libX11 libXext libXfixes libXdamage libXtst \
        || true
      ;;
    arch|manjaro|endeavouros|garuda|arcolinux)
      pkg_install \
        python python-pip \
        qt6-base qt6-wayland \
        xcb-util-cursor xcb-util-keysyms xcb-util-wm \
        xcb-util-image xcb-util-renderutil \
        libxkbcommon-x11 wayland \
        pkgconf \
        xorg-server-xvfb xorg-xauth xclip \
        openssh ffmpeg xdg-utils \
        xorg-xsetroot xorg-xprop xorg-xwininfo xfce4 xfce4-terminal openbox tint2 xterm libx11 libxext libxfixes libxdamage libxtst \
        || true
      ;;
    opensuse*|sles)
      pkg_install \
        python3 python3-pip python3-devel gcc make \
        openssl-devel libffi-devel \
        xorg-x11-server-extra xorg-x11-xauth xclip \
        openssh ffmpeg xdg-utils \
        xorg-x11-utils xfce4-session xfce4-terminal openbox tint2 xterm libX11-6 libXext6 libXfixes3 libXdamage1 libXtst6 \
        || true
      ;;
    alpine)
      pkg_install \
        python3 py3-pip python3-dev musl-dev gcc make \
        qt6-qtbase qt6-qtwayland libxkbcommon-x11 wayland-libs-client \
        pkgconfig wayland-dev \
        xvfb-xauth xclip openssh ffmpeg dbus-x11 \
        xvfb xauth openbox tint2 xterm xsetroot libx11 libxext libxfixes libxdamage libxtst xfce4 xfce4-terminal \
        || true
      ;;
    *)
      err "Please install Python 3.11+, Qt6, X11/Wayland, OpenSSH, ffmpeg manually."
      ;;
  esac
  case "$DISTRO" in
    ubuntu|debian|linuxmint|pop) pkg_install python3-pam || pkg_install libpam0g-dev || true;;
    fedora|centos|rhel|rocky|almalinux) pkg_install python-pam pam-devel || true;;
    arch|manjaro|endeavouros|garuda|arcolinux) pkg_install python-pam || true;;
    opensuse*|sles) pkg_install python-pam || true;;
    alpine) pkg_install py3-pam || true;;
  esac
}

# ---- server session defaults (Linux, --component server) -------------------
# Resolve the concrete package name for a logical window-manager name on the
# current distro. Base mapping (per spec): openbox->openbox, plasma->plasma
# -desktop, xfce->xfce4, xterm->xterm. Anything not in the table is passed
# through verbatim so an operator can request a raw package name. Distro
# overrides apply where the canonical name differs from the base mapping.
wm_package() {
  local wm="$1" pkg
  case "$wm" in
    openbox) pkg="openbox";;
    plasma)  pkg="plasma-desktop";;
    xfce)    pkg="xfce4";;
    xterm)   pkg="xterm";;
    *)       pkg="$wm";;
  esac
  case "${DISTRO:-}" in
    fedora|rhel|centos|rocky|almalinux)
      # Fedora has no xfce4 metapackage; the session launcher is the right
      # unit to install (and the per-session window_manager command).
      if [[ "$wm" == "xfce" ]]; then pkg="xfce4-session"; fi
      ;;
  esac
  echo "$pkg"
}

# Binary name used both for the idempotency "already installed?" check and as
# the per-session window_manager command written into server.toml. For
# openbox/xterm the logical name IS the binary; plasma/xfce need the real
# launcher.
wm_binary() {
  case "$1" in
    openbox) echo "openbox";;
    plasma)  echo "plasmashell";;
    xfce)    echo "xfce4-session";;
    xterm)   echo "xterm";;
    *)       echo "$1";;
  esac
}

# Is the "server" component in the expanded component list for this run?
wants_server() {
  local sel comps c
  sel="$(default_components)"
  comps="$(expand_components "$sel")"
  for c in $comps; do
    [[ "$c" == "server" ]] && return 0
  done
  return 1
}

# Idempotency check: is the WM binary already on PATH? Wrapped so tests (and
# future logic) can override it without re-implementing the command -v dance.
wm_present() { command -v "$1" >/dev/null 2>&1; }

# Root-aware filesystem helpers so install_session_defaults works whether the
# installer runs as root (the common `sudo bash` case) or as a sudoer with
# passwordless sudo (cached by ensure_sudo in install_system_deps).
exists_root() {
  if [[ "${EUID:-$(id -u)}" -eq 0 ]]; then
    [[ -e "$1" ]]
  else
    if [[ "$1" == "$HOME"* ]]; then
      [[ -e "$1" ]]
    else
      sudo -n test -e "$1" 2>/dev/null
    fi
  fi
}
mkdir_root() {
  if [[ "${EUID:-$(id -u)}" -eq 0 ]]; then
    mkdir -p "$1"
  else
    if [[ "$1" == "$HOME"* ]]; then
      mkdir -p "$1"
    else
      sudo -n mkdir -p "$1"
    fi
  fi
}
write_root() {  # $1 = absolute path, $2 = file content
  if [[ "${EUID:-$(id -u)}" -eq 0 ]]; then
    printf '%s' "$2" > "$1"
  else
    if [[ "$1" == "$HOME"* ]]; then
      printf '%s' "$2" > "$1"
    else
      printf '%s' "$2" | sudo -n tee "$1" >/dev/null
    fi
  fi
}

# Install a window manager for the server (X11 backend) and generate
# /etc/ssh-remote-desktop/server.toml on first run. Idempotent: re-runs do not
# reinstall an already-present WM and never overwrite an existing server.toml.
# Only acts for --component server (or the default "both" on Linux, which
# includes server). Sets SERVER_WM_WARN="yes" when the server will end up
# without a window_manager so print_next_steps can warn about the black screen.
install_session_defaults() {
  [[ "$OS" == "linux" ]] || return 0
  wants_server || return 0

  local server_toml
  if [[ "${EUID:-$(id -u)}" -eq 0 ]]; then
    server_toml="/etc/ssh-remote-desktop/server.toml"
  else
    server_toml="$HOME/.config/ssh-remote-desktop/server.toml"
  fi

  if [[ "$WITH_WM_REQUESTED" != "yes" ]]; then
    # No WM requested: try to auto-detect an existing window manager
    local found_wm=""
    for cand in openbox plasmashell xfce4-session i3 mwm twm fluxbox blackbox awesome; do
      if command -v "$cand" >/dev/null 2>&1; then
        found_wm="$cand"
        break
      fi
    done

    if [[ -n "$found_wm" ]]; then
      case "$found_wm" in
        openbox) WITH_WM="openbox";;
        plasmashell) WITH_WM="plasma";;
        xfce4-session) WITH_WM="xfce";;
        *) WITH_WM="$found_wm";;
      esac
      log "Auto-detected existing window manager: $found_wm. Using it for configuration."
    else
      # No window manager installed on the system! Default to openbox and install it.
      WITH_WM="openbox"
    fi
  fi

  # Bare --with-wm / --with-wm= (empty value) -> openbox default.
  local wm="$WITH_WM"
  if [[ -z "$wm" ]]; then wm="openbox"; fi

  local wm_cmd="$wm"
  case "$wm" in
    xfce)   wm_cmd="startxfce4";;
    plasma) wm_cmd="startplasma-x11";;
    openbox) wm_cmd="openbox";;
  esac

  local pkg bin
  pkg="$(wm_package "$wm")"
  bin="$(wm_binary "$wm")"

  # Install the WM package only when its binary is missing -- keeps re-runs
  # idempotent regardless of the package manager's reinstall behaviour.
  if ! wm_present "$bin"; then
    log "Installing window manager: $pkg (for --with-wm=$wm)"
    if ! pkg_install "$pkg"; then
      warn "Failed to install $pkg. The server will start, but without a WM you'll get a black screen. Install $pkg manually and set window_manager in $server_toml."
      SERVER_WM_WARN="yes"
    fi
  else
    log "Window manager already present: $bin (skipping install of $pkg)."
  fi

  # Generate server.toml ONLY when it does not exist; never overwrite an
  # operator-edited config (idempotency + safe re-runs).
  if exists_root "$server_toml"; then
    warn "$server_toml already exists -- leaving it untouched. Edit it manually if you want window_manager = \"$wm_cmd\"."
    SERVER_WM_WARN="no"
    return 0
  fi
  if ! mkdir_root "$(dirname "$server_toml")"; then
    warn "Could not create $(dirname "$server_toml"). Skipping server.toml generation."
    SERVER_WM_WARN="yes"
    return 0
  fi
  local content
  content=$(cat <<TOML
# $server_toml
# Generated by the ssh-remote-desktop installer (--component server --with-wm=$wm).
# Safe to edit: re-running the installer will NOT overwrite this file.
backend = "x11"
session_geometry = [1920, 1080]
window_manager = "$wm_cmd"
TOML
)
  if write_root "$server_toml" "$content"; then
    log "Wrote $server_toml (backend=x11, session_geometry=[1920,1080], window_manager=\"$wm_cmd\")."
    SERVER_WM_WARN="no"
  else
    warn "Could not write $server_toml. Set window_manager = \"$wm_cmd\" manually to avoid a black screen."
    SERVER_WM_WARN="yes"
  fi
}

maybe_build() {
  if [[ "$BUILD" == "no" ]]; then return; fi
  if ! command -v cc >/dev/null 2>&1 && ! command -v gcc >/dev/null 2>&1; then
    log "No C compiler found, skipping binary build."
    return
  fi
  log "Building standalone binaries with Nuitka (this may take a while)…"
  # shellcheck disable=SC1091
  source "$TARGET_DIR/.venv/bin/activate"
  pip install -q nuitka zstandard ordered-set
  chmod +x "$TARGET_DIR"/build_*.sh
  mkdir -p "$TARGET_DIR/bin"
  if [[ "$OS" == "linux" ]]; then
    (cd "$TARGET_DIR" && bash build_client_linux.sh && mv dist/rd-client bin/rd-client) || log "client build failed"
    (cd "$TARGET_DIR" && bash build_server_linux.sh && mv dist/rd-server bin/rd-server) || log "server build failed"
    (cd "$TARGET_DIR" && bash build_server_gui_linux.sh && mv dist/rd-server-gui bin/rd-server-gui) || log "server-gui build failed"
  else
    log "macOS: no preconfigured build script — pip-installed packages only."
  fi
}

# ---- desktop entries, icons, PATH ------------------------------------------
# Absolute install locations for launchers/icons. System-wide when running as
# root (server install), user-local otherwise (client install).
launcher_dirs() {
  if [[ "${EUID:-$(id -u)}" -eq 0 ]]; then
    BINDIR="/usr/local/bin"
    APPSDIR="/usr/share/applications"
    ICONDIR="/usr/share/icons/hicolor/scalable/apps"
  else
    BINDIR="$HOME/.local/bin"
    APPSDIR="$HOME/.local/share/applications"
    ICONDIR="$HOME/.local/share/icons/hicolor/scalable/apps"
  fi
  mkdir -p "$BINDIR" "$APPSDIR" "$ICONDIR"
}

# Resolve the absolute path of an installed component binary, preferring the
# prebuilt binary in $TARGET_DIR/bin and falling back to the venv console
# script. Empty if the component isn't installed.
resolve_component_bin() {
  local name="$1" target=""
  if [[ -x "$TARGET_DIR/bin/$name" ]]; then
    target="$TARGET_DIR/bin/$name"
  elif [[ -x "$TARGET_DIR/.venv/bin/$name" ]]; then
    target="$TARGET_DIR/.venv/bin/$name"
  fi
  echo "$target"
}

# Write a scalable SVG icon for a component. $1 = component (client|server-gui)
# $2 = absolute output path. Keeps everything self-contained (no external icon
# downloads) so the launcher shows a real icon even on minimal themes.
write_icon_svg() {
  local comp="$1" out="$2"
  local svg
  if [[ "$comp" == "client" ]]; then
    # Monitor + blue SSH-arrow, signalling "remote screen".
    svg='<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64" width="64" height="64">
  <rect x="6" y="10" width="52" height="38" rx="4" fill="#1f2937" stroke="#3b82f6" stroke-width="2"/>
  <rect x="10" y="14" width="44" height="28" rx="2" fill="#0f172a"/>
  <path d="M14 32l8-10 6 7 6-9 8 12" fill="none" stroke="#60a5fa" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"/>
  <rect x="22" y="50" width="20" height="4" rx="2" fill="#3b82f6"/>
  <rect x="16" y="54" width="32" height="3" rx="1.5" fill="#1f2937"/>
</svg>'
  else
    # Server rack + green status LED, signalling "managed server".
    svg='<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64" width="64" height="64">
  <rect x="12" y="8" width="40" height="48" rx="4" fill="#1f2937" stroke="#10b981" stroke-width="2"/>
  <rect x="16" y="14" width="32" height="10" rx="2" fill="#0f172a"/>
  <rect x="16" y="28" width="32" height="10" rx="2" fill="#0f172a"/>
  <rect x="16" y="42" width="32" height="10" rx="2" fill="#0f172a"/>
  <circle cx="42" cy="19" r="2" fill="#34d399"/>
  <circle cx="42" cy="33" r="2" fill="#34d399"/>
  <circle cx="42" cy="47" r="2" fill="#34d399"/>
  <rect x="20" y="16" width="16" height="2" rx="1" fill="#374151"/>
  <rect x="20" y="30" width="16" height="2" rx="1" fill="#374151"/>
  <rect x="20" y="44" width="16" height="2" rx="1" fill="#374151"/>
</svg>'
  fi
  printf '%s' "$svg" > "$out"
  chmod 0644 "$out"
}

# Create a .desktop launcher for a component. $1 = component (client|server-gui),
# $2 = absolute binary path, $3 = display name, $4 = generic name, $5 = icon name.
# Write the rd-launch session-env wrapper next to the binaries. The wrapper
# reconstructs WAYLAND_DISPLAY / XDG_RUNTIME_DIR for menu-launched apps (the
# classic "click the menu entry and nothing happens" case on Wayland, where
# D-Bus / systemd --user activation doesn't propagate the interactive shell
# env). Prefer the committed scripts/rd-launch.sh when available; fall back to
# an inline heredoc for curl|bash binary installs where the repo scripts
# aren't present. The inline heredoc is kept byte-identical to
# scripts/rd-launch.sh — tests/test_install_launcher.py enforces that.
write_rd_launch() {
  local out="$TARGET_DIR/bin/rd-launch"
  mkdir -p "$(dirname "$out")"
  local src=""
  [[ -n "${HERE:-}" && -f "$HERE/scripts/rd-launch.sh" ]] && src="$HERE/scripts/rd-launch.sh"
  [[ -z "$src" && -f "$TARGET_DIR/scripts/rd-launch.sh" ]] && src="$TARGET_DIR/scripts/rd-launch.sh"
  if [[ -n "$src" ]]; then
    install -m 0755 "$src" "$out"
  else
    # >>> rd-launch begin (keep in sync with scripts/rd-launch.sh)
    cat > "$out" <<'RDLAUNCH'
#!/usr/bin/env bash
# rd-launch — session-env wrapper for rd-client / rd-server-gui.
# Reconstructs WAYLAND_DISPLAY / XDG_RUNTIME_DIR for menu-launched apps so Qt
# can find a display on Wayland. Usage: rd-launch <real-binary> [args...]
set -euo pipefail

if [[ $# -lt 1 ]]; then
  echo "rd-launch: missing binary argument" >&2
  exit 64
fi
BIN="$1"; shift

if [[ -z "${XDG_RUNTIME_DIR:-}" ]]; then
  uid="$(id -u 2>/dev/null || echo 0)"
  if [[ -d "/run/user/${uid}" ]]; then
    export XDG_RUNTIME_DIR="/run/user/${uid}"
  fi
fi

if [[ -z "${WAYLAND_DISPLAY:-}" && -n "${XDG_RUNTIME_DIR:-}" && -d "${XDG_RUNTIME_DIR}" ]]; then
  for cand in "${XDG_RUNTIME_DIR}"/wayland-*; do
    [[ -S "$cand" ]] || continue
    WAYLAND_DISPLAY="${cand##*/}"
    export WAYLAND_DISPLAY
    break
  done
fi

exec "$BIN" "$@"
RDLAUNCH
    # <<< rd-launch end
    chmod 0755 "$out"
  fi
  # Expose on PATH via the launcher bindir so the .desktop Exec path is stable
  # and doesn't depend on $TARGET_DIR being on PATH.
  ln -sf "$out" "$BINDIR/rd-launch" 2>/dev/null || true
}

write_desktop_entry() {
  local comp="$1" bin="$2" name="$3" generic="$4" icon="$5" launcher="${6:-}"
  local file="$APPSDIR/ssh-remote-desktop-${comp}.desktop"
  local exec_line="$bin"
  # Route through rd-launch when present: it restores the Wayland session env
  # (WAYLAND_DISPLAY / XDG_RUNTIME_DIR) that menu-launched processes often
  # lack, so the GUI actually opens instead of dying silently.
  [[ -n "$launcher" && -x "$launcher" ]] && exec_line="$launcher $bin"
  cat > "$file" <<DESKTOP
[Desktop Entry]
Type=Application
Version=1.0
Name=${name}
GenericName=${generic}
Comment=SSH Remote Desktop
Exec=${exec_line}
Icon=${icon}
Terminal=false
Categories=Network;RemoteAccess;Qt;
Keywords=ssh;remote;desktop;rdp;vnc;
StartupWMClass=ssh-remote-desktop
DESKTOP
  chmod 0644 "$file"
}

# Build launchers + icons for every installed component. Client installs get a
# client launcher; server installs get the server-gui launcher. Idempotent.
install_desktop_entries() {
  [[ "$OS" == "linux" ]] || return 0
  launcher_dirs
  # Install the session-env wrapper before building .desktop entries so the
  # Exec= line can route through it (fixes menu launches on Wayland, where the
  # launched process often lacks WAYLAND_DISPLAY / XDG_RUNTIME_DIR).
  write_rd_launch
  local sel comps comp bin icon_name
  sel="$(default_components)"
  comps="$(expand_components "$sel")"
  for comp in $comps; do
    case "$comp" in
      client)
        bin="$(resolve_component_bin rd-client)"
        [[ -z "$bin" ]] && continue
        # Symlink into the launcher bindir so the .desktop path is stable and
        # doesn't depend on $TARGET_DIR being on PATH.
        ln -sf "$bin" "$BINDIR/rd-client" 2>/dev/null || true
        icon_name="ssh-remote-desktop-client"
        write_icon_svg client "$ICONDIR/${icon_name}.svg"
        write_desktop_entry client "$BINDIR/rd-client" \
          "SSH Remote Desktop — Client" "Remote Desktop Client" "$icon_name" \
          "$BINDIR/rd-launch"
        log "Created launcher: $APPSDIR/ssh-remote-desktop-client.desktop"
        ;;
      server)
        # The rd-server daemon has no GUI launcher (it runs headless under
        # systemd / as a daemon), so no .desktop entry here. BUT we must still
        # symlink rd-server into $BINDIR so that `rd-server-gui` (and the user's
        # terminal) can find it via PATH — the DaemonController falls back to
        # `shutil.which("rd-server")` when the systemd unit isn't installed.
        bin="$(resolve_component_bin rd-server)"
        if [[ -n "$bin" ]]; then
          ln -sf "$bin" "$BINDIR/rd-server" 2>/dev/null || true
        fi
        ;;
      server-gui)
        # Create the control-panel launcher only when rd-server-gui is actually
        # installed — otherwise the menu would show a dead entry pointing at a
        # missing binary.
        bin="$(resolve_component_bin rd-server-gui)"
        [[ -z "$bin" ]] && continue
        ln -sf "$bin" "$BINDIR/rd-server-gui" 2>/dev/null || true
        icon_name="ssh-remote-desktop-server-gui"
        write_icon_svg server-gui "$ICONDIR/${icon_name}.svg"
        write_desktop_entry server-gui "$BINDIR/rd-server-gui" \
          "SSH Remote Desktop — Server Panel" "Server Management Panel" "$icon_name" \
          "$BINDIR/rd-launch"
        log "Created launcher: $APPSDIR/ssh-remote-desktop-server-gui.desktop"
        ;;
    esac
  done
  # Refresh the desktop database so the entries show up immediately in menus
  # that honour it (GNOME, KDE, XFCE). Best-effort — ignore failures.
  if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database "$APPSDIR" >/dev/null 2>&1 || true
  fi
}

# Idempotently ensure $HOME/.local/bin is on PATH for interactive shells, so a
# user can type `rd-client` / `rd-server-gui` in a fresh terminal without
# manual setup. Only touches the user's own rc files (skipped when root).
# Checks whether ~/.local/bin is already on PATH for *this* session; if not,
# appends the right line to the rc matching $SHELL (bash / zsh / fish) once
# (marker-guarded — never duplicated on re-runs) and prints the exact line to
# run for the current session.
# True when running as root (EUID 0). Extracted so tests can override it without
# dropping privileges (setpriv fails under non-root CI runners, and EUID is
# readonly in bash).
srd_is_root() { [[ "${EUID:-$(id -u)}" -eq 0 ]]; }

ensure_path() {
  [[ "$OS" == "linux" ]] || return 0
  srd_is_root && return 0
  local bindir="$HOME/.local/bin"
  # Already on PATH in this session — nothing to do.
  case ":${PATH:-}:" in
    *:"$bindir":*) return 0;;
  esac
  # Pick the rc that matches the user's login shell.
  local sh="${SHELL:-/bin/bash}" rc=""
  case "$sh" in
    *fish) rc="$HOME/.config/fish/config.fish";;
    *zsh)  rc="$HOME/.zshrc";;
    *)     rc="$HOME/.bashrc";;
  esac
  local marker='# ssh-remote-desktop installer: local bin on PATH'
  if [[ -n "$rc" ]]; then
    mkdir -p "$(dirname "$rc")"
    if ! grep -qF "$marker" "$rc" 2>/dev/null; then
      if [[ "$rc" == *config.fish ]]; then
        # shellcheck disable=SC2016  # $HOME and $PATH are literal for the rc file
        printf '\nset -gx PATH $HOME/.local/bin $PATH  %s\n' "$marker" >> "$rc"
      else
        # shellcheck disable=SC2016  # $HOME and $PATH are literal for the rc file
        printf '\nexport PATH="$HOME/.local/bin:$PATH"  %s\n' "$marker" >> "$rc"
      fi
      log "Added $HOME/.local/bin to PATH in $rc (takes effect in a new shell)."
    fi
  fi
  # Warn with the exact line to run for the current session — even if the rc
  # already had it (this shell still lacks it).
  warn "$HOME/.local/bin is not on PATH for this session."
  if [[ "$rc" == *config.fish ]]; then
    # shellcheck disable=SC2016  # literal user-facing hint, not expansion
    printf '    set -gx PATH $HOME/.local/bin $PATH\n' >&2
  else
    # shellcheck disable=SC2016  # literal user-facing hint, not expansion
    printf '    export PATH="$HOME/.local/bin:$PATH"\n' >&2
  fi
  printf '  (added to %s; run the line above now, or open a new shell)\n' "$rc" >&2
}

# ---- diagnostics ----------------------------------------------------------
# Print a self-contained health report: which rd-* commands resolve on PATH
# and where they point (flagging broken symlinks), whether ~/.local/bin is on
# PATH, the effective Qt platform + display env, and the presence of the
# system libraries Qt6 needs for the xcb/wayland plugins. Exits 0 so it can be
# run any time without side effects.
do_diagnose() {
  log "ssh-remote-desktop diagnostics"
  echo
  echo "== Commands on PATH =="
  local name p target
  for name in rd-server rd-client rd-server-gui rd-launch; do
    p="$(command -v "$name" 2>/dev/null || true)"
    if [[ -n "$p" ]]; then
      target="$(readlink -f "$p" 2>/dev/null || true)"
      if [[ -n "$target" && "$target" != "$p" ]]; then
        printf '  %-16s %s -> %s\n' "$name" "$p" "$target"
      else
        printf '  %-16s %s\n' "$name" "$p"
      fi
      if [[ ! -e "${target:-$p}" ]]; then
        printf '    ! BROKEN: target does not exist\n'
      fi
    else
      printf '  %-16s NOT FOUND on PATH\n' "$name"
    fi
  done
  echo
  echo "== PATH =="
  local bindir="$HOME/.local/bin"
  if [[ ":${PATH:-}:" == *":${bindir}:"* ]]; then
    echo "  $bindir is on PATH"
  else
    echo "  $bindir is NOT on PATH"
    # shellcheck disable=SC2016  # literal user-facing hint, not expansion
    echo '    fix: export PATH="$HOME/.local/bin:$PATH"'
  fi
  echo
  echo "== Qt platform =="
  printf '  QT_QPA_PLATFORM=%s\n' "${QT_QPA_PLATFORM:-<unset>}"
  printf '  DISPLAY=%s  WAYLAND_DISPLAY=%s\n' "${DISPLAY:-<unset>}" "${WAYLAND_DISPLAY:-<unset>}"
  printf '  XDG_SESSION_TYPE=%s\n' "${XDG_SESSION_TYPE:-<unset>}"
  echo
  if [[ "$OS" == "linux" ]]; then
    echo "== System libraries (Qt6 xcb/wayland) =="
    local lib found dir
    for lib in libxcb-cursor.so libxcb-keysyms.so libxcb-wm.so libxcb-image.so \
               libxcb-render-util.so libxkbcommon-x11.so libwayland-client.so libEGL.so; do
      found="$( { ldconfig -p 2>/dev/null || true; } | awk -v l="$lib" '$0 ~ l {print $NF; exit}')"
      if [[ -z "$found" ]]; then
        for dir in /usr/lib /usr/lib/x86_64-linux-gnu /usr/lib64 /lib /lib64; do
          if [[ -e "$dir/$lib" ]]; then found="$dir/$lib"; break; fi
        done
      fi
      if [[ -n "$found" ]]; then
        printf '  %-24s OK (%s)\n' "$lib" "$found"
      else
        printf '  %-24s MISSING\n' "$lib"
      fi
    done
    echo
  fi

  echo "== Black Screen Diagnostics =="
  # 1. Check for Xvfb on PATH
  if command -v Xvfb >/dev/null 2>&1; then
    echo "  Xvfb: OK ($(command -v Xvfb))"
  else
    echo "  Xvfb: MISSING (X11 virtual framebuffer is required for headless server)"
  fi

  # 2. Check for Window Manager on PATH
  local found_wm=""
  for wm in openbox plasmashell xfce4-session i3 mwm twm fluxbox blackbox awesome startxfce4; do
    if command -v "$wm" >/dev/null 2>&1; then
      found_wm="$wm"
      break
    fi
  done
  if [[ -n "$found_wm" ]]; then
    echo "  Window Manager: OK ($found_wm found)"
  else
    echo "  Window Manager: MISSING (Without a WM, the client screen will be black. Install openbox, xfce4 or another WM)"
  fi

  # 3. Check for python-xlib and mss
  local py; py=$(find_python 2>/dev/null || echo "python3")
  if "$py" -c "import mss" >/dev/null 2>&1; then
    echo "  python-mss: OK"
  else
    echo "  python-mss: MISSING (Required for screen capture)"
  fi
  if "$py" -c "import Xlib" >/dev/null 2>&1; then
    echo "  python-xlib: OK"
  else
    echo "  python-xlib: MISSING (Required for inputs and cursor)"
  fi

  # 4. Check for XDG_RUNTIME_DIR
  if [[ -n "${XDG_RUNTIME_DIR:-}" ]]; then
    if [[ -d "$XDG_RUNTIME_DIR" && -w "$XDG_RUNTIME_DIR" ]]; then
      echo "  XDG_RUNTIME_DIR: OK ($XDG_RUNTIME_DIR is directory and writable)"
    else
      echo "  XDG_RUNTIME_DIR: WARNING ($XDG_RUNTIME_DIR is set but not writable or not a directory)"
    fi
  else
    echo "  XDG_RUNTIME_DIR: WARNING (not set in environment)"
  fi
  echo

  echo "== Troubleshooting =="
  echo "  If a GUI fails to open with a Qt platform-plugin error, re-run with:"
  echo "    QT_DEBUG_PLUGINS=1 rd-server-gui   (or rd-client)"
  echo "  and inspect the plugin load errors printed to stderr."
}

# ---- post-install wiring ---------------------------------------------------
# When run under sudo, $HOME is root's home — but the *invoking* user is the
# one who'll type `rd-server` in their terminal. Resolve that user's home via
# SUDO_USER + getent so we can drop the PATH symlinks where they actually
# matter (and overwrite stale ones from a previous non-sudo install that would
# otherwise shadow the new /usr/local/bin binary). Prints the home path on
# stdout, empty when not running under sudo or it can't be resolved.
real_user_home() {
  local user="${SUDO_USER:-}"
  [[ -z "$user" ]] && return 0
  [[ "$user" == "root" ]] && return 0
  local home
  home=$(getent passwd "$user" 2>/dev/null | awk -F: '{print $6}')
  [[ -n "$home" ]] && echo "$home"
}

# Drop the rd-* PATH symlinks into $1 (a home dir), preferring prebuilt
# binaries in $TARGET_DIR/bin and falling back to the venv console scripts.
# `ln -sf` overwrites any existing symlink (including a stale one pointing at
# an old install dir), so the user always ends up with the freshly-installed
# binary regardless of which ~/.local/bin entry was on PATH first.
install_user_symlinks() {
  local home="$1"
  local bindir="$home/.local/bin"
  mkdir -p "$bindir"
  local name target
  for name in rd-server rd-client rd-server-gui rd-launch; do
    target=""
    if [[ -x "$TARGET_DIR/bin/$name" ]]; then
      target="$TARGET_DIR/bin/$name"
    elif [[ -x "$TARGET_DIR/.venv/bin/$name" ]]; then
      target="$TARGET_DIR/.venv/bin/$name"
    fi
    if [[ -n "$target" ]]; then
      ln -sf "$target" "$bindir/$name" 2>/dev/null || true
    fi
  done
}

post_install() {
  mkdir -p "$HOME/.config/ssh-remote-desktop"
  # Drop PATH symlinks into the current $HOME's ~/.local/bin. Under sudo this
  # is /root (harmless), but we also re-point the *invoking* user's symlinks
  # below so their terminal actually runs the new binary instead of a stale
  # ~/.local/bin/rd-* left over from a previous non-sudo install.
  install_user_symlinks "$HOME"
  # When run under sudo, $HOME is root's; the real user is $SUDO_USER. Repoint
  # THEIR ~/.local/bin/rd-* symlinks at the freshly-installed binary so `rd-server`
  # typed in their shell resolves to this install (overwriting any stale link
  # that would otherwise shadow /usr/local/bin and run an old, crashing binary).
  local rhome
  rhome=$(real_user_home)
  if [[ -n "$rhome" && "$rhome" != "$HOME" ]]; then
    install_user_symlinks "$rhome"
    log "Linked rd-* into $rhome/.local/bin (invoking user via sudo)."
  fi
  # Application-menu launchers + icons, and ensure ~/.local/bin is on PATH for
  # fresh shells so the user never needs to type a setup command.
  install_desktop_entries
  ensure_path
}

# ---- uninstall -------------------------------------------------------------
do_uninstall() {
  if [[ "$UNINSTALL" != "yes" ]]; then return; fi
  log "Uninstalling ssh-remote-desktop…"
  # 1. The install directory (prebuilt binaries, venv, sources).
  rm -rf "$TARGET_DIR"
  # 2. The PATH symlinks.
  for name in rd-server rd-client rd-server-gui rd-launch; do
    if [[ -L "$HOME/.local/bin/$name" || -e "$HOME/.local/bin/$name" ]]; then
      local link_target
      link_target=$(readlink -f "$HOME/.local/bin/$name" 2>/dev/null || true)
      # Only remove links that pointed into our install dir; never touch a
      # user's manually-placed binary of the same name elsewhere.
      if [[ "$link_target" == "$TARGET_DIR"* ]] || [[ -z "$link_target" ]]; then
        rm -f "$HOME/.local/bin/$name"
        log "Removed symlink ~/.local/bin/$name"
      fi
    fi
  done
  # 2b. System-wide symlinks (root install path).
  for name in rd-server rd-client rd-server-gui rd-launch; do
    if [[ -L "/usr/local/bin/$name" ]]; then
      local link_target
      link_target=$(readlink -f "/usr/local/bin/$name" 2>/dev/null || true)
      if [[ "$link_target" == "$TARGET_DIR"* ]] || [[ -z "$link_target" ]]; then
        rm -f "/usr/local/bin/$name"
      fi
    fi
  done
  # 2c. Application-menu launchers + icons we created.
  for dir in "$HOME/.local/share/applications" "/usr/share/applications"; do
    rm -f "$dir/ssh-remote-desktop-client.desktop" \
          "$dir/ssh-remote-desktop-server-gui.desktop" 2>/dev/null || true
  done
  for dir in "$HOME/.local/share/icons/hicolor/scalable/apps" "/usr/share/icons/hicolor/scalable/apps"; do
    rm -f "$dir/ssh-remote-desktop-client.svg" \
          "$dir/ssh-remote-desktop-server-gui.svg" 2>/dev/null || true
  done
  if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database >/dev/null 2>&1 || true
  fi
  # 3. The config dir, but only if it is empty (never wipe user keys/hosts).
  local cfgdir="$HOME/.config/ssh-remote-desktop"
  if [[ -d "$cfgdir" ]] && [[ -z "$(find "$cfgdir" -mindepth 1 -maxdepth 1 -print -quit)" ]]; then
    rmdir "$cfgdir"
    log "Removed empty config dir $cfgdir"
  fi
  # 4. System-wide server config under /etc/ssh-remote-desktop -- same policy
  # as the user config above: only remove server.toml when it is empty, and
  # only remove the directory when it is empty. A config the operator edited
  # (e.g. a non-empty server.toml with a custom window_manager) is never
  # wiped by an uninstall/re-install cycle.
  local syscfg="/etc/ssh-remote-desktop"
  if [[ -d "$syscfg" ]]; then
    local sysfile="$syscfg/server.toml"
    if [[ -f "$sysfile" && ! -s "$sysfile" ]]; then
      if rm -f "$sysfile" 2>/dev/null; then
        log "Removed empty $sysfile"
      fi
    fi
    if [[ -z "$(find "$syscfg" -mindepth 1 -maxdepth 1 -print -quit)" ]]; then
      if rmdir "$syscfg" 2>/dev/null; then
        log "Removed empty config dir $syscfg"
      fi
    fi
  fi
  log "Uninstall complete. (PATH edits in ~/.bashrc / ~/.profile left in place — remove the 'ssh-remote-desktop installer' lines manually if desired.)"
  exit 0
}

# ---- main flow -------------------------------------------------------------
main() {
  detect_os
  detect_arch
  detect_distro
  log "Detected: $OS / $ARCH / ${DISTRO:-n/a}"
  log "Target directory: $TARGET_DIR"
  log "Mode: $MODE (build=$BUILD, from-source=$FROM_SOURCE)"

  if [[ "${DIAGNOSE:-no}" == "yes" ]]; then
    do_diagnose
    exit 0
  fi

  do_uninstall

  install_system_deps || warn "Some system packages failed to install; continuing."
  install_session_defaults || warn "Server session defaults setup incomplete; continuing."

  # --run mode: try prebuilt release binaries first (unless --from-source).
  if [[ "$MODE" == "run" && "$FROM_SOURCE" == "no" && "$OS" != "windows" ]]; then
    if install_release_binaries; then
      post_install
      log "Installation complete (prebuilt binaries from release)."
      print_next_steps
      exit 0
    fi
    warn "Not all binaries available from the release; falling back to source."
  fi

  # Source path (also used by --dev / --both / --from-source / binary fallback).
  case "$MODE" in
    dev|both) clone_dev;;
    run) fetch_release_tarball;;
  esac
  setup_venv
  maybe_build
  post_install

  log "Installation complete (from source)."
  print_next_steps
}

print_next_steps() {
  cat <<NEXT

✅ Installation complete.

Launch from your application menu (no terminal needed):
  • "SSH Remote Desktop — Client"      → connect to a remote machine
  • "SSH Remote Desktop — Server Panel" → manage this machine's server

The launchers appear in GNOME/KDE/XFCE menus under Network ▸ Remote Access.
A new shell will also have ~/.local/bin on PATH automatically.

First-time client connection (or just click the launcher and fill the dialog):
  rd-client --host YOUR-SERVER --user YOUR-USER --key-path ~/.ssh/id_ed25519

Server side: the panel creates the config and host key for you on first start,
or pre-create them with:
  rd-server --config /etc/ssh-remote-desktop/server.toml

See $TARGET_DIR/README.md for the full guide.
NEXT
  if [[ "${SERVER_WM_WARN:-no}" == "yes" ]]; then
    cat <<'WMWARN'

⚠️  Server (X11 backend): без window_manager в server.toml будет ЧЁРНЫЙ ЭКРАН.
   Задайте его вручную или переустановите с --with-wm=..., например:
     ... | sudo bash -s -- --component server --with-wm=openbox
   Либо допишите в существующий конфиг:
     echo 'window_manager = "openbox"' | sudo tee -a /etc/ssh-remote-desktop/server.toml
WMWARN
  fi
}

# Run only when executed or piped (curl|bash), not when sourced by the
# test-suite (which calls individual functions like ensure_path directly).
# BASH_SOURCE[1] is set when sourced from another script; SRD_NO_RUN_MAIN=1 is
# an explicit opt-out for inline `bash -c 'source …'` test harnesses.
if [[ -z "${BASH_SOURCE[1]:-}" && "${SRD_NO_RUN_MAIN:-0}" != "1" ]]; then
  main "$@"
fi
