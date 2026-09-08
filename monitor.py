#!/usr/bin/env python3
"""Orquestração de uma execução do monitor.

Ordem que importa: os documentos só são marcados como notificados DEPOIS do
envio bem-sucedido. Marcar antes faria uma falha de envio apagar os documentos
para sempre — eles nunca mais apareceriam, já que também não ficam lidos no
Prodoc.
"""
from __future__ import annotations

import json
import logging

import documentos as doc_utils
import entrega
from configuracao import secoes_ativas
from estado import Estado
from fuso import agora
from prodoc_client import ProdocClient, ProdocError

logger = logging.getLogger(__name__)

ARQUIVO_RESULTADO = "resultado_prodoc.json"

ALERTA_AUTENTICACAO = "autenticacao"
ALERTA_LISTAGEM = "listagem"
ALERTA_RESUMO = "resumo"
ALERTA_FILTRO = "filtro_destino"


def _alertar(config: dict, estado: Estado, tipo: str, titulo: str, detalhe: str) -> None:
    """Manda o alerta ao grupo, respeitando o cooldown por tipo.

    Sem cooldown, uma falha persistente viraria uma mensagem por hora. Se o
    próprio canal estiver fora, sobra o log — e o código de saída != 0, que o
    systemd registra.
    """
    if not estado.pode_alertar(tipo):
        logger.warning("Alerta '%s' suprimido pelo cooldown. Detalhe: %s", tipo, detalhe)
        return
    try:
        entrega.enviar(config, entrega.formatar_alerta(titulo, detalhe))
        logger.info("Alerta '%s' enviado ao grupo.", tipo)
    except entrega.EntregaError as e:
        logger.error("Não consegui enviar o alerta '%s' (%s). Detalhe original: %s", tipo, e, detalhe)
    estado.registrar_alerta(tipo, detalhe)


def _coletar(cliente: ProdocClient, config: dict, estado: Estado) -> tuple[list[dict], list[str]]:
    """Lê as seções ativas e devolve (documentos inéditos, seções cujo filtro foi ignorado)."""
    parametros = config.get("resumo", {})
    campos_trecho = parametros.get("campos_trecho", []) or []
    minimo = parametros.get("min_caracteres_trecho", 80)

    novos: list[dict] = []
    filtros_ignorados: list[str] = []
    for secao in secoes_ativas(config):
        nome = secao.get("nome", "seção sem nome")
        unidade = secao.get("unidade_organizacional_id")
        if not unidade:
            logger.error("Seção '%s' sem unidade_organizacional_id no config. Pulando.", nome)
            continue

        crus = cliente.listar_documentos(unidade)

        # Entrada e saída pedem critérios diferentes de "novidade". Um documento
        # recebido é novo enquanto não foi lido. Um documento que a própria
        # unidade emitiu nasce com lido=True — nos 10 da amostra, sem exceção —
        # então "não lido" nunca o selecionaria. Para esses, novidade é
        # simplesmente ainda não ter sido avisado, e quem garante isso é o
        # estado persistente.
        entrada = [d for d in crus if not doc_utils.eh_saida(d) and doc_utils.esta_nao_lido(d)]
        saida = [d for d in crus if doc_utils.eh_saida(d)] if secao.get("monitorar_saida") else []
        if saida:
            logger.info("Seção %s: %d documento(s) emitidos pela própria seção.", nome, len(saida))

        normalizados = doc_utils.normalizar_lista(entrada + saida, nome, campos_trecho, minimo)

        # A caixa hoje só recebe ABM, mas se passar a receber outra seção o
        # filtro evita avisar o que não é desta seção.
        filtro = secao.get("filtro_destino")
        if filtro and normalizados:
            if not any(d.get("destino") for d in normalizados):
                # Se o campo sumir do payload, filtrar por ele descartaria tudo
                # em silêncio — e silêncio é indistinguível de "nada novo".
                # Melhor avisar demais do que emudecer sem ninguém perceber.
                logger.warning(
                    "Seção %s: nenhum documento traz o campo 'destino'. O filtro "
                    "'%s' foi ignorado para não descartar tudo em silêncio.",
                    nome, filtro,
                )
                filtros_ignorados.append(nome)
            else:
                antes = len(normalizados)
                normalizados = [d for d in normalizados if d.get("destino") == filtro]
                if antes != len(normalizados):
                    logger.info(
                        "Seção %s: %d documento(s) descartados por destino != %s.",
                        nome, antes - len(normalizados), filtro,
                    )

        ineditos = estado.filtrar_novos(normalizados)
        logger.info(
            "Seção %s: %d na listagem, %d recebidos não lidos, %d emitidos, %d ainda não avisados.",
            nome, len(crus), len(entrada), len(saida), len(ineditos),
        )
        novos.extend(ineditos)

    return novos, filtros_ignorados


