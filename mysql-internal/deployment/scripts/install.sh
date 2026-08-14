#!/usr/bin/env bash
set -euo pipefail

start_service=false
if [[ "${1:-}" == "--start" ]]; then
  start_service=true
elif [[ $# -gt 0 ]]; then
  echo "usage: sudo $0 [--start]" >&2
  exit 2
fi

if [[ $EUID -ne 0 ]]; then
  echo "install.sh must run as root" >&2
  exit 1
fi

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "$script_dir/../../.." && pwd)"
install_root=/opt/azure-mysql-monitoring
config_root=/etc/azure-mysql-monitoring
state_root=/var/lib/azure-mysql-monitoring

if ! id mysqlmon >/dev/null 2>&1; then
  useradd --system --no-create-home --shell /usr/sbin/nologin mysqlmon
fi

apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y ca-certificates python3 python3-venv

install -d -o root -g root -m 0755 "$install_root/collector" "$install_root/sql"
install -d -o root -g mysqlmon -m 0750 "$config_root"
install -d -o mysqlmon -g mysqlmon -m 0750 \
  "$state_root" "$state_root/cursors" "$state_root/spool"

cp -a "$repo_root/mysql-internal/collector/." "$install_root/collector/"
cp -a "$repo_root/mysql-internal/sql/." "$install_root/sql/"

python3 -m venv "$install_root/venv"
"$install_root/venv/bin/pip" install --disable-pip-version-check --upgrade pip
"$install_root/venv/bin/pip" install --disable-pip-version-check \
  -r "$install_root/collector/requirements-adx.txt"

install -o root -g root -m 0644 \
  "$repo_root/mysql-internal/deployment/systemd/azure-mysql-monitoring.service" \
  /etc/systemd/system/azure-mysql-monitoring.service

if [[ ! -e "$config_root/monitoring.yaml" ]]; then
  install -o root -g mysqlmon -m 0640 \
    "$repo_root/mysql-internal/deployment/config/monitoring.example.yaml" \
    "$config_root/monitoring.yaml"
fi
if [[ ! -e "$config_root/collector.env" ]]; then
  install -o root -g mysqlmon -m 0640 \
    "$repo_root/mysql-internal/deployment/config/collector.env.example" \
    "$config_root/collector.env"
fi

systemctl daemon-reload
if $start_service; then
  systemctl enable --now azure-mysql-monitoring.service
fi

echo "Installed. Edit $config_root/monitoring.yaml and collector.env, then run:"
echo "  sudo systemctl enable --now azure-mysql-monitoring.service"
