#!/usr/bin/env bash
# ==========================================
# Telegram Bot Music
# Created by FacuSecX
# https://github.com/FacuSecX/
# ==========================================

# Instalador de MusicBotTelegram para Debian 12/13 con systemd.
# Uso: sudo bash instalarbotmusica.sh
# Repositorio previsto: https://github.com/FacuSecX/MusicBotTelegram.git
set -Eeuo pipefail
set +x
umask 077
export DEBIAN_FRONTEND=noninteractive
export LC_ALL=C
step='comprobaciones iniciales'
staging=''
install_base=''
install_dir=''
service_name=''
service_user=''
proxy=''

fail() { printf 'ERROR: %s\n' "$1" >&2; exit 1; }
cleanup() {
  if [[ -n "$staging" && -d "$staging" && "$staging" == "$install_base"/.musicbot-clone.* ]]; then
    rm -rf -- "$staging"
  fi
}
trap cleanup EXIT
trap 'printf "Falló el paso: %s. El código, la configuración y los datos existentes se conservan.\n" "$step" >&2' ERR

if [[ "${1:-}" == --help || "${1:-}" == -h ]]; then
  cat <<'HELP'
Uso: sudo bash instalarbotmusica.sh
Instala MusicBotTelegram en Debian 12/13. Solicita servicio, WARP opcional,
Token, ChatID y una cuenta MEGA opcional para exportar playlists y favoritos.
Los datos rechazados se piden de nuevo sin descartar la instalación.
El Token se guarda solo en .env con permisos 600 y no se imprime.
La cuenta MEGA se guarda también en .env con permisos 600, nunca en los backups.

Opciones de administración/prueba explícitas:
  MUSICBOT_REPOSITORY   URL Git; predeterminada github.com/FacuSecX/MusicBotTelegram.git
  MUSICBOT_INSTALL_BASE Ruta base absoluta; predeterminada /opt/musicbot
  MUSICBOT_TEST_API_BASE=http://127.0.0.1:PUERTO
  MUSICBOT_ALLOW_TEST_API=1 permite solo ese servidor local para pruebas aisladas.
Las pruebas deben usar un servicio único y un token ficticio, nunca el bot activo.
HELP
  exit 0
