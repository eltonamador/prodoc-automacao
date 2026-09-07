#!/usr/bin/env python3
"""Geração dos resumos via Claude, em uma única chamada com saída estruturada.

Duas mudanças em relação à versão original, que fazia uma chamada de texto livre
por documento:

1. Todos os documentos vão numa requisição só — menos latência, menos custo, e o
   modelo enxerga a caixa inteira, então consegue comparar urgências entre os
   documentos em vez de julgar cada um no vácuo.
2. A resposta é validada contra um JSON Schema (`output_config.format`), o que
   permite triagem — urgência, se exige ação, prazo — em vez de um parágrafo solto.

Se a saída estruturada falhar por qualquer motivo, o código cai automaticamente
no caminho antigo (uma chamada de texto por documento). Degradar é sempre
preferível a não avisar.
"""

from __future__ import annotations

import json
import logging

import anthropic

logger = logging.getLogger(__name__)

SISTEMA = (
    "Você faz a triagem da caixa de entrada da Academia de Bombeiro Militar do Amapá "
    "(ABM) para um aviso rápido no WhatsApp. Escreva em português do Brasil, sem "
    "markdown e sem repetir o número do documento.\n\n"
    "IMPORTANTE: você recebe apenas a identificação do documento — tipo, assunto e "
    "unidade de origem — e nunca o texto integral, porque abrir o documento o marcaria "
    "como lido. O assunto já será exibido ao leitor logo acima do seu texto, então "
    "parafraseá-lo não ajuda em nada. Escreva o que o assunto NÃO diz: em uma frase "
    "curta, o que isso tende a exigir da Academia, ou a que rotina pertence. Quando não "
    "houver nada a acrescentar, diga que é informativo e pare — uma frase honesta e "
    "curta vale mais que uma paráfrase.\n\n"
    "Nunca invente prazos, números, nomes ou exigências que não estejam nos dados.\n\n"
    "Calibragem da urgência (a maioria dos documentos é baixa):\n"
    "- alta: prazo explícito, convocação, exigência de resposta formal, ou risco "
    "operacional imediato;\n"
    "- media: pede uma providência da Academia, sem prazo declarado;\n"
    "- baixa: informativo, escala de rotina, circular ampla, ou recebido como cópia.\n"
    "Use 'alta' com parcimônia. Se tudo parecer igual, é sinal de que é tudo baixa."
)

ESQUEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["documentos"],
    "properties": {
        "documentos": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["ref", "resumo", "urgencia", "acao_requerida", "prazo"],
                "properties": {
                    "ref": {
                        "type": "integer",
                        "description": "O número de referência do documento na lista enviada.",
                    },
                    "resumo": {
                        "type": "string",
                        "description": (
                            "Uma frase de até 25 palavras que acrescente algo ao assunto, sem "
                            "parafraseá-lo. Até 60 palavras quando houver trecho do documento."
                        ),
                    },
                    "urgencia": {"type": "string", "enum": ["alta", "media", "baixa"]},
                    "acao_requerida": {
                        "type": "boolean",
                        "description": "Verdadeiro só se o documento pedir uma providência do destinatário.",
                    },
                    "prazo": {
                        "type": ["string", "null"],
                        "description": "Prazo citado literalmente no documento, ou null.",
                    },
                },
            },
        }
    },
}

ORIGEM_TRECHO = "trecho"
ORIGEM_TITULO = "titulo"
ORIGEM_ERRO = "erro"

LIMITE_TRECHO = 1200
TOKENS_POR_DOCUMENTO = 220
TOKENS_MINIMO = 1024
TOKENS_MAXIMO = 8192


def _prazo_confiavel(documento: dict, prazo: str | None) -> str | None:
    """Descarta prazo que não esteja ancorado no texto que realmente enviamos.

    Sem o trecho, o modelo só viu tipo, assunto e remetente. Um prazo que não
    aparece nesses campos é invenção — e um prazo falso num aviso do corpo de
    bombeiros é pior do que nenhum prazo.
    """
    if not prazo:
        return None
    if documento.get("trecho"):
        return prazo
    if prazo.strip().lower() in str(documento.get("assunto", "")).lower():
        return prazo
    logger.warning(
        "Prazo '%s' descartado em %s: documento sem trecho e o prazo não está no assunto.",
        prazo, documento.get("numero"),
    )
    return None


def _bloco_do_documento(indice: int, documento: dict) -> str:
    """Monta o que sabemos do documento.

    A listagem do Prodoc não traz o corpo do documento (verificado contra os
    dados reais da ABM), então o que existe é a identificação. Vale reunir tudo
    que ela oferece — inclusive o assunto de distribuição, que às vezes descreve
    o caso em linguagem mais concreta que o assunto formal.
    """
    linhas = [
        f"[{indice}]",
        f"Tipo: {documento['tipo']}",
        f"Assunto: {documento['assunto']}",
    ]
    if documento.get("assunto_alternativo"):
        linhas.append(f"Assunto na distribuição: {documento['assunto_alternativo']}")
    linhas.append(f"Enviado por: {documento['remetente']}")
    if documento.get("copia"):
        linhas.append("Recebido como CÓPIA (encaminhado para conhecimento, não endereçado diretamente).")

    trecho = documento.get("trecho")
    if trecho:
        linhas.append(f"Trecho do documento: {trecho[:LIMITE_TRECHO]}")
    else:
        linhas.append(
            "Texto do documento: NÃO DISPONÍVEL — não abrimos o documento, "
            "para não marcá-lo como lido no sistema."
        )
    return "\n".join(linhas)


