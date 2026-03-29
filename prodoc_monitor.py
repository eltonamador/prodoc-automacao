#!/usr/bin/env python3
"""
Automação de Verificação Periódica de Mensagens - Prodoc
Extrai documentos não lidos e gera resumos via Claude API
"""

import os
import sys
import json
import logging
from datetime import datetime
from typing import List, Dict, Optional
import requests
from anthropic import Anthropic

# Configuração de logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class ProdocMonitor:
    """Gerenciador de monitoramento do Prodoc"""

    def __init__(self, config_file: str = "config.json"):
        """Inicializa o monitor com configurações"""
        self.config = self._load_config(config_file)
        self.session = requests.Session()
        self.prodoc_user = os.getenv("PRODOC_USER")
        self.prodoc_password = os.getenv("PRODOC_PASSWORD")
        self.claude_api_key = os.getenv("CLAUDE_API_KEY")

        # Validar credenciais
        if not all([self.prodoc_user, self.prodoc_password, self.claude_api_key]):
            raise ValueError("Credenciais ausentes. Defina as variáveis de ambiente: "
                           "PRODOC_USER, PRODOC_PASSWORD, CLAUDE_API_KEY")

        self.claude_client = Anthropic(api_key=self.claude_api_key)
        self.documentos_processados = []

    @staticmethod
    def _load_config(config_file: str) -> Dict:
        """Carrega arquivo de configuração"""
        try:
            with open(config_file, 'r', encoding='utf-8') as f:
                return json.load(f)
        except FileNotFoundError:
            logger.error(f"Arquivo de configuração não encontrado: {config_file}")
            sys.exit(1)
        except json.JSONDecodeError as e:
            logger.error(f"Erro ao parsing JSON: {e}")
            sys.exit(1)

    def autenticar_prodoc(self) -> bool:
        """Autentica no Prodoc via login com CSRF token"""
        logger.info("Autenticando no Prodoc...")
        try:
            base_url = self.config['prodoc']['base_url']
            timeout = self.config['prodoc']['timeout']

            # Passo 1: Buscar a página de login para obter o CSRF token
            logger.info(f"Buscando página de login: {base_url}/login")
            login_page = self.session.get(
                f"{base_url}/login",
                timeout=timeout
            )
            logger.info(f"Status da página de login: {login_page.status_code}")

            # Extrair CSRF token do HTML
            import re
            token_match = re.search(
                r'<meta name="csrf-token" content="([^"]+)"', login_page.text
            )
            if not token_match:
                token_match = re.search(
                    r'<input[^>]+name="_token"[^>]+value="([^"]+)"', login_page.text
                )

            if not token_match:
                logger.error("CSRF token não encontrado na página de login")
                return False

            csrf_token = token_match.group(1)
            logger.info("CSRF token obtido com sucesso")

            # Passo 2: Fazer POST com credenciais
            logger.info("Enviando credenciais de login...")
            response = self.session.post(
                f"{base_url}/login",
                data={
                    'email': self.prodoc_user,
                    'password': self.prodoc_password,
                    '_token': csrf_token
                },
                timeout=timeout,
                allow_redirects=True
            )

            logger.info(f"Status após login: {response.status_code} | URL final: {response.url}")

            # Verificar se autenticou (URL final deve ser dashboard, não login)
            if 'login' in response.url:
                logger.error("Autenticação falhou — redirecionado de volta para login")
                return False

            logger.info("✅ Autenticação bem-sucedida!")
            return True

        except requests.RequestException as e:
            logger.error(f"Erro ao conectar ao Prodoc: {e}")
            return False

    def buscar_documentos_nao_lidos(self) -> List[Dict]:
        """Busca documentos não lidos da API Prodoc"""
        logger.info("Buscando documentos não lidos...")

        try:
            # Montar URL da API
            endpoint = self.config['prodoc']['endpoint_paginate'].format(
                instituicao_id=self.config['prodoc']['instituicao_id'],
                unidade_id=self.config['prodoc']['unidade_organizacional_id']
            )

            url = f"{self.config['prodoc']['base_url']}{endpoint}"

            # Parâmetros da requisição
            params = self.config['api_parameters'].copy()

            # Fazer requisição
            response = self.session.get(
                url,
                params=params,
                timeout=self.config['prodoc']['timeout'],
                headers={'X-Requested-With': 'XMLHttpRequest'}
            )

            if response.status_code != 200:
                logger.error(f"Erro HTTP {response.status_code} ao buscar documentos")
                return []

            dados = response.json()

            # Filtrar apenas documentos não lidos (lido == false)
            documentos_nao_lidos = [
                doc for doc in dados.get('data', [])
                if doc.get('documento', {}).get('lido') == False
            ]

            logger.info(f"Encontrados {len(documentos_nao_lidos)} documentos não lidos")
            return documentos_nao_lidos

        except requests.RequestException as e:
            logger.error(f"Erro ao buscar documentos: {e}")
            return []
        except (KeyError, json.JSONDecodeError) as e:
            logger.error(f"Erro ao processar resposta: {e}")
            return []

    def extrair_metadados(self, documento: Dict) -> Optional[Dict]:
        """Extrai metadados relevantes do documento"""
        try:
            return {
                'numero': documento.get('numero', 'N/A'),
                'tipo': documento.get('tipo', {}).get('nome', 'N/A'),
                'assunto': documento.get('assunto', {}).get('assunto', 'N/A'),
                'remetente': documento.get('instituicao_origem_nome', 'N/A'),
                'data': documento.get('documento_tramitacao_id', 'N/A'),
                'data_criacao': documento.get('data_criacao', 'N/A'),
                'id_documento': documento.get('documento', {}).get('id', 'N/A')
            }
        except Exception as e:
            logger.warning(f"Erro ao extrair metadados: {e}")
            return None

    def gerar_resumo_claude(self, metadados: Dict) -> str:
        """Gera resumo usando Claude API baseado nos metadados"""
        try:
            prompt = f"""Gere um resumo breve e objetivo (50-100 palavras) sobre este documento:

Tipo: {metadados['tipo']}
Assunto: {metadados['assunto']}
Remetente: {metadados['remetente']}

Resumo deve ser em português, conciso e prático."""

            response = self.claude_client.messages.create(
                model=self.config['claude']['model'],
                max_tokens=self.config['claude']['max_tokens'],
                messages=[
                    {
                        "role": "user",
                        "content": prompt
                    }
                ]
            )

            resumo = response.content[0].text.strip()
            logger.info(f"Resumo gerado para: {metadados['numero']}")
            return resumo

        except Exception as e:
            logger.error(f"Erro ao gerar resumo: {e}")
            return "Erro ao gerar resumo"

    def processar_documentos(self, documentos: List[Dict]) -> List[Dict]:
        """Processa lista de documentos e gera resumos"""
        resultados = []

        for i, doc in enumerate(documentos, 1):
            logger.info(f"Processando documento {i}/{len(documentos)}")

            metadados = self.extrair_metadados(doc)
            if not metadados:
                continue

            resumo = self.gerar_resumo_claude(metadados)

            resultado = {
                **metadados,
                'resumo': resumo,
                'processado_em': datetime.now().strftime(self.config['output']['timestamp_format'])
            }

            resultados.append(resultado)

        self.documentos_processados = resultados
        return resultados

    def salvar_resultado(self, output_file: str = "resultado_prodoc.json") -> None:
        """Salva resultado em arquivo JSON"""
        try:
            output_data = {
                'timestamp': datetime.now().isoformat(),
                'total_documentos': len(self.documentos_processados),
                'documentos': self.documentos_processados
            }

            with open(output_file, 'w', encoding='utf-8') as f:
                json.dump(output_data, f, ensure_ascii=False, indent=2)

            logger.info(f"Resultado salvo em: {output_file}")

            # Também exibir no log para GitHub Actions
            logger.info("=" * 60)
            logger.info("RESULTADO DA EXECUÇÃO")
            logger.info("=" * 60)
            logger.info(f"Total de documentos processados: {len(self.documentos_processados)}")
            for doc in self.documentos_processados:
                logger.info(f"\n📄 {doc['numero']} - {doc['assunto']}")
                logger.info(f"   Tipo: {doc['tipo']}")
                logger.info(f"   Remetente: {doc['remetente']}")
                logger.info(f"   Resumo: {doc['resumo']}")

        except Exception as e:
            logger.error(f"Erro ao salvar resultado: {e}")

    def executar(self) -> bool:
        """Executa o fluxo completo de monitoramento"""
        logger.info("Iniciando monitoramento do Prodoc...")

        try:
            # Autenticar
            if not self.autenticar_prodoc():
                logger.error("Falha na autenticação")
                return False

            # Buscar documentos não lidos
            documentos = self.buscar_documentos_nao_lidos()
            if not documentos:
                logger.info("Nenhum documento não lido encontrado")
                self.salvar_resultado()
                return True

            # Processar documentos
            self.processar_documentos(documentos)

            # Salvar resultado
            self.salvar_resultado()

            logger.info("Monitoramento concluído com sucesso!")
            return True

        except Exception as e:
            logger.error(f"Erro durante execução: {e}")
            return False


def main():
    """Ponto de entrada do script"""
    monitor = ProdocMonitor()
    sucesso = monitor.executar()
    sys.exit(0 if sucesso else 1)


if __name__ == "__main__":
    main()
