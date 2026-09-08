#!/usr/bin/env bash
# Instala o monitor Prodoc nesta máquina (VPS): dependências, serviço e agenda.
#
# Uso:
#   sudo bash deploy/instalar.sh
#
# É idempotente: rodar de novo atualiza sem duplicar nada. Não sobrescreve o
# .env nem o estado de documentos já avisados.

set -euo pipefail

DESTINO="${DESTINO:-/opt/prodoc-automacao}"
ORIGEM="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
USUARIO_SERVICO="${SUDO_USER:-$(whoami)}"

passo() { printf '\n\033[1m==> %s\033[0m\n' "$1"; }
erro()  { printf '\033[31mERRO: %s\033[0m\n' "$1" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || erro "rode com sudo: sudo bash deploy/instalar.sh"

passo "1/6 Verificando o uv"
if ! command -v uv >/dev/null; then
    erro "uv não encontrado. Instale com:
    curl -LsSf https://astral.sh/uv/install.sh | sh
  ou pelo gerenciador de pacotes da distribuição, e rode este script de novo."
fi
echo "  $(uv --version)"

passo "2/6 Copiando o projeto para $DESTINO"
mkdir -p "$DESTINO"
# Copia só o que vem do repositório. O .env e o estado_notificados.json não
# estão nesta lista, então sobrevivem intactos a cada reinstalação.
cp "$ORIGEM"/*.py "$ORIGEM"/config.json "$ORIGEM"/pyproject.toml \
   "$ORIGEM"/uv.lock "$ORIGEM"/README.md "$DESTINO"/
mkdir -p "$DESTINO/tests" && cp "$ORIGEM"/tests/*.py "$DESTINO/tests/"
chown -R "$USUARIO_SERVICO":"$USUARIO_SERVICO" "$DESTINO"
echo "  copiado (o .env e o estado existentes foram preservados)"

passo "3/6 Instalando dependências"
sudo -u "$USUARIO_SERVICO" env -C "$DESTINO" uv sync --locked
echo "  ambiente pronto em $DESTINO/.venv"

passo "4/6 Conferindo o .env"
if [ ! -f "$DESTINO/.env" ]; then
    echo "  .env AUSENTE. Depois deste script, rode como $USUARIO_SERVICO:"
    echo "      cd $DESTINO && uv run prodoc_monitor.py configurar"
else
    chmod 600 "$DESTINO/.env"; chown "$USUARIO_SERVICO":"$USUARIO_SERVICO" "$DESTINO/.env"
    faltando=""
    for chave in PRODOC_USER PRODOC_PASSWORD CLAUDE_API_KEY OPENCLAW_DESTINO; do
        grep -qE "^$chave=.+" "$DESTINO/.env" || faltando="$faltando $chave"
    done
    [ -n "$faltando" ] && echo "  .env presente, mas sem valor em:$faltando" || echo "  .env completo"
fi

passo "5/6 Instalando o serviço e o timer"
sed "s|/opt/prodoc-automacao|$DESTINO|g; s|__USUARIO__|$USUARIO_SERVICO|" \
    "$ORIGEM/deploy/prodoc-monitor.service" > /etc/systemd/system/prodoc-monitor.service
cp "$ORIGEM/deploy/prodoc-monitor.timer" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now prodoc-monitor.timer
echo "  timer ativo"

passo "6/6 Estado final"
systemctl list-timers prodoc-monitor.timer --no-pager 2>/dev/null | head -3 | sed 's/^/  /'

cat <<FIM

Instalado em $DESTINO.

Próximos passos, como $USUARIO_SERVICO:
  cd $DESTINO
  uv run prodoc_monitor.py configurar        # se o .env ainda não estiver completo
  uv run prodoc_monitor.py monitorar --dry-run   # confere a mensagem sem enviar
  uv run prodoc_monitor.py testar-envio      # testa o canal do WhatsApp

Acompanhar as execuções:
  journalctl -u prodoc-monitor.service -f

Rodar uma vez agora:
  sudo systemctl start prodoc-monitor.service
FIM
