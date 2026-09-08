# prodoc-automacao

Monitor de documentos não lidos no [Prodoc](https://prodoc.ap.gov.br) (tramitação
do CBMAP), com resumo via Claude e aviso automático num grupo de WhatsApp.

**Princípio central: nenhum documento é aberto.** Abrir marca a mensagem como
lida no Prodoc. O resumo sai do trecho que a própria listagem devolve, com
fallback para o título. A trava está em `prodoc_client.ProdocClient._get`, que
valida toda URL contra uma allowlist antes de qualquer requisição.

## Comandos

```bash
uv run prodoc_monitor.py configurar              # grava o .env (pergunta no terminal)
uv run prodoc_monitor.py descobrir               # mapeia seções e campos (só leitura)
uv run prodoc_monitor.py monitorar --dry-run     # mostra a mensagem, não envia
uv run prodoc_monitor.py monitorar               # verifica e envia
uv run prodoc_monitor.py testar-envio            # testa só o canal de entrega
```

## Instalação

O projeto usa [uv](https://docs.astral.sh/uv/) — um comando resolve Python,
dependências e ambiente virtual, a partir do `pyproject.toml` e do `uv.lock`.

```bash
git clone https://github.com/eltonamador/prodoc-automacao.git
cd prodoc-automacao && uv sync
uv run prodoc_monitor.py configurar
```

`configurar` pergunta usuário, senha e chaves com `getpass`: nada aparece na
tela nem no histórico do shell, e o `.env` sai com permissão 0600.

Sem `uv` disponível, o caminho antigo continua valendo:

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
```

`requirements.txt` é **gerado** a partir do lock (`uv export`) — a fonte da
verdade é o `pyproject.toml`.

O `.env` nunca é commitado. Variáveis reais do ambiente têm precedência sobre
ele, então o `EnvironmentFile` do systemd continua mandando na VPS.

## Configuração

Tudo em `config.json`:

| Chave | Para quê |
|---|---|
| `secoes[]` | Seções monitoradas. Hoje só a ABM; adicionar outra é uma entrada na lista. |
| `resumo.campos_trecho` | Caminhos pontilhados do trecho, em ordem de preferência (ex.: `documento.conteudo`). Vazio = só título. Preencha com a saída de `descobrir`. |
| `resumo.min_caracteres_trecho` | Abaixo disso, o trecho é considerado insuficiente e cai no fallback. |
| `resumo.saida_estruturada` | `true` faz uma chamada só para todos os documentos, com triagem validada por JSON Schema. `false` volta ao modo antigo, um documento por chamada. |
| `entrega.openclaw` | Como disparar a mensagem: `modo` `http` ou `cli`. A URL e o grupo de destino ficam no `.env` (`OPENCLAW_URL`, `OPENCLAW_DESTINO`), não aqui — o repositório é público. |
| `estado.cooldown_alerta_horas` | Intervalo mínimo entre dois alertas do mesmo tipo. |

## Estado

`estado_notificados.json` guarda o que já foi avisado. **Ele é essencial**: como
os documentos nunca ficam lidos no Prodoc, sem esse arquivo cada execução
reenviaria tudo de novo. Fica fora do Git — faça backup dele na VPS, não do
repositório.

## Como descobrir o identificador de um grupo de WhatsApp

Necessário ao apontar o monitor para um grupo novo. O OpenClaw **não** lista os
grupos da conta: `directory groups list` só mostra os que ele já usou, e
`directory peers` volta vazio. O identificador está no store do Baileys, no
*nome* dos arquivos de chave de grupo.

1. Descubra o seu LID (o id interno do WhatsApp) a partir do seu telefone:

```bash
grep -la "SEU_NUMERO" /docker/openclaw-2iph/data/.openclaw/credentials/whatsapp/default/lid-mapping-*
```

O nome do arquivo é `lid-mapping-<SEU_LID>_reverse.json`.

2. Mande uma mensagem qualquer no grupo desejado, pelo celular.

3. Liste os grupos onde você tem chave, por horário — o mais recente é aquele:

```bash
D=/docker/openclaw-2iph/data/.openclaw/credentials/whatsapp/default
ls -t "$D" | grep -a "g\.us--SEU_LID_1--" | head -5
```

4. Confirme com um envio de teste antes de confiar: `testar-envio`. O
identificador sozinho não prova qual grupo é.

## Deploy na VPS

```bash
sudo cp -r . /opt/prodoc-automacao
cd /opt/prodoc-automacao && uv sync           # cria o .venv que o systemd usa
sudo cp deploy/prodoc-monitor.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now prodoc-monitor.timer
```

Acompanhamento:

```bash
systemctl list-timers prodoc-monitor.timer
journalctl -u prodoc-monitor.service -n 50
```

Cadência: de hora em hora nos dias úteis (06h–18h) e duas vezes por dia no fim
de semana, em horário de Brasília.

## Alertas

Falha de autenticação, de leitura ou da API do Claude vira mensagem no grupo —
não só linha de log. Cada tipo respeita um cooldown de 24h, para que uma falha
persistente não gere uma mensagem por hora. Se o próprio canal de entrega
estiver fora, sobra o log e o código de saída diferente de zero, que o systemd
registra.

## O que a listagem do Prodoc realmente entrega

Verificado contra a caixa da ABM em 07/09/2026, com 100 documentos:

- **Não existe trecho do documento.** O campo mais longo é o assunto (~65
  caracteres, com HTML). O corpo só sairia abrindo o documento, o que o
  marcaria como lido. Então a triagem trabalha com a identificação.
- A unidade `bKnmJoD6WZ` **é** a caixa da ABM: `destino` = ABM nos 100.
- O remetente útil é `origem.nome` (a unidade). `instituicao_origem_nome` é
  "CORPO DE BOMBEIROS MILITAR DO ESTADO DO AMAPÁ" em todos — não distingue nada.
- `documento.lido` diz o que é novo; `copia` marca o que veio para conhecimento.

## Triagem

Os documentos vão para o Claude numa **única chamada**, com a resposta validada
contra um JSON Schema. Como não há texto para resumir, o modelo não parafraseia
o assunto: ele classifica urgência, diz se exige providência, e acrescenta em
uma frase o que aquilo tende a significar para a Academia. A mensagem é
reordenada — o que exige providência vem primeiro.

Duas travas contra invenção:

- prazo que não esteja no texto que realmente enviamos é descartado em código;
- se o campo `destino` sumir do payload, o filtro de seção é **ignorado** em vez
  de descartar tudo — e isso vira alerta. Silêncio é indistinguível de "nada
  novo", e foi assim que a versão anterior falhou sem ninguém perceber.

Se a saída estruturada falhar por qualquer motivo, o código volta sozinho ao
modo antigo — uma chamada de texto simples por documento.

## Testes e lint

```bash
uv run pytest
uv run ruff check .
```

## A falha original

Levantado no histórico de execuções do próprio Actions (166 execuções):

- O workflow **rodou agendado e falhou em todas as execuções** até 26/04/2026,
  quando parou de rodar. O passo que quebrava era "Executar monitoramento
  Prodoc", e o "Upload resultado" ficava *skipped* — o script saía com erro
  antes de gravar qualquer arquivo.
- Os logs daquelas execuções passaram da retenção de 90 dias, então a exceção
  exata não é recuperável. A assinatura — morrer antes de gravar o resultado,
  sempre — é compatível com o `ValueError("Credenciais ausentes")` que o
  construtor levantava quando um secret estava vazio. É hipótese, não fato.
- Por que as execuções pararam em 26/04 não foi determinado. A regra dos 60
  dias de inatividade do GitHub não explica: o último commit é de 29/03, 28
  dias antes.

Um segundo defeito, esse verificado ao vivo: os booleanos do `config.json` iam
para a URL como `"False"` com maiúscula, e o Prodoc responde HTTP 500 a isso. O
código antigo tratava o 500 como caixa vazia e encerraria com **sucesso** — não
foi o que deixou os jobs vermelhos, mas tornaria o monitor inútil de qualquer
forma. Corrigido na camada de transporte.

## Por que não roda mais no GitHub Actions

O agendamento no Actions se mostrou frágil: as execuções falharam por meses e
depois simplesmente pararam, sem que a causa seja recuperável hoje. Além disso,
o repositório é público — o log do Actions exibiria assunto e remetente de
documentos internos do CBMAP. A execução agendada agora é só na VPS, com
systemd; o workflow que restou roda apenas lint e testes, que não tocam em dado
real.
