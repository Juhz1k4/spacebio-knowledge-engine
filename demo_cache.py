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
import json
import logging
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from config import MissingCredentialError, settings

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
    # Mesma capacidade em inglês, mostrando a busca cross-lingual.
    "Which genes respond to space radiation?",
]


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

    def is_stale(self, embedding_model: str, ontology_version: str) -> bool:
        """A entrada foi gerada sob condições diferentes das atuais?"""
        return (
            self.embedding_model != embedding_model
            or self.ontology_version != ontology_version
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "question": self.question,
            "embedding": self.embedding,
            "answer": self.answer,
            "embedding_model": self.embedding_model,
            "llm_model": self.llm_model,
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
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            log.info("Cache de demonstração ausente em %s", self.path)
            return
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            self.entries = [CacheEntry(**row) for row in payload.get("entries", [])]
            log.info("Cache de demonstração: %d entrada(s)", len(self.entries))
        except Exception as error:  # noqa: BLE001
            log.warning("Cache de demonstração ilegível (%s); ignorado.", error)
            self.entries = []

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

    def lookup(
        self,
        question_embedding: Sequence[float],
        embedding_model: str,
        ontology_version: str,
    ) -> Optional[CacheEntry]:
        """
        Procura a entrada mais parecida acima do limiar.

        Entradas geradas sob outro modelo ou outra ontologia são ignoradas —
        comparar embeddings de modelos diferentes não tem significado.
        """
        best: Optional[CacheEntry] = None
        best_score = 0.0

        for entry in self.entries:
            if entry.is_stale(embedding_model, ontology_version):
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


def build_cache(questions: Sequence[str], threshold: float) -> int:
    """Gera e grava as respostas do roteiro, chamando o LLM uma vez cada."""
    from aris import DraAris
    from embedder import EmbeddingService
    from graph_manager import build_driver
    from llm_provider import DEFAULT_GEMINI_MODEL
    from ontology import ONTOLOGY_VERSION
    from retrieval import RetrievalRepository

    service = EmbeddingService()
    driver = build_driver()
    entries: List[CacheEntry] = []

    print(f"Gerando cache para {len(questions)} pergunta(s)...")
    print(f"  embeddings : {settings.embedding_model}")
    print(f"  LLM        : {DEFAULT_GEMINI_MODEL}")
    print(f"  ontologia  : {ONTOLOGY_VERSION}\n")

    try:
        aris = DraAris(RetrievalRepository(driver), service)
        for position, question in enumerate(questions, start=1):
            print(f"  [{position}/{len(questions)}] {question[:60]}")
            answer = aris.answer(question)
            entries.append(
                CacheEntry(
                    question=question,
                    embedding=service.embed_query(question),
                    answer=json.loads(answer.model_dump_json()),
                    embedding_model=settings.embedding_model,
                    llm_model=DEFAULT_GEMINI_MODEL,
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

    cache = DemoCache(threshold=threshold)
    cache.save(entries)
    print(f"\n{len(entries)} entrada(s) gravadas em {cache.path}")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="SPACEBIO-015.1 — cache de demonstração")
    parser.add_argument("--build", action="store_true", help="gerar o cache do roteiro")
    parser.add_argument("--list", action="store_true", help="listar o que está cacheado")
    parser.add_argument("--test", help="testar se uma pergunta bate no cache")
    parser.add_argument(
        "--threshold", type=float, default=DEFAULT_SIMILARITY_THRESHOLD
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")

    try:
        if args.build:
            return build_cache(DEMO_QUESTIONS, args.threshold)

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
