#!/usr/bin/env bash
# ==========================================
# Telegram Bot Music
# Created by FacuSecX
# https://github.com/FacuSecX/
# ==========================================

# Debian 12/13: install a loopback-only WARP SOCKS5 proxy for Music Bot.
# Run manually as root: sudo bash deploy/install-warp-proxy.sh
# This script is an installation artifact; generating it does not run it.
#
# Official package/key instructions: https://pkg.cloudflareclient.com/
# CLI/protocol reference: https://developers.cloudflare.com/warp-client/get-started/linux/
# Verified command interface: cloudflare-warp 2026.8.2115.
# WARP is configured in proxy mode BEFORE connect; no global VPN mode is used.
# The existing device registration is preserved and never printed.
# The bot's configuration and systemd unit are not modified here.
set -Eeuo pipefail
umask 077
export DEBIAN_FRONTEND=noninteractive
export LC_ALL=C

step='preflight'
work_dir=''
proxy_configured=0

cleanup() {
  if [[ -n "$work_dir" && "$work_dir" == /tmp/musicbot-warp-install.* && -d "$work_dir" ]]; then
    rm -f -- "$work_dir/before-routes.json" "$work_dir/after-routes.json" \
      "$work_dir/pubkey.gpg" "$work_dir/keyring.gpg"
    rmdir -- "$work_dir" 2>/dev/null || true
  fi
}

abort() {
  trap - ERR
  # Fail closed if setup or verification fails after selecting proxy mode.
  # No existing registration, routing rule, or route is deleted manually.
  if [[ "$proxy_configured" == 1 ]] && command -v warp-cli >/dev/null 2>&1; then
    timeout 15 warp-cli --accept-tos disconnect >/dev/null 2>&1 || true
  fi
  printf 'ERROR: %s\n' "$1" >&2
  exit 1
}
trap cleanup EXIT
trap 'abort "Falló el paso: $step. No se imprimen datos privados del cliente."' ERR

[[ "${EUID}" == 0 ]] || abort 'Ejecutá este archivo con sudo o como root.'
[[ -f /etc/os-release ]] || abort 'No se pudo identificar Debian.'
# shellcheck disable=SC1091
. /etc/os-release
[[ "${ID:-}" == debian && "${VERSION_ID:-}" =~ ^(12|13)$ && "${VERSION_CODENAME:-}" =~ ^(bookworm|trixie)$ ]] || \
  abort 'Este instalador está preparado para Debian 12 (bookworm) o Debian 13 (trixie).'
command -v systemctl >/dev/null 2>&1 || abort 'Se requiere systemd.'

step='dependencias de instalación'
apt-get update -qq >/dev/null
apt-get install -y -qq ca-certificates curl gnupg iproute2 python3 >/dev/null
work_dir="$(mktemp -d /tmp/musicbot-warp-install.XXXXXX)"

snapshot_routes() {
  # Include defaults in ALL tables so an unintended global tunnel cannot hide
  # behind policy routing. Expiry countdowns are transient, not route changes.
  python3 - "$1" <<'PY'
import json
from pathlib import Path
import subprocess
import sys
snapshot = {}
for family in ('-4', '-6'):
    payload = subprocess.check_output(
        ['ip', '-j', family, 'route', 'show', 'table', 'all', 'default'],
        text=True)
    routes = json.loads(payload)
    for route in routes:
        route.pop('expires', None)
    snapshot[family] = sorted(routes, key=lambda value: json.dumps(value, sort_keys=True))
Path(sys.argv[1]).write_text(json.dumps(snapshot, sort_keys=True), encoding='utf-8')
PY
}
snapshot_routes "$work_dir/before-routes.json"

step='cliente oficial de Cloudflare'
if command -v warp-cli >/dev/null 2>&1; then
  # Preserve an installed official client and its existing APT source/key path.
  # Adding the same repository with another Signed-By path breaks APT updates.
  printf 'Cliente WARP: existente, conservado.\n'
else
  official_source_exists() {
    python3 - <<'PYAPT'
from pathlib import Path
import re
import sys
pattern = re.compile(r'https://pkg[.]cloudflareclient[.]com(?:/|\s|$)')
files = [Path('/etc/apt/sources.list')]
files += sorted(Path('/etc/apt/sources.list.d').glob('*.list'))
files += sorted(Path('/etc/apt/sources.list.d').glob('*.sources'))
for source in files:
    if not source.is_file():
        continue
    text = source.read_text(encoding='utf-8', errors='replace')
    if source.suffix != '.sources':
        for line in text.splitlines():
            line = line.split('#', 1)[0].strip()
            if line.startswith('deb ') and pattern.search(line):
                sys.exit(0)
    else:
        # Read active Deb822 stanzas, including continuation lines in URIs.
        for paragraph in re.split(r'\n\s*\n', text):
            fields = {}
            current = None
            for line in paragraph.splitlines():
                if not line.strip() or line.lstrip().startswith('#'):
                    continue
                if line[:1].isspace() and current:
                    fields[current] += ' ' + line.strip()
                elif ':' in line:
                    current, value = line.split(':', 1)
                    current = current.lower()
                    fields[current] = value.strip()
            if (fields.get('enabled', 'yes').lower() != 'no'
                    and 'deb' in fields.get('types', '').split()
                    and pattern.search(fields.get('uris', ''))):
                sys.exit(0)
sys.exit(1)
PYAPT
  }
  if official_source_exists; then
    printf 'Repositorio oficial: existente, conservado.\n'
  else
    step='repositorio oficial de Cloudflare'
    curl --fail --silent --show-error --location --proto '=https' --proto-redir '=https' --tlsv1.2 \
      https://pkg.cloudflareclient.com/pubkey.gpg --output "$work_dir/pubkey.gpg"
    gpg --batch --yes --dearmor --output "$work_dir/keyring.gpg" "$work_dir/pubkey.gpg"
    install -o root -g root -m 0644 "$work_dir/keyring.gpg" \
      /usr/share/keyrings/cloudflare-warp-archive-keyring.gpg
    printf '%s\n' \
      "deb [signed-by=/usr/share/keyrings/cloudflare-warp-archive-keyring.gpg] https://pkg.cloudflareclient.com/ $VERSION_CODENAME main" \
      > /etc/apt/sources.list.d/cloudflare-client.list
    chmod 0644 /etc/apt/sources.list.d/cloudflare-client.list
  fi
  step='instalación de WARP'
  apt-get update -qq >/dev/null
  apt-get install -y -qq cloudflare-warp >/dev/null
  printf 'Paquete oficial: instalado.\n'
