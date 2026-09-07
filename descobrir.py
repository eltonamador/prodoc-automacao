#!/usr/bin/env python3
"""Modo descoberta: responde as duas perguntas que travam o resto do projeto.

1. Qual é o unidade_organizacional_id da seção ABM?
2. Qual campo do JSON de listagem carrega o "trecho" do documento?

É estritamente somente-leitura: usa apenas rotas da allowlist do ProdocClient,
então nenhum documento é aberto nem marcado como lido.
"""
from __future__ import annotations

import json
import logging
import re

from prodoc_client import ProdocClient, ProdocError

logger = logging.getLogger(__name__)

ARQUIVO_PAYLOAD = "descoberta_payload.json"
ARQUIVO_UNIDADES = "descoberta_unidades.json"
MAX_UNIDADES_SONDADAS = 30
TAMANHO_MINIMO_TRECHO = 80


# ----------------------------------------------------------------------
# 1. Descoberta das unidades/seções
# ----------------------------------------------------------------------

def extrair_unidades_do_html(pagina: str) -> list[dict[str, str]]:
    """Garimpa ids de unidade organizacional e seus rótulos no HTML do dashboard.

    O Prodoc não expõe um endpoint de listagem de unidades que a allowlist
    permita, então os candidatos vêm de três formatos que costumam aparecer
    na mesma página: <option>, links e blobs JSON embutidos.
    """
    encontrados = {}

    def registrar(uid: str | None, rotulo: str | None, origem: str) -> None:
        if not uid:
            return
        atual = encontrados.setdefault(uid, {"id": uid, "rotulo": None, "origens": []})
        if rotulo and not atual["rotulo"]:
            atual["rotulo"] = re.sub(r"\s+", " ", rotulo).strip()
        if origem not in atual["origens"]:
            atual["origens"].append(origem)

    for uid, rotulo in re.findall(
        r'<option[^>]+value=["\']([A-Za-z0-9_-]{6,})["\'][^>]*>([^<]{2,120})</option>', pagina
    ):
        registrar(uid, rotulo, "option")

    for uid in re.findall(r'unidade[_-]?organizacionais?/([A-Za-z0-9_-]{6,})', pagina):
        registrar(uid, None, "link")

    for uid in re.findall(r'"unidade_organizacional_id"\s*:\s*"([A-Za-z0-9_-]{6,})"', pagina):
        registrar(uid, None, "json")

    for bloco in re.findall(r'\{[^{}]{0,400}?"[a-z_]*nome"[^{}]{0,400}?\}', pagina):
        uid = re.search(r'"id"\s*:\s*"([A-Za-z0-9_-]{6,})"', bloco)
        nome = re.search(r'"[a-z_]*nome"\s*:\s*"([^"]{2,120})"', bloco)
        if uid and nome:
            registrar(uid.group(1), nome.group(1), "json-nome")

    return list(encontrados.values())


def sondar_unidade(cliente: ProdocClient, unidade_id: str) -> dict:
    """Testa se a unidade responde à listagem. Somente leitura."""
    resultado = {"id": unidade_id, "acessivel": False, "total_documentos": 0, "erro": None}
    try:
        documentos = cliente.listar_documentos(unidade_id)
    except ProdocError as e:
        resultado["erro"] = str(e)
        return resultado
    resultado["acessivel"] = True
    resultado["total_documentos"] = len(documentos)
    resultado["nao_lidos"] = sum(1 for d in documentos if _valor_lido(d) is False)
    return resultado


def _valor_lido(documento: dict) -> bool | None:
    """Procura o campo 'lido' nos dois níveis possíveis da estrutura."""
    if isinstance(documento.get("documento"), dict) and "lido" in documento["documento"]:
        return documento["documento"]["lido"]
    return documento.get("lido")


# ----------------------------------------------------------------------
# 2. Mapeamento dos campos (achar o "trecho")
# ----------------------------------------------------------------------

def achatar(objeto, prefixo: str = "") -> dict[str, object]:
    """Achata um dict aninhado em caminhos pontilhados: documento.assunto.nome."""
    plano = {}
    if isinstance(objeto, dict):
        for chave, valor in objeto.items():
            caminho = f"{prefixo}.{chave}" if prefixo else str(chave)
            if isinstance(valor, (dict, list)):
                plano.update(achatar(valor, caminho))
            else:
                plano[caminho] = valor
    elif isinstance(objeto, list):
        # Só o primeiro item: basta para revelar a forma da estrutura.
        if objeto:
            plano.update(achatar(objeto[0], f"{prefixo}[]"))
        else:
            plano[prefixo] = []
    else:
        plano[prefixo] = objeto
    return plano


