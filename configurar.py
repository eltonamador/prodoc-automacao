#!/usr/bin/env python3
"""Configuração interativa: escreve o .env sem que nenhum segredo passe por terceiros.

A senha é lida com getpass — não aparece na tela, não fica no histórico do shell
e não entra em log algum. O arquivo é criado com permissão 0600.
"""

from __future__ import annotations

import getpass
import os
import stat

ARQUIVO = ".env"

CAMPOS = [
    ("PRODOC_USER", "Usuário do Prodoc (e-mail de login)", False, True),
    ("PRODOC_PASSWORD", "Senha do Prodoc", True, True),
    ("CLAUDE_API_KEY", "Chave da API do Claude (console.anthropic.com)", True, True),
    ("OPENCLAW_URL", "URL do OpenClaw para enviar mensagem (ex: http://127.0.0.1:8080/send)", False, False),
    ("OPENCLAW_DESTINO", "Identificador do grupo de WhatsApp de destino", False, False),
    ("OPENCLAW_TOKEN", "Token do OpenClaw (deixe vazio se não exigir)", True, False),
]


def _ler_existente() -> dict[str, str]:
    valores: dict[str, str] = {}
    if not os.path.exists(ARQUIVO):
        return valores
    with open(ARQUIVO, encoding="utf-8") as arquivo:
        for linha in arquivo:
            linha = linha.strip()
            if linha and not linha.startswith("#") and "=" in linha:
                chave, _, valor = linha.partition("=")
                valores[chave.strip()] = valor.strip()
    return valores


def _perguntar(rotulo: str, secreto: bool, atual: str) -> str:
    if atual:
        dica = "(já preenchido — Enter mantém)"
    elif not secreto:
        dica = "(Enter deixa vazio)"
    else:
        dica = ""
    pergunta = f"{rotulo} {dica}: ".replace("  ", " ")
    resposta = getpass.getpass(pergunta) if secreto else input(pergunta)
    return resposta.strip() or atual


def executar() -> int:
    print("\nConfiguração do monitor Prodoc.")
    print("Senhas e chaves não aparecem na tela e o arquivo fica só para você (chmod 600).\n")

    atuais = _ler_existente()
    valores: dict[str, str] = {}

    for chave, rotulo, secreto, obrigatorio in CAMPOS:
        valor = _perguntar(rotulo, secreto, atuais.get(chave, ""))
        while obrigatorio and not valor:
            print("  Esse campo é obrigatório.")
            valor = _perguntar(rotulo, secreto, "")
        valores[chave] = valor

    linhas = [
        "# Gerado por: python3 prodoc_monitor.py configurar",
        "# Não commite este arquivo (já está no .gitignore).",
        "",
    ]
    for chave, _rotulo, _secreto, _obrigatorio in CAMPOS:
        linhas.append(f"{chave}={valores[chave]}")

    with open(ARQUIVO, "w", encoding="utf-8") as arquivo:
        arquivo.write("\n".join(linhas) + "\n")
    os.chmod(ARQUIVO, stat.S_IRUSR | stat.S_IWUSR)

    preenchidos = [c for c, *_ in CAMPOS if valores[c]]
    vazios = [c for c, *_ in CAMPOS if not valores[c]]
    print(f"\n.env gravado (0600). Preenchidos: {', '.join(preenchidos)}")
    if vazios:
        print(f"Ainda vazios: {', '.join(vazios)} — rode este comando de novo quando tiver os valores.")
    print("\nPróximo passo: python3 prodoc_monitor.py descobrir\n")
    return 0
