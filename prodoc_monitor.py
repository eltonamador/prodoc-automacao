#!/usr/bin/env python3
"""Monitor de documentos não lidos do Prodoc, com resumo via Claude e entrega no WhatsApp.

Comandos:
    configurar      Grava o .env com as credenciais, perguntando no terminal.
    descobrir       Mapeia as seções da conta e os campos do JSON de listagem.
    diagnosticar    Investiga por que a listagem está respondendo erro.
    monitorar       Verifica os documentos novos e envia o resumo.
    testar-envio    Manda uma mensagem de teste pelo canal de entrega.

Princípio central: nenhum documento é aberto. Abrir marca a mensagem como lida
no Prodoc — a trava que garante isso vive em prodoc_client.ProdocClient._get.
"""
from __future__ import annotations

import argparse
import logging
import sys
import warnings

from configuracao import carregar_config, carregar_credenciais
from fuso import agora

# O Python 3.9 do macOS é compilado com LibreSSL, e o urllib3 avisa sobre isso a
# cada execução. É ruído cosmético que atrapalha enxergar o erro real nos logs.
# Fica antes de qualquer import de requests, que é sempre sob demanda.
warnings.filterwarnings("ignore", message=".*OpenSSL 1.1.1+.*")


class _FormatadorHorarioLocal(logging.Formatter):
    """Carimba os logs em horário de Amapá, não no relógio da VPS (UTC).

    O systemd timer já dispara no horário local certo — mas sem isto, o
    'quando aconteceu' dentro do log fica 3h à frente do relógio de quem lê.
    """

    def formatTime(self, record, datefmt=None):
        return agora().strftime(datefmt or "%Y-%m-%d %H:%M:%S")


logging.basicConfig(level=logging.INFO)
logging.getLogger().handlers[0].setFormatter(
    _FormatadorHorarioLocal("%(asctime)s -03 - %(levelname)s - %(message)s")
)
logger = logging.getLogger(__name__)


def _cmd_descobrir(args) -> int:
    import descobrir  # importado sob demanda: não precisa do SDK do Claude
    from prodoc_client import ProdocError

    config = carregar_config(args.config)
    credenciais = carregar_credenciais()
    try:
        return descobrir.executar(config, credenciais)
    except ProdocError as e:
        logger.error("Descoberta interrompida: %s", e)
        return 1


def _cmd_diagnosticar(args) -> int:
    import diagnosticar
    from prodoc_client import ProdocError

    config = carregar_config(args.config)
    credenciais = carregar_credenciais()
    try:
        return diagnosticar.executar(config, credenciais)
    except ProdocError as e:
        logger.error("Diagnóstico interrompido: %s", e)
        return 1


def _cmd_configurar(args) -> int:
    import configurar

    return configurar.executar()


def _cmd_monitorar(args) -> int:
    import monitor

    config = carregar_config(args.config)
    credenciais = carregar_credenciais()
    return monitor.executar(
        config, credenciais, dry_run=args.dry_run, marcar_sem_enviar=args.marcar_sem_enviar
    )


def _cmd_testar_envio(args) -> int:
    import entrega

    config = carregar_config(args.config)
    return entrega.testar(config, args.mensagem)


def construir_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="prodoc_monitor.py",
        description="Monitor do Prodoc — seção ABM, sem abrir documentos.",
    )
    parser.add_argument(
        "--config", default="config.json", help="caminho do config.json (padrão: config.json)"
    )
    sub = parser.add_subparsers(dest="comando")

    p_desc = sub.add_parser("descobrir", help="mapeia seções e campos da listagem (somente leitura)")
    p_desc.set_defaults(func=_cmd_descobrir)

    p_diag = sub.add_parser("diagnosticar", help="investiga por que a listagem está falhando")
    p_diag.set_defaults(func=_cmd_diagnosticar)

    p_cfg = sub.add_parser("configurar", help="grava o .env com as credenciais (pergunta no terminal)")
    p_cfg.set_defaults(func=_cmd_configurar)

    p_mon = sub.add_parser("monitorar", help="verifica documentos novos e envia o resumo")
    p_mon.add_argument(
        "--dry-run",
        action="store_true",
        help="mostra a mensagem que seria enviada, sem enviar e sem gravar estado",
    )
    p_mon.add_argument(
        "--marcar-sem-enviar",
        action="store_true",
        help="registra os não lidos atuais como já avisados, sem enviar nada "
             "(use na estreia, para não despejar o acumulado no grupo)",
    )
    p_mon.set_defaults(func=_cmd_monitorar)

    p_teste = sub.add_parser("testar-envio", help="envia uma mensagem de teste pelo canal configurado")
    p_teste.add_argument(
        "--mensagem",
        default="Teste do monitor Prodoc — se você recebeu isto, a entrega está funcionando.",
        help="texto da mensagem de teste",
    )
    p_teste.set_defaults(func=_cmd_testar_envio)

    return parser


def main() -> int:
    parser = construir_parser()
    args = parser.parse_args()
    if not getattr(args, "comando", None):
        parser.print_help()
        return 2
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
