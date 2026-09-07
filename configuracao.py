#!/usr/bin/env python3
"""Carregamento de config.json e das credenciais do .env.

O .env é lido por um parser próprio (poucas linhas) em vez de python-dotenv:
uma dependência a menos para instalar e manter atualizada na VPS.
"""
from __future__ import annotations

import json
import logging
import os
import sys

logger = logging.getLogger(__name__)

CAMINHO_PADRAO = "config.json"
VARIAVEIS_OBRIGATORIAS = ("PRODOC_USER", "PRODOC_PASSWORD", "CLAUDE_API_KEY")


def carregar_env(caminho: str = ".env") -> None:
    """Carrega o .env no ambiente sem sobrescrever variáveis já definidas.

    Variáveis reais do ambiente têm precedência sobre o arquivo, para que a
    execução na VPS (systemd EnvironmentFile) continue mandando.
    """
    if not os.path.exists(caminho):
        return
    with open(caminho, encoding="utf-8") as arquivo:
        for numero, linha in enumerate(arquivo, 1):
            linha = linha.strip()
            if not linha or linha.startswith("#") or "=" not in linha:
                continue
            chave, _, valor = linha.partition("=")
            chave = chave.strip()
            valor = valor.strip().strip('"').strip("'")
            if not chave:
                logger.warning("%s linha %d ignorada (chave vazia)", caminho, numero)
                continue
            os.environ.setdefault(chave, valor)


def carregar_config(caminho: str = CAMINHO_PADRAO) -> dict:
    try:
        with open(caminho, encoding="utf-8") as arquivo:
            return json.load(arquivo)
    except FileNotFoundError:
        logger.error("Arquivo de configuração não encontrado: %s", caminho)
        sys.exit(1)
    except json.JSONDecodeError as e:
        logger.error("JSON inválido em %s: %s", caminho, e)
        sys.exit(1)


def carregar_credenciais() -> dict[str, str]:
    """Devolve as credenciais do ambiente, falhando com mensagem específica."""
    carregar_env()
    faltando = [v for v in VARIAVEIS_OBRIGATORIAS if not os.getenv(v)]
    if faltando:
        logger.error(
            "Credenciais ausentes: %s. Copie .env.example para .env e preencha "
            "(cp .env.example .env && chmod 600 .env).",
            ", ".join(faltando),
        )
        sys.exit(1)
    return {v: os.environ[v] for v in VARIAVEIS_OBRIGATORIAS}


def secoes_ativas(config: dict) -> list[dict]:
    """Seções marcadas como ativas no config, na ordem declarada."""
    return [s for s in config.get("secoes", []) if s.get("ativa", True)]
