"""Testes do monitor Prodoc.

O payload real ainda não foi capturado (fase de descoberta), então os fixtures
cobrem as DUAS formas possíveis — campos no nível de cima e aninhados em
`documento` — que é exatamente a ambiguidade que causava o bug de metadados.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import documentos as doc_utils
import entrega
import resumo as resumo_mod
from estado import Estado, chave_do_documento
from prodoc_client import ProdocClient, RotaNaoPermitida, _regex_da_rota

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
with open(os.path.join(RAIZ, "config.json"), encoding="utf-8") as _f:
    CONFIG = json.load(_f)

DOC_ANINHADO = {
    "documento": {
        "id": "abc123",
        "lido": False,
        "numero": "OF 45/2026",
        "tipo": {"nome": "Ofício"},
        "assunto": {"assunto": "Escala de serviço do mês"},
        "conteudo": "Comunico a Vossa Senhoria a escala de serviço para o mês corrente, "
                    "conforme aprovada pelo comando, com as substituições já indicadas.",
    },
    "instituicao_origem_nome": "CBMAP - Comando Geral",
    "data_criacao": "2026-09-01",
}

DOC_PLANO = {
    "id": "xyz789",
    "lido": False,
    "numero": "MEM 12/2026",
    "tipo": {"nome": "Memorando"},
    "assunto": {"assunto": "Solicitação de material"},
    "instituicao_origem_nome": "1º GBM",
    "created_at": "2026-09-02",
}


# ----------------------------------------------------------------------
# A garantia central: nunca abrir um documento
# ----------------------------------------------------------------------

def _cliente():
    return ProdocClient(CONFIG, "usuario", "senha")


def test_allowlist_permite_listagem_e_login():
    cliente = _cliente()
    cliente._validar_rota("https://prodoc.ap.gov.br/login")
    cliente._validar_rota("https://prodoc.ap.gov.br/dashboard")
    cliente._validar_rota(
        "https://prodoc.ap.gov.br/instituicoes/e7KB27q8lD/unidade_organizacionais/bKnmJoD6WZ"
        "/documentos/paginate/select"
    )


@pytest.mark.parametrize("rota", [
    "/documentos/abc123",
    "/documentos/abc123/visualizar",
    "/instituicoes/e7KB27q8lD/documentos/abc123/abrir",
    "/documentos/abc123/marcar-lido",
])
def test_allowlist_bloqueia_abertura_de_documento(rota):
    """Abrir marca como lido no Prodoc — tem que falhar antes de sair da máquina."""
    with pytest.raises(RotaNaoPermitida):
        _cliente()._validar_rota("https://prodoc.ap.gov.br" + rota)


def test_regex_da_rota_deriva_do_template():
    padrao = _regex_da_rota("/a/{x}/b/{y}/c")
    import re
    assert re.match(padrao, "/a/111/b/222/c")
    assert not re.match(padrao, "/a/111/b/222/c/d")


# ----------------------------------------------------------------------
# Metadados: funciona nas duas formas do payload
# ----------------------------------------------------------------------

def test_metadados_forma_aninhada():
    m = doc_utils.normalizar(DOC_ANINHADO, "ABM", [], 80)
    assert m["numero"] == "OF 45/2026"
    assert m["tipo"] == "Ofício"
    assert m["assunto"] == "Escala de serviço do mês"
    assert m["remetente"] == "CBMAP - Comando Geral"


def test_metadados_forma_plana():
    m = doc_utils.normalizar(DOC_PLANO, "ABM", [], 80)
    assert m["numero"] == "MEM 12/2026"
    assert m["assunto"] == "Solicitação de material"
    assert m["data"] == "2026-09-02"


def test_campo_ausente_vira_padrao_legivel_nao_na():
    m = doc_utils.normalizar({"lido": False}, "ABM", [], 80)
    assert m["assunto"] == "sem assunto"
    assert m["remetente"] == "origem não informada"


def test_nao_lido_so_quando_explicitamente_falso():
    assert doc_utils.esta_nao_lido(DOC_ANINHADO)
    assert not doc_utils.esta_nao_lido({"documento": {"lido": True}})
    # Campo ausente conta como lido: nunca inundar o grupo por mudança de formato.
    assert not doc_utils.esta_nao_lido({"numero": "1"})


# ----------------------------------------------------------------------
# Trecho e fallback
# ----------------------------------------------------------------------

def test_usa_primeiro_campo_com_tamanho_suficiente():
    trecho, campo = doc_utils.escolher_trecho(
        DOC_ANINHADO, ["documento.resumo", "documento.conteudo"], 80
    )
    assert campo == "documento.conteudo"
    assert trecho.startswith("Comunico a Vossa Senhoria")


def test_fallback_quando_trecho_curto_demais():
    curto = {"documento": {"conteudo": "ok"}}
    trecho, campo = doc_utils.escolher_trecho(curto, ["documento.conteudo"], 80)
    assert (trecho, campo) == (None, None)


def test_fallback_quando_nenhum_campo_configurado():
    trecho, campo = doc_utils.escolher_trecho(DOC_ANINHADO, [], 80)
    assert (trecho, campo) == (None, None)


# ----------------------------------------------------------------------
# Dedupe: o documento nunca fica lido, então o estado é a única defesa
# ----------------------------------------------------------------------

def test_chave_usa_id_quando_existe():
    assert chave_do_documento("abc123", "OF 1", "x") == "id:abc123"


def test_chave_sem_id_e_estavel_e_distingue_documentos():
    a = chave_do_documento(None, "OF 1/2026", "Escala")
    assert a == chave_do_documento("N/A", "OF 1/2026", "Escala")
    assert a != chave_do_documento(None, "OF 2/2026", "Escala")


def test_dedupe_nao_reenvia_o_mesmo_documento(tmp_path):
    caminho = str(tmp_path / "estado.json")
    docs = [doc_utils.normalizar(DOC_ANINHADO, "ABM", [], 80)]

    primeira = Estado(caminho)
    assert len(primeira.filtrar_novos(docs)) == 1
    primeira.marcar_notificados(docs)
    primeira.salvar()

    segunda = Estado(caminho)
    assert segunda.filtrar_novos(docs) == []


def test_poda_remove_registros_antigos(tmp_path):
    caminho = str(tmp_path / "estado.json")
    estado = Estado(caminho, reter_dias=30)
    antigo = (datetime.now() - timedelta(days=60)).isoformat(timespec="seconds")
    estado.documentos = {
        "id:velho": {"notificado_em": antigo},
        "id:novo": {"notificado_em": datetime.now().isoformat(timespec="seconds")},
    }
    estado.salvar()
    assert set(Estado(caminho).documentos) == {"id:novo"}


def test_cooldown_de_alerta(tmp_path):
    estado = Estado(str(tmp_path / "estado.json"), cooldown_alerta_horas=24)
    assert estado.pode_alertar("autenticacao")
    estado.registrar_alerta("autenticacao", "falhou")
    assert not estado.pode_alertar("autenticacao")
    estado.limpar_alerta("autenticacao")
    assert estado.pode_alertar("autenticacao")


def test_estado_corrompido_nao_derruba_e_preserva_copia(tmp_path):
    caminho = tmp_path / "estado.json"
    caminho.write_text("{ isso não é json")
    estado = Estado(str(caminho))
    assert estado.documentos == {}
    assert (tmp_path / "estado.json.corrompido").exists()


# ----------------------------------------------------------------------
# Mensagem
# ----------------------------------------------------------------------

def test_divide_sem_cortar_documento():
    documento = "linha um\nlinha dois"
    texto = "\n\n".join([documento] * 10)
    partes = entrega.dividir(texto, 60)
    assert len(partes) > 1
    for parte in partes:
        assert "linha um\nlinha dois" in parte
        assert parte.rstrip().endswith(")")  # marcador (n/total)


def test_mensagem_curta_nao_e_dividida():
    assert entrega.dividir("oi", 3500) == ["oi"]


def test_documento_sem_resumo_avisa_em_vez_de_omitir():
    m = doc_utils.normalizar(DOC_PLANO, "ABM", [], 80)
    m["resumo"], m["origem_resumo"] = "", "erro"
    assert "não foi possível gerar o resumo" in entrega.formatar_documento(1, m)


def test_resumo_de_titulo_e_sinalizado():
    m = doc_utils.normalizar(DOC_PLANO, "ABM", [], 80)
    m["resumo"], m["origem_resumo"] = "Pedido de material.", "titulo"
    assert "a partir do título" in entrega.formatar_documento(1, m)


def test_entrega_sem_destino_falha_com_mensagem_util(monkeypatch):
    monkeypatch.delenv("OPENCLAW_DESTINO", raising=False)
    config = {"entrega": {"openclaw": {"modo": "http", "url": "http://x", "destino": ""}}}
    with pytest.raises(entrega.EntregaError, match="OPENCLAW_DESTINO"):
        entrega.enviar(config, "oi")


# ----------------------------------------------------------------------
# Triagem: prioridade e apresentação
# ----------------------------------------------------------------------

def _doc(numero, urgencia="baixa", acao=False, prazo=None):
    return {
        "tipo": "Ofício", "numero": numero, "remetente": "X", "assunto": "Y", "data": "",
        "resumo": "resumo", "origem_resumo": "trecho",
        "urgencia": urgencia, "acao_requerida": acao, "prazo": prazo,
    }


def test_acao_requerida_vem_antes_de_tudo():
    ordem = entrega.ordenar_por_prioridade([
        _doc("A", urgencia="alta"),
        _doc("B", urgencia="baixa", acao=True),
    ])
    assert [d["numero"] for d in ordem] == ["B", "A"]


def test_dentro_do_mesmo_grupo_ordena_por_urgencia():
    ordem = entrega.ordenar_por_prioridade([
        _doc("A", urgencia="baixa"), _doc("B", urgencia="alta"), _doc("C", urgencia="media"),
    ])
    assert [d["numero"] for d in ordem] == ["B", "C", "A"]


def test_empate_preserva_ordem_original():
    ordem = entrega.ordenar_por_prioridade([_doc("A"), _doc("B"), _doc("C")])
    assert [d["numero"] for d in ordem] == ["A", "B", "C"]


def test_prazo_aparece_junto_da_providencia():
    texto = entrega.formatar_documento(1, _doc("A", acao=True, prazo="10/09/2026"))
    assert "Exige providência" in texto and "10/09/2026" in texto


def test_cabecalho_conta_quantos_exigem_providencia():
    texto = entrega.formatar_mensagem([_doc("A", acao=True), _doc("B", acao=True), _doc("C")], "ABM")
    assert "2 exigem providência" in texto


def test_documento_sem_triagem_nao_quebra_a_formatacao():
    """Fallback de texto simples não preenche urgência — não pode dar KeyError."""
    cru = {"tipo": "Ofício", "numero": "A", "remetente": "X", "assunto": "Y",
           "resumo": "r", "origem_resumo": "trecho"}
    assert "1. Ofício A" in entrega.formatar_documento(1, cru)
    assert entrega.ordenar_por_prioridade([cru]) == [cru]


# ----------------------------------------------------------------------
# Resumidor: lote estruturado e degradação
# ----------------------------------------------------------------------



def _resumidor(monkeypatch, respostas):
    """Resumidor com _chamar substituído; 'respostas' é uma lista consumida em ordem."""
    r = resumo_mod.Resumidor({"resumo": {"model": "m"}}, api_key="x")
    fila = list(respostas)

    def falso(prompt, max_tokens, esquema):
        valor = fila.pop(0)
        if isinstance(valor, Exception):
            raise valor
        return valor

    monkeypatch.setattr(r, "_chamar", falso)
    return r


def test_lote_estruturado_distribui_resumos_por_ref(monkeypatch):
    docs = [
        {"numero": "A", "tipo": "Ofício", "assunto": "a", "remetente": "x", "trecho": "t" * 200},
        {"numero": "B", "tipo": "Memorando", "assunto": "b", "remetente": "y", "trecho": None},
    ]
    payload = json.dumps({"documentos": [
        {"ref": 1, "resumo": "primeiro", "urgencia": "alta", "acao_requerida": True, "prazo": "amanhã"},
        {"ref": 2, "resumo": "segundo", "urgencia": "baixa", "acao_requerida": False, "prazo": None},
    ]})
    resumidor = _resumidor(monkeypatch, [payload])
    resumidor.resumir_lista(docs)

    assert docs[0]["resumo"] == "primeiro"
    assert docs[0]["origem_resumo"] == "trecho"
    assert docs[0]["acao_requerida"] is True and docs[0]["prazo"] == "amanhã"
    # Sem trecho, a origem tem que ser 'titulo' mesmo vindo do lote.
    assert docs[1]["origem_resumo"] == "titulo"
    assert resumidor.falhas == 0


def test_ref_faltando_no_lote_vira_falha_do_documento(monkeypatch):
    docs = [{"numero": "A", "tipo": "O", "assunto": "a", "remetente": "x", "trecho": "t" * 200},
            {"numero": "B", "tipo": "M", "assunto": "b", "remetente": "y", "trecho": None}]
    payload = json.dumps({"documentos": [
        {"ref": 1, "resumo": "só o primeiro", "urgencia": "baixa", "acao_requerida": False, "prazo": None}
    ]})
    _resumidor(monkeypatch, [payload]).resumir_lista(docs)
    assert docs[0]["resumo"] == "só o primeiro"
    assert docs[1]["origem_resumo"] == "erro"


def test_json_invalido_cai_no_modo_um_a_um(monkeypatch):
    docs = [{"numero": "A", "tipo": "O", "assunto": "a", "remetente": "x", "trecho": "t" * 200}]
    # 1ª resposta: lixo (estruturado falha). 2ª: texto simples do fallback.
    resumidor = _resumidor(monkeypatch, ["isso não é json", "resumo em texto simples"])
    resumidor.resumir_lista(docs)
    assert docs[0]["resumo"] == "resumo em texto simples"
    assert resumidor.falhas == 0


def test_saida_estruturada_desligada_usa_direto_o_modo_antigo(monkeypatch):
    docs = [{"numero": "A", "tipo": "O", "assunto": "a", "remetente": "x", "trecho": None}]
    r = resumo_mod.Resumidor({"resumo": {"saida_estruturada": False}}, api_key="x")
    monkeypatch.setattr(r, "_chamar", lambda p, m, e: "texto direto")
    r.resumir_lista(docs)
    assert docs[0]["resumo"] == "texto direto"
    assert docs[0]["origem_resumo"] == "titulo"


def test_env_sobrepoe_destino_do_config(monkeypatch):
    monkeypatch.setenv("OPENCLAW_DESTINO", "grupo-do-env")
    monkeypatch.setenv("OPENCLAW_URL", "http://env/enviar")
    oc = entrega._config_openclaw({"entrega": {"openclaw": {"destino": "do-config", "url": "http://config"}}})
    assert oc["destino"] == "grupo-do-env" and oc["url"] == "http://env/enviar"


def test_prazo_inventado_sem_trecho_e_descartado(monkeypatch):
    """Prazo falso num aviso do corpo de bombeiros é pior que nenhum prazo."""
    docs = [{"numero": "A", "tipo": "O", "assunto": "Solicitação de material",
             "remetente": "x", "trecho": None}]
    payload = json.dumps({"documentos": [
        {"ref": 1, "resumo": "r", "urgencia": "alta", "acao_requerida": True, "prazo": "12/09/2026"}
    ]})
    _resumidor(monkeypatch, [payload]).resumir_lista(docs)
    assert docs[0]["prazo"] is None


def test_prazo_presente_no_assunto_e_mantido(monkeypatch):
    docs = [{"numero": "A", "tipo": "O", "assunto": "Responder até 12/09/2026",
             "remetente": "x", "trecho": None}]
    payload = json.dumps({"documentos": [
        {"ref": 1, "resumo": "r", "urgencia": "alta", "acao_requerida": True, "prazo": "12/09/2026"}
    ]})
    _resumidor(monkeypatch, [payload]).resumir_lista(docs)
    assert docs[0]["prazo"] == "12/09/2026"


def test_prazo_com_trecho_e_preservado(monkeypatch):
    docs = [{"numero": "A", "tipo": "O", "assunto": "a", "remetente": "x",
             "trecho": "Solicito resposta impreterivelmente até 12/09/2026." * 3}]
    payload = json.dumps({"documentos": [
        {"ref": 1, "resumo": "r", "urgencia": "alta", "acao_requerida": True, "prazo": "12/09/2026"}
    ]})
    _resumidor(monkeypatch, [payload]).resumir_lista(docs)
    assert docs[0]["prazo"] == "12/09/2026"