def _prompt_lote(documentos: list[dict]) -> str:
    blocos = "\n\n".join(_bloco_do_documento(i, d) for i, d in enumerate(documentos, 1))
    return (
        f"Faça a triagem dos {len(documentos)} documentos abaixo. Devolva um item para "
        f"cada um, usando o mesmo número de referência entre colchetes.\n\n{blocos}"
    )


class Resumidor:
    def __init__(self, config: dict, api_key: str):
        parametros = config.get("resumo", {})
        self.model = parametros.get("model", "claude-haiku-4-5")
        self.tokens_por_documento = parametros.get("max_tokens_por_documento", TOKENS_POR_DOCUMENTO)
        self.estruturado = parametros.get("saida_estruturada", True)
        self.cliente = anthropic.Anthropic(api_key=api_key)
        self.falhas = 0
        self.ultimo_erro = ""

    # ------------------------------------------------------------------

    def _max_tokens(self, quantidade: int) -> int:
        return max(TOKENS_MINIMO, min(TOKENS_MAXIMO, quantidade * self.tokens_por_documento))

    def _chamar(self, prompt: str, max_tokens: int, esquema: dict | None):
        argumentos = {
            "model": self.model,
            "max_tokens": max_tokens,
            "system": SISTEMA,
            "messages": [{"role": "user", "content": prompt}],
        }
        if esquema is not None:
            # Formato confirmado no SDK instalado (anthropic.types.OutputConfigParam):
            # output_config.format = {"type": "json_schema", "schema": ...}
            argumentos["output_config"] = {"format": {"type": "json_schema", "schema": esquema}}
        resposta = self.cliente.messages.create(**argumentos)
        return "".join(
            bloco.text for bloco in resposta.content if getattr(bloco, "type", "") == "text"
        ).strip()

    # ------------------------------------------------------------------

    def _lote_estruturado(self, documentos: list[dict]) -> bool:
        """Uma chamada para todos. True se preencheu os resumos."""
        texto = self._chamar(_prompt_lote(documentos), self._max_tokens(len(documentos)), ESQUEMA)
        dados = json.loads(texto)

        por_ref = {}
        for item in dados.get("documentos", []):
            try:
                por_ref[int(item["ref"])] = item
            except (KeyError, TypeError, ValueError):
                continue

        faltando = 0
        for indice, documento in enumerate(documentos, 1):
            item = por_ref.get(indice)
            if not item or not (item.get("resumo") or "").strip():
                faltando += 1
                documento["resumo"], documento["origem_resumo"] = "", ORIGEM_ERRO
                continue
            documento["resumo"] = item["resumo"].strip()
            documento["origem_resumo"] = ORIGEM_TRECHO if documento.get("trecho") else ORIGEM_TITULO
            documento["urgencia"] = item.get("urgencia", "baixa")
            documento["acao_requerida"] = bool(item.get("acao_requerida"))
            documento["prazo"] = _prazo_confiavel(documento, item.get("prazo"))

        self.falhas = faltando
        if faltando:
            logger.warning("%d de %d documentos vieram sem resumo no lote.", faltando, len(documentos))
        return faltando < len(documentos)

    def _um_a_um(self, documentos: list[dict]) -> None:
        """Caminho de degradação: uma chamada de texto simples por documento."""
        logger.info("Caindo para o modo texto simples, um documento por chamada.")
        self.falhas = 0
        for indice, documento in enumerate(documentos, 1):
            logger.info("Resumindo %d/%d — %s", indice, len(documentos), documento.get("numero"))
            tem_trecho = bool(documento.get("trecho"))
            prompt = (
                f"Resuma em até {'70' if tem_trecho else '25'} palavras:\n\n"
                + _bloco_do_documento(1, documento)
            )
            try:
                texto = self._chamar(prompt, 400, None)
            except (anthropic.APIError, anthropic.APIConnectionError) as e:
                self.falhas += 1
                self.ultimo_erro = str(e)
                logger.error("Falha ao resumir %s: %s", documento.get("numero"), e)
                documento["resumo"], documento["origem_resumo"] = "", ORIGEM_ERRO
                continue

            if texto:
                documento["resumo"] = texto
                documento["origem_resumo"] = ORIGEM_TRECHO if tem_trecho else ORIGEM_TITULO
            else:
                self.falhas += 1
                documento["resumo"], documento["origem_resumo"] = "", ORIGEM_ERRO

    # ------------------------------------------------------------------

    def resumir_lista(self, documentos: list[dict]) -> list[dict]:
        """Preenche resumo, origem_resumo e (quando possível) a triagem."""
        for documento in documentos:
            documento.setdefault("urgencia", "baixa")
            documento.setdefault("acao_requerida", False)
            documento.setdefault("prazo", None)

        if self.estruturado:
            try:
                if self._lote_estruturado(documentos):
                    logger.info("Triagem de %d documentos em uma chamada.", len(documentos))
                    return documentos
                logger.warning("O lote estruturado não resolveu nenhum documento.")
            except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as e:
                # Credencial inválida não melhora tentando de novo.
                self.falhas = len(documentos)
                self.ultimo_erro = str(e)
                logger.error("CLAUDE_API_KEY recusada: %s", e)
                for documento in documentos:
                    documento["resumo"], documento["origem_resumo"] = "", ORIGEM_ERRO
                return documentos
            except (anthropic.APIError, anthropic.APIConnectionError, ValueError, KeyError) as e:
                self.ultimo_erro = str(e)
                logger.warning("Saída estruturada indisponível (%s).", e)

        self._um_a_um(documentos)
        return documentos
