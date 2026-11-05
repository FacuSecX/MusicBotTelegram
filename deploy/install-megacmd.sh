#!/usr/bin/env bash
# ==========================================
# Telegram Bot Music
# Created by FacuSecX
# https://github.com/FacuSecX/
# ==========================================

# Install the official signed MEGAcmd package for Debian 12/13.
# No account is opened here and existing MEGA sessions are not changed.
set -Eeuo pipefail
set +x
umask 077
export DEBIAN_FRONTEND=noninteractive
export LC_ALL=C

fail() { printf 'ERROR: %s\n' "$1" >&2; exit 1; }
[[ "$EUID" == 0 ]] || fail 'Ejecutá la instalación de MEGAcmd como root.'
[[ -r /etc/os-release ]] || fail 'No se pudo identificar Debian.'
# shellcheck disable=SC1091
. /etc/os-release
[[ "${ID:-}" == debian && "${VERSION_ID:-}" =~ ^(12|13)$ ]] || fail 'MEGAcmd requiere Debian 12 o 13 en este instalador.'
architecture="$(dpkg --print-architecture)"
[[ "$architecture" =~ ^(amd64|arm64)$ ]] || fail 'La arquitectura de MEGAcmd debe ser amd64 o arm64.'

complete=1
for command in mega-cmd mega-put mega-export mega-get mega-whoami; do
  command -v "$command" >/dev/null 2>&1 || complete=0
done
if [[ "$complete" == 1 ]]; then
  printf 'MEGAcmd ya está disponible; se conserva su instalación.\n'
  exit 0
fi

apt-get install -y -qq ca-certificates curl gnupg
work_dir="$(mktemp -d /tmp/musicbot-megacmd.XXXXXX)"
cleanup() {
  if [[ -n "$work_dir" && "$work_dir" == /tmp/musicbot-megacmd.* && -d "$work_dir" && ! -L "$work_dir" ]]; then
    rm -rf -- "$work_dir"
  fi
}
trap cleanup EXIT
chmod 0755 "$work_dir"
repository="https://mega.nz/linux/repo/Debian_$VERSION_ID"
curl --fail --silent --show-error --location --proto '=https' --tlsv1.2 \
  --connect-timeout 20 --max-time 60 "$repository/Release.key" -o "$work_dir/Release.key"
fingerprint="$(gpg --batch --show-keys --with-colons "$work_dir/Release.key" 2>/dev/null | awk -F: '$1=="fpr" {print $10; exit}')"
[[ "$fingerprint" == B01C811880480C854C73EC7E1A664B787094A482 ]] || fail 'La clave de firma del repositorio de MEGA no coincide con la esperada.'
gpg --batch --yes --dearmor --output "$work_dir/mega.gpg" "$work_dir/Release.key"
chmod 0644 "$work_dir/mega.gpg"
printf 'deb [arch=%s signed-by=%s] %s ./\n' "$architecture" "$work_dir/mega.gpg" "$repository" > "$work_dir/mega.list"
chmod 0644 "$work_dir/mega.list"
install -d -m 0755 "$work_dir/lists" "$work_dir/download"
if id _apt >/dev/null 2>&1; then chown _apt:root "$work_dir/download"; fi
apt_options=(-o "Dir::Etc::sourcelist=$work_dir/mega.list" -o 'Dir::Etc::sourceparts=-' -o "Dir::State::lists=$work_dir/lists")
# A temporary, separately signed repository avoids replacing an existing MEGA source.
apt-get "${apt_options[@]}" update -qq
(
  cd "$work_dir/download"
  apt-get "${apt_options[@]}" download megacmd
)
packages=("$work_dir/download"/megacmd_*.deb)
[[ ${#packages[@]} == 1 && -f "${packages[0]}" ]] || fail 'No se obtuvo un paquete firmado de MEGAcmd.'
# Local package authentication was checked against signed repository metadata;
# normal Debian repositories resolve its system dependencies.
apt-get install -y -qq -- "${packages[0]}"
for command in mega-cmd mega-put mega-export mega-get mega-whoami; do
  command -v "$command" >/dev/null 2>&1 || fail 'La instalación de MEGAcmd quedó incompleta.'
done
printf 'MEGAcmd instalado desde el repositorio oficial firmado.\n'
