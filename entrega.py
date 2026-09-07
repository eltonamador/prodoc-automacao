#!/usr/bin/env python3
"""Formatação e envio das mensagens pelo OpenClaw.

O adaptador é dirigido por config (HTTP ou CLI) porque a forma exata de disparo
do OpenClaw na VPS ainda vai ser confirmada. Trocar de canal depois significa
mexer só neste arquivo.
"""
from __future__ import annotations

import logging
import os
import subprocess

logger = logging.getLogger(__name__)

VARIAVEL_TOKEN = "OPENCLAW_TOKEN"


class EntregaError(Exception):
    """Falha ao enviar a mensagem pelo canal configurado."""


# ----------------------------------------------------------------------
# Formatação
# ----------------------------------------------------------------------

ORDEM_URGENCIA = {"alta": 0, "media": 1, "baixa": 2}
# Só a urgência alta ganha marcador. "media" era 1:1 com a linha "Exige
# providência" logo abaixo, e um ícone que duplica o texto ao lado é ruído.
MARCA_URGENCIA = {"alta": "🔴"}


def ordenar_por_prioridade(documentos: list[dict]) -> list[dict]:
    """Quem exige providência aparece primeiro; depois por urgência, depois na ordem original.

    Numa caixa com dez documentos o que importa é não perder o que tem prazo —
    e a leitura no celular costuma parar no terceiro item.
    """
    return sorted(
        documentos,
        key=lambda d: (
            not d.get("acao_requerida", False),
            ORDEM_URGENCIA.get(d.get("urgencia", "baixa"), 2),
        ),
    )


def formatar_documento(indice: int, documento: dict, marcar_sem_trecho: bool = True) -> str:
    marca = MARCA_URGENCIA.get(documento.get("urgencia", "baixa"), "")
    linhas = [
        f"{marca} *{indice}. {documento['tipo']} {documento['numero']}*".strip(),
    ]

    if documento.get("acao_requerida"):
        prazo = documento.get("prazo")
        linhas.append(f"*Exige providência*{f' — prazo: {prazo}' if prazo else ''}")

    de = documento["remetente"]
    linhas.append(f"De: {de}" + (" _(cópia)_" if documento.get("copia") else ""))
    if documento.get("data"):
        linhas.append(f"Data: {documento['data']}")
    linhas.append(f"Assunto: {documento['assunto']}")

    resumo = (documento.get("resumo") or "").strip()
    if resumo:
        linhas.append("")
        linhas.append(resumo)
        if marcar_sem_trecho and documento.get("origem_resumo") == "titulo":
            linhas.append("_(só pela identificação)_")
    else:
        linhas.append("")
        linhas.append("_(não foi possível gerar o resumo desta vez)_")

    return "\n".join(linhas)


def formatar_mensagem(documentos: list[dict], secao: str) -> str:
    ordenados = ordenar_por_prioridade(documentos)
    plural = "documento novo" if len(ordenados) == 1 else "documentos novos"
    cabecalho = f"*Prodoc — {secao}*\n{len(ordenados)} {plural} não lidos"

    com_acao = sum(1 for d in ordenados if d.get("acao_requerida"))
    if com_acao:
        cabecalho += f" · *{com_acao} exige{'m' if com_acao > 1 else ''} providência*"

    # A ressalva vale para todos quando nenhum documento trouxe texto: repeti-la
    # em cada item era ruído. Se só alguns vierem sem trecho, marca-se por item.
    sem_trecho = [d for d in ordenados if d.get("origem_resumo") == "titulo"]
    todos_sem_trecho = bool(sem_trecho) and len(sem_trecho) == len(ordenados)
    if todos_sem_trecho:
        cabecalho += "\n_Avaliado só pela identificação — os documentos não foram abertos._"

    # Quando a ressalva já está no cabeçalho, repeti-la por item é ruído.
    blocos = [
        formatar_documento(i, d, marcar_sem_trecho=not todos_sem_trecho)
        for i, d in enumerate(ordenados, 1)
    ]
    return cabecalho + "\n\n" + "\n\n".join(blocos)


def formatar_alerta(titulo: str, detalhe: str) -> str:
    return (
        f"*Prodoc — falha no monitor*\n{titulo}\n\n{detalhe}\n\n"
        "_A verificação automática não rodou. Você pode não estar vendo documentos novos._"
    )


