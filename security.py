# -*- coding: utf-8 -*-
"""
SPACEBIO E3-08 — camada de segurança da API

Reúne as defesas de transporte e de abuso: cabeçalhos HTTP de segurança,
limitação de taxa e validação de entrada. As defesas contra injeção de prompt
ficam em `prompts.py`, porque são texto e não middleware.

POR QUE MIDDLEWARE PRÓPRIO E NÃO slowapi
-----------------------------------------
`slowapi` instalaria três pacotes (slowapi, limits, Deprecated) para uma
funcionalidade que cabe em cinquenta linhas legíveis. O valor real dele é o
backend Redis, que permite compartilhar a contagem entre vários processos --
e esta API roda em um processo só.

Numa auditoria de segurança, código que se lê inteiro vale mais que
dependência que se confia. Menos uma coisa para quebrar na véspera.

LIMITAÇÃO CONHECIDA E DELIBERADA
--------------------------------
A contagem vive na MEMÓRIA do processo. Com `uvicorn --workers N`, cada
worker teria a sua, e o limite efetivo seria N vezes maior. Para o modo de
operação atual (um worker) está correto; escalar exige trocar o armazenamento
por Redis. Está registrado em docs/SECURITY_AUDIT.md em vez de escondido.
"""

from __future__ import annotations

import logging
import os
import time
from collections import defaultdict, deque
from typing import Deque, Dict, Tuple

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Cabeçalhos de segurança
# ---------------------------------------------------------------------------

# Cada cabeçalho abaixo fecha uma classe de ataque. Os comentários dizem qual,
# porque "adicionamos os headers de segurança" sem saber contra o quê é
# ritual, não defesa.
SECURITY_HEADERS: Dict[str, str] = {
    # Impede o navegador de adivinhar o tipo do conteúdo. Sem isso, uma
    # resposta JSON que por acaso comece com HTML pode ser interpretada e
    # executada como página -- o vetor clássico de XSS via upload.
    "X-Content-Type-Options": "nosniff",

    # Proíbe a página de ser embutida em iframe. Esta API devolve JSON e não
    # deveria ser enquadrada por ninguém; o cabeçalho também protege a
    # documentação interativa do FastAPI (/docs) contra clickjacking.
    "X-Frame-Options": "DENY",

    # Pedido explicitamente na E3-08. VALE REGISTRAR A RESSALVA: o XSS Auditor
    # do Chrome foi REMOVIDO em 2019, e o OWASP hoje recomenda o valor `0`,
    # porque o modo de bloqueio abriu vazamentos entre origens (XS-Leaks) em
    # navegadores que ainda o implementavam.
    #
    # Mantido em "1; mode=block" conforme a especificação da issue. A proteção
    # de fato contra XSS neste projeto vem de outro lugar: o frontend não usa
    # `dangerouslySetInnerHTML` e a Content-Security-Policy abaixo.
    "X-XSS-Protection": "1; mode=block",

    # Não vazar a URL completa (que pode conter a pergunta do usuário) para
    # sites externos ao clicar num link de DOI ou do PMC.
    "Referrer-Policy": "strict-origin-when-cross-origin",

    # A API não tem interface própria: não carrega script, estilo ou imagem de
    # lugar nenhum. `default-src 'none'` é a política mais restritiva possível
    # e é exatamente a correta aqui. `frame-ancestors 'none'` repete a intenção
    # do X-Frame-Options para navegadores que já o ignoram.
    "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'; base-uri 'none'",

    # A API não precisa de câmera, microfone, geolocalização nem pagamento.
    # Negar explicitamente reduz o estrago caso alguma página seja servida
    # daqui por engano.
    "Permissions-Policy": "geolocation=(), microphone=(), camera=(), payment=()",

    # Alguns proxies e balanceadores anexam caches compartilhados. Uma resposta
    # com a evidência de uma pergunta não deve ser guardada por intermediário.
    "Cache-Control": "no-store",
}