fi

step='daemon de WARP'
# enable --now is idempotent and does not restart an already active daemon.
systemctl enable --now warp-svc.service >/dev/null 2>&1
systemctl is-active --quiet warp-svc.service || abort 'El daemon WARP no está activo.'

step='registro del dispositivo'
# registration show can include account/device identifiers: capture privately.
registration_present=0
registration_missing=0
for attempt in {1..15}; do
  if registration_state="$(timeout 15 warp-cli --accept-tos registration show 2>&1)"; then
    registration_present=1
    break
  fi
  case "$registration_state" in
    *"Missing registration"*|*"missing registration"*|*"not registered"*|*"Not registered"*|*"No registration"*|*"no registration"*)
      registration_missing=1
      break
      ;;
  esac
  sleep 1
done
if [[ "$registration_present" == 1 ]]; then
  registration_status='existente, conservado'
else
  # Register only on an explicit missing-registration response. Unknown failures
  # must be reviewed manually; never replace an existing registration blindly.
  case "$registration_missing" in
    1)
      timeout 45 warp-cli --accept-tos registration new >/dev/null 2>&1
      registration_status='nuevo'
      ;;
    *)
      unset registration_state
      abort 'No se pudo comprobar el registro. Revisá el cliente manualmente; no se creó ni reemplazó ningún registro.'
      ;;
  esac
fi
unset registration_state
printf 'Registro: %s.\n' "$registration_status"

step='modo proxy'
# Retry only while the newly started daemon opens its control socket.
# Never run `connect` before this command has succeeded.
for attempt in {1..15}; do
  if timeout 15 warp-cli --accept-tos mode proxy >/dev/null 2>&1; then
    proxy_configured=1
    break
  fi
  sleep 1
done
[[ "$proxy_configured" == 1 ]] || abort 'No se pudo seleccionar el modo proxy.'

step='puerto local y protocolo'
timeout 15 warp-cli --accept-tos proxy port 40000 >/dev/null 2>&1
timeout 15 warp-cli --accept-tos tunnel protocol set MASQUE >/dev/null 2>&1
# Reassert the mode immediately before connecting, including after registration.
timeout 15 warp-cli --accept-tos mode proxy >/dev/null 2>&1
timeout 45 warp-cli --accept-tos connect >/dev/null 2>&1

step='listener privado'
listener_ready=0
for attempt in {1..30}; do
  if python3 - <<'PY'
import subprocess
import sys
rows = subprocess.check_output(
    ['ss', '-H', '-ltnp', 'sport = :40000'], text=True).splitlines()
# Require the WARP process and exact loopback address. Reject public or IPv6
# wildcard listeners instead of silently exposing an unauthenticated proxy.
ok = bool(rows) and all(
    len(row.split()) >= 4 and row.split()[3] == '127.0.0.1:40000'
    and '"warp-svc"' in row for row in rows)
sys.exit(0 if ok else 1)
PY
  then
    listener_ready=1
    break
  fi
  sleep 1
done
[[ "$listener_ready" == 1 ]] || abort 'No se confirmó un listener WARP exclusivamente en 127.0.0.1:40000.'

step='rutas IPv4 e IPv6'
snapshot_routes "$work_dir/after-routes.json"
cmp -s "$work_dir/before-routes.json" "$work_dir/after-routes.json" || \
  abort 'Las rutas predeterminadas cambiaron. WARP se desconecta para revisión; no se aplican cambios globales manuales.'

step='conexión del proxy'
# The trace includes the public IP; keep it in memory and print only WARP status.
trace="$(curl --fail --silent --show-error --max-time 30 \
  --socks5-hostname 127.0.0.1:40000 https://www.cloudflare.com/cdn-cgi/trace)"
printf '%s\n' "$trace" | grep -Eq '^warp=(on|plus)$' || \
  abort 'El proxy escucha, pero no se confirmó el túnel WARP.'
unset trace

printf 'Servicio: habilitado y activo.\n'
printf 'Modo: proxy local; protocolo MASQUE.\n'
printf 'Listener: 127.0.0.1:40000, propiedad de WARP.\n'
printf 'Rutas predeterminadas IPv4/IPv6: sin cambios.\n'
printf 'Comprobación WARP: correcta.\n'
printf 'Endpoint para la aplicación: socks5h://127.0.0.1:40000\n'
printf 'El bot conserva su configuración actual.\n'