def _salvar_resultado(config: dict, novos: list[dict]) -> None:
    """Grava a última execução em disco — só para depuração local (gitignored)."""
    formato = config.get("output", {}).get("timestamp_format", "%Y-%m-%d %H:%M:%S")
    dados = {
        "timestamp": agora().strftime(formato),
        "total_documentos": len(novos),
        "documentos": novos,
    }
    try:
        with open(ARQUIVO_RESULTADO, "w", encoding="utf-8") as arquivo:
            json.dump(dados, arquivo, ensure_ascii=False, indent=2)
    except OSError as e:
        logger.warning("Não consegui gravar %s: %s", ARQUIVO_RESULTADO, e)


def executar(
    config: dict,
    credenciais: dict[str, str],
    dry_run: bool = False,
    marcar_sem_enviar: bool = False,
) -> int:
    parametros_estado = config.get("estado", {})
    estado = Estado(
        caminho=parametros_estado.get("arquivo", "estado_notificados.json"),
        reter_dias=parametros_estado.get("reter_dias", 90),
        cooldown_alerta_horas=parametros_estado.get("cooldown_alerta_horas", 24),
    )

    secoes = secoes_ativas(config)
    if not secoes:
        logger.error("Nenhuma seção ativa em config.json['secoes'].")
        return 1

    cliente = ProdocClient(config, credenciais["PRODOC_USER"], credenciais["PRODOC_PASSWORD"])

    # --- autenticação --------------------------------------------------
    try:
        cliente.autenticar()
        cliente.validar_sessao(secoes[0]["unidade_organizacional_id"])
    except ProdocError as e:
        _alertar(config, estado, ALERTA_AUTENTICACAO, "Não consegui entrar no Prodoc", str(e))
        estado.salvar()
        return 1
    estado.limpar_alerta(ALERTA_AUTENTICACAO)

    # --- coleta ---------------------------------------------------------
    try:
        novos, filtros_ignorados = _coletar(cliente, config, estado)
    except ProdocError as e:
        _alertar(config, estado, ALERTA_LISTAGEM, "Falha ao ler a caixa do Prodoc", str(e))
        estado.salvar()
        return 1
    estado.limpar_alerta(ALERTA_LISTAGEM)

    if filtros_ignorados:
        _alertar(
            config, estado, ALERTA_FILTRO,
            "O campo de seção sumiu da listagem do Prodoc",
            "Seções afetadas: {}. Os documentos estão sendo avisados sem o filtro, "
            "então pode chegar coisa de outra seção. O formato do Prodoc mudou.".format(
                ", ".join(filtros_ignorados)
            ),
        )
    else:
        estado.limpar_alerta(ALERTA_FILTRO)

    if not novos:
        logger.info("Nenhum documento novo. Nada a enviar.")
        estado.salvar()  # aproveita para podar registros antigos
        return 0

    # --- adoção do backlog ----------------------------------------------
    if marcar_sem_enviar:
        # Usado na estreia: a caixa acumulou meses de não lidos e despejar tudo
        # de uma vez no grupo não ajuda ninguém. Não gera resumo, então também
        # não gasta chamada de API.
        estado.marcar_notificados(novos)
        estado.salvar()
        print(f"\n{len(novos)} documento(s) marcados como avisados, sem enviar nada:\n")
        for documento in novos:
            print(f"  {documento['numero']} — {documento['assunto'][:70]}")
        print("\nA partir daqui, só o que chegar de novo será avisado.\n")
        return 0

    # --- resumos --------------------------------------------------------
    from resumo import Resumidor  # importado aqui para o dry-run falhar cedo se faltar a chave

    resumidor = Resumidor(config, credenciais["CLAUDE_API_KEY"])
    resumidor.resumir_lista(novos)

    if resumidor.falhas == len(novos):
        _alertar(config, estado, ALERTA_RESUMO, "A API do Claude não respondeu",
                 f"Nenhum dos {len(novos)} documentos pôde ser resumido. Eles vão sem resumo.")
    elif resumidor.falhas == 0:
        estado.limpar_alerta(ALERTA_RESUMO)

    _salvar_resultado(config, novos)

    # --- entrega --------------------------------------------------------
    secao_titulo = novos[0]["secao"] if len({d["secao"] for d in novos}) == 1 else "todas as seções"
    mensagem = entrega.formatar_mensagem(novos, secao_titulo)

    if dry_run:
        print("\n" + "=" * 72)
        print("DRY-RUN — mensagem que seria enviada (nada foi enviado, estado intocado)")
        print("=" * 72 + "\n")
        limite = config.get("entrega", {}).get("max_caracteres_mensagem", 3500)
        for numero, bloco in enumerate(entrega.dividir(mensagem, limite), 1):
            print(f"--- bloco {numero} ({len(bloco)} caracteres) ---")
            print(bloco)
            print()
        return 0

    try:
        blocos = entrega.enviar(config, mensagem)
    except entrega.EntregaError as e:
        # Estado NÃO é marcado: os documentos precisam ser reenviados no próximo ciclo.
        logger.error("Envio falhou: %s", e)
        estado.salvar()
        return 1

    estado.marcar_notificados(novos)
    estado.salvar()
    logger.info("%d documento(s) enviados em %d bloco(s).", len(novos), blocos)
    return 0
