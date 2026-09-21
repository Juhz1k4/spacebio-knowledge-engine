"""
SpaceBio — API HTTP (ponto de entrada da aplicação)

Servidor FastAPI que expõe o motor de conhecimento. Suba com:

    uvicorn main:app --reload

ENDPOINTS
---------
    GET  /                 saudação simples
    GET  /api/v1/health    estado do corpus, do índice e do provedor de LLM
    POST /api/v1/chat      pergunta à Dra. Aris, com o contrato de evidência (§14)
    POST /chat             legado, mantido para o frontend atual

O QUE ACONTECE NO STARTUP
-------------------------
O `lifespan` abre UM driver Neo4j para toda a aplicação (§21) e carrega o
modelo de embeddings uma única vez. Fazer isso por requisição custaria
segundos e memória a cada pergunta.

Falha de conexão no startup NÃO derruba a API: os endpoints que dependem do
grafo respondem 503 com mensagem clara, e os demais seguem servindo. Isso
permite subir a API para diagnóstico mesmo com o banco fora do ar.

DEGRADAÇÃO SEM LLM
------------------
A API sobe sem `GOOGLE_API_KEY`. Nesse caso a Dra. Aris devolve as passagens
recuperadas em vez da síntese — o contrato de evidência continua completo,
só sem o texto gerado. Ver `aris._evidence_only_answer()`.

ORDEM DE LEITURA PARA QUEM ESTÁ CHEGANDO
-----------------------------------------
    config.py      credenciais e parâmetros
    schema.py      contrato de dados (PublicationRecord, ChunkRecord)
    retrieval.py   como a evidência é encontrada
    evidence.py    o formato da resposta
    aris.py        como as duas se juntam
    este arquivo   como isso vira HTTP
"""

from contextlib import asynccontextmanager
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional
import logging
import os

from config import MissingCredentialError, settings
from evidence import EvidenceAnswer
from graph_manager import build_driver
from retrieval import RetrievalRepository

log = logging.getLogger(__name__)

# --- CARREGAMENTO DE MODELOS E CONFIGURAÇÕES ---
# REMOVIDO (SPACEBIO-012): o pipeline de summarization era carregado aqui e
# nunca usado — resíduo de um endpoint de resumo que saiu do código. Além de
# custar ~1 GB de RAM e vários segundos de startup à toa, ele derrubava a API
# inteira no import: transformers 5.x removeu a task "summarization" do
# registry. Se o resumo voltar, reintroduzir com um modelo suportado.
#
# REMOVIDO (SPACEBIO-015): o spaCy e o cliente Gemini global também saíram.
# O spaCy servia à extração de keywords do RAG v1, substituído pela busca
# híbrida; e o cliente do Gemini agora vive atrás do LLMProvider (§31), para
# que trocar de fornecedor não signifique mexer na API. Nada disso é carregado
# no import — o startup ficou segundos mais rápido e a API sobe mesmo sem
# GOOGLE_API_KEY, servindo recuperação de evidência sem geração de texto.

