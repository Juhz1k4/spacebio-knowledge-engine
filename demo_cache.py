"""
SPACEBIO-015.1 — Cache de respostas para demonstração

Serve respostas pré-computadas para um roteiro de perguntas, sem acionar o
LLM. Protege a apresentação do limite de 20 requisições/dia do free tier do
Gemini, que se esgota nos ensaios de véspera.

COMO A PERGUNTA É RECONHECIDA
-----------------------------
Por similaridade de embedding, não por igualdade de texto. Ninguém digita a
pergunta exatamente como foi cacheada: um acento a mais, "qual" no lugar de
"quais", e um cache por chave literal erra. O limiar de 0,98 é deliberadamente
alto — reconhece a mesma pergunta reformulada, não uma pergunta parecida.

Como os embeddings já estão normalizados, a similaridade é o produto interno.
O custo é uma única vetorização (~20 ms em CPU), sem chamada de rede.

O CACHE PRECISA SABER QUANDO ESTÁ VELHO
---------------------------------------
Cada entrada guarda o modelo de embeddings, o modelo de LLM e a versão da
ontologia usados na gravação. Se qualquer um mudou, a entrada é ignorada: uma
resposta gerada sobre outro corpus, ou vetorizada por outro modelo, não é a
mesma resposta — e servir isso numa avaliação seria pior que perder a quota.

HONESTIDADE NA DEMONSTRAÇÃO
---------------------------
Toda resposta vinda do cache carrega `cached: true` nos warnings e a data de
geração. Uma demonstração que esconde o cache está mentindo sobre o que o
sistema faz ao vivo.

Uso:
    python demo_cache.py --build          # grava o cache do roteiro
    python demo_cache.py --list           # mostra o que está cacheado
    python demo_cache.py --test "..."     # testa se uma pergunta bate
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from config import MissingCredentialError, settings

# A mesma normalização do roteador de intenção, de propósito: duas definições
# de "a mesma pergunta" acabariam divergindo, e a diferença seria invisível
# até alguém digitar sem acento durante a demonstração.
from intent import normalize as normalize_question

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

log = logging.getLogger(__name__)

CACHE_FILE = "demo_cache.json"

# Alto de propósito: reconhecer a MESMA pergunta reescrita, nunca uma pergunta
# diferente sobre tema próximo. Abaixo disto, prefira gerar ao vivo.
DEFAULT_SIMILARITY_THRESHOLD = 0.98

# Roteiro da demonstração. Cada uma exercita um comportamento diferente do
# sistema — não são apenas perguntas "que funcionam".
DEMO_QUESTIONS: List[str] = [
    # Caminho feliz em português: evidência, citações, entidades.
    "Como a microgravidade afeta a densidade óssea?",
    # Gene específico: exercita o canal léxico, que é o que encontra um
    # símbolo como RUNX2 quando a busca semântica se perde no tema geral.
    "Qual o papel do gene RUNX2 na formação óssea durante o voo espacial?",
    # Mesma capacidade em inglês, mostrando a busca cross-lingual.
    "Which genes respond to space radiation?",
    # Modelo animal: o corpus é forte em roedores, e a pergunta mostra
    # entidades de organismo e missão no painel.
    #
    # A formulação importa. "Quais experimentos com camundongos foram feitos
    # na Estação Espacial Internacional?" pontua 0,910 e é RECUSADA -- fica
    # abaixo do limiar de 0,92 apesar de ser plenamente do domínio. Perguntas
    # por INVENTÁRIO ("quais experimentos foram feitos") casam mal com um
    # corpus que descreve resultados, não catálogos. Reformulada para o
    # EFEITO, sobe para 0,9244.
    "Como o voo espacial afeta a fisiologia de camundongos?",
    # Radiação e DNA: o quarto chip da tela inicial.
    "Como a radiação espacial causa danos ao DNA?",
]

# E3-05 -- hash das REGRAS sob as quais a resposta foi gerada.
#
# O prompt define como a Dra. Aris cita, quando recusa e em que idioma
# escreve. Mudar uma dessas regras e continuar servindo respostas antigas
# mostraria, numa demonstração, um comportamento que o sistema não tem mais.
# É a invalidação que faltava: modelo e ontologia já eram conferidos, o
# contrato de geração não.
def prompt_fingerprint() -> str:
    """Impressão digital do prompt de sistema em vigor."""
    from prompts import SYSTEM_PROMPT

    return hashlib.sha256(SYSTEM_PROMPT.encode("utf-8")).hexdigest()[:16]


@dataclass
class CacheEntry:
    """Uma resposta pré-computada e o contexto em que foi gerada."""

    question: str
    embedding: List[float]
    answer: Dict[str, Any]
    embedding_model: str
    llm_model: str
    ontology_version: str
    generated_at: str
    # Default vazio, e por isso no fim: um cache gravado antes da E3-05 nao
    # tem este campo, e carregar com CacheEntry(**row) quebraria sem ele.
    prompt_hash: str = ""

    def is_stale(
        self,
        embedding_model: str,
        ontology_version: str,
        prompt_hash: Optional[str] = None,
    ) -> bool:
        """
        A entrada foi gerada sob condições diferentes das atuais?

        Três eixos, e cada um invalida por um motivo distinto:

          modelo de embedding  comparar vetores de modelos diferentes não tem
                               significado algum — o número sai, e é lixo;
          ontologia            as entidades do painel mudariam;
          prompt               as REGRAS mudaram. A resposta em cache obedece
                               a um contrato de citação, recusa e idioma que o
                               sistema não pratica mais (E3-05).

        `prompt_hash` vazio na entrada significa cache anterior à E3-05: não
        invalida, para não derrubar um cache existente na véspera da demo. A
        conferência passa a valer assim que o cache for regerado.
        """
        if self.embedding_model != embedding_model:
            return True
        if self.ontology_version != ontology_version:
            return True
        if prompt_hash and self.prompt_hash and self.prompt_hash != prompt_hash:
            return True
        return False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "question": self.question,
            "embedding": self.embedding,
            "answer": self.answer,
            "embedding_model": self.embedding_model,
            "llm_model": self.llm_model,
            "prompt_hash": self.prompt_hash,
            "ontology_version": self.ontology_version,
            "generated_at": self.generated_at,
        }


class DemoCache:
    """
    Cache de respostas por similaridade de pergunta.

    Carregado uma vez no startup da API; a consulta é um produto interno
    contra poucas entradas, então o custo é desprezível.
    """

    def __init__(
        self,
        path: Optional[Path] = None,
        threshold: float = DEFAULT_SIMILARITY_THRESHOLD,
    ):
        self.path = path or (settings.data_dir / CACHE_FILE)
        self.threshold = threshold
        self.entries: List[CacheEntry] = []
        # Índice de casamento exato. Inicializado aqui, e não só no _load:
        # sem arquivo de cache o _load retorna cedo, e lookup_exact quebraria
        # com AttributeError num caminho que deveria apenas devolver None.
        self._exact: Dict[str, CacheEntry] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            log.info("Cache de demonstração ausente em %s", self.path)
            return
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            self.entries = [CacheEntry(**row) for row in payload.get("entries", [])]
            self._exact = {normalize_question(e.question): e for e in self.entries}
            log.info("Cache de demonstração: %d entrada(s)", len(self.entries))
        except Exception as error:  # noqa: BLE001
            log.warning("Cache de demonstração ilegível (%s); ignorado.", error)
            self.entries = []
            self._exact = {}

    def save(self, entries: Sequence[CacheEntry]) -> None:
        payload = {
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "similarity_threshold": self.threshold,
            "entries": [entry.to_dict() for entry in entries],
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        self.entries = list(entries)

    # ------------------------------------------------------------------ #

    def lookup_exact(
        self,
        question: str,
        embedding_model: str,
        ontology_version: str,
        prompt_hash: Optional[str] = None,
    ) -> Optional[CacheEntry]:
        """
        Casamento exato da pergunta, por texto normalizado (E3-05).

        POR QUE ESTE CAMINHO EXISTE SEPARADO DO DE SIMILARIDADE
        -------------------------------------------------------
        O de similaridade precisa do vetor da pergunta, e obter o vetor é uma
        passagem pelo modelo de embeddings -- dezenas de milissegundos que
        acontecem ANTES de sequer saber se há acerto. Num roteiro de
        demonstração as perguntas são digitadas ou clicadas exatamente como
        foram cacheadas, então o caminho comum não precisa pagar isso.

        A normalização é a mesma do roteador de intenção: minúsculas, sem
        acento, sem pontuação, espaços colapsados. Assim "Como a
        microgravidade afeta a densidade óssea?" e "como a microgravidade
        afeta a densidade ossea" são a mesma chave -- o que importa quando
        alguém digita sem acento no meio da apresentação.

        Um dicionário, então o custo é de busca em tabela hash e não cresce
        com o tamanho do cache.
        """
        entry = self._exact.get(normalize_question(question))
        if entry is None:
            return None
        if entry.is_stale(embedding_model, ontology_version, prompt_hash):
            log.info("Cache: acerto exato descartado por estar desatualizado")
            return None
        log.info("Cache HIT exato: %.50s", entry.question)
        return entry

    def lookup(
        self,
        question_embedding: Sequence[float],
        embedding_model: str,
        ontology_version: str,
        prompt_hash: Optional[str] = None,
    ) -> Optional[CacheEntry]:
        """
        Procura a entrada mais parecida acima do limiar.

        Entradas geradas sob outro modelo ou outra ontologia são ignoradas —
        comparar embeddings de modelos diferentes não tem significado.
        """
        best: Optional[CacheEntry] = None
        best_score = 0.0

        for entry in self.entries:
            if entry.is_stale(embedding_model, ontology_version, prompt_hash):
                continue
            if len(entry.embedding) != len(question_embedding):
                continue
            # Vetores normalizados: produto interno é a similaridade.
            score = sum(a * b for a, b in zip(entry.embedding, question_embedding))
            if score > best_score:
                best, best_score = entry, score

        if best is not None and best_score >= self.threshold:
            log.info("Cache HIT (%.4f): %.50s", best_score, best.question)
            return best

        log.info("Cache MISS (melhor %.4f, limiar %.2f)", best_score, self.threshold)
        return None

    def __len__(self) -> int:
        return len(self.entries)


def mark_as_cached(answer: Dict[str, Any], entry: CacheEntry) -> Dict[str, Any]:
    """
    Anexa à resposta a informação de que veio do cache.

    Não é opcional: uma demonstração que apresenta resposta cacheada como
    geração ao vivo engana quem assiste.
    """
    result = dict(answer)
    warnings = list(result.get("warnings") or [])
    warnings.append(
        f"Resposta pré-computada em {entry.generated_at[:10]} "
        f"({entry.llm_model}), servida do cache de demonstração."
    )
    result["warnings"] = warnings
    return result


# --------------------------------------------------------------------- #
# Construção do cache
# --------------------------------------------------------------------- #


def build_cache(
    questions: Sequence[str], threshold: float, rebuild: bool = False
) -> int:
    """
    Gera e grava as respostas do roteiro, chamando o LLM uma vez cada.

    Por padrão é INCREMENTAL: pergunta já cacheada e ainda válida (mesmo
    modelo, mesma ontologia, mesmo prompt) é mantida. O free tier são 20
    requisições por dia, e regerar o roteiro inteiro para acrescentar uma
    pergunta gasta o orçamento à toa -- justamente na véspera da demonstração,
    quando ele importa.

    `rebuild=True` força tudo, para quando o prompt ou o corpus mudarem.
    """
    from aris import DraAris
    from embedder import EmbeddingService
    from graph_manager import build_driver
    from llm_provider import DEFAULT_GEMINI_MODEL
    from ontology import ONTOLOGY_VERSION
    from retrieval import RetrievalRepository

    entries: List[CacheEntry] = []
    rejeitadas: List[tuple] = []

    # A checagem do que já existe vem ANTES de abrir o driver e carregar o
    # modelo: quando o roteiro inteiro já está em cache, não há motivo para
    # pagar segundos de startup nem abrir conexão com o grafo.
    reaproveitadas: List[CacheEntry] = []
    if not rebuild:
        existente = DemoCache(threshold=threshold)
        fingerprint = prompt_fingerprint()
        for entry in existente.entries:
            if entry.question in questions and not entry.is_stale(
                settings.embedding_model, ONTOLOGY_VERSION, fingerprint
            ):
                reaproveitadas.append(entry)
        if reaproveitadas:
            aproveitadas = {e.question for e in reaproveitadas}
            questions = [q for q in questions if q not in aproveitadas]
            print(f"{len(reaproveitadas)} entrada(s) ainda válidas, mantidas.")

    if not questions:
        print("Nada a gerar: o roteiro inteiro já está em cache e válido.")
        return 0

    service = EmbeddingService()
    driver = build_driver()

    print(f"Gerando cache para {len(questions)} pergunta(s)...")
    print(f"  embeddings : {settings.embedding_model}")
    print(f"  LLM        : {DEFAULT_GEMINI_MODEL}")
    print(f"  ontologia  : {ONTOLOGY_VERSION}\n")

    try:
        aris = DraAris(RetrievalRepository(driver), service)
        for position, question in enumerate(questions, start=1):
            print(f"  [{position}/{len(questions)}] {question[:60]}")
            answer = aris.answer(question)

            # E3-05 -- so entra no cache resposta que presta.
            #
            # Cachear uma degradacao serviria a falha PARA SEMPRE: a pergunta
            # do roteiro devolveria "sintese indisponivel" em toda demo, sem
            # nunca mais tentar gerar. Uma recusa cacheada e pior ainda, por
            # parecer que o corpus nao cobre o tema.
            #
            # O modo de falha provavel aqui e a quota (20 requisicoes por dia
            # por modelo), que e justamente o motivo de este cache existir --
            # entao o caso nao e hipotetico.
            if answer.status != "ok" or not answer.grounded:
                rejeitadas.append((question, answer.status, list(answer.warnings)))
                print(f"        DESCARTADA: status={answer.status} grounded={answer.grounded}")
                continue

            entries.append(
                CacheEntry(
                    question=question,
                    embedding=service.embed_query(question),
                    answer=json.loads(answer.model_dump_json()),
                    embedding_model=settings.embedding_model,
                    llm_model=DEFAULT_GEMINI_MODEL,
                    prompt_hash=prompt_fingerprint(),
                    ontology_version=ONTOLOGY_VERSION,
                    generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                )
            )
            trace = answer.retrieval
            print(
                f"        grounded={answer.grounded} "
                f"citadas={trace.chunks_used}/{trace.chunks_considered} "
                f"score={trace.top_score}"
            )
    finally:
        driver.close()

    if rejeitadas:
        print(f"\n{len(rejeitadas)} pergunta(s) NAO entraram no cache:")
        for question, status, warnings in rejeitadas:
            print(f"  {question[:60]}")
            print(f"      status={status}")
            for warning in warnings[:2]:
                print(f"      {warning[:100]}")

    if not entries:
        print("\nNenhuma resposta utilizavel. O cache NAO foi sobrescrito.")
        return 1


    cache = DemoCache(threshold=threshold)
    cache.save(reaproveitadas + entries)
    print(
        f"\n{len(reaproveitadas) + len(entries)} entrada(s) em {cache.path} "
        f"({len(reaproveitadas)} reaproveitada(s), {len(entries)} nova(s))"
    )
    if rejeitadas:
        print("Rode de novo para completar o roteiro quando a causa for resolvida.")
        return 1
    return 0


def refresh_metadata(threshold: float) -> int:
    """
    Atualiza os metadados bibliográficos das fontes já cacheadas (E3-06).

    POR QUE NÃO SIMPLESMENTE REGERAR
    --------------------------------
    Enriquecer metadados não muda uma vírgula do texto gerado: a resposta da
    Dra. Aris, suas citações [n] e os trechos recuperados continuam os mesmos.
    O que mudou foi o que se sabe SOBRE as publicações -- autores e ano, que
    vieram do Crossref depois.

    Regerar custaria uma chamada de LLM por pergunta do roteiro, e o free tier
    são 20 por dia. Gastar quota para reescrever um texto idêntico é o tipo de
    desperdício que só se percebe quando ela acaba na véspera.

    O casamento é por `chunk_id`, que é determinístico e identifica o trecho
    exato de onde a publicação vem.
    """
    from graph_manager import build_driver
    from ontology import ONTOLOGY_VERSION
    from retrieval import RetrievalRepository

    cache = DemoCache(threshold=threshold)
    if not cache.entries:
        print("Cache vazio: nada a atualizar.")
        return 1

    chunk_ids = [
        source["chunk_id"]
        for entry in cache.entries
        for source in entry.answer.get("sources", [])
        if source.get("chunk_id")
    ]
    if not chunk_ids:
        print("Nenhuma fonte com chunk_id no cache.")
        return 1

    driver = build_driver()
    try:
        repo = RetrievalRepository(driver)
        # Busca as passagens atuais, que agora trazem os metadados do Crossref.
        atuais = {p.chunk_id: p for p in repo.passages_by_ids(chunk_ids)}
    finally:
        driver.close()

    atualizadas = faltantes = 0
    for entry in cache.entries:
        for source in entry.answer.get("sources", []):
            passagem = atuais.get(source.get("chunk_id"))
            if passagem is None:
                faltantes += 1
                continue
            source["citation"] = {
                "authors": passagem.authors or [],
                "year": passagem.publication_year,
                "journal": passagem.journal,
                "volume": passagem.volume,
                "issue": passagem.issue,
                "pages": passagem.pages,
            }
            atualizadas += 1

    cache.save(cache.entries)
    print(f"  {atualizadas} fonte(s) com metadados atualizados")
    if faltantes:
        print(f"  {faltantes} fonte(s) sem correspondência no grafo (chunk removido?)")

    completas = sum(
        1
        for entry in cache.entries
        for source in entry.answer.get("sources", [])
        if source.get("citation", {}).get("authors")
        and source.get("citation", {}).get("year")
    )
    print(f"  {completas}/{atualizadas} com autores E ano (citação completa)")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="SPACEBIO-015.1 — cache de demonstração")
    parser.add_argument("--build", action="store_true", help="gerar o cache do roteiro")
    parser.add_argument(
        "--refresh-metadata",
        dest="refresh_metadata",
        action="store_true",
        help="atualizar só os metadados bibliográficos, sem chamar o LLM",
    )
    parser.add_argument(
        "--rebuild",
        action="store_true",
        help="regerar TUDO, ignorando o que ja esta em cache (gasta quota)",
    )
    parser.add_argument("--list", action="store_true", help="listar o que está cacheado")
    parser.add_argument("--test", help="testar se uma pergunta bate no cache")
    parser.add_argument(
        "--threshold", type=float, default=DEFAULT_SIMILARITY_THRESHOLD
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")

    try:
        if args.refresh_metadata:
            return refresh_metadata(args.threshold)

        if args.build or args.rebuild:
            return build_cache(DEMO_QUESTIONS, args.threshold, rebuild=args.rebuild)

        cache = DemoCache(threshold=args.threshold)

        if args.list:
            print(f"{len(cache)} entrada(s) em {cache.path}\n")
            for entry in cache.entries:
                print(f"  {entry.question}")
                print(
                    f"    gerada em {entry.generated_at[:16]} · {entry.llm_model} · "
                    f"{entry.embedding_model.split('/')[-1]}"
                )
                print(f"    grounded={entry.answer.get('grounded')}")
            return 0

        if args.test:
            from embedder import EmbeddingService
            from ontology import ONTOLOGY_VERSION

            service = EmbeddingService()
            hit = cache.lookup(
                service.embed_query(args.test),
                settings.embedding_model,
                ONTOLOGY_VERSION,
            )
            if hit:
                print(f"HIT: {hit.question}")
                print(f"  resposta: {hit.answer['answer'][:120]}...")
            else:
                print("MISS — a pergunta seria gerada ao vivo")
            return 0

        parser.print_help()
        return 0

    except MissingCredentialError as error:
        print(f"\nCONFIGURAÇÃO AUSENTE:\n{error}")
        return 2
    except Exception as error:  # noqa: BLE001
        print(f"\nERRO: {type(error).__name__}: {error}")
        import traceback

        traceback.print_exc()
        return 3


if __name__ == "__main__":
    sys.exit(main())
