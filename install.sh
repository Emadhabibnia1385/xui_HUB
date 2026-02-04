#!/bin/bash
set -Eeuo pipefail

REPO="https://github.com/Emadhabibnia1385/xui_HUB.git"
DIR="/opt/xui_HUB"
SERVICE="xuihub"

R='\033[31m'; G='\033[32m'; Y='\033[33m'; C='\033[36m'; M='\033[35m'; B='\033[1m'; N='\033[0m'

header() {
  clear 2>/dev/null || true
  echo -e "${C}╔════════════════════════════════════════════════════════════════════════╗${N}"
  echo -e "${C}║${N}                                                                        ${C}║${N}"
  echo -e "${C}║${N}  ${B}${M}██╗  ██╗██╗   ██╗██╗     ██╗  ██╗██╗   ██╗██████╗                   ${N}  ${C}║${N}"
  echo -e "${C}║${N}  ${B}${M}╚██╗██╔╝██║   ██║██║     ██║  ██║██║   ██║██╔══██╗                  ${N}  ${C}║${N}"
  echo -e "${C}║${N}  ${B}${M} ╚███╔╝ ██║   ██║██║     ███████║██║   ██║██████╔╝                  ${N}  ${C}║${N}"
  echo -e "${C}║${N}  ${B}${M} ██╔██╗ ██║   ██║██║     ██╔══██║██║   ██║██╔══██╗                  ${N}  ${C}║${N}"
  echo -e "${C}║${N}  ${B}${M}██╔╝ ██╗╚██████╔╝███████╗██║  ██║╚██████╔╝██████╔╝                  ${N}  ${C}║${N}"
  echo -e "${C}║${N}  ${B}${M}╚═╝  ╚═╝ ╚═════╝ ╚══════╝╚═╝  ╚═╝ ╚═════╝ ╚═════╝                   ${N}  ${C}║${N}"
  echo -e "${C}║${N}                                                                        ${C}║${N}"
  echo -e "${C}║${N}              ${B}🤖 xui_HUB - 3x-ui Panel Manager Bot${N}                      ${C}║${N}"
  echo -e "${C}║${N}                                                                        ${C}║${N}"
  echo -e "${C}║${N}                 ${B}Developer:${N} t.me/EmadHabibnia                           ${C}║${N}"
  echo -e "${C}║${N}                 ${B}Repo:${N} github.com/Emadhabibnia1385/xui_HUB              ${C}║${N}"
  echo -e "${C}║${N}                                                                        ${C}║${N}"
  echo -e "${C}╚════════════════════════════════════════════════════════════════════════╝${N}"
  echo ""
}

err() { echo -e "${R}✗ $*${N}" >&2; exit 1; }
ok()  { echo -e "${G}✓ $*${N}"; }
info(){ echo -e "${Y}➜ $*${N}"; }

on_error() {
  echo -e "${R}✗ Error on line ${BASH_LINENO[0]}${N}"
}
trap on_error ERR

check_root() {
  if [[ $EUID -ne 0 ]]; then
    err "Please run with sudo or as root"
  fi
}

ensure_safe_cwd() {
  cd / 2>/dev/null || true
}

install_prereqs() {
  info "Installing prerequisites..."
  apt-get update -y
  apt-get install -y git python3 python3-venv python3-pip curl rsync
}

clone_or_update_repo() {
  info "Downloading xui_HUB..."

  mkdir -p "$DIR"

  if [[ -d "$DIR/.git" ]]; then
    info "Repository exists. Updating..."
    cd "$DIR"
    git fetch --all --prune
    git reset --hard origin/main
  else
    rm -rf "$DIR"
    mkdir -p "$DIR"
    git clone "$REPO" "$DIR"
    cd "$DIR"
  fi

  [[ -f "$DIR/bot.py" ]] || err "bot.py not found after download. Repo content missing?"
  [[ -f "$DIR/requirements.txt" ]] || err "requirements.txt not found after download."
}

setup_venv() {
  info "Setting up Python environment..."
  if [[ ! -d "$DIR/venv" ]]; then
    python3 -m venv "$DIR/venv"
  fi

  "$DIR/venv/bin/pip" install --upgrade pip wheel
  "$DIR/venv/bin/pip" install -r "$DIR/requirements.txt"
}