def dividir(texto: str, limite: int) -> list[str]:
    """Quebra a mensagem em blocos, sempre em quebras de linha duplas.

    Nunca corta um documento ao meio; se um único documento estourar o limite,
    ele vai inteiro num bloco só (melhor uma mensagem longa que um texto cortado).
    """
    if len(texto) <= limite:
        return [texto]

    partes: list[str] = []
    atual = ""
    for bloco in texto.split("\n\n"):
        candidato = bloco if not atual else f"{atual}\n\n{bloco}"
        if len(candidato) > limite and atual:
            partes.append(atual)
            atual = bloco
        else:
            atual = candidato
    if atual:
        partes.append(atual)

    total = len(partes)
    return [f"{p} ({i}/{total})" for i, p in enumerate(partes, 1)]


# ----------------------------------------------------------------------
# Transporte
# ----------------------------------------------------------------------

def _config_openclaw(config: dict) -> dict:
    openclaw = dict(config.get("entrega", {}).get("openclaw", {}))

    # O repositório é público: URL do gateway e identificador do grupo vêm do
    # .env e sobrepõem o config.json, que fica só com os valores estruturais.
    for chave, variavel in (("url", "OPENCLAW_URL"), ("destino", "OPENCLAW_DESTINO")):
        valor = os.getenv(variavel)
        if valor:
            openclaw[chave] = valor

    if not openclaw.get("destino"):
        raise EntregaError(
            "Destino do OpenClaw não configurado. Rode 'python3 prodoc_monitor.py configurar' "
            "ou defina OPENCLAW_DESTINO no .env."
        )
    return openclaw


def _enviar_http(openclaw: dict, texto: str) -> None:
    import requests

    url = openclaw.get("url")
    if not url:
        raise EntregaError("entrega.openclaw.url não configurada no config.json.")

    corpo = {
        openclaw.get("campo_destino", "chat_id"): openclaw["destino"],
        openclaw.get("campo_texto", "text"): texto,
    }
    cabecalhos = {"Content-Type": "application/json"}
    token = os.getenv(VARIAVEL_TOKEN)
    if token:
        nome = openclaw.get("cabecalho_auth", "Authorization")
        cabecalhos[nome] = token if nome != "Authorization" else f"Bearer {token}"

    try:
        resposta = requests.post(
            url, json=corpo, headers=cabecalhos, timeout=openclaw.get("timeout", 20)
        )
    except requests.RequestException as e:
        raise EntregaError(f"Não consegui falar com o OpenClaw em {url}: {e}") from e

    if resposta.status_code >= 400:
        raise EntregaError(
            f"OpenClaw respondeu HTTP {resposta.status_code}: {resposta.text[:300]}"
        )


def _enviar_cli(openclaw: dict, texto: str) -> None:
    modelo = openclaw.get("comando") or []
    if not modelo:
        raise EntregaError("entrega.openclaw.comando não configurado no config.json.")

    comando = [
        parte.replace("{destino}", openclaw["destino"]).replace("{texto}", texto)
        for parte in modelo
    ]
    try:
        processo = subprocess.run(
            comando, capture_output=True, text=True, timeout=openclaw.get("timeout", 20)
        )
    except FileNotFoundError as e:
        raise EntregaError(f"Comando do OpenClaw não encontrado: {comando[0]}") from e
    except subprocess.TimeoutExpired as e:
        raise EntregaError("Comando do OpenClaw travou (timeout).") from e

    if processo.returncode != 0:
        saida = (processo.stderr or processo.stdout)[:300]
        raise EntregaError(
            f"Comando do OpenClaw saiu com código {processo.returncode}: {saida}"
        )


def enviar(config: dict, texto: str) -> int:
    """Envia o texto, quebrando em blocos quando necessário. Devolve nº de blocos."""
    openclaw = _config_openclaw(config)
    modo = openclaw.get("modo", "http")
    limite = config.get("entrega", {}).get("max_caracteres_mensagem", 3500)

    if modo == "http":
        envio = _enviar_http
    elif modo == "cli":
        envio = _enviar_cli
    else:
        raise EntregaError(f"entrega.openclaw.modo inválido: {modo} (use 'http' ou 'cli').")

    blocos = dividir(texto, limite)
    for numero, bloco in enumerate(blocos, 1):
        logger.info("Enviando bloco %d/%d (%d caracteres)...", numero, len(blocos), len(bloco))
        envio(openclaw, bloco)
    return len(blocos)


def testar(config: dict, mensagem: str) -> int:
    from configuracao import carregar_env

    carregar_env()
    try:
        blocos = enviar(config, mensagem)
    except EntregaError as e:
        logger.error("Falha no envio de teste: %s", e)
        return 1
    logger.info("Mensagem de teste enviada em %d bloco(s). Confira o grupo.", blocos)
    return 0
