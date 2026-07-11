#!/usr/bin/env bash
#
# install.sh -- install or update ServeLocal on a Raspberry Pi.
#
# Safe to re-run: if /var/lib/servelocal already exists, this updates the
# code in place WITHOUT touching your existing mealplanner.db (your meals
# and settings are preserved across updates).
#
# Usage:
#   sudo ./install.sh
#
# Optional overrides:
#   sudo INSTALL_DIR=/opt/servelocal SERVICE_USER=myuser ./install.sh
#
set -euo pipefail

INSTALL_DIR="${INSTALL_DIR:-/var/lib/servelocal}"
SERVICE_USER="${SERVICE_USER:-pi}"
SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

log()  { echo -e "\033[1;36m==>\033[0m $*"; }
warn() { echo -e "\033[1;33m!!\033[0m $*"; }

if [[ $EUID -ne 0 ]]; then
    echo "Please run this with sudo: sudo ./install.sh" >&2
    exit 1
fi

if ! id "$SERVICE_USER" &>/dev/null; then
    echo "User '$SERVICE_USER' does not exist. Create it first, or set SERVICE_USER=<existing user>." >&2
    exit 1
fi

IS_UPDATE=false
if [[ -d "$INSTALL_DIR" ]]; then
    IS_UPDATE=true
    log "Existing install found at $INSTALL_DIR -- updating in place."
else
    log "Fresh install into $INSTALL_DIR."
fi

# ---------------------------------------------------------- prerequisites --

NEED_APT=false
for pkg in python3-venv python3-pip fonts-dejavu-core; do
    dpkg -s "$pkg" &>/dev/null || NEED_APT=true
done
if $NEED_APT; then
    log "Installing OS packages (python3-venv, python3-pip, fonts-dejavu-core)…"
    apt-get update -qq
    apt-get install -y python3-venv python3-pip fonts-dejavu-core
else
    log "OS packages already present, skipping apt."
fi

# Raspberry Pi OS normally puts the default user in these groups already;
# this is just a defensive no-op if so, so SPI/GPIO access works without root.
for grp in gpio spi; do
    if getent group "$grp" &>/dev/null; then
        usermod -aG "$grp" "$SERVICE_USER" || true
    fi
done

# ------------------------------------------------------- stop for update --

if $IS_UPDATE; then
    log "Stopping services before updating files…"
    systemctl stop servelocal_planner.service 2>/dev/null || true
    systemctl stop servelocal_display.service 2>/dev/null || true
fi

# --------------------------------------------------------------- deploy --

log "Copying application files to $INSTALL_DIR…"
mkdir -p "$INSTALL_DIR"
rsync -a --delete \
    --exclude='.git' \
    --exclude='.gitignore' \
    --exclude='mealplanner.db' \
    --exclude='__pycache__' \
    --exclude='venv' \
    --exclude='install.sh' \
    --exclude='*.service' \
    "$SOURCE_DIR"/ "$INSTALL_DIR"/

chown -R "$SERVICE_USER":"$SERVICE_USER" "$INSTALL_DIR"

# ----------------------------------------------------------- Python env --

if [[ ! -d "$INSTALL_DIR/venv" ]]; then
    log "Creating Python virtual environment…"
    sudo -u "$SERVICE_USER" python3 -m venv "$INSTALL_DIR/venv"
fi

log "Installing/updating Python dependencies…"
sudo -u "$SERVICE_USER" "$INSTALL_DIR/venv/bin/pip" install --quiet --upgrade pip
sudo -u "$SERVICE_USER" "$INSTALL_DIR/venv/bin/pip" install --quiet -r "$INSTALL_DIR/requirements.txt"

# ------------------------------------------------------------- systemd --

log "Installing systemd unit files…"
for svc in servelocal_planner.service servelocal_display.service; do
    # substitute the actual install dir / user into the checked-in unit
    # files, in case INSTALL_DIR or SERVICE_USER were overridden
    sed -e "s#/var/lib/servelocal#${INSTALL_DIR}#g" \
        -e "s#^User=pi#User=${SERVICE_USER}#" \
        "$SOURCE_DIR/$svc" > "/etc/systemd/system/$svc"
done

log "Reloading systemd…"
systemctl daemon-reload

log "Enabling and starting services…"
systemctl enable --now servelocal_display.service
systemctl enable --now servelocal_planner.service

# ------------------------------------------------------------- summary --

echo
log "Done. $( $IS_UPDATE && echo 'Updated' || echo 'Installed' ) at $INSTALL_DIR (running as $SERVICE_USER)."
echo "   Web UI:      http://$(hostname -I | awk '{print $1}'):8080"
echo "   Status:      systemctl status servelocal_planner servelocal_display"
echo "   Logs:        journalctl -u servelocal_planner -f"
echo "                journalctl -u servelocal_display -f"
