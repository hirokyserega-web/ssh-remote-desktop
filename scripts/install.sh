#!/usr/bin/env bash
# Universal installer for ssh-remote-desktop.
# Detects platform, picks the right installer, and forwards all arguments.
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/hirokyserega-web/ssh-remote-desktop/main/scripts/install.sh | bash
#   ... | bash -s -- --dev
#   ... | bash -s -- --no-build
set -euo pipefail

REPO_URL="https://github.com/hirokyserega-web/ssh-remote-desktop"
REPO_RAW="https://raw.githubusercontent.com/hirokyserega-web/ssh-remote-desktop/main"
TARGET_DIR="${SSH_REMOTE_DESKTOP_DIR:-$HOME/.local/share/ssh-remote-desktop}"
MODE="run"    # run | dev | both
BUILD="auto"  # auto | yes | no
PYTHON_BIN=""

usage() {
  cat <<USAGE
ssh-remote-desktop installer

Usage: install.sh [options]
  --dev         Install development checkout (git clone, editable pip install)
  --run         Install stable release into ~/.local/share/ssh-remote-desktop (default)
  --both        Install dev checkout AND try to build a binary
  --no-build    Skip compiling binaries, just install Python deps
  --build       Force building binaries with Nuitka
  --dir PATH    Install into a different directory
  --python BIN  Use a specific Python interpreter
  -h, --help    Show this help

Environment:
  SSH_REMOTE_DESKTOP_DIR  default install directory
USAGE
}

log() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
err() { printf '\033[1;31mERR:\033[0m %s\n' "$*" >&2; }

# ---- argument parsing ------------------------------------------------------
ARGS=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --dev) MODE="dev"; shift;;
    --run) MODE="run"; shift;;
    --both) MODE="both"; shift;;
    --no-build) BUILD="no"; shift;;
    --build) BUILD="yes"; shift;;
    --dir) TARGET_DIR="$2"; shift 2;;
    --python) PYTHON_BIN="$2"; shift 2;;
    -h|--help) usage; exit 0;;
    *) ARGS+=("$1"); shift;;
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

detect_distro() {
  if [[ -f /etc/os-release ]]; then
    . /etc/os-release
    DISTRO="${ID:-unknown}"
  else
    DISTRO="unknown"
  fi
}

need_cmd() {
  command -v "$1" >/dev/null 2>&1
}

# ---- package manager helpers -----------------------------------------------
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
      sudo -n pacman -S --noconfirm "${pkgs[@]}"
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
    err "This installer needs sudo to install system packages. Re-run as a sudoer, or install manually."
    exit 1
  fi
}

# ---- Python interpreter ----------------------------------------------------
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

# ---- platform-specific system deps -----------------------------------------
install_system_deps() {
  ensure_sudo
  case "$OS" in
    linux)
      # Common build deps + Qt, X11, Wayland client libs, ssh tooling.
      # Crypto/ssh server deps below.
      case "$DISTRO" in
        ubuntu|debian|linuxmint|pop)
          pkg_install \
            python3 python3-pip python3-venv python3-dev build-essential \
            libssl-dev libffi-dev libxcb-cursor0 libxkbcommon0 \
            libxcb-shape0 libxcb-shm0 libxcb-xinerama0 libxcb-randr0 \
            libxcb-render0 libxcb-render-util0 libxcb-image0 libxcb-keysyms1 \
            libxcb-icccm4 libxcb-sync1 libxcb-xfixes0 libxcb-xkb1 \
            libqt6gui6 libqt6widgets6 libqt6network6 libqt6waylandclient6 \
            qb6-wayland qt6-wayland qml6-module-qtquick qml6-module-qtqml-workerscript \
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
      # PAM (broker needs python-pam)
      case "$DISTRO" in
        ubuntu|debian|linuxmint|pop) pkg_install python3-pam || pkg_install libpam0g-dev || true;;
        fedora|centos|rhel|rocky|almalinux) pkg_install python-pam pam-devel || true;;
        arch|manjaro) pkg_install python-pam || true;;
        alpine) pkg_install py3-pam || true;;
      esac
      ;;
    macos)
      if ! need_cmd brew; then
        err "Homebrew not found. Install it from https://brew.sh first."
        exit 1
      fi
      brew install python@3.12 qt openssh xclip || true
      ;;
    windows)
      log "Windows: install Python 3.12+ from python.org and ensure 'Add to PATH' is checked."
      log "Qt runtime is bundled with PySide6; no extra system install needed."
      ;;
  esac
}

# ---- source retrieval ------------------------------------------------------
fetch_release_tarball() {
  log "Downloading ssh-remote-desktop release tarball…"
  mkdir -p "$TARGET_DIR"
  local tarball
  tarball=$(mktemp -t srd-XXXXXX.tar.gz)
  local url
  url=$(curl -fsSL "$REPO_RAW/VERSION" 2>/dev/null | tr -d '[:space:]' || true)
  if [[ -z "$url" ]]; then
    url="$REPO_URL/archive/refs/heads/main.tar.gz"
  else
    url="$REPO_URL/archive/refs/tags/v${url}.tar.gz"
  fi
  curl -fsSL "$url" -o "$tarball"
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

# ---- venv + python deps ----------------------------------------------------
setup_venv() {
  local py; py=$(find_python)
  log "Using Python: $($py --version 2>&1) ($py)"
  log "Creating virtual environment…"
  "$py" -m venv "$TARGET_DIR/.venv"
  # shellcheck disable=SC1091
  source "$TARGET_DIR/.venv/bin/activate"
  pip install --upgrade pip wheel setuptools
  log "Installing Python dependencies…"
  pip install -r "$TARGET_DIR/requirements.txt"
  # pip install the project itself in dev mode for the `server` / `client` /
  # `common` / `crypto` importable modules when running from source.
  pip install -e "$TARGET_DIR"
}

# ---- optional Nuitka build -------------------------------------------------
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
  if [[ "$OS" == "windows" ]]; then
    bash "$TARGET_DIR/build_client_windows.sh" || log "client build failed"
    bash "$TARGET_DIR/build_server_linux.sh" || log "server build skipped (Linux-only)"
  elif [[ "$OS" == "linux" ]]; then
    bash "$TARGET_DIR/build_client_linux.sh" || log "client build failed"
    bash "$TARGET_DIR/build_server_linux.sh" || log "server build failed"
  else
    log "macOS: no preconfigured build script — pip-installed packages only."
  fi
}

# ---- post-install wiring ---------------------------------------------------
post_install() {
  mkdir -p "$HOME/.config/ssh-remote-desktop"
  # Symlink into ~/.local/bin if writable.
  if [[ -d "$HOME/.local/bin" ]] || mkdir -p "$HOME/.local/bin"; then
    [[ -L "$HOME/.local/bin/rd-server" ]] || \
      ln -sf "$TARGET_DIR/.venv/bin/rd-server" "$HOME/.local/bin/rd-server" 2>/dev/null || true
    [[ -L "$HOME/.local/bin/rd-client" ]] || \
      ln -sf "$TARGET_DIR/.venv/bin/rd-client" "$HOME/.local/bin/rd-client" 2>/dev/null || true
  fi
}

# ---- main flow -------------------------------------------------------------
main() {
  detect_os
  detect_distro
  log "Detected: $OS / $DISTRO"
  log "Target directory: $TARGET_DIR"
  log "Mode: $MODE (build=$BUILD)"

  install_system_deps || log "Some system packages failed to install; continuing."

  case "$MODE" in
    dev|both) clone_dev;;
    run) fetch_release_tarball;;
  esac

  setup_venv
  maybe_build
  post_install

  log "Installation complete."
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
