#!/usr/bin/env python3
"""Hora local do CBMAP (Amapá), usada em todo timestamp gerado pelo monitor.

A VPS roda com o relógio do sistema em UTC — só o systemd timer sabe converter
para horário de Amapá (via o sufixo America/Belem no OnCalendar). Sem este
módulo, os logs e o estado salvo ficam em UTC puro, 3h à frente do horário real,
o que já causou confusão ao ler o journalctl.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

FUSO = ZoneInfo("America/Belem")


def agora() -> datetime:
    return datetime.now(FUSO)