configure_env() {
  echo ""
  info "Bot Configuration"
  read -r -p "Enter your Telegram Bot TOKEN: " BOT_TOKEN
  [[ -n "${BOT_TOKEN// }" ]] || err "TOKEN cannot be empty"

  read -r -p "Enter your Admin Chat ID (numeric): " ADMIN_ID
  [[ "$ADMIN_ID" =~ ^-?[0-9]+$ ]] || err "ADMIN_CHAT_ID must be numeric"

  cat > "$DIR/.env" << EOF
TOKEN=$BOT_TOKEN
ADMIN_CHAT_ID=$ADMIN_ID
EOF
  chmod 600 "$DIR/.env"
}

create_systemd_service() {
  info "Creating systemd service..."
  cat > "/etc/systemd/system/$SERVICE.service" << EOF
[Unit]
Description=xui_HUB Telegram Bot
After=network.target

[Service]
Type=simple
WorkingDirectory=$DIR
EnvironmentFile=$DIR/.env
ExecStart=$DIR/venv/bin/python $DIR/bot.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

  systemctl daemon-reload
  systemctl enable "$SERVICE" >/dev/null 2>&1 || true
}

start_service() {
  systemctl restart "$SERVICE"
  ok "xui_HUB installed successfully!"
  echo ""
  systemctl status "$SERVICE" --no-pager -l || true
}

install_bot() {
  ensure_safe_cwd
  install_prereqs
  clone_or_update_repo
  setup_venv
  configure_env
  create_systemd_service
  start_service
}

update_bot() {
  ensure_safe_cwd
  [[ -d "$DIR/.git" ]] || err "Not installed. Please run Install first."
  info "Updating xui_HUB..."
  clone_or_update_repo
  setup_venv
  systemctl restart "$SERVICE"
  ok "Updated successfully!"
}

edit_config() {
  ensure_safe_cwd
  [[ -f "$DIR/.env" ]] || err "Config file not found. Please install first."
  nano "$DIR/.env"
  systemctl restart "$SERVICE"
  ok "Configuration updated and bot restarted!"
}

remove_bot() {
  ensure_safe_cwd
  read -r -p "Are you sure you want to remove xui_HUB? (yes/no): " confirm
  if [[ "$confirm" != "yes" ]]; then
    info "Cancelled"
    return
  fi

  systemctl stop "$SERVICE" 2>/dev/null || true
  systemctl disable "$SERVICE" 2>/dev/null || true
  rm -f "/etc/systemd/system/$SERVICE.service"
  systemctl daemon-reload
  rm -rf "$DIR"
  ok "xui_HUB removed completely"
}

show_menu() {
  echo -e "${B}1)${N} Install / Reinstall"
  echo -e "${B}2)${N} Update from GitHub"
  echo -e "${B}3)${N} Edit Config (.env)"
  echo -e "${B}4)${N} Start Bot"
  echo -e "${B}5)${N} Stop Bot"
  echo -e "${B}6)${N} Restart Bot"
  echo -e "${B}7)${N} View Live Logs"
  echo -e "${B}8)${N} Bot Status"
  echo -e "${B}9)${N} Uninstall"
  echo -e "${B}0)${N} Exit"
  echo ""
}

main() {
  check_root
  ensure_safe_cwd

  while true; do
    header
    show_menu

    read -r -p "Select option [0-9]: " choice

    case "${choice:-}" in
      1)
        install_bot
        echo ""
        read -r -p "Press Enter to continue..."
        ;;
      2)
        update_bot
        echo ""
        read -r -p "Press Enter to continue..."
        ;;
      3)
        edit_config
        echo ""
        read -r -p "Press Enter to continue..."
        ;;
      4)
        systemctl start "$SERVICE"
        ok "Bot started"
        echo ""
        read -r -p "Press Enter to continue..."
        ;;
      5)
        systemctl stop "$SERVICE"
        ok "Bot stopped"
        echo ""
        read -r -p "Press Enter to continue..."
        ;;
      6)
        systemctl restart "$SERVICE"
        ok "Bot restarted"
        echo ""
        read -r -p "Press Enter to continue..."
        ;;
      7)
        echo -e "${Y}Press Ctrl+C to exit logs${N}"
        sleep 1
        journalctl -u "$SERVICE" -f
        ;;
      8)
        systemctl status "$SERVICE" --no-pager -l
        echo ""
        read -r -p "Press Enter to continue..."
        ;;
      9)
        remove_bot
        echo ""
        read -r -p "Press Enter to continue..."
        ;;
      0)
        echo "Goodbye!"
        exit 0
        ;;
      *)
        echo -e "${R}Invalid option${N}"
        sleep 1
        ;;
    esac
  done
}

main
