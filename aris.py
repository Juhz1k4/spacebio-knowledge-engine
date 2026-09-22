"""
SPACEBIO-015 — Dra. Aris com grounding verificado

Liga tudo o que a Fase 1 construiu:

    pergunta
       -> EmbeddingService          (SPACEBIO-010)
       -> hybrid_search             (SPACEBIO-013: vetorial + BM25, fundidos por RRF)
       -> limiar de evidência       (§15.7: recusar em vez de inventar)
       -> prompt de grounding       (SPACEBIO-015)
       -> LLMProvider               (§31)
       -> verificação de citações   (§14: chunks_used medido, não estimado)
       -> EvidenceAnswer            (SPACEBIO-014)

O que distingue este fluxo de "RAG com um prompt bom": ele nunca confia na
palavra do modelo. Duas travas independentes:

TRAVA 1 — antes do modelo. Se a melhor passagem recuperada fica abaixo do
limiar de similaridade, a pergunta é recusada sem gastar uma chamada de API.
Medido neste corpus: perguntas do domínio pontuam 0,89–0,92 e perguntas fora
dele 0,59–0,66; o limiar de 0,72 fica no vão entre os dois.

TRAVA 2 — depois do modelo. As citações [n] do texto são conferidas contra as
fontes que existem. Citação fora do intervalo é fabricação: fica registrada em
`warnings` e a resposta é marcada como não fundamentada. Uma resposta que não
cita nada também não passa.
"""

from __future__ import annotations

import logging
import os
import time
from typing import List, Optional

from evidence import (
    EvidenceAnswer,
    RetrievalTrace,
    build_sources,
    insufficient_evidence_answer,
    verify_citations,
)
from intent import MetaIntent, classify as classify_intent
from llm_provider import LLMError, LLMProvider, LLMUnavailable, default_provider
from prompts import SYSTEM_PROMPT, build_user_prompt
from retrieval import RetrievalRepository

log = logging.getLogger(__name__)

# Limiar de similaridade de cosseno abaixo do qual não há evidência utilizável.
# Calibrado em 2026-09-02 contra o corpus de 493 publicações: perguntas do
# domínio ficaram em 0,8959–0,9217 e perguntas fora dele em 0,5875–0,6555.
# 0,72 fica confortavelmente no vão. Note que o piso NÃO é zero: com vetores
# normalizados, qualquer pergunta pontua ~0,6 — sem limiar, o modelo receberia
# "evidência" para qualquer coisa.
# 0.92, nao 0.72: o multilingual-e5-small comprime a faixa de scores e o valor
# do MiniLM nao se transporta entre as escalas. Ver .env.example para a
# calibracao e por que este limiar sozinho nao separa dominio de nao-dominio.
EVIDENCE_THRESHOLD = float(os.getenv("EVIDENCE_THRESHOLD", "0.92"))

# Quantas passagens vão ao prompt. Acima de ~8 o modelo começa a diluir a
# atenção e a citar menos; abaixo de 3 ele fica sem material para relacionar.
DEFAULT_TOP_K = 6


def meta_answer(
    question: str, intent: MetaIntent, threshold: float
) -> EvidenceAnswer:
    """
    Constrói a resposta para uma pergunta operacional.

    Devolve o mesmo EvidenceAnswer de sempre, para que o cliente não precise
    de um caminho especial. Dois campos merecem atenção:

    `grounded=False` — a resposta NÃO vem do corpus, e o contrato precisa
    dizer isso. Marcar como fundamentada seria mentir: não há passagem
    nenhuma sustentando o texto, ele é escrito por nós.

    `sources=[]` — não há o que citar. É coerente com grounded=False, e a
    interface já trata esse par: `isEvidenceRefusal()` no frontend distingue
    "sem fontes" de "com fontes mas sem síntese".

    O warning identifica a natureza da resposta, para que ninguém a confunda
    com recuperação do corpus numa avaliação.
    """
    return EvidenceAnswer(
        answer=intent.answer,
        sources=[],
        entities=[],
        retrieval=RetrievalTrace(
            query=question,
            chunks_considered=0,
            chunks_used=0,
            top_score=None,
            evidence_threshold=threshold,
            channels=[],
        ),
        grounded=False,
        warnings=[
            "Resposta sobre o funcionamento do sistema, escrita por nós — "
            "não foi recuperada do corpus científico."
        ],
    )


