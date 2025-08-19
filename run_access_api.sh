#!/usr/bin/env bash
# run_access_api.sh — RECRIAR TUDO a partir dos arquivos do repositório
#
# Este script sobrescreve a instalação existente com base nos arquivos locais:
#   - ./access_api.py           → /opt/access_api/access_api.py
#   - ./requirements.txt        → venv (instalação limpa)
#   - ./access_api.service      → /etc/systemd/system/access_api.service
#   - ./.env                    → /etc/access_api.env
#
# Comportamento:
# - Para e reinicia o serviço systemd.
# - Remove e recria o venv e o diretório do app.
# - Copia e aplica permissões adequadas.
# - Sem prompts interativos; falha se faltarem arquivos de origem.

set -euo pipefail

SERVICE_NAME="access_api.service"
SERVICE_DST="/etc/systemd/system/${SERVICE_NAME}"

APP_DIR="/opt/access_api"
VENV_DIR="${APP_DIR}/venv"
CODE_SRC="./access_api.py"              # arquivo local de origem
CODE_DST="${APP_DIR}/access_api.py"
ENV_SRC=".env"                          # arquivo local de origem
ENV_DST="/etc/access_api.env"
REQ_SRC="./requirements.txt"
UNIT_SRC="./access_api.service"

SUDO=""
if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then SUDO="sudo"; fi

step(){ echo "[$(date +%H:%M:%S)] $*"; }

require_file(){ local f="$1"; [[ -f "$f" ]] || { echo "ERRO: arquivo obrigatório não encontrado: $f" >&2; exit 1; }; }

step "[1/8] Conferindo arquivos de origem…"
require_file "$CODE_SRC"
require_file "$REQ_SRC"
require_file "$UNIT_SRC"
require_file "$ENV_SRC"

step "[2/8] Garantindo usuário e pastas…"
if ! getent passwd access_api >/dev/null; then
  $SUDO useradd --system --no-create-home --home /nonexistent \
                --shell /usr/sbin/nologin --user-group access_api
else
  $SUDO usermod -s /usr/sbin/nologin -d /nonexistent access_api || true
fi
$SUDO mkdir -p "$APP_DIR" /var/log/access_api

step "[3/8] Parando serviço (se ativo)…"
$SUDO systemctl stop "$SERVICE_NAME" 2>/dev/null || true

step "[4/8] Recriando app dir e venv…"
# Remove diretório do app inteiro (inclui venv antigo) e recria limpo
$SUDO rm -rf "$APP_DIR"
$SUDO mkdir -p "$APP_DIR"
$SUDO chown -R access_api:access_api "$APP_DIR" /var/log/access_api

# Assegura suporte a venv
if ! python3 -m venv --help >/dev/null 2>&1; then
  if command -v apt-get >/dev/null 2>&1; then
    $SUDO apt-get update -y
    $SUDO apt-get install -y python3-venv
  else
    echo "python3-venv ausente e não foi possível instalar automaticamente." >&2; exit 1
  fi
fi

$SUDO python3 -m venv "$VENV_DIR"
$SUDO "${VENV_DIR}/bin/python" -m pip install --upgrade pip setuptools wheel
$SUDO "${VENV_DIR}/bin/pip" install -r "$REQ_SRC"

step "[5/8] Copiando código e .env (sobrescrevendo)…"
$SUDO install -m 0644 "$CODE_SRC" "$CODE_DST"
$SUDO chown access_api:access_api "$CODE_DST"

# Copia .env do repositório diretamente para /etc
[[ -f "$ENV_DST" ]] && $SUDO cp -f "$ENV_DST" "${ENV_DST}.bak.$(date +%s)" || true
$SUDO install -m 0644 "$ENV_SRC" "$ENV_DST"

step "[6/8] Instalando unit do systemd (sobrescrevendo)…"
[[ -f "$SERVICE_DST" ]] && $SUDO cp -f "$SERVICE_DST" "${SERVICE_DST}.bak.$(date +%s)" || true
$SUDO install -m 0644 "$UNIT_SRC" "$SERVICE_DST"

step "[7/8] Reload/enable/start…"
$SUDO systemctl daemon-reload
if ! $SUDO systemctl is-enabled --quiet "$SERVICE_NAME"; then
  $SUDO systemctl enable "$SERVICE_NAME"
fi
$SUDO systemctl start "$SERVICE_NAME"

step "[8/8] Status:"
$SUDO systemctl --no-pager --full status "$SERVICE_NAME" || true
echo "OK. Recriado a partir dos arquivos locais. App: ${CODE_DST} | venv: ${VENV_DIR} | env: ${ENV_DST}"
