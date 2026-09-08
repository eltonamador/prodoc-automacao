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

passo "1/6 Escolhendo o gerenciador de ambiente"
# uv é o caminho preferido (usa o uv.lock, instalação exata). Sem ele, o venv
# da própria distribuição resolve com o requirements.txt, que é gerado do lock.
# Assim a VPS não precisa ganhar ferramenta nova só para isto rodar.
if command -v uv >/dev/null; then
    GERENCIADOR=uv
    echo "  uv encontrado: $(uv --version)"
else
    GERENCIADOR=venv
    command -v python3 >/dev/null || erro "nem uv nem python3 encontrados"
    python3 -c 'import venv' 2>/dev/null || erro "módulo venv ausente: apt install python3-venv"
    echo "  uv ausente; usando python3 -m venv ($(python3 --version))"
fi

passo "2/6 Copiando o projeto para $DESTINO"
mkdir -p "$DESTINO"
if [ "$ORIGEM" = "$DESTINO" ]; then
    echo "  já está em $DESTINO (nada a copiar)"
else
# Copia só o que vem do repositório. O .env e o estado_notificados.json não
# estão nesta lista, então sobrevivem intactos a cada reinstalação.
    cp "$ORIGEM"/*.py "$ORIGEM"/config.json "$ORIGEM"/pyproject.toml \
       "$ORIGEM"/uv.lock "$ORIGEM"/requirements.txt "$ORIGEM"/README.md "$DESTINO"/
    mkdir -p "$DESTINO/tests" && cp "$ORIGEM"/tests/*.py "$DESTINO/tests/"
    echo "  copiado (o .env e o estado existentes foram preservados)"
fi
chown -R "$USUARIO_SERVICO":"$USUARIO_SERVICO" "$DESTINO"

passo "3/6 Instalando dependências"
if [ "$GERENCIADOR" = uv ]; then
    sudo -u "$USUARIO_SERVICO" env -C "$DESTINO" uv sync --locked
else
    sudo -u "$USUARIO_SERVICO" python3 -m venv "$DESTINO/.venv"
    sudo -u "$USUARIO_SERVICO" "$DESTINO/.venv/bin/pip" install --quiet --upgrade pip
    sudo -u "$USUARIO_SERVICO" "$DESTINO/.venv/bin/pip" install --quiet -r "$DESTINO/requirements.txt"
fi
echo "  ambiente pronto em $DESTINO/.venv"
echo "  SDK: $("$DESTINO/.venv/bin/python" -c 'import anthropic;print("anthropic", anthropic.__version__)')"

passo "4/6 Conferindo o .env"
if [ ! -f "$DESTINO/.env" ]; then
    echo "  .env AUSENTE. Depois deste script, rode como $USUARIO_SERVICO:"
    echo "      cd $DESTINO && .venv/bin/python prodoc_monitor.py configurar"
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
  .venv/bin/python prodoc_monitor.py configurar        # se o .env não estiver completo
  .venv/bin/python prodoc_monitor.py monitorar --dry-run   # confere sem enviar
  .venv/bin/python prodoc_monitor.py testar-envio      # testa o canal do WhatsApp

Acompanhar as execuções:
  journalctl -u prodoc-monitor.service -f

Rodar uma vez agora:
  sudo systemctl start prodoc-monitor.service
FIM