class DraAris:
    """
    A assistente científica, com procedência verificada.

    Recebe repositório e provedor prontos — ambos têm ciclo de vida de
    aplicação (§21), não de pergunta.
    """

    def __init__(
        self,
        repository: RetrievalRepository,
        embedding_service,
        provider: Optional[LLMProvider] = None,
        threshold: float = EVIDENCE_THRESHOLD,
        top_k: int = DEFAULT_TOP_K,
        demo_cache=None,
    ):
        """
        Args:
            demo_cache: DemoCache opcional (SPACEBIO-015.1). Quando presente,
                perguntas do roteiro são servidas sem acionar o LLM — protege
                a apresentação do limite de 20 requisições/dia do free tier.
                Desligado por padrão: o cache só entra quando explicitamente
                injetado, para que ninguém avalie o sistema sem saber disso.
        """
        self.repository = repository
        self.embeddings = embedding_service
        self.provider = provider or default_provider()
        self.threshold = threshold
        self.top_k = top_k
        self.demo_cache = demo_cache

    # ------------------------------------------------------------------ #

    def answer(self, question: str, top_k: Optional[int] = None) -> EvidenceAnswer:
        """
        Responde a uma pergunta com evidência do corpus.

        Sempre devolve um EvidenceAnswer completo — inclusive quando recusa.
        A ausência de evidência é um resultado, não uma exceção.
        """
        if not question or not question.strip():
            raise ValueError("Pergunta vazia.")

        question = question.strip()
        top_k = top_k or self.top_k
        started = time.time()

        # --- Roteamento de intenção, antes de tudo ---
        # "Quem é você?" não tem resposta no corpus, e seguir o caminho normal
        # devolveria a recusa por falta de evidência — tecnicamente correta e
        # péssima para quem só quer entender a ferramenta. Também economiza a
        # quota do LLM, que perguntas operacionais consumiriam à toa.
        meta = classify_intent(question)
        if meta is not None:
            log.info("Intenção operacional (%s): %r", meta.name, question[:50])
            return meta_answer(question, meta, self.threshold)

        # --- Recuperação ---
        vector = self.embeddings.embed_query(question)

        # --- Cache de demonstração (SPACEBIO-015.1) ---
        # Consultado DEPOIS de vetorizar (a chave é o embedding) e ANTES da
        # busca, que é o trabalho caro. Uma falha aqui nunca derruba a
        # resposta: o cache é conveniência, não caminho crítico.
        if self.demo_cache is not None:
            try:
                from demo_cache import mark_as_cached
                from ontology import ONTOLOGY_VERSION

                hit = self.demo_cache.lookup(
                    vector, self.embeddings.model_name, ONTOLOGY_VERSION
                )
                if hit is not None:
                    log.info("Cache de demonstração: HIT para %r", question[:50])
                    return EvidenceAnswer(**mark_as_cached(hit.answer, hit))
            except Exception as error:  # noqa: BLE001
                log.warning("Cache de demonstração falhou (%s); seguindo ao vivo.", error)

        results = self.repository.hybrid_search(question, vector, top_k=top_k)

        if not results:
            log.info("Sem resultados para %r.", question[:60])
            return insufficient_evidence_answer(
                query=question,
                threshold=self.threshold,
                reason="A busca não retornou nenhuma passagem.",
            )

        # --- TRAVA 1: limiar de evidência, antes de chamar o modelo ---
        # O score da fusão RRF não é comparável entre queries; o que decide é a
        # similaridade semântica da melhor passagem.
        top_score = max(
            (r.passage.score for r in results if r.semantic_rank is not None),
            default=None,
        )
        if top_score is None or top_score < self.threshold:
            log.info(
                "Evidência insuficiente para %r (melhor=%.3f, limiar=%.2f).",
                question[:50],
                top_score or 0.0,
                self.threshold,
            )
            return insufficient_evidence_answer(
                query=question,
                threshold=self.threshold,
                considered=len(results),
                top_score=top_score,
                reason="Nenhuma passagem atingiu o limiar mínimo de similaridade.",
            )

        sources = build_sources(results)
        channels = sorted({channel for r in results for channel in r.matched_channels})

        # --- Geração ---
        warnings: List[str] = []
        try:
            response = self.provider.generate(
                SYSTEM_PROMPT, build_user_prompt(question, sources)
            )
            answer_text = response.text
        except LLMUnavailable as error:
            log.error("Provedor indisponível: %s", error)
            return self._evidence_only_answer(question, sources, top_score, channels, str(error))
        except LLMError as error:
            log.error("Falha na geração: %s", error)
            return self._evidence_only_answer(question, sources, top_score, channels, str(error))

        # Resposta cortada por limite de tokens pode ter perdido citações no
        # meio. Precisa ser dito: senão a verificação abaixo apenas conclui
        # "não fundamentada", sem revelar que a causa foi truncamento.
        if response.finish_reason == "MAX_TOKENS":
            warnings.append(
                "A resposta foi interrompida por limite de tokens e pode estar "
                "incompleta. Aumente LLM_MAX_OUTPUT_TOKENS."
            )
        elif response.finish_reason in ("SAFETY", "RECITATION"):
            warnings.append(
                f"O provedor interrompeu a geração ({response.finish_reason})."
            )

        # --- TRAVA 2: as citações apontam para fontes reais? ---
        verification = verify_citations(answer_text, sources)

        if verification["invalid"]:
            warnings.append(
                f"O modelo citou fontes inexistentes: "
                f"{verification['invalid']} (só há {len(sources)}). "
                "Essas citações foram desconsideradas."
            )
        if not verification["cited"]:
            warnings.append(
                "A resposta não citou nenhuma passagem — não é possível verificar "
                "em que evidência ela se apoia."
            )

        grounded = bool(verification["cited"]) and not verification["invalid"]

        elapsed = time.time() - started
        log.info(
            "Resposta em %.2fs: %d/%d fontes citadas, grounded=%s",
            elapsed,
            verification["used"],
            len(sources),
            grounded,
        )

        # Entidades das passagens EFETIVAMENTE citadas — não de tudo o que foi
        # recuperado. O contrato descreve a evidência que sustenta a resposta,
        # não o material que o retriever considerou e a resposta ignorou.
        cited_chunk_ids = [
            source.chunk_id for source in sources if source.citation_index in verification["cited"]
        ]
        entities = self._entities_for(cited_chunk_ids)

        return EvidenceAnswer(
            answer=answer_text,
            sources=sources,
            entities=entities,
            retrieval=RetrievalTrace(
                query=question,
                chunks_considered=len(sources),
                chunks_used=verification["used"],
                top_score=round(top_score, 4),
                evidence_threshold=self.threshold,
                channels=channels,
            ),
            grounded=grounded,
            warnings=warnings,
        )

    # ------------------------------------------------------------------ #

    def health(self) -> dict:
        stats = self.repository.corpus_stats()
        return {
            "llm": self.provider.health(),
            "corpus": stats,
            "evidence_threshold": self.threshold,
            "top_k": self.top_k,
            "demo_cache": len(self.demo_cache) if self.demo_cache else 0,
        }

    def _entities_for(self, chunk_ids: List[str]) -> List[dict]:
        """
        Entidades da ontologia presentes nas passagens citadas (§14).

        Falha silenciosa é deliberada: o campo `entities` enriquece a resposta,
        mas não a sustenta. Se o grafo ainda não foi anotado ou a consulta
        falha, a resposta continua válida com a lista vazia — o que não pode
        acontecer é a anotação derrubar uma resposta bem fundamentada.
        """
        if not chunk_ids:
            return []
        try:
            return self.repository.entities_for_chunks(chunk_ids)
        except Exception as error:  # noqa: BLE001
            log.warning("Não foi possível anexar entidades: %s", error)
            return []

    def _evidence_only_answer(
        self,
        question: str,
        sources,
        top_score: float,
        channels: List[str],
        error: str,
    ) -> EvidenceAnswer:
        """
        Degradação quando o LLM falha, mas a evidência existe.

        Em vez de devolver erro, entrega as passagens encontradas. O usuário
        perde a síntese e mantém o essencial — os trechos e suas fontes.
        """
        return EvidenceAnswer(
            answer=(
                "Não consegui redigir a síntese agora, mas encontrei no corpus as "
                f"passagens abaixo, que são a evidência disponível para a sua pergunta. "
                f"({len(sources)} trechos recuperados.)"
            ),
            sources=sources,
            retrieval=RetrievalTrace(
                query=question,
                chunks_considered=len(sources),
                chunks_used=0,
                top_score=round(top_score, 4),
                evidence_threshold=self.threshold,
                channels=channels,
            ),
            grounded=False,
            warnings=[f"Geração indisponível: {error}"],
        )