# HSTS só é aplicado sobre HTTPS -- ver a explicação em
# SecurityHeadersMiddleware.dispatch.
HSTS_HEADER = "Strict-Transport-Security"
HSTS_VALUE = "max-age=31536000; includeSubDomains"


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """
    Anexa os cabeçalhos de segurança a TODA resposta, inclusive as de erro.

    Middleware e não dependência de rota: uma exceção não tratada gera uma
    resposta que nunca passou pelo handler, e é justamente nela que o corpo
    pode conter detalhe interno.
    """

    async def dispatch(self, request: Request, call_next) -> Response:
        response = await call_next(request)

        for nome, valor in SECURITY_HEADERS.items():
            # `setdefault`: uma rota que já tenha definido um cabeçalho sabe
            # mais sobre o seu caso do que este middleware genérico.
            response.headers.setdefault(nome, valor)

        # POR QUE HSTS É CONDICIONAL
        # --------------------------
        # O navegador IGNORA Strict-Transport-Security recebido por HTTP --
        # é o que a RFC 6797 manda, senão um atacante na rede poderia injetá-lo
        # e causar negação de serviço.
        #
        # Mandá-lo em HTTP é, portanto, inofensivo e inútil. O risco está do
        # outro lado: em desenvolvimento, um HSTS acidental sobre localhost
        # gruda no navegador por um ano e passa a forçar HTTPS em TODA porta
        # local, quebrando outros projetos da máquina. Por isso, só em HTTPS.
        if request.url.scheme == "https":
            response.headers.setdefault(HSTS_HEADER, HSTS_VALUE)

        # O CABEÇALHO `Server` NÃO PODE SER RESOLVIDO AQUI
        # -------------------------------------------------
        # O uvicorn injeta `server: uvicorn` na camada de PROTOCOLO, depois de
        # a resposta ASGI já ter saído deste middleware. Definir o cabeçalho
        # aqui não substitui o dele: os dois vão juntos, e o cliente costuma
        # ler o primeiro -- o do uvicorn.
        #
        # Medido: com este middleware ativo, `curl` sobre HTTP real continuava
        # recebendo `Server: uvicorn`. O TestClient NÃO reproduz isso, porque
        # não passa pela camada de protocolo -- ou seja, um teste feito só com
        # TestClient daria falso positivo. Vale o registro: testar cabeçalho de
        # servidor exige HTTP de verdade.
        #
        # A supressão real é uma opção do servidor:
        #     uvicorn main:app --no-server-header
        # Está no docstring do main.py e em docs/SECURITY_AUDIT.md.
        #
        # Impacto: divulgação de versão. É reconhecimento, não exploração --
        # mas não há motivo para entregar a informação de graça.
        return response


# ---------------------------------------------------------------------------
# Limitação de taxa
# ---------------------------------------------------------------------------

# Dois limites, porque duas rotas têm custos muito diferentes.
#
# /api/v1/chat aciona embedding, busca no grafo e uma chamada de LLM. O free
# tier do Gemini são 20 requisições POR DIA: um script simples esgotaria a
# cota do dia em segundos, e a demonstração morreria sem que ninguém tivesse
# invadido nada. É o OWASP LLM04 (Model Denial of Service) na sua forma mais
# barata.
CHAT_LIMIT = int(os.getenv("RATE_LIMIT_CHAT", "10"))
CHAT_WINDOW = int(os.getenv("RATE_LIMIT_CHAT_WINDOW", "60"))

# As demais rotas são baratas, mas um flood ainda consome conexões do pool do
# Neo4j.
GLOBAL_LIMIT = int(os.getenv("RATE_LIMIT_GLOBAL", "60"))
GLOBAL_WINDOW = int(os.getenv("RATE_LIMIT_GLOBAL_WINDOW", "60"))

# Rotas caras, com limite próprio.
EXPENSIVE_PATHS = ("/api/v1/chat", "/chat")

# O health check é consultado por monitoramento e pela própria tela; limitá-lo
# criaria um alarme falso de indisponibilidade.
EXEMPT_PATHS = ("/api/v1/health",)

# Teto de memória: sem isto, um atacante variando o IP de origem faria o
# dicionário crescer até o processo cair -- o limitador viraria o próprio
# vetor de negação de serviço. Ao estourar, a tabela é limpa; o efeito é
# reiniciar a contagem, que é o modo de falha benigno.
MAX_TRACKED_CLIENTS = 10_000


