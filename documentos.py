#!/usr/bin/env python3
"""Leitura dos documentos devolvidos pela listagem do Prodoc.

A versão anterior tinha um bug silencioso aqui: o filtro lia `documento.lido`
(aninhado) enquanto os metadados liam `numero`/`tipo`/`assunto` no nível de
cima. Se esses campos moram dentro de `documento`, todo prompt ia para o Claude
com "N/A". Em vez de apostar num nível, cada campo agora é procurado numa lista
de caminhos candidatos — funciona nas duas formas e sobrevive a mudanças.
"""
from __future__ import annotations

import logging
from collections.abc import Sequence

from estado import chave_do_documento

logger = logging.getLogger(__name__)

CAMINHOS = {
    "id": ["documento.id", "id", "documento_id"],
    "numero": ["numero", "documento.numero", "documento_numero", "documento.numero_documento"],
    "tipo": ["tipo.nome", "documento.tipo.nome", "tipo_documento.nome",
             "documento.tipo_documento.nome", "tipo"],
    "assunto": ["assunto.assunto", "assunto.nome", "documento.assunto.assunto",
                "documento.assunto.nome", "assunto", "documento.assunto"],
    "remetente": ["instituicao_origem_nome", "documento.instituicao_origem_nome",
                  "unidade_organizacional_origem_nome", "origem.nome", "remetente.nome", "remetente"],
    "data": ["data_criacao", "documento.data_criacao", "created_at", "documento.created_at",
             "data_tramitacao", "documento_tramitacao.created_at"],
    "lido": ["documento.lido", "lido", "documento_tramitacao.lido"],
}

SEM_VALOR = ("", "N/A", "None", None)


def valor_em(documento: dict, caminho: str):
    """Busca um caminho pontilhado ('documento.assunto.nome'). None se faltar."""
    atual = documento
    for parte in caminho.split("."):
        if not isinstance(atual, dict) or parte not in atual:
            return None
        atual = atual[parte]
    return atual


def primeiro_valor(documento: dict, caminhos: Sequence[str], padrao=None):
    """Primeiro caminho que devolve algo com conteúdo."""
    for caminho in caminhos:
        valor = valor_em(documento, caminho)
        if isinstance(valor, str):
            valor = valor.strip()
        if valor not in SEM_VALOR and valor is not None:
            return valor
    return padrao


def esta_nao_lido(documento: dict) -> bool:
    """True apenas quando o Prodoc afirma que não foi lido.

    Ausência do campo conta como lido, para nunca inundar o grupo por engano
    quando o formato mudar.
    """
    return primeiro_valor(documento, CAMINHOS["lido"], padrao=None) is False


def escolher_trecho(documento: dict, campos: Sequence[str], minimo: int) -> tuple[str | None, str | None]:
    """Primeiro campo configurado com texto longo o bastante para resumir.

    Devolve (texto, nome_do_campo). (None, None) aciona o fallback de só título.
    """
    for campo in campos:
        valor = valor_em(documento, campo)
        if isinstance(valor, str):
            texto = " ".join(valor.split())
            if len(texto) >= minimo:
                return texto, campo
    return None, None


def normalizar(documento: dict, secao: str, campos_trecho: Sequence[str], minimo: int) -> dict:
    """Converte um item cru da listagem no formato usado pelo resto do monitor."""
    numero = str(primeiro_valor(documento, CAMINHOS["numero"], padrao="sem número"))
    assunto = str(primeiro_valor(documento, CAMINHOS["assunto"], padrao="sem assunto"))
    documento_id = primeiro_valor(documento, CAMINHOS["id"])
    trecho, campo_trecho = escolher_trecho(documento, campos_trecho, minimo)

    return {
        "chave": chave_do_documento(documento_id, numero, assunto),
        "id": documento_id,
        "secao": secao,
        "numero": numero,
        "tipo": str(primeiro_valor(documento, CAMINHOS["tipo"], padrao="documento")),
        "assunto": assunto,
        "remetente": str(primeiro_valor(documento, CAMINHOS["remetente"], padrao="origem não informada")),
        "data": str(primeiro_valor(documento, CAMINHOS["data"], padrao="")),
        "trecho": trecho,
        "campo_trecho": campo_trecho,
    }


def normalizar_lista(documentos: list[dict], secao: str,
                     campos_trecho: Sequence[str], minimo: int) -> list[dict]:
    normalizados = [normalizar(d, secao, campos_trecho, minimo) for d in documentos]

    faltando = [n["numero"] for n in normalizados if n["assunto"] == "sem assunto"]
    if faltando:
        # Sintoma do bug antigo: se TODOS caírem no padrão, os caminhos em
        # CAMINHOS não batem com o payload atual e precisam ser revistos.
        logger.warning(
            "%d de %d documentos sem assunto identificado (ex: %s). "
            "Rode 'descobrir' e confira os caminhos em documentos.CAMINHOS.",
            len(faltando), len(normalizados), ", ".join(faltando[:3]),
        )
    return normalizados
