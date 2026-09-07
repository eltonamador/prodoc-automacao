#!/usr/bin/env python3
"""Estado persistente de "o que já foi avisado".

Por que isto existe: o monitor não abre os documentos, então eles nunca ficam
lidos no Prodoc. Antes, era o "lido" que impedia o reprocessamento. Sem este
arquivo, cada execução reenviaria os mesmos documentos ao grupo, para sempre.

Guarda também o último envio de cada tipo de alerta, para que uma falha
persistente não vire uma mensagem por hora.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
from collections.abc import Iterable
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

VERSAO = 1


def chave_do_documento(documento_id: str | None, numero: str, assunto: str) -> str:
    """Identidade estável de um documento.

    Prefere o id do Prodoc. Sem ele, cai num hash de número+assunto — pior que
    o id, mas estável entre execuções, que é o que o dedupe exige.
    """
    if documento_id and str(documento_id) not in ("", "N/A", "None"):
        return f"id:{documento_id}"
    digest = hashlib.sha1(f"{numero}|{assunto}".encode()).hexdigest()[:16]
    return f"hash:{digest}"


class Estado:
    def __init__(self, caminho: str = "estado_notificados.json",
                 reter_dias: int = 90, cooldown_alerta_horas: int = 24):
        self.caminho = caminho
        self.reter_dias = reter_dias
        self.cooldown_alerta_horas = cooldown_alerta_horas
        self.documentos: dict[str, dict] = {}
        self.alertas: dict[str, dict] = {}
        self._carregar()

    # ------------------------------------------------------------------

    def _carregar(self) -> None:
        if not os.path.exists(self.caminho):
            logger.info("Estado novo (arquivo %s ainda não existe).", self.caminho)
            return
        try:
            with open(self.caminho, encoding="utf-8") as arquivo:
                dados = json.load(arquivo)
        except (ValueError, OSError) as e:
            # Um estado corrompido não pode derrubar o monitor, mas perdê-lo em
            # silêncio causaria reenvio de tudo — por isso o arquivo é preservado.
            reserva = f"{self.caminho}.corrompido"
            logger.error("Estado ilegível (%s). Preservando cópia em %s e começando vazio.", e, reserva)
            with contextlib.suppress(OSError):
                os.replace(self.caminho, reserva)
            return

        self.documentos = dados.get("documentos", {}) or {}
        self.alertas = dados.get("alertas", {}) or {}
        logger.info("Estado carregado: %d documentos já notificados.", len(self.documentos))

    def salvar(self) -> None:
        """Grava de forma atômica: escreve num temporário e só então substitui."""
        self._podar()
        dados = {
            "versao": VERSAO,
            "atualizado_em": datetime.now().isoformat(timespec="seconds"),
            "documentos": self.documentos,
            "alertas": self.alertas,
        }
        temporario = f"{self.caminho}.tmp"
        with open(temporario, "w", encoding="utf-8") as arquivo:
            json.dump(dados, arquivo, ensure_ascii=False, indent=2)
        os.replace(temporario, self.caminho)
        logger.info("Estado salvo (%d documentos).", len(self.documentos))

    def _podar(self) -> None:
        limite = datetime.now() - timedelta(days=self.reter_dias)
        antes = len(self.documentos)
        self.documentos = {
            chave: registro
            for chave, registro in self.documentos.items()
            if _data_do_registro(registro, "notificado_em") >= limite
        }
        removidos = antes - len(self.documentos)
        if removidos:
            logger.info("Podados %d registros com mais de %d dias.", removidos, self.reter_dias)

    # --- documentos ---------------------------------------------------

    def ja_notificado(self, chave: str) -> bool:
        return chave in self.documentos

    def filtrar_novos(self, documentos: Iterable[dict]) -> list[dict]:
        """Devolve só os documentos ainda não avisados, na ordem recebida."""
        return [d for d in documentos if not self.ja_notificado(d["chave"])]

    def marcar_notificados(self, documentos: Iterable[dict]) -> None:
        agora = datetime.now().isoformat(timespec="seconds")
        for documento in documentos:
            self.documentos[documento["chave"]] = {
                "notificado_em": agora,
                "numero": documento.get("numero", ""),
                "secao": documento.get("secao", ""),
            }

    # --- alertas ------------------------------------------------------

    def pode_alertar(self, tipo: str) -> bool:
        """True se este tipo de alerta ainda não foi enviado dentro do cooldown."""
        registro = self.alertas.get(tipo)
        if not registro:
            return True
        ultimo = _data_do_registro(registro, "ultimo_em")
        return datetime.now() - ultimo >= timedelta(hours=self.cooldown_alerta_horas)

    def registrar_alerta(self, tipo: str, mensagem: str = "") -> None:
        self.alertas[tipo] = {
            "ultimo_em": datetime.now().isoformat(timespec="seconds"),
            "mensagem": mensagem[:500],
        }

    def limpar_alerta(self, tipo: str) -> None:
        """Chamado quando a execução volta a funcionar, para o próximo alerta sair na hora."""
        self.alertas.pop(tipo, None)


def _data_do_registro(registro: dict, campo: str) -> datetime:
    """Lê uma data ISO do estado; datas ilegíveis são tratadas como muito antigas."""
    try:
        return datetime.fromisoformat(registro.get(campo, ""))
    except (ValueError, TypeError):
        return datetime.min