# --- CONFIGURAÇÃO NEO4J ---
# Credenciais vêm do ambiente (.env), nunca do código — Master Briefing §8.2.
METADATA_FILE = settings.metadata_file


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Ciclo de vida do driver Neo4j (Master Briefing §21).

    Um único driver — e portanto um único pool de conexões — para toda a
    aplicação. A versão anterior criava um driver por request e nunca o
    fechava, vazando conexões a cada pergunta feita à Dra. Aris.

    Falha de conexão no startup não derruba a API: os endpoints que dependem
    do grafo respondem 503 com mensagem clara, e os demais seguem servindo.
    """
    app.state.neo4j_driver = None
    app.state.retrieval = None
    app.state.aris = None

    try:
        app.state.neo4j_driver = build_driver()
        app.state.neo4j_driver.verify_connectivity()
        app.state.retrieval = RetrievalRepository(app.state.neo4j_driver)
        print("Driver Neo4j inicializado.")
    except MissingCredentialError as error:
        print(f"!!! Neo4j não configurado: {error}")
    except Exception as error:
        print(f"!!! Não foi possível conectar ao Neo4j: {error}")

    # A Dra. Aris carrega o modelo de embeddings uma única vez, no startup.
    # Fazer isso por request custaria segundos e memória a cada pergunta.
    if app.state.retrieval is not None:
        try:
            from aris import DraAris
            from embedder import EmbeddingService

            app.state.aris = DraAris(app.state.retrieval, EmbeddingService())
            print("Dra. Aris pronta (SPACEBIO-014 + 015).")
        except Exception as error:  # noqa: BLE001
            print(f"!!! Dra. Aris indisponível: {error}")

    yield

    if app.state.neo4j_driver is not None:
        app.state.neo4j_driver.close()
        print("Driver Neo4j encerrado.")


app = FastAPI(
    title="Space Biology Knowledge Engine API",
    description="API com a assistente de IA Dra. Aris, agora com busca no grafo de conhecimento (RAG).",
    version="1.0.0", # Versão 1.0 - Funcionalidade principal completa!
    lifespan=lifespan,
)


def get_retrieval(request: Request) -> RetrievalRepository:
    """Injeta o repositório de recuperação, ou 503 se o grafo está indisponível."""
    repository = getattr(request.app.state, "retrieval", None)
    if repository is None:
        raise HTTPException(
            status_code=503,
            detail="Camada de recuperação indisponível: verifique o Neo4j e o .env.",
        )
    return repository


def get_aris(request: Request):
    """Injeta a Dra. Aris, ou 503 se ela não pôde ser inicializada."""
    aris = getattr(request.app.state, "aris", None)
    if aris is None:
        raise HTTPException(
            status_code=503,
            detail="Dra. Aris indisponível: verifique o Neo4j, o corpus e o .env.",
        )
    return aris

# --- CONFIGURAÇÃO CORS ---
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:8080", "http://localhost:5173", "http://127.0.0.1:8080", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- MODELOS DE DADOS ---
class ChatRequest(BaseModel):
    question: str
    top_k: Optional[int] = None


class ChatResponse(BaseModel):
    """Resposta enxuta, para o cliente que só quer o texto."""
    answer: str


# --- ENDPOINTS DA API ---

@app.get("/")
def read_root():
    return {"message": "Bem-vindo ao motor de conhecimento de Biologia Espacial!"}


@app.get("/api/v1/health")
def health(request: Request):
    """Estado do corpus, do índice e do provedor de LLM."""
    aris = getattr(request.app.state, "aris", None)
    if aris is None:
        return {"status": "degraded", "detail": "Dra. Aris não inicializada"}
    return {"status": "ok", **aris.health()}


@app.post("/api/v1/chat", response_model=EvidenceAnswer)
def chat_with_evidence(request: ChatRequest, aris=Depends(get_aris)) -> EvidenceAnswer:
    """
    Pergunta à Dra. Aris, com o contrato de evidência completo (§14).

    Devolve a resposta, as passagens que a sustentam com procedência, o rastro
    da recuperação e se a resposta está de fato fundamentada.

    Não existe caminho de erro para "não sei": a falta de evidência volta como
    200 com `grounded=false` e `sources` vazio. Ausência de evidência é um
    resultado científico legítimo (§15.7), não uma exceção.
    """
    if not request.question or not request.question.strip():
        raise HTTPException(status_code=422, detail="A pergunta não pode ser vazia.")

    try:
        return aris.answer(request.question, top_k=request.top_k)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except Exception as error:  # noqa: BLE001
        raise HTTPException(
            status_code=500, detail=f"{type(error).__name__}: {error}"
        ) from error


@app.post("/chat", response_model=ChatResponse)
def chat_with_dr_aris(request: ChatRequest, aris=Depends(get_aris)) -> ChatResponse:
    """
    Endpoint legado, mantido para o frontend atual.

    Agora é servido pela mesma Dra. Aris fundamentada de /api/v1/chat — o que
    mudou é só o formato de saída. A versão anterior deste endpoint dizia ao
    modelo "tentarei responder com meu conhecimento geral" quando o grafo não
    achava nada, exatamente o fallback paramétrico que o §9.2 aponta como a
    principal erosão de confiança do sistema. Isso não existe mais.

    Prefira /api/v1/chat: sem as fontes, o cliente não tem como verificar nada.
    """
    if not request.question or not request.question.strip():
        raise HTTPException(status_code=422, detail="A pergunta não pode ser vazia.")

    result: EvidenceAnswer = aris.answer(request.question, top_k=request.top_k)

    # Anexa as fontes ao texto, já que este formato não tem campo para elas.
    if result.cited_sources:
        referencias = "\n\n---\nFontes:\n" + "\n".join(
            f"[{s.citation_index}] {s.title}"
            + (f" — DOI: {s.doi}" if s.doi else "")
            + (f"\n    {s.url}" if s.url else "")
            for s in result.cited_sources
        )
        return ChatResponse(answer=result.answer + referencias)

    return ChatResponse(answer=result.answer)

