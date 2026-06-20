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
VERSION=""      # specific release version (X.Y.Z), empty = latest
FROM_SOURCE="no"
COMPONENT=""    # client | server | both; empty = auto (both on linux, client elsewhere)

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
  --component C    client | server | both (default: both on Linux, client elsewhere)
  --uninstall      Remove the install (binary, venv, sources, symlinks, empty config)
  -h, --help       Show this help

Environment:
  SSH_REMOTE_DESKTOP_DIR  default install directory
USAGE
}

log() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33mWARN:\033[0m %s\n' "$*" >&2; }
err() { printf '\033[1;31mERR:\033[0m %s\n' "$*" >&2; }

# ---- argument parsing ------------------------------------------------------
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
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
    --uninstall) UNINSTALL="yes"; shift;;
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
  if [[ -f /etc/os-release ]]; then
    # shellcheck disable=SC1091
    . /etc/os-release
    DISTRO="${ID:-unknown}"
  else
    DISTRO="unknown"
  fi
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
        linux) echo "client server";;
        *) echo "client";;
      esac
      ;;
    *) echo "$sel";;
  esac
}

asset_name() {
  # $1 = component (client|server)
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
    arch|manjaro|endeavouros)
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
verify_sha256() {
  local archive="$1" version="$2" base sums_url sums_file expected actual
  base="$(basename "$archive")"
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
  expected=$(grep -E " ${base}\$" "$sums_file" | awk '{print $1}' | head -1)
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
    if ! verify_sha256 "$archive" "$VERSION"; then
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
  pip install -e "$TARGET_DIR"
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
        xvfb xauth xclip openssh-client openssh-server ffmpeg \
        xdg-utils dbus-x11 \
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
        || true
      ;;
    arch|manjaro)
      pkg_install \
        python python-pip \
        qt6-base qt6-wayland \
        xcb-util-cursor libxkbcommon-x11 wayland \
        xorg-server-xvfb xauth xclip \
        openssh ffmpeg xdg-utils \
        || true
      ;;
    alpine)
      pkg_install \
        python3 py3-pip python3-dev musl-dev gcc make \
        qt6-qtbase qt6-qtwayland libxkbcommon-x11 wayland-libs-client \
        xvfb-xauth xclip openssh ffmpeg dbus-x11 \
        || true
      ;;
    *)
      err "Please install Python 3.11+, Qt6, X11/Wayland, OpenSSH, ffmpeg manually."
      ;;
  esac
  case "$DISTRO" in
    ubuntu|debian|linuxmint|pop) pkg_install python3-pam || pkg_install libpam0g-dev || true;;
    fedora|centos|rhel|rocky|almalinux) pkg_install python-pam pam-devel || true;;
    arch|manjaro) pkg_install python-pam || true;;
    alpine) pkg_install py3-pam || true;;
  esac
}

# ---- optional Nuitka build (source path) -----------------------------------
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
  else
    log "macOS: no preconfigured build script — pip-installed packages only."
  fi
}

# ---- post-install wiring ---------------------------------------------------
post_install() {
  mkdir -p "$HOME/.config/ssh-remote-desktop"
  if [[ -d "$HOME/.local/bin" ]] || mkdir -p "$HOME/.local/bin"; then
    # Prefer prebuilt binaries in $TARGET_DIR/bin; fall back to the venv's
    # console scripts when installed from source.
    for name in rd-server rd-client; do
      local target=""
      if [[ -x "$TARGET_DIR/bin/$name" ]]; then
        target="$TARGET_DIR/bin/$name"
      elif [[ -x "$TARGET_DIR/.venv/bin/$name" ]]; then
        target="$TARGET_DIR/.venv/bin/$name"
      fi
      if [[ -n "$target" ]]; then
        ln -sf "$target" "$HOME/.local/bin/$name" 2>/dev/null || true
      fi
    done
  fi
}

# ---- uninstall -------------------------------------------------------------
do_uninstall() {
  if [[ "$UNINSTALL" != "yes" ]]; then return; fi
  log "Uninstalling ssh-remote-desktop…"
  # 1. The install directory (prebuilt binaries, venv, sources).
  rm -rf "$TARGET_DIR"
  # 2. The PATH symlinks.
  for name in rd-server rd-client; do
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
  # 3. The config dir, but only if it is empty (never wipe user keys/hosts).
  local cfgdir="$HOME/.config/ssh-remote-desktop"
  if [[ -d "$cfgdir" ]] && [[ -z "$(find "$cfgdir" -mindepth 1 -maxdepth 1 -print -quit)" ]]; then
    rmdir "$cfgdir"
    log "Removed empty config dir $cfgdir"
  fi
  log "Uninstall complete."
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

  do_uninstall

  install_system_deps || warn "Some system packages failed to install; continuing."

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

Next steps:

  1. Add ~/.local/bin to PATH (or open a new shell):
       export PATH="$HOME/.local/bin:$PATH"

  2. Generate a host key (auto-generated on first run, but you can pre-create it):
       rd-server --config /etc/ssh-remote-desktop/server.toml

  3. On the client, open the key manager (Help → SSH keys) and copy your
     public key into the server's ~/.ssh/authorized_keys.

  4. Launch the client:
       rd-client --host YOUR-SERVER --user YOUR-USER --key-path ~/.ssh/id_ed25519

See $TARGET_DIR/README.md for the full guide.
NEXT
}

main "$@"
