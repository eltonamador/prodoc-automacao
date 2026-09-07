#!/usr/bin/env python3
"""Cliente HTTP do Prodoc — estritamente somente-leitura de listagens.

Trava de segurança do projeto: abrir um documento no Prodoc marca ele como lido,
e isso é exatamente o que este monitor não pode fazer. Por isso todo o tráfego
passa por um único método `_get`, que valida a URL contra uma allowlist derivada
do config.json. Qualquer rota de detalhe/abertura levanta RotaNaoPermitida em
vez de ser requisitada.
"""
from __future__ import annotations

import html
import logging
import re
import urllib.parse

import requests

logger = logging.getLogger(__name__)


class ProdocError(Exception):
    """Falha de comunicação, autenticação ou formato de resposta do Prodoc."""


class RotaNaoPermitida(ProdocError):
    """Tentativa de acessar rota fora da allowlist de somente-leitura."""


def _regex_da_rota(template: str) -> str:
    """Converte um template de endpoint do config em regex de path.

    "/instituicoes/{instituicao_id}/.../paginate/select"
        -> r"^/instituicoes/[^/]+/\\.\\.\\./paginate/select$"

    Derivar a allowlist do próprio config evita que ela fique dessincronizada
    quando o endpoint mudar.
    """
    partes = re.split(r"\{[a-z_]+\}", template)
    return "^" + "[^/]+".join(re.escape(p) for p in partes) + "$"


