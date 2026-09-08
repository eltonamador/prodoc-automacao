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
import monitor as monitor_mod
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


def test_ressalva_do_trecho_vai_para_o_cabecalho_quando_vale_para_todos():
    """Repetir a ressalva em cada item era ruído: sem trecho é a regra, não a exceção."""
    docs = []
    for numero in ("A", "B"):
        m = doc_utils.normalizar(dict(DOC_PLANO, numero=numero), "ABM", [], 80)
        m["resumo"], m["origem_resumo"] = "resumo", "titulo"
        docs.append(m)
    texto = entrega.formatar_mensagem(docs, "ABM")
    assert "não foram abertos" in texto
    assert texto.count("_(só pela identificação)_") == 0


def test_ressalva_fica_no_item_quando_só_alguns_ficaram_sem_trecho():
    com = doc_utils.normalizar(dict(DOC_PLANO, numero="A"), "ABM", [], 80)
    com["resumo"], com["origem_resumo"] = "resumo", "trecho"
    sem = doc_utils.normalizar(dict(DOC_PLANO, numero="B"), "ABM", [], 80)
    sem["resumo"], sem["origem_resumo"] = "resumo", "titulo"
    texto = entrega.formatar_mensagem([com, sem], "ABM")
    assert texto.count("_(só pela identificação)_") == 1
    assert "não foram abertos" not in texto


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


# ----------------------------------------------------------------------
# Serialização de parâmetros: a causa raiz da falha original
# ----------------------------------------------------------------------

def test_booleanos_viram_minusculo_na_query():
    """O Prodoc responde HTTP 500 a "False" com maiúscula (confirmado ao vivo)."""
    saida = ProdocClient._serializar_params(
        {"search": False, "filtros": False, "findDocumento": True, "length": 100, "order": "desc"}
    )
    assert saida["search"] == "false"
    assert saida["filtros"] == "false"
    assert saida["findDocumento"] == "true"
    # Não-booleanos passam intactos.
    assert saida["length"] == 100 and saida["order"] == "desc"


def test_config_do_projeto_mantem_booleanos_json_de_verdade():
    """A correção fica no transporte; o config não deve virar strings disfarçadas."""
    assert CONFIG["api_parameters"]["search"] is False


# ----------------------------------------------------------------------
# Forma real do payload da ABM (verificada em 07/09/2026).
# Conteúdo sintético: dado interno do CBMAP não entra em repositório público.
# ----------------------------------------------------------------------

DOC_REAL = {
    "id": "x6X4NN1a60",
    "documento": {"id": "x6X4NN1a60", "lido": False, "tipo": 726},
    "documento_tramitacao_id": "x6X4NN1a60",
    "tipo": {"0": "OFIC", "nome": "OFICIO INTERNO"},
    "numero": "0090/2026/T4 CFSD BM 2026.2 - CG/CBMAP",
    "assunto": {"assunto": "<strong>ESCALA N&ordm; 009/2026 COORDENAÇÃO</strong>",
                "id": "x6X4NN1a60", "url": "https://prodoc.ap.gov.br/documentos/x"},
    "assunto_controle_distribuicao": "ESCALA Nº 009/2026 COORDENAÇÃO",
    "origem": {"sigla": "T4 CFSD BM 2026.2 - CG", "nome": "T4 CFSD BM 2026.2 - COORDENAÇÃO GERAL"},
    "destino": "ABM",
    "destino_nome": "ACADEMIA DE BOMBEIRO MILITAR",
    "instituicao_origem": "CBMAP",
    "instituicao_origem_nome": "CORPO DE BOMBEIROS MILITAR DO ESTADO DO AMAPÁ",
    "instituicao_destino_nome": "CORPO DE BOMBEIROS MILITAR DO ESTADO DO AMAPÁ",
    "data_criacao": "06/09/2026 21:15",
    "copia": True,
}


def test_payload_real_extrai_todos_os_metadados():
    m = doc_utils.normalizar(DOC_REAL, "ABM", [], 80)
    assert m["numero"] == "0090/2026/T4 CFSD BM 2026.2 - CG/CBMAP"
    assert m["tipo"] == "OFICIO INTERNO"
    assert m["destino"] == "ABM"
    assert m["data"] == "06/09/2026 21:15"
    assert m["copia"] is True
    assert m["chave"] == "id:x6X4NN1a60"


def test_html_e_entidades_do_assunto_sao_limpos():
    m = doc_utils.normalizar(DOC_REAL, "ABM", [], 80)
    assert m["assunto"] == "ESCALA Nº 009/2026 COORDENAÇÃO"
    assert "<strong>" not in m["assunto"] and "&ordm;" not in m["assunto"]


def test_remetente_usa_a_unidade_e_nao_a_instituicao():
    """instituicao_origem_nome é igual nos 100 documentos — não distingue nada."""
    m = doc_utils.normalizar(DOC_REAL, "ABM", [], 80)
    assert m["remetente"] == "T4 CFSD BM 2026.2 - COORDENAÇÃO GERAL"


def test_assunto_alternativo_e_omitido_quando_repete_o_assunto():
    m = doc_utils.normalizar(DOC_REAL, "ABM", [], 80)
    assert m["assunto_alternativo"] == ""


def test_assunto_alternativo_aparece_quando_acrescenta_informacao():
    doc = dict(DOC_REAL, assunto_controle_distribuicao="COLÉGIO X - SOLICITAÇÃO DE PALESTRA")
    m = doc_utils.normalizar(doc, "ABM", [], 80)
    assert m["assunto_alternativo"] == "COLÉGIO X - SOLICITAÇÃO DE PALESTRA"


