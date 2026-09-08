#!/usr/bin/env bash
# Levanta como o OpenClaw está rodando nesta máquina, para configurar a entrega
# do monitor Prodoc. Somente leitura: não instala, não altera, não reinicia nada.
#
# Valores que parecem token/senha são mascarados antes de imprimir — a saída
# pode ser colada numa conversa com segurança.

set -uo pipefail

titulo() { printf '\n=== %s ===\n' "$1"; }
mascarar() { sed -E 's/((token|secret|key|password|senha|apikey|api_key)[":= ]+)[^",[:space:]]{4,}/\1***MASCARADO***/Ig'; }

printf 'Relatório OpenClaw — %s em %s\n' "$(date '+%d/%m/%Y %H:%M')" "$(hostname)"

titulo "1. Serviços systemd suspeitos"
systemctl list-units --type=service --all 2>/dev/null \
  | grep -iE 'claw|whats|wpp|baileys|gateway|bot|assist' \
  | awk '{printf "  %-40s %s %s\n", $1, $3, $4}' || echo "  (nenhum)"

titulo "2. Processos em execução"
ps aux 2>/dev/null | grep -iE 'claw|whats|wpp|baileys' | grep -v grep \
  | awk '{printf "  pid=%-8s %s\n", $2, substr($0, index($0,$11))}' | cut -c1-160 || echo "  (nenhum)"

titulo "3. Portas escutando (sem 22/80/443)"
if command -v ss >/dev/null; then
  ss -tlnp 2>/dev/null | tail -n +2 | grep -vE ':(22|80|443)\s' | sed 's/^/  /' || echo "  (nenhuma)"
else
  netstat -tlnp 2>/dev/null | grep -vE ':(22|80|443)\s' | sed 's/^/  /' || echo "  (ss/netstat indisponível)"
fi

titulo "4. Containers Docker"
if command -v docker >/dev/null; then
  docker ps --format '  {{.Names}} | {{.Image}} | {{.Ports}}' 2>/dev/null || echo "  (sem permissão ou daemon parado)"
else
  echo "  (docker não instalado)"
fi

titulo "5. Processos pm2"
command -v pm2 >/dev/null && pm2 list 2>/dev/null | sed 's/^/  /' || echo "  (pm2 não instalado)"

titulo "6. Arquivos de configuração"
for caminho in ~/.openclaw ~/.config/openclaw /opt/openclaw /etc/openclaw \
               /var/lib/openclaw ~/openclaw ~/.clawdbot ~/.config/clawdbot; do
  [ -e "$caminho" ] && { echo "  ENCONTRADO: $caminho"; ls -la "$caminho" 2>/dev/null | head -12 | sed 's/^/      /'; }
done
echo "  --- busca ampla ---"
find / -maxdepth 5 -iname '*openclaw*' -o -maxdepth 5 -iname '*clawdbot*' 2>/dev/null \
  | grep -vE '^/(proc|sys)' | head -15 | sed 's/^/  /' || echo "  (nada)"

titulo "7. Conteúdo das configs (valores sensíveis mascarados)"
for caminho in ~/.openclaw/config.json ~/.config/openclaw/config.json \
               /opt/openclaw/.env /etc/openclaw/config.json ~/.openclaw/.env; do
  if [ -f "$caminho" ]; then
    echo "  --- $caminho ---"
    mascarar < "$caminho" | head -40 | sed 's/^/      /'
  fi
done

titulo "8. O que responde nas portas locais"
for porta in 3000 3001 4000 5000 8000 8080 8081 8082 18789; do
  resposta=$(curl -s -m 2 -o /dev/null -w '%{http_code}' "http://127.0.0.1:$porta/" 2>/dev/null)
  [ -n "$resposta" ] && [ "$resposta" != "000" ] && echo "  porta $porta -> HTTP $resposta"
done

titulo "9. Ambiente"
echo "  SO: $(. /etc/os-release 2>/dev/null && echo "$PRETTY_NAME" || uname -s)"
echo "  systemd: $(systemctl --version 2>/dev/null | head -1 || echo 'ausente')"
echo "  fuso: $(timedatectl show -p Timezone --value 2>/dev/null || cat /etc/timezone 2>/dev/null || echo '?')"
echo "  python3: $(python3 --version 2>&1)"
echo "  uv: $(command -v uv >/dev/null && uv --version || echo 'não instalado')"

printf '\n=== FIM — pode colar tudo acima ===\n'
