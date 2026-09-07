#!/usr/bin/env python3
"""Diagnóstico da listagem do Prodoc quando ela responde erro.

Em vez de adivinhar a causa de um HTTP 500, este modo reúne evidência: confirma
se a sessão está de fato autenticada, e então tenta a listagem com várias
serializações de parâmetro, reportando o que cada uma devolve.

Continua estritamente somente-leitura — só usa rotas da allowlist.
"""

from __future__ import annotations

import json
import logging
import re

from prodoc_client import ProdocClient, ProdocError

logger = logging.getLogger(__name__)

ARQUIVO_RESPOSTAS = "diagnostico_respostas.json"

# Os nomes dos parâmetros são de DataTables, mas o formato enviado era achatado
# e com booleanos Python (que viram a string "False" na URL). Cada variante
# testa uma hipótese diferente sobre o que o servidor espera.
VARIANTES = [
    ("atual (booleano Python -> 'False')", {
        "length": 100, "start": 0, "search": False, "column": "documento_tramitacoes.created_at",
        "order": "desc", "filtros": False, "findDocumento": False,
    }),
    ("booleanos em minúsculo", {
        "length": 100, "start": 0, "search": "false", "column": "documento_tramitacoes.created_at",
        "order": "desc", "filtros": "false", "findDocumento": "false",
    }),
    ("booleanos como 0", {
        "length": 100, "start": 0, "search": 0, "column": "documento_tramitacoes.created_at",
        "order": "desc", "filtros": 0, "findDocumento": 0,
    }),
    ("booleanos como string vazia", {
        "length": 100, "start": 0, "search": "", "column": "documento_tramitacoes.created_at",
        "order": "desc", "filtros": "", "findDocumento": "",
    }),
    ("sem os booleanos", {
        "length": 100, "start": 0, "column": "documento_tramitacoes.created_at", "order": "desc",
    }),
    ("só paginação", {"length": 100, "start": 0}),
    ("estilo DataTables", {
        "draw": 1, "start": 0, "length": 100, "search[value]": "", "search[regex]": "false",
        "order[0][column]": 0, "order[0][dir]": "desc",
    }),
    ("sem parâmetro nenhum", {}),
]


def _resumo_do_corpo(texto: str) -> str:
    """Extrai a parte útil de uma página de erro do Laravel."""
    for padrao in (
        r'"message"\s*:\s*"([^"]{5,300})"',
        r'<title>([^<]{5,200})</title>',
        r'class="exception_title"[^>]*>(.{5,200}?)<',
    ):
        achado = re.search(padrao, texto, re.IGNORECASE | re.DOTALL)
        if achado:
            return re.sub(r"\s+", " ", achado.group(1)).strip()
    return re.sub(r"\s+", " ", texto[:200]).strip()