def test_payload_real_e_reconhecido_como_nao_lido():
    assert doc_utils.esta_nao_lido(DOC_REAL)
    assert not doc_utils.esta_nao_lido({**DOC_REAL, "documento": {"id": "x", "lido": True}})


def test_copia_aparece_marcada_na_mensagem():
    m = doc_utils.normalizar(DOC_REAL, "ABM", [], 80)
    m["resumo"], m["origem_resumo"] = "resumo", "titulo"
    assert "(cópia)" in entrega.formatar_documento(1, m)


def test_so_urgencia_alta_ganha_marcador():
    """Marcador em 'media' duplicava a linha 'Exige providência' logo abaixo."""
    assert "🔴" in entrega.formatar_documento(1, _doc("A", urgencia="alta"))
    for nivel in ("media", "baixa"):
        texto = entrega.formatar_documento(1, _doc("A", urgencia=nivel))
        assert "🔴" not in texto and "🟡" not in texto and "⚪" not in texto
        assert texto.startswith("*1. Ofício A*")


# ----------------------------------------------------------------------
# Filtro de seção: nunca emudecer em silêncio
# ----------------------------------------------------------------------



class _ClienteFake:
    def __init__(self, documentos):
        self._documentos = documentos

    def listar_documentos(self, unidade):
        return self._documentos


def _config_uma_secao():
    return {
        "secoes": [{"nome": "ABM", "unidade_organizacional_id": "u1",
                    "ativa": True, "filtro_destino": "ABM"}],
        "resumo": {"campos_trecho": [], "min_caracteres_trecho": 80},
    }


def test_filtro_descarta_documento_de_outra_secao(tmp_path):
    docs = [dict(DOC_REAL, destino="ABM"), dict(DOC_REAL, destino="EFR", id="outro",
                 documento={"id": "outro", "lido": False})]
    novos, ignorados = monitor_mod._coletar(
        _ClienteFake(docs), _config_uma_secao(), Estado(str(tmp_path / "e.json"))
    )
    assert [d["destino"] for d in novos] == ["ABM"]
    assert ignorados == []


def test_campo_destino_ausente_ignora_o_filtro_em_vez_de_apagar_tudo(tmp_path):
    """Se o Prodoc parar de mandar 'destino', filtrar por ele silenciaria o monitor."""
    sem_destino = {k: v for k, v in DOC_REAL.items() if k not in ("destino", "destino_nome")}
    novos, ignorados = monitor_mod._coletar(
        _ClienteFake([sem_destino]), _config_uma_secao(), Estado(str(tmp_path / "e.json"))
    )
    assert len(novos) == 1, "o documento não pode sumir por causa de um campo que o Prodoc removeu"
    assert ignorados == ["ABM"], "e a seção afetada precisa ser reportada para virar alerta"


# ----------------------------------------------------------------------
# Entrega por CLI: código de saída zero não é prova de entrega
# ----------------------------------------------------------------------

def test_json_de_erro_no_stdout_vira_falha_mesmo_com_saida_zero():
    """Se um erro de envio passasse por entregue, o documento sumiria para sempre."""
    with pytest.raises(entrega.EntregaError, match="recusou"):
        entrega._conferir_json_de_saida('{"error": "not connected"}')
    with pytest.raises(entrega.EntregaError, match="recusou"):
        entrega._conferir_json_de_saida('{"ok": false, "message": "sessão caiu"}')


def test_dry_run_esquecido_no_comando_e_detectado():
    with pytest.raises(entrega.EntregaError, match="dry-run"):
        entrega._conferir_json_de_saida('{"action": "send", "dryRun": true}')


def test_saida_de_sucesso_ou_nao_json_passa():
    entrega._conferir_json_de_saida('{"action": "send", "ok": true}')
    entrega._conferir_json_de_saida("mensagem enviada")
    entrega._conferir_json_de_saida("")


def test_marcar_sem_enviar_registra_tudo_e_nao_envia(tmp_path, monkeypatch):
    """Estreia: adota o backlog sem despejar meses de documentos no grupo."""
    enviados = []
    monkeypatch.setattr(entrega, "enviar", lambda config, texto: enviados.append(texto))
    monkeypatch.setattr(monitor_mod.entrega, "enviar", lambda config, texto: enviados.append(texto))

    class _Cliente:
        def __init__(self, *a, **k): pass
        def autenticar(self): pass
        def validar_sessao(self, u): pass
        def listar_documentos(self, u): return [DOC_REAL]

    monkeypatch.setattr(monitor_mod, "ProdocClient", _Cliente)
    monkeypatch.chdir(tmp_path)

    config = {
        "secoes": [{"nome": "ABM", "unidade_organizacional_id": "u1", "ativa": True}],
        "resumo": {"campos_trecho": []},
        "estado": {"arquivo": str(tmp_path / "estado.json")},
    }
    cred = {"PRODOC_USER": "u", "PRODOC_PASSWORD": "p", "CLAUDE_API_KEY": "k"}

    assert monitor_mod.executar(config, cred, marcar_sem_enviar=True) == 0
    assert enviados == [], "não pode enviar nada"
    assert Estado(str(tmp_path / "estado.json")).ja_notificado("id:x6X4NN1a60")


def test_concordancia_do_cabecalho_no_singular_e_no_plural():
    """'1 documento novo não lidos' saiu numa mensagem real antes desta correção."""
    um = entrega.formatar_mensagem([_doc("A")], "ABM")
    assert "1 documento novo não lido" in um
    assert "não lidos" not in um.split("\n")[1]

    varios = entrega.formatar_mensagem([_doc("A"), _doc("B")], "ABM")
    assert "2 documentos novos não lidos" in varios
