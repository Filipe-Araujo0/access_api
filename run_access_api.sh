#!/usr/bin/env bash
# access_api_setup.sh — idempotente + venv + .env interativo
set -euo pipefail

SERVICE_NAME="access_api.service"
SERVICE_DST="/etc/systemd/system/${SERVICE_NAME}"

APP_DIR="/opt/access_api"
VENV_DIR="${APP_DIR}/venv"
CODE_SRC="./access_api.py"                # espera o arquivo na pasta atual
CODE_DST="${APP_DIR}/access_api.py"
ENV_FILE="/etc/access_api.env"
REQ_FILE="${APP_DIR}/requirements.txt"
DEFAULT_PKGS=("fastapi" "uvicorn[standard]" "httpx")

SUDO=""
if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then SUDO="sudo"; fi

step(){ echo "[$(date +%H:%M:%S)] $*"; }

step "[1/8] Usuário e grupo…"
if ! getent passwd access_api >/dev/null; then
  $SUDO useradd --system --no-create-home --home /nonexistent \
                --shell /usr/sbin/nologin --user-group access_api
else
  $SUDO usermod -s /usr/sbin/nologin -d /nonexistent access_api || true
fi

step "[2/8] Pastas e permissões…"
$SUDO mkdir -p "${APP_DIR}" /var/log/access_api
$SUDO chown -R access_api:access_api "${APP_DIR}" /var/log/access_api

step "[3/8] .env (criação interativa se não existir)…"
if [[ ! -f "${ENV_FILE}" ]]; then
  # usa valor de ambiente se já exportado, senão pergunta
  if [[ -n "${UPSTREAM_BASE_URL:-}" ]]; then
    upstream="${UPSTREAM_BASE_URL}"
  else
    read -rp "Informe o UPSTREAM_BASE_URL (ex.: https://example.com): " upstream
    while [[ -z "${upstream}" || ! "${upstream}" =~ ^https?://[^[:space:]]+$ ]]; do
      echo "Valor inválido. Deve começar com http:// ou https://"
      read -rp "UPSTREAM_BASE_URL: " upstream
    done
  fi

  $SUDO tee "${ENV_FILE}" >/dev/null <<EOF
UPSTREAM_BASE_URL="${upstream}"
LIMIT_PER_MINUTE=200
ACTIVE_WINDOW_SECONDS=5
BURST_WINDOW_SECONDS=30
PREFER_WAIT_DEFAULT=10
OUTBOUND_MAX_CONNECTIONS=30
OUTBOUND_MAX_KEEPALIVE=20
LOG_LEVEL=INFO
LOG_FORMAT=json
LOG_FILE=/var/log/access_api/access_api.log
LOG_MAX_BYTES=10485760
LOG_BACKUP_COUNT=5
EOF
  echo "Criado ${ENV_FILE} com UPSTREAM_BASE_URL=${upstream}"
else
  echo "${ENV_FILE} já existe. Mantendo."
fi

step "[4/8] Unit do systemd…"
UNIT_CONTENT='[Unit]
Description=Access API Proxy (FastAPI + Uvicorn)
After=network-online.target
Wants=network-online.target

[Service]
WorkingDirectory=/opt/access_api
EnvironmentFile=/etc/access_api.env
ExecStart=/opt/access_api/venv/bin/python -m uvicorn access_api:app --app-dir /opt/access_api --host 0.0.0.0 --port 5812 --workers 1
User=access_api
Group=access_api
Restart=always
RestartSec=2
TimeoutStopSec=10
Environment=PYTHONUNBUFFERED=1
StandardOutput=journal
StandardError=journal
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
'
TMP_UNIT="$(mktemp)"
printf "%s" "$UNIT_CONTENT" > "$TMP_UNIT"
NEED_INSTALL=1
if [[ -f "${SERVICE_DST}" ]] && cmp -s "$TMP_UNIT" "${SERVICE_DST}"; then
  NEED_INSTALL=0
fi
if [[ "$NEED_INSTALL" -eq 1 ]]; then
  [[ -f "${SERVICE_DST}" ]] && $SUDO cp -f "${SERVICE_DST}" "${SERVICE_DST}.bak.$(date +%s)" || true
  $SUDO install -m 0644 "$TMP_UNIT" "${SERVICE_DST}"
fi
rm -f "$TMP_UNIT"

step "[5/8] Venv…"
if ! python3 -m venv --help >/dev/null 2>&1; then
  if command -v apt-get >/dev/null 2>&1; then
    $SUDO apt-get update -y
    $SUDO apt-get install -y python3-venv
  else
    echo "python3-venv ausente." >&2; exit 1
  fi
fi
if [[ ! -d "$VENV_DIR" ]]; then
  $SUDO python3 -m venv "$VENV_DIR"
fi
$SUDO "${VENV_DIR}/bin/python" -m pip install --upgrade pip setuptools wheel
if [[ -f "$REQ_FILE" ]]; then
  $SUDO "${VENV_DIR}/bin/pip" install -r "$REQ_FILE"
else
  $SUDO "${VENV_DIR}/bin/pip" install "${DEFAULT_PKGS[@]}"
fi

step "[6/8] Código access_api.py…"
if [[ ! -f "$CODE_SRC" ]]; then
  echo "ERRO: ${CODE_SRC} não encontrado na pasta atual." >&2; exit 1
fi
if [[ ! -f "$CODE_DST" ]] || ! cmp -s "$CODE_SRC" "$CODE_DST"; then
  $SUDO install -m 0644 "$CODE_SRC" "$CODE_DST"
fi
$SUDO chown access_api:access_api "$CODE_DST"


step "[7/8] Reload/enable/start…"
$SUDO systemctl daemon-reload
if ! $SUDO systemctl is-enabled --quiet "$SERVICE_NAME"; then
  $SUDO systemctl enable "$SERVICE_NAME"
fi
if $SUDO systemctl is-active --quiet "$SERVICE_NAME"; then
  $SUDO systemctl restart "$SERVICE_NAME"
else
  $SUDO systemctl start "$SERVICE_NAME"
fi

step "[8/8] Status:"
$SUDO systemctl --no-pager --full status "$SERVICE_NAME" || true
echo "OK. App: ${CODE_DST}  |  venv: ${VENV_DIR}  |  env: ${ENV_FILE}"