def mapear_campos(documentos: list[dict]) -> list[dict]:
    """Estatísticas por campo textual, para revelar quem carrega o trecho."""
    acumulado = {}
    for documento in documentos:
        for caminho, valor in achatar(documento).items():
            registro = acumulado.setdefault(
                caminho, {"campo": caminho, "ocorrencias": 0, "tamanhos": [], "exemplo": None, "tipo": None}
            )
            registro["ocorrencias"] += 1
            registro["tipo"] = type(valor).__name__
            if isinstance(valor, str):
                texto = valor.strip()
                registro["tamanhos"].append(len(texto))
                if texto and (registro["exemplo"] is None or len(texto) > len(registro["exemplo"])):
                    registro["exemplo"] = texto

    campos = []
    for registro in acumulado.values():
        tamanhos = registro.pop("tamanhos")
        registro["tamanho_medio"] = round(sum(tamanhos) / len(tamanhos)) if tamanhos else 0
        registro["tamanho_maximo"] = max(tamanhos) if tamanhos else 0
        campos.append(registro)

    campos.sort(key=lambda c: c["tamanho_medio"], reverse=True)
    return campos


def distribuir_valores(documentos: list[dict], campos: list[str], limite: int = 12) -> dict:
    """Contagem dos valores de campos categóricos.

    É o que diz se a caixa já vem filtrada por seção ou se é preciso filtrar
    no código — e quais outros valores existiriam para configurar depois.
    """
    resultado = {}
    for campo in campos:
        contagem: dict[str, int] = {}
        for documento in documentos:
            valor = valor_em(documento, campo)
            if valor is None:
                continue
            chave = str(valor)[:60]
            contagem[chave] = contagem.get(chave, 0) + 1
        if contagem:
            ordenado = sorted(contagem.items(), key=lambda par: -par[1])
            resultado[campo] = {"total_distintos": len(ordenado), "top": ordenado[:limite]}
    return resultado


def valor_em(documento: dict, caminho: str):
    atual = documento
    for parte in caminho.split("."):
        if not isinstance(atual, dict) or parte not in atual:
            return None
        atual = atual[parte]
    return atual


def candidatos_a_trecho(campos: list[dict]) -> list[dict]:
    """Campos textuais longos o bastante para render um resumo de verdade."""
    return [
        c for c in campos
        if c["tipo"] == "str" and c["tamanho_medio"] >= TAMANHO_MINIMO_TRECHO
    ]


# ----------------------------------------------------------------------
# Execução
# ----------------------------------------------------------------------