class RateLimitMiddleware(BaseHTTPMiddleware):
    """
    Janela deslizante por cliente, em memória.

    Janela deslizante e não contador fixo: com janela fixa de 60s, um cliente
    envia o limite inteiro nos últimos segundos de uma janela e o limite
    inteiro de novo nos primeiros da seguinte -- o dobro do pretendido, no
    instante mais concentrado possível.
    """

    def __init__(self, app, enabled: bool = True):
        super().__init__(app)
        self.enabled = enabled
        # (ip, balde) -> instantes das requisições recentes
        self._hits: Dict[Tuple[str, str], Deque[float]] = defaultdict(deque)

    def _client_id(self, request: Request) -> str:
        """
        Identifica o cliente.

        X-Forwarded-For é lido, com a ressalva de que ele é FALSIFICÁVEL por
        quem fala direto com a API. Só é confiável atrás de um proxy que o
        reescreva. Em desenvolvimento não há proxy, e o cabeçalho não chega.
        Registrado para que quem colocar isto atrás de um balanceador saiba
        que precisa revisar esta função.
        """
        encaminhado = request.headers.get("x-forwarded-for")
        if encaminhado:
            return encaminhado.split(",")[0].strip()
        return request.client.host if request.client else "desconhecido"

    def _limits_for(self, path: str) -> Tuple[str, int, int]:
        if path.startswith(EXPENSIVE_PATHS):
            return "chat", CHAT_LIMIT, CHAT_WINDOW
        return "global", GLOBAL_LIMIT, GLOBAL_WINDOW

    async def dispatch(self, request: Request, call_next) -> Response:
        path = request.url.path
        if not self.enabled or path.startswith(EXEMPT_PATHS):
            return await call_next(request)

        # O preflight do CORS não consome nada e precisa passar, senão o
        # navegador reporta erro de CORS -- que mandaria o desenvolvedor
        # depurar a política de origem em vez do limite de taxa.
        if request.method == "OPTIONS":
            return await call_next(request)

        balde, limite, janela = self._limits_for(path)
        chave = (self._client_id(request), balde)
        agora = time.monotonic()

        if len(self._hits) > MAX_TRACKED_CLIENTS:
            log.warning("Tabela de rate limit estourou; reiniciando a contagem.")
            self._hits.clear()

        marcas = self._hits[chave]
        while marcas and agora - marcas[0] > janela:
            marcas.popleft()

        if len(marcas) >= limite:
            espera = int(janela - (agora - marcas[0])) + 1
            log.warning("Limite de taxa atingido por %s em %s", chave[0], path)
            return JSONResponse(
                status_code=429,
                content={
                    "detail": (
                        f"Muitas requisições. Limite de {limite} a cada "
                        f"{janela}s nesta rota. Tente de novo em {espera}s."
                    ),
                    "retry_after": espera,
                },
                headers={
                    "Retry-After": str(espera),
                    "X-RateLimit-Limit": str(limite),
                    "X-RateLimit-Remaining": "0",
                    **SECURITY_HEADERS,
                },
            )

        marcas.append(agora)
        response = await call_next(request)
        response.headers["X-RateLimit-Limit"] = str(limite)
        response.headers["X-RateLimit-Remaining"] = str(max(0, limite - len(marcas)))
        return response


# ---------------------------------------------------------------------------
# Validação de entrada
# ---------------------------------------------------------------------------

# Uma pergunta legítima de biologia espacial não passa disto. O limite não é
# estético: o texto da pergunta vai para o prompt, e um texto enorme é o
# veículo mais simples de injeção de prompt -- espaço para enterrar
# "ignore as instruções anteriores" no meio de ruído, longe do início, onde a
# atenção do modelo é menor.
#
# Também limita o custo: tokens de entrada são cobrados, e o orçamento de
# saída é compartilhado com o raciocínio interno do modelo.
MAX_QUESTION_CHARS = int(os.getenv("MAX_QUESTION_CHARS", "2000"))

# Caracteres de controle não aparecem em pergunta digitada por gente. Aparecem
# em payload montado para confundir parser ou para quebrar a delimitação do
# prompt.
_CONTROL_CHARS = {chr(c) for c in range(32) if chr(c) not in "\n\r\t"}


def sanitize_question(question: str) -> str:
    """
    Normaliza a pergunta antes de qualquer uso.

    NÃO tenta detectar injeção de prompt por palavra-chave. Bloquear "ignore
    as instruções" é teatro: a variação é infinita e o filtro só ensina o
    atacante a reescrever. A defesa real está em `prompts.py` -- delimitação
    explícita da entrada e instrução de precedência -- e nas travas que
    verificam a resposta DEPOIS de gerada, que é onde a injeção teria de
    aparecer para causar dano.

    O que esta função faz é remover o que não pode existir numa pergunta
    legítima e cortar o que excede o limite.

    Raises:
        ValueError: pergunta vazia. O chamador traduz para HTTP 422.
    """
    if not isinstance(question, str):
        raise ValueError("A pergunta precisa ser texto.")

    limpa = "".join(c for c in question if c not in _CONTROL_CHARS)
    # Colapsa espaço em branco: 5.000 espaços entre duas palavras são uma
    # pergunta curta inflada para passar por longa.
    limpa = " ".join(limpa.split())

    if not limpa:
        raise ValueError("A pergunta não pode ser vazia.")

    if len(limpa) > MAX_QUESTION_CHARS:
        # Trunca em vez de recusar: uma pergunta longa demais costuma ser
        # alguém colando um parágrafo inteiro, não um ataque, e recusar seria
        # hostil. O que importa é que o prompt receba um tamanho limitado.
        limpa = limpa[:MAX_QUESTION_CHARS].rsplit(" ", 1)[0]

    return limpa