def executar(config: dict, credenciais: dict[str, str]) -> int:
    cliente = ProdocClient(config, credenciais["PRODOC_USER"], credenciais["PRODOC_PASSWORD"])
    cliente.autenticar()

    secoes = config.get("secoes") or []
    if not secoes:
        logger.error("Nenhuma seção em config.json['secoes'].")
        return 1
    unidade = secoes[0]["unidade_organizacional_id"]
    instituicao = config["prodoc"]["instituicao_id"]

    registro: dict = {"instituicao_id": instituicao, "unidade_organizacional_id": unidade}

    # --- 1. A sessão está mesmo autenticada? --------------------------
    print("\n" + "=" * 72)
    print("1. A SESSÃO ESTÁ AUTENTICADA?")
    print("=" * 72)

    try:
        pagina = cliente.buscar_html("/dashboard")
    except ProdocError as e:
        print(f"\n  Não consegui abrir /dashboard: {e}")
        pagina = ""

    tem_form_login = bool(re.search(r'name=["\']password["\']', pagina))
    marcas = [m for m in ("logout", "sair", "dashboard", "unidade_organizacional")
              if m.lower() in pagina.lower()]
    autenticado = bool(pagina) and not tem_form_login

    print(f"\n  /dashboard devolveu {len(pagina)} caracteres")
    print(f"  contém formulário de senha: {'SIM (não autenticado)' if tem_form_login else 'não'}")
    print(f"  marcas de área logada encontradas: {', '.join(marcas) or 'nenhuma'}")
    print(f"\n  >> Veredito: {'SESSÃO VÁLIDA' if autenticado else 'SESSÃO INVÁLIDA — o login não pegou'}")

    registro["sessao_autenticada"] = autenticado
    registro["cookies"] = sorted(cliente.session.cookies.keys())
    print(f"  cookies da sessão: {', '.join(registro['cookies']) or 'nenhum'}")

    # --- 2. Ids reconhecidos pelo dashboard ---------------------------
    if pagina:
        ids = set(re.findall(r'unidade[_-]?organizacionais?/([A-Za-z0-9_-]{6,})', pagina))
        ids |= set(re.findall(r'"unidade_organizacional_id"\s*:\s*"([A-Za-z0-9_-]{6,})"', pagina))
        registro["ids_no_dashboard"] = sorted(ids)
        print("\n" + "=" * 72)
        print("2. IDS DE UNIDADE QUE APARECEM NO DASHBOARD")
        print("=" * 72)
        if ids:
            for uid in sorted(ids):
                print(f"  {uid}{'   <-- é o do config.json' if uid == unidade else ''}")
            if unidade not in ids:
                print(f"\n  >> ATENÇÃO: {unidade} (do config.json) NÃO aparece no dashboard.")
                print("     Id provavelmente obsoleto — forte candidato a causa do erro 500.")
        else:
            print("  Nenhum id encontrado no HTML (o dashboard pode montar a lista via JavaScript).")

    # --- 3. Variantes de parâmetro ------------------------------------
    print("\n" + "=" * 72)
    print("3. LISTAGEM COM DIFERENTES FORMATOS DE PARÂMETRO")
    print("=" * 72)

    caminho = config["prodoc"]["endpoint_paginate"].format(
        instituicao_id=instituicao, unidade_id=unidade
    )
    tentativas = []
    for rotulo, params in VARIANTES:
        try:
            resposta = cliente._get(caminho, params=params, headers={
                "X-Requested-With": "XMLHttpRequest",
                "Accept": "application/json, text/javascript, */*; q=0.01",
            })
        except ProdocError as e:
            print(f"\n  [{rotulo}] erro de rede: {e}")
            tentativas.append({"variante": rotulo, "erro": str(e)})
            continue

        tipo = resposta.headers.get("Content-Type", "").split(";")[0]
        eh_json = "json" in tipo.lower()
        marca = "OK" if resposta.status_code == 200 and eh_json else "falhou"
        print(f"\n  [{rotulo}] -> HTTP {resposta.status_code} | {tipo or 'sem tipo'} | {marca}")
        print(f"      {_resumo_do_corpo(resposta.text)[:180]}")

        tentativas.append({
            "variante": rotulo, "params": {k: str(v) for k, v in params.items()},
            "status": resposta.status_code, "content_type": tipo,
            "corpo": resposta.text[:4000],
        })
        if resposta.status_code == 200 and eh_json:
            print("      >> ESTA FUNCIONOU.")

    registro["tentativas"] = tentativas
    with open(ARQUIVO_RESPOSTAS, "w", encoding="utf-8") as arquivo:
        json.dump(registro, arquivo, ensure_ascii=False, indent=2)

    vencedoras = [t for t in tentativas if t.get("status") == 200 and "json" in (t.get("content_type") or "")]
    print("\n" + "=" * 72)
    if vencedoras:
        print(f"CONCLUSÃO: {len(vencedoras)} variante(s) funcionaram. A primeira é a correta:")
        print(f"  {vencedoras[0]['variante']}")
    elif not autenticado:
        print("CONCLUSÃO: o login não está pegando. O erro 500 é consequência disso.")
    else:
        print("CONCLUSÃO: sessão válida, mas nenhuma variante funcionou.")
        print("Provável causa: o id da unidade ou o caminho do endpoint mudaram.")
    print(f"Detalhes completos em {ARQUIVO_RESPOSTAS}")
    print("=" * 72 + "\n")
    return 0