def executar(config: dict, credenciais: dict[str, str]) -> int:
    cliente = ProdocClient(config, credenciais["PRODOC_USER"], credenciais["PRODOC_PASSWORD"])
    cliente.autenticar()

    secoes = config.get("secoes") or []
    unidade_base = secoes[0]["unidade_organizacional_id"] if secoes else None
    if not unidade_base:
        logger.error("Nenhuma seção configurada em config.json['secoes'].")
        return 1

    cliente.validar_sessao(unidade_base)

    # --- Unidades disponíveis -----------------------------------------
    print("\n" + "=" * 72)
    print("1. UNIDADES / SEÇÕES VISÍVEIS NA SUA CONTA")
    print("=" * 72)

    try:
        pagina = cliente.buscar_html("/dashboard")
        candidatos = extrair_unidades_do_html(pagina)
    except ProdocError as e:
        logger.warning("Não consegui ler o dashboard (%s). Sigo só com a unidade do config.", e)
        candidatos = []

    conhecidos = {s["unidade_organizacional_id"] for s in secoes if s.get("unidade_organizacional_id")}
    for uid in conhecidos:
        if not any(c["id"] == uid for c in candidatos):
            candidatos.append({"id": uid, "rotulo": "(do config.json)", "origens": ["config"]})

    if len(candidatos) > MAX_UNIDADES_SONDADAS:
        logger.warning(
            "%d candidatos encontrados; sondando só os %d primeiros.",
            len(candidatos), MAX_UNIDADES_SONDADAS,
        )
        candidatos = candidatos[:MAX_UNIDADES_SONDADAS]

    unidades = []
    for candidato in candidatos:
        sonda = sondar_unidade(cliente, candidato["id"])
        sonda["rotulo"] = candidato.get("rotulo")
        sonda["origens"] = candidato.get("origens", [])
        unidades.append(sonda)

    acessiveis = [u for u in unidades if u["acessivel"]]
    if not acessiveis:
        print("\n  Nenhuma unidade respondeu à listagem além da configurada.")
    for unidade in sorted(acessiveis, key=lambda u: u["total_documentos"], reverse=True):
        marca = " <-- está no config.json" if unidade["id"] in conhecidos else ""
        print("\n  id: {}{}".format(unidade["id"], marca))
        print("  rótulo: {}".format(unidade.get("rotulo") or "(não identificado no HTML)"))
        print("  documentos na listagem: {} (não lidos: {})".format(
            unidade["total_documentos"], unidade.get("nao_lidos", "?")))

    inacessiveis = [u for u in unidades if not u["acessivel"]]
    if inacessiveis:
        print("\n  Candidatos que não respondem como unidade (ignore): {}".format(
            ", ".join(u["id"] for u in inacessiveis)))

    print("\n  >> Compare com a URL do dashboard para identificar qual é a ABM.")

    with open(ARQUIVO_UNIDADES, "w", encoding="utf-8") as arquivo:
        json.dump(unidades, arquivo, ensure_ascii=False, indent=2)
    print(f"  >> Detalhes salvos em {ARQUIVO_UNIDADES}")

    # --- Campos da listagem -------------------------------------------
    print("\n" + "=" * 72)
    print("2. CAMPOS DA LISTAGEM (procurando o 'trecho' do documento)")
    print("=" * 72)

    documentos = cliente.listar_documentos(unidade_base)
    if not documentos:
        print(f"\n  A listagem da unidade {unidade_base} voltou vazia — sem amostra para mapear.")
        print("  Rode de novo quando houver documentos na caixa.")
        return 0

    campos = mapear_campos(documentos)
    candidatos_trecho = candidatos_a_trecho(campos)

    print(f"\n  {len(documentos)} documentos na amostra, {len(campos)} campos distintos.")
    print(f"\n  Campos textuais com média >= {TAMANHO_MINIMO_TRECHO} caracteres (candidatos a trecho):")
    if not candidatos_trecho:
        print("\n    NENHUM. A listagem não traz trecho utilizável —")
        print("    o resumo vai usar o fallback de só o título.")
    for campo in candidatos_trecho:
        exemplo = (campo["exemplo"] or "")[:160].replace("\n", " ")
        print("\n    {}".format(campo["campo"]))
        print("      média {} / máx {} caracteres".format(campo["tamanho_medio"], campo["tamanho_maximo"]))
        print(f"      exemplo: {exemplo}...")

    print("\n  Demais campos textuais (curtos — servem de metadado, não de trecho):")
    curtos = [c for c in campos if c["tipo"] == "str" and c not in candidatos_trecho]
    for campo in curtos[:20]:
        exemplo = (campo["exemplo"] or "")[:60].replace("\n", " ")
        print("    {:<50} {:>4}c  {}".format(campo["campo"][:50], campo["tamanho_medio"], exemplo))

    print("\n  Campo 'lido' localizado em: {}".format(
        ", ".join(c["campo"] for c in campos if c["campo"].endswith("lido")) or "NÃO ENCONTRADO"))

    # --- distribuição dos campos categóricos --------------------------
    print("\n" + "=" * 72)
    print("3. DISTRIBUIÇÃO DOS CAMPOS DE CLASSIFICAÇÃO")
    print("=" * 72)

    campos_categoricos = [
        "destino", "destino_nome", "instituicao_destino", "instituicao_origem",
        "tipo.nome", "copia", "distribuicao", "apenas_criado", "documento.lido",
    ]
    distribuicao = distribuir_valores(documentos, campos_categoricos)
    for campo, dados in distribuicao.items():
        print(f"\n  {campo}  ({dados['total_distintos']} valor(es) distinto(s))")
        for valor, quantidade in dados["top"]:
            print(f"      {quantidade:>4}x  {valor}")

    nao_lidos = [d for d in documentos if valor_em(d, "documento.lido") is False]
    print(f"\n  Não lidos na amostra: {len(nao_lidos)} de {len(documentos)}")
    if nao_lidos:
        print("  Destino dos não lidos:")
        for valor, quantidade in distribuir_valores(nao_lidos, ["destino"]).get(
            "destino", {}
        ).get("top", []):
            print(f"      {quantidade:>4}x  {valor}")

    amostra = {
        "distribuicao": distribuicao,
        "unidade_organizacional_id": unidade_base,
        "total_documentos": len(documentos),
        "campos": campos,
        "documento_exemplo": documentos[0],
    }
    with open(ARQUIVO_PAYLOAD, "w", encoding="utf-8") as arquivo:
        json.dump(amostra, arquivo, ensure_ascii=False, indent=2)

    print(f"\n  >> Payload completo salvo em {ARQUIVO_PAYLOAD} (não é commitado).")
    print("\n" + "=" * 72)
    print("Próximo passo: me diga qual id é a ABM e qual campo tem o trecho,")
    print("ou apenas cole a saída acima.")
    print("=" * 72 + "\n")
    return 0
