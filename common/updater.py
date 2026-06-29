import os
import subprocess
import sys
import logging

log = logging.getLogger("rd.updater")

INSTALL_URL = "https://raw.githubusercontent.com/hirokyserega-web/ssh-remote-desktop/main/scripts/install.sh"

def run_update():
    """Run the universal installer to update the application."""
    print("Checking for updates and running installer...")
    
    # Check if we have curl or wget
    has_curl = subprocess.run(["command", "-v", "curl"], capture_output=True, shell=True).returncode == 0
    
    if has_curl:
        cmd = f"curl -fsSL {INSTALL_URL} | bash"
    else:
        # Fallback to wget
        cmd = f"wget -qO- {INSTALL_URL} | bash"
    
    # If we are root, we can run it directly. 
    # If not, the installer might prompt for sudo anyway for system deps,
    # but the binaries usually go to ~/.local/bin if not root.
    # However, if rd-server is installed in /opt or /usr/local/bin, 
    # we might need sudo.
    
    # We use 'bash -s --' to pass any additional arguments if needed in the future.
    try:
        # We use shell=True to allow the pipe.
        # We don't use sudo here automatically to avoid forcing it if not needed,
        # the install.sh script handles sudo internally where required.
        subprocess.run(cmd, shell=True, check=True)
        print("\nUpdate completed successfully.")
        return 0
    except subprocess.CalledProcessError as e:
        print(f"\nError during update: {e}", file=sys.stderr)
        return 1