class ProdocClient:
    """Sessão autenticada no Prodoc, limitada a leitura de listagens."""

    def __init__(self, config: dict, usuario: str, senha: str):
        prodoc = config["prodoc"]
        self.base_url = prodoc["base_url"].rstrip("/")
        self.timeout = prodoc.get("timeout", 30)
        self.instituicao_id = prodoc["instituicao_id"]
        self.endpoint_paginate = prodoc["endpoint_paginate"]
        self.parametros_listagem = dict(config.get("api_parameters", {}))

        self._usuario = usuario
        self._senha = senha
        self._autenticado = False

        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "prodoc-automacao/2.0 (monitor somente-leitura)",
            "Accept-Language": "pt-BR,pt;q=0.9",
        })

        # Allowlist: login, telas de navegação e a listagem paginada. Nada mais.
        self._rotas_permitidas = [
            re.compile(r"^/$"),
            re.compile(r"^/login$"),
            re.compile(r"^/dashboard/?$"),
            re.compile(_regex_da_rota(self.endpoint_paginate)),
        ]

    # ------------------------------------------------------------------
    # Camada de transporte
    # ------------------------------------------------------------------

    def _validar_rota(self, url: str) -> None:
        path = urllib.parse.urlparse(url).path
        for padrao in self._rotas_permitidas:
            if padrao.match(path):
                return
        raise RotaNaoPermitida(
            f"Rota bloqueada pela allowlist de somente-leitura: {path}. "
            "Abrir documentos marcaria a mensagem como lida no Prodoc."
        )

    def _get(self, caminho: str, **kwargs) -> requests.Response:
        """Único ponto de saída HTTP de leitura. Valida a rota antes de chamar."""
        url = f"{self.base_url}{caminho}"
        self._validar_rota(url)
        try:
            return self.session.get(url, timeout=self.timeout, **kwargs)
        except requests.RequestException as e:
            raise ProdocError(f"Erro de rede em GET {caminho}: {e}") from e

    # ------------------------------------------------------------------
    # Autenticação
    # ------------------------------------------------------------------

    def _extrair_csrf(self, html_login: str) -> str | None:
        """Procura o CSRF token em três lugares, do mais específico ao mais estável.

        O regex do <meta> sozinho era o ponto único de falha da versão anterior:
        qualquer mudança no HTML do Prodoc derrubava a autenticação em silêncio.
        O cookie XSRF-TOKEN (padrão do Laravel) sobrevive a mudanças de layout.
        """
        for padrao, origem in (
            (r'<meta[^>]+name=["\']csrf-token["\'][^>]+content=["\']([^"\']+)', "meta csrf-token"),
            (r'<input[^>]+name=["\']_token["\'][^>]+value=["\']([^"\']+)', "input _token"),
            (r'<input[^>]+value=["\']([^"\']+)["\'][^>]+name=["\']_token["\']', "input _token (invertido)"),
        ):
            achado = re.search(padrao, html_login, re.IGNORECASE)
            if achado:
                logger.info("CSRF token obtido via %s", origem)
                return html.unescape(achado.group(1))

        cookie = self.session.cookies.get("XSRF-TOKEN")
        if cookie:
            logger.info("CSRF token obtido via cookie XSRF-TOKEN")
            return urllib.parse.unquote(cookie)

        return None

    def autenticar(self) -> None:
        """Autentica e confirma o sucesso com um teste real de leitura.

        Levanta ProdocError com mensagem específica em cada modo de falha, para
        que o alerta que chega no WhatsApp diga o que quebrou.
        """
        logger.info("Autenticando no Prodoc...")

        pagina = self._get("/login")
        if pagina.status_code != 200:
            raise ProdocError(
                f"Página de login respondeu HTTP {pagina.status_code} (esperado 200). "
                "O Prodoc pode estar fora do ar."
            )

        csrf = self._extrair_csrf(pagina.text)
        if not csrf:
            raise ProdocError(
                "CSRF token não encontrado na página de login. "
                "O HTML do Prodoc provavelmente mudou — é preciso revisar "
                "_extrair_csrf() em prodoc_client.py."
            )

        try:
            resposta = self.session.post(
                f"{self.base_url}/login",
                data={"email": self._usuario, "password": self._senha, "_token": csrf},
                headers={"X-XSRF-TOKEN": csrf},
                timeout=self.timeout,
                allow_redirects=True,
            )
        except requests.RequestException as e:
            raise ProdocError(f"Erro de rede ao enviar credenciais: {e}") from e

        if resposta.status_code >= 500:
            raise ProdocError(
                f"Prodoc respondeu HTTP {resposta.status_code} ao login (erro do servidor)."
            )

        self._autenticado = True
        logger.info("POST de login concluído (HTTP %s). Validando sessão...", resposta.status_code)

    def validar_sessao(self, unidade_id: str) -> None:
        """Confirma a autenticação pelo único teste que importa: a listagem devolve JSON.

        A versão anterior considerava sucesso "a URL final não contém /login", o
        que passa mesmo quando o Prodoc devolve a tela de login com HTTP 200.
        """
        resposta = self._requisitar_listagem(unidade_id, length=1)
        tipo = resposta.headers.get("Content-Type", "")
        if resposta.status_code != 200:
            raise ProdocError(
                f"Listagem respondeu HTTP {resposta.status_code} — sessão provavelmente inválida "
                "(usuário ou senha incorretos?)."
            )
        if "json" not in tipo.lower():
            raise ProdocError(
                "Listagem devolveu '{}' em vez de JSON — o Prodoc retornou a tela "
                "de login. Credenciais inválidas ou sessão recusada.".format(tipo or "sem Content-Type")
            )
        logger.info("Sessão validada: a listagem respondeu JSON.")

    # ------------------------------------------------------------------
    # Leitura de listagens
    # ------------------------------------------------------------------

    def _requisitar_listagem(self, unidade_id: str, length: int | None = None) -> requests.Response:
        caminho = self.endpoint_paginate.format(
            instituicao_id=self.instituicao_id,
            unidade_id=unidade_id,
        )
        params = dict(self.parametros_listagem)
        if length is not None:
            params["length"] = length
        return self._get(caminho, params=params, headers={
            "X-Requested-With": "XMLHttpRequest",
            "Accept": "application/json, text/javascript, */*; q=0.01",
        })

    def listar_documentos(self, unidade_id: str) -> list[dict]:
        """Devolve a lista crua de tramitações da unidade, sem abrir nada."""
        resposta = self._requisitar_listagem(unidade_id)
        if resposta.status_code != 200:
            raise ProdocError(
                f"Listagem da unidade {unidade_id} respondeu HTTP {resposta.status_code}."
            )
        try:
            dados = resposta.json()
        except ValueError as e:
            raise ProdocError(
                f"Listagem da unidade {unidade_id} não devolveu JSON válido."
            ) from e
        if not isinstance(dados, dict) or "data" not in dados:
            forma = list(dados)[:10] if isinstance(dados, dict) else type(dados).__name__
            raise ProdocError(
                f"Formato inesperado na listagem da unidade {unidade_id}: chaves {forma}."
            )
        return dados.get("data") or []

    def buscar_html(self, caminho: str) -> str:
        """GET de uma tela de navegação permitida (usado só pelo modo descobrir)."""
        resposta = self._get(caminho)
        if resposta.status_code != 200:
            raise ProdocError(f"{caminho} respondeu HTTP {resposta.status_code}.")
        return resposta.text