if __name__ == "__main__":
    import argparse
    import sys

    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description="Pergunte à Dra. Aris")
    parser.add_argument("question", nargs="+", help="a pergunta")
    parser.add_argument("--top-k", type=int, default=DEFAULT_TOP_K)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )

    from embedder import EmbeddingService
    from graph_manager import build_driver

    driver = build_driver()
    try:
        aris = DraAris(RetrievalRepository(driver), EmbeddingService())
        result = aris.answer(" ".join(args.question), top_k=args.top_k)

        print("=" * 78)
        print(result.answer)
        print("=" * 78)

        if result.cited_sources:
            print("\nFONTES CITADAS\n" + "-" * 78)
            for source in result.cited_sources:
                print(f"[{source.citation_index}] {source.title[:66]}")
                print(f"    {source.journal or '—'} · DOI: {source.doi or 'não disponível'}")
                print(f"    {source.url}")
                print(f"    relevância {source.relevance:.5f} · canais: {', '.join(source.channels)}")
                print(f"    \"{' '.join(source.passage.split())[:150]}...\"\n")

        trace = result.retrieval
        print(
            f"grounded={result.grounded} · "
            f"{trace.chunks_used}/{trace.chunks_considered} passagens citadas · "
            f"melhor similaridade {trace.top_score} (limiar {trace.evidence_threshold})"
        )
        for warning in result.warnings:
            print(f"AVISO: {warning}")
    finally:
        driver.close()
