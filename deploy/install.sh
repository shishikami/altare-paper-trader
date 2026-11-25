#!/usr/bin/env bash
# PREPARATION ONLY in this PR. Run intentionally on server only after separate approval.
# Ubuntu/Debian systemd; stages files but does NOT initialize/enable/start anything.
set -euo pipefail
[[ ${1:-} == --stage-only ]] || { echo 'Usage: sudo bash deploy/install.sh --stage-only RELEASE_ID' >&2; exit 2; }
[[ $EUID -eq 0 ]] || { echo 'staging needs root for isolated account/paths' >&2; exit 2; }
release=${2:-}
[[ $release =~ ^[a-zA-Z0-9._-]{7,64}$ && $release != *..* ]] || { echo 'invalid immutable release ID' >&2; exit 2; }
/usr/bin/python3 -c 'import sys; assert sys.version_info >= (3, 11)'
command -v systemctl >/dev/null
source_root=$(cd -- "$(dirname -- "$0")/.." && pwd)
target=/opt/paper-trader/releases/$release
[[ ! -e $target && ! -e /etc/systemd/system/paper-trader.service && ! -L /opt/paper-trader/current && ! -e /opt/paper-trader/current ]] || {
  echo 'Existing installation detected. Refuse overwrite; use documented reviewed upgrade.' >&2; exit 2;
}
# Refuse adopting any preexisting account or deployment directories blindly.
! getent passwd papertrader >/dev/null || { echo 'papertrader account already exists; manual review required' >&2; exit 2; }
for path in /opt/paper-trader /etc/paper-trader /var/lib/paper-trader; do
  [[ ! -e $path ]] || { echo "Existing path $path; manual review required" >&2; exit 2; }
done
useradd --system --user-group --home-dir /var/lib/paper-trader --no-create-home --shell /usr/sbin/nologin papertrader
install -d -m 0755 /opt/paper-trader/releases "$target" /etc/paper-trader
install -d -o papertrader -g papertrader -m 0700 /var/lib/paper-trader
install -d -m 0755 "$target/papertrader"
# Explicit allowlist, not a copy of the repository or any existing account state.
for file in __init__.py __main__.py core.py feed.py; do
  install -m 0644 "$source_root/papertrader/$file" "$target/papertrader/$file"
done
install -m 0640 -o root -g papertrader "$source_root/config/example.json" /etc/paper-trader/config.json
install -m 0644 "$source_root/deploy/paper-trader.service" /etc/systemd/system/paper-trader.service
ln -s "$target" /opt/paper-trader/current
systemctl daemon-reload
printf '%s\n' 'Staged only. Parameters remain unconfirmed. No ledger initialized, no service enabled/started.'