fi
[[ $# == 0 ]] || fail 'No se reconocieron los argumentos. Usá --help.'
[[ "$EUID" == 0 ]] || fail 'Ejecutá el instalador con sudo o como root.'
[[ -r /etc/os-release ]] || fail 'No se pudo identificar el sistema.'
# shellcheck disable=SC1091
. /etc/os-release
[[ "${ID:-}" == debian && "${VERSION_ID:-}" =~ ^(12|13)$ ]] || fail 'Se requiere Debian 12 o Debian 13.'
command -v systemctl >/dev/null || fail 'Se requiere systemd para crear el servicio.'
systemctl show-environment >/dev/null 2>&1 || fail 'systemd debe estar ejecutándose; no se simula el arranque del servicio.'
install_base="${MUSICBOT_INSTALL_BASE:-/opt/musicbot}"
install_base="${install_base%/}"
[[ "$install_base" =~ ^/[A-Za-z0-9_./-]+$ && "$install_base" != / && ! "$install_base" =~ (^|/)\.\.?(/|$) ]] || fail 'La ruta base debe ser absoluta, sin espacios ni componentes relativos.'
[[ "$(realpath -m -- "$install_base")" == "$install_base" ]] || fail 'La ruta base no puede contener enlaces ni componentes relativos.'
repository="${MUSICBOT_REPOSITORY:-https://github.com/FacuSecX/MusicBotTelegram.git}"
[[ -n "$repository" && "$repository" != -* && "$repository" != *$'\n'* && "$repository" != *$'\r'* ]] || fail 'El repositorio configurado no es válido.'
api_root='https://api.telegram.org'
allow_test_api=0
if [[ -n "${MUSICBOT_TEST_API_BASE:-}" ]]; then
  [[ "${MUSICBOT_ALLOW_TEST_API:-}" == 1 ]] || fail 'El servidor local de prueba requiere MUSICBOT_ALLOW_TEST_API=1.'
  api_root="$MUSICBOT_TEST_API_BASE"
  allow_test_api=1
fi

step='dependencias del sistema'
printf 'Instalando dependencias de Debian %s…\n' "$VERSION_ID"
apt-get update -qq
apt-get install -y -qq git python3 python3-venv python3-pip ffmpeg curl ca-certificates gnupg iproute2 rsync
[[ "$(dpkg --print-architecture)" =~ ^(amd64|arm64)$ ]] || fail 'Este paquete está preparado para Debian de 64 bits amd64 o arm64.'
python3 -c 'import sys; assert sys.version_info >= (3,11)' || fail 'Se necesita Python 3.11 o superior.'

prompt() {
  local label="$1" secret="${2:-0}" value=''
  printf '%s' "$label" >&2
  if [[ "$secret" == 1 && -t 0 ]]; then
    IFS= read -r -s value || fail 'Entrada finalizada. Podés volver a ejecutar el instalador; los archivos se conservan.'
    printf '\n' >&2
  else
    IFS= read -r value || fail 'Entrada finalizada. Podés volver a ejecutar el instalador; los archivos se conservan.'
  fi
  REPLY="$value"
}
while true; do
  prompt 'Nombre del servicio [MusicBot]: '
  service_name="${REPLY:-MusicBot}"
  service_name="${service_name%.service}"
  if [[ "${service_name,,}" == musicbot ]]; then service_name='MusicBot'; fi
  if [[ "$service_name" =~ ^[A-Za-z][A-Za-z0-9_-]{0,47}$ ]]; then break; fi
  printf 'Usá un nombre corto con letras, números, guion o guion bajo.\n' >&2
done
slug="${service_name,,}"
service_user="mb_${slug//-/_}"
if (( ${#service_user} > 31 )); then
  suffix="$(printf '%s' "$service_user" | sha256sum)"
  service_user="${service_user:0:22}_${suffix:0:8}"
fi
install_dir="$install_base/$slug"
unit_path="/etc/systemd/system/$service_name.service"
marker="$install_dir/.musicbot-install.json"
managed=0
if [[ -f "$marker" && ! -L "$marker" && ! -L "$install_dir" ]]; then
  if python3 - "$marker" "$service_name" "$service_user" <<'PYMARK'
import json,sys
try:
 data=json.load(open(sys.argv[1]));assert data['service']==sys.argv[2] and data['user']==sys.argv[3] and data['installer']=='MusicBotTelegram'
except (OSError,ValueError,AssertionError,KeyError):raise SystemExit(1)
PYMARK
  then managed=1; fi
fi
load_state="$(systemctl show "$service_name.service" -p LoadState --value 2>/dev/null || true)"
if [[ -e "$install_dir" || -L "$install_dir" || -e "$unit_path" || ( "$load_state" != not-found && -n "$load_state" ) ]]; then
  if [[ "$managed" != 1 ]]; then
    fail 'Ese nombre o directorio pertenece a otra instalación. Elegí otro nombre; no se modificó el servicio existente.'
  fi
  prompt 'Ya existe esta instalación. ¿Actualizar conservando .env y data? [s/N]: '
  [[ "${REPLY,,}" == s || "${REPLY,,}" == si ]] || fail 'Actualización cancelada; el servicio existente se conserva.'
fi
need_user=0
if getent passwd "$service_user" >/dev/null; then
  [[ "$managed" == 1 ]] || fail 'El usuario del servicio ya existe y no pertenece a esta instalación. Elegí otro nombre.'
else
  need_user=1
fi
install -d -m 0755 -- "$install_base"
staging="$(mktemp -d "$install_base/.musicbot-clone.XXXXXX")"
step='clonación del repositorio'
printf 'Obteniendo el código del repositorio…\n'
if ! GIT_TERMINAL_PROMPT=0 git clone --quiet -- "$repository" "$staging/source"; then
  fail 'No se pudo clonar el repositorio. Verificá que exista y sea accesible; luego volvé a ejecutar el instalador. No se modificó ningún servicio.'
fi
for required in requirements.txt musicbot/bot.py deploy/installer_support.py deploy/install-warp-proxy.sh deploy/install-megacmd.sh deploy/mega_setup.py; do
  [[ -f "$staging/source/$required" ]] || fail 'El repositorio no tiene la estructura requerida de MusicBotTelegram.'
done
[[ ! -e "$staging/source/.env" ]] || fail 'El repositorio contiene un archivo .env privado; retiralo antes de instalar.'
if [[ "$need_user" == 1 ]]; then
  useradd --system --user-group --home-dir "$install_dir" --no-create-home --shell /usr/sbin/nologin "$service_user"
fi
install -d -o root -g "$service_user" -m 0750 -- "$install_dir"
if [[ ! -f "$marker" ]]; then
  python3 - "$marker" "$service_name" "$service_user" <<'PYMARK'
import json,sys
from pathlib import Path
Path(sys.argv[1]).write_text(json.dumps({'installer':'MusicBotTelegram','service':sys.argv[2],'user':sys.argv[3]}))
PYMARK
fi
# Stop only the confirmed managed service while replacing its application code.
if [[ "$managed" == 1 && "$load_state" != not-found ]]; then systemctl stop "$service_name.service"; fi
rsync -a --delete --exclude=.env --exclude=.venv --exclude=data --exclude=.musicbot-install.json "$staging/source/" "$install_dir/"
chown -R root:"$service_user" -- "$install_dir"
find "$install_dir" -type d -not -path "$install_dir/data*" -not -path "$install_dir/.venv*" -exec chmod 0750 {} +
find "$install_dir" -type f -not -path "$install_dir/data/*" -not -path "$install_dir/.venv/*" -exec chmod g+r {} +
install -d -o "$service_user" -g "$service_user" -m 0700 -- "$install_dir/data"
if [[ -d "$install_dir/.venv" ]]; then chown -R "$service_user:$service_user" -- "$install_dir/.venv"; fi
chown -R "$service_user:$service_user" -- "$install_dir/data"
if [[ -f "$install_dir/.env" ]]; then chown "$service_user:$service_user" "$install_dir/.env"; chmod 0600 "$install_dir/.env"; fi
cd "$install_dir"
step='entorno Python y dependencias del bot'
if [[ ! -x "$install_dir/.venv/bin/python" ]]; then
  install -d -o "$service_user" -g "$service_user" -m 0750 -- "$install_dir/.venv"
  runuser -u "$service_user" -- python3 -m venv "$install_dir/.venv"
fi
runuser -u "$service_user" -- "$install_dir/.venv/bin/python" -m pip install --disable-pip-version-check --no-cache-dir -q -r "$install_dir/requirements.txt"
runuser -u "$service_user" -- "$install_dir/.venv/bin/python" -c 'import telegram, yt_dlp, dotenv; print("Dependencias Python: correctas")'
runuser -u "$service_user" -- "$install_dir/.venv/bin/node" --version
ffmpeg -version | head -n 1 || true
support="$install_dir/deploy/installer_support.py"
api_options=(--api-root "$api_root")
if [[ "$allow_test_api" == 1 ]]; then api_options+=(--allow-test-api); fi
"$install_dir/.venv/bin/python" "$support" check-api "${api_options[@]}" >/dev/null

step='salida opcional de YouTube'
prompt '¿Usar el proxy local WARP para YouTube? [S/n]: '
if [[ "${REPLY,,}" != n && "${REPLY,,}" != no ]]; then
  if command -v warp-cli >/dev/null; then
    # Do not alter a shared daemon's mode or registration on existing servers.
    if ! "$install_dir/.venv/bin/python" "$support" check-warp; then
      fail 'El cliente WARP existente no está en modo proxy local seguro. Se conservó su configuración; repetí la instalación sin WARP o revisá ese cliente.'
    fi
  else
    bash "$install_dir/deploy/install-warp-proxy.sh"
  fi
  proxy='socks5h://127.0.0.1:40000'
fi
step='verificación del Token y ChatID'
printf 'Abrí tu bot en Telegram y tocá Iniciar antes de indicar el ChatID.\n'
while true; do
  prompt 'Token de BotFather (oculto): ' 1
  bot_token="$REPLY"
  prompt 'ChatID de tu chat privado: '
  chat_id="$REPLY"
  while true; do
    if printf '%s\0%s\0' "$bot_token" "$chat_id" | "$install_dir/.venv/bin/python" "$support" validate "${api_options[@]}"; then
      credentials_ok=1
      break
    else
      credential_status=$?
      credentials_ok=0
    fi
    if [[ "$credential_status" == 12 ]]; then
      prompt 'Conexión no disponible. Enter reintenta los mismos datos; C permite cambiarlos: '
      if [[ "${REPLY,,}" != c ]]; then continue; fi
    fi
    break
  done
  [[ "$credentials_ok" == 1 ]] && break
  printf 'La instalación se conserva. Volvé a introducir Token y ChatID.\n'
done
printf '%s\0%s\0' "$bot_token" "$chat_id" | "$install_dir/.venv/bin/python" "$support" write-env --destination "$install_dir/.env" --proxy "$proxy" "${api_options[@]}"
unset bot_token chat_id REPLY
chown "$service_user:$service_user" "$install_dir/.env"
chmod 0600 "$install_dir/.env"

step='cliente de backups MEGA'
bash "$install_dir/deploy/install-megacmd.sh"
mega_home="$install_dir/data/mega-home"
install -d -o "$service_user" -g "$service_user" -m 0700 -- "$mega_home"
prompt '¿Configurar MEGA para exportar backups? [S/n]: '
if [[ "${REPLY,,}" != n && "${REPLY,,}" != no ]]; then
  step='verificación privada de MEGA'
  mega_setup_private() {
    if [[ -n "$mega_code" ]]; then
      printf '%s\0%s\0%s\0' "$mega_email" "$mega_password" "$mega_code"
    else
      printf '%s\0%s\0' "$mega_email" "$mega_password"
    fi | runuser -u "$service_user" -- "$install_dir/.venv/bin/python" "$install_dir/deploy/mega_setup.py" setup --home "$mega_home"
  }
  while true; do
    prompt 'Correo de MEGA: '
    mega_email="$REPLY"
    prompt 'Contraseña de MEGA (oculta): ' 1
    mega_password="$REPLY"
    mega_code=''
    mega_ok=0
    while true; do
      if mega_result="$(mega_setup_private)"; then
        mega_ok=1
        break
      else
        mega_status=$?
      fi
      if [[ "$mega_status" == 11 ]]; then
        while true; do
          prompt 'Código de doble factor de MEGA (vacío para cambiar cuenta): ' 1
          mega_code="$REPLY"
          [[ -n "$mega_code" ]] || break 2
          [[ "$mega_code" =~ ^[0-9]{6}$ ]] && break
          printf 'El código debe tener seis números.\n'
        done
      elif [[ "$mega_status" == 12 ]]; then
        prompt 'Conexión MEGA no disponible. Enter reintenta; C permite cambiar los datos: '
        [[ "${REPLY,,}" != c ]] || break
      else
        break
      fi
    done
    [[ "$mega_ok" == 1 ]] && break
    unset mega_password mega_code mega_email REPLY
    printf 'MEGA no confirmó la cuenta. La instalación se conserva; volvé a introducir los datos.\n'
  done
  mega_remote_dir="$(printf '%s' "$mega_result" | "$install_dir/.venv/bin/python" -c 'import json,sys; data=json.load(sys.stdin); assert data.get("ok") is True; print(data["remote_dir"])')"
  printf '%s\0%s\0' "$mega_email" "$mega_password" | "$install_dir/.venv/bin/python" "$support" write-mega --destination "$install_dir/.env" \
    --mega-home data/mega-home --mega-remote-dir "$mega_remote_dir"
  chown "$service_user:$service_user" "$install_dir/.env"
  chmod 0600 "$install_dir/.env"
  unset mega_result mega_remote_dir mega_ok mega_status mega_password mega_code mega_email REPLY
else
  printf 'Se conserva cualquier sesión previa. Sin sesión, los enlaces públicos de importación siguen disponibles.\n'
fi
unset mega_home REPLY

step='servicio systemd'
warp_unit=''
warp_pre=''
if [[ -n "$proxy" ]]; then
  warp_unit=' warp-svc.service'
  warp_pre="ExecStartPre=$install_dir/.venv/bin/python $support check-warp"
fi
cat > "$unit_path" <<UNIT
# Managed by instalarbotmusica.sh (MusicBotTelegram)
[Unit]
Description=MusicBotTelegram - $service_name
After=network-online.target$warp_unit
Wants=network-online.target$warp_unit
StartLimitIntervalSec=300
StartLimitBurst=5

[Service]
Type=simple
User=$service_user
Group=$service_user
WorkingDirectory=$install_dir
Environment=PATH=$install_dir/.venv/bin:/usr/bin:/bin
Environment=PYTHONUNBUFFERED=1
Environment=PYTHONIOENCODING=utf-8
$warp_pre
ExecStart=$install_dir/.venv/bin/python -m musicbot.bot
Restart=on-failure
RestartSec=10
TimeoutStopSec=30
KillMode=control-group
UMask=0077
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths=$install_dir/data
MemoryMax=768M
CPUQuota=80%
# MEGAcmd keeps SDK worker threads in addition to Python/Node/FFmpeg.
TasksMax=128

[Install]
WantedBy=multi-user.target
UNIT
chmod 0644 "$unit_path"
systemd-analyze verify "$unit_path"
systemctl daemon-reload
systemctl enable "$service_name.service" >/dev/null
systemctl restart "$service_name.service"
sleep 3
systemctl is-active --quiet "$service_name.service" || fail 'El servicio no quedó activo. Los archivos se conservan; consultá journalctl para ese servicio.'
printf '\nInstalación completada.\nServicio: %s.service\nDirectorio: %s\nConfiguración privada: %s/.env (600)\n' "$service_name" "$install_dir" "$install_dir"
printf 'Estado: activo y habilitado para iniciar con Debian.\n'
printf 'Administración: systemctl status %s.service\n' "$service_name"
printf 'Las restricciones de YouTube dependen también de la salida de red y no se eliminan por instalar el bot.\n'
