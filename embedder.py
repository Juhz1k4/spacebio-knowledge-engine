"""
SPACEBIO-010 — Embedding Pipeline

Gera embeddings densos para ChunkRecords usando sentence-transformers.

Baseline (Master Briefing §11):
    sentence-transformers/all-MiniLM-L6-v2 — 384 dimensões

Decisões de implementação:

1. NORMALIZAÇÃO L2 (normalize_embeddings=True)
   O índice do Neo4j usa similaridade 'cosine'. Vetores normalizados tornam
   cosseno == produto interno e mantêm os scores estáveis entre diferentes
   backends de busca. Custo zero, previne divergência silenciosa.

2. DIMENSÃO VERIFICADA CONTRA O MODELO
   A dimensão não é assumida: é lida do modelo carregado e comparada com o
   valor esperado (384). Um modelo diferente que produza outra dimensão
   quebraria o índice vetorial do Neo4j de forma silenciosa — aqui isso vira
   um erro explícito (EmbeddingDimensionError).

3. TOKEN COUNT REAL
   O chunker (SPACEBIO-009) usa contagem de palavras como proxy e deixou o
   TODO de substituir por tokenização real nesta issue. Aqui recontamos com o
   tokenizer do próprio modelo, que é o número que realmente importa para
   truncamento e custo.

4. DETECÇÃO DE TRUNCAMENTO
   all-MiniLM-L6-v2 processa no máximo 256 word-pieces. Texto além disso é
   descartado SILENCIOSAMENTE pelo modelo — ou seja, evidência científica que
   o RAG nunca conseguiria recuperar. O serviço reporta quantos chunks foram
   truncados para que a configuração do chunker possa ser ajustada.

Uso:
    from chunker import DocumentChunker
    from embedder import EmbeddingService

    chunks = DocumentChunker().chunk_publication(pub)

    service = EmbeddingService()                  # CPU por padrão
    embedded = service.embed_chunks(chunks)       # ChunkRecords com embedding
    print(service.last_report)                    # estatísticas do lote

Nota de roadmap: na Fase 2 este baseline deve ser comparado com embeddings
científicos (SciBERT, PubMedBERT, BGE) — ver ADR-003.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Iterable, List, Sequence

from config import settings
from schema import ChunkRecord

if TYPE_CHECKING:  # evita importar torch/transformers só para type hints
    from sentence_transformers import SentenceTransformer

log = logging.getLogger(__name__)

# Constantes públicas (importadas por graph_manager.py e test_pipeline.py)
EMBEDDING_MODEL = settings.embedding_model
EMBEDDING_DIMENSION = settings.embedding_dimension


class EmbeddingDimensionError(RuntimeError):
    """Modelo produziu embeddings com dimensão incompatível com o índice vetorial."""


def _model_dimension(model: "SentenceTransformer") -> int:
    """
    Lê a dimensão do modelo de forma compatível entre versões.

    sentence-transformers 6.x renomeou `get_sentence_embedding_dimension`
    para `get_embedding_dimension`; o nome antigo ainda funciona, mas emite
    FutureWarning.
    """
    getter = getattr(model, "get_embedding_dimension", None)
    if getter is None:
        getter = model.get_sentence_embedding_dimension
    return int(getter())


@dataclass
class EmbeddingReport:
    """Estatísticas do último lote processado."""

    chunks_total: int = 0
    chunks_embedded: int = 0
    chunks_skipped: int = 0
    chunks_truncated: int = 0
    max_tokens_seen: int = 0
    model_max_tokens: int = 0

    def __str__(self) -> str:
        truncation_pct = (
            100.0 * self.chunks_truncated / self.chunks_embedded if self.chunks_embedded else 0.0
        )
        return (
            f"chunks={self.chunks_embedded}/{self.chunks_total} "
            f"(ignorados={self.chunks_skipped}) "
            f"truncados={self.chunks_truncated} ({truncation_pct:.1f}%) "
            f"max_tokens={self.max_tokens_seen}/{self.model_max_tokens}"
        )


class EmbeddingService:
    """
    Serviço de embeddings para o pipeline de Evidence RAG.

    O modelo é carregado sob demanda (lazy loading) na primeira chamada,
    para que importar este módulo continue barato em processos que só
    precisam das constantes (ex.: criação do índice no Neo4j).
    """

    def __init__(
        self,
        model_name: str = EMBEDDING_MODEL,
        device: str = settings.embedding_device,
        expected_dimension: int = EMBEDDING_DIMENSION,
        normalize: bool = True,
        recount_tokens: bool = True,
    ):
        """
        Args:
            model_name: modelo sentence-transformers (default: all-MiniLM-L6-v2).
            device: "cpu", "cuda" ou "mps".
            expected_dimension: dimensão exigida pelo índice vetorial do Neo4j.
            normalize: aplica normalização L2 nos vetores (recomendado p/ cosine).
            recount_tokens: substitui o token_count aproximado do chunker pela
                contagem real do tokenizer do modelo.
        """
        self.model_name = model_name
        self.device = device
        self.expected_dimension = expected_dimension
        self.normalize = normalize
        self.recount_tokens = recount_tokens

        self._model: "SentenceTransformer | None" = None
        self.last_report = EmbeddingReport()

    # ------------------------------------------------------------------ #
    # Ciclo de vida do modelo
    # ------------------------------------------------------------------ #

    @property
    def model(self) -> "SentenceTransformer":
        """Retorna o modelo carregado, carregando-o na primeira chamada."""
        if self._model is None:
            self._load_model()
        return self._model

    def _load_model(self) -> None:
        # Import local: sentence-transformers puxa torch (~segundos de import).
        from sentence_transformers import SentenceTransformer

        log.info("Carregando modelo de embeddings '%s' em %s...", self.model_name, self.device)
        self._model = SentenceTransformer(self.model_name, device=self.device)

        actual = _model_dimension(self._model)
        if actual != self.expected_dimension:
            raise EmbeddingDimensionError(
                f"O modelo '{self.model_name}' produz embeddings de {actual} dimensões, "
                f"mas o pipeline (e o índice vetorial do Neo4j) espera {self.expected_dimension}.\n"
                f"Ajuste EMBEDDING_MODEL/EMBEDDING_DIMENSION no .env e recrie o índice."
            )

        log.info(
            "Modelo carregado (dim=%d, max_seq_length=%d tokens).",
            actual,
            self.max_tokens,
        )

    @property
    def dimension(self) -> int:
        """Dimensão real dos embeddings produzidos pelo modelo."""
        return _model_dimension(self.model)

    @property
    def max_tokens(self) -> int:
        """Máximo de tokens processados por texto (o excedente é truncado)."""
        return int(self.model.max_seq_length)

    # ------------------------------------------------------------------ #
    # Embedding
    # ------------------------------------------------------------------ #

    def count_tokens(self, text: str) -> int:
        """
        Conta tokens reais do texto usando o tokenizer do modelo.

        truncation=False e verbose=False são deliberados: queremos o
        comprimento REAL (para detectar truncamento) sem o aviso que o
        transformers emite ao ver sequências acima do limite do modelo.
        """
        encoded = self.model.tokenizer(
            text, add_special_tokens=True, truncation=False, verbose=False
        )
        return len(encoded["input_ids"])

    def embed_texts(
        self,
        texts: Sequence[str],
        batch_size: int = settings.embedding_batch_size,
        show_progress: bool = False,
    ) -> List[List[float]]:
        """
        Gera embeddings para uma lista de textos, preservando a ordem.

        Returns:
            Lista de vetores (listas de float, prontas para o driver Neo4j).
        """
        if not texts:
            return []

        vectors = self.model.encode(
            list(texts),
            batch_size=batch_size,
            convert_to_numpy=True,
            normalize_embeddings=self.normalize,
            show_progress_bar=show_progress,
        )
        return [vector.tolist() for vector in vectors]

    def embed_query(self, text: str) -> List[float]:
        """
        Gera o embedding de uma pergunta do usuário (lado da recuperação).

        Usado pela busca vetorial (SPACEBIO-012/013). Precisa usar exatamente
        o mesmo modelo e a mesma normalização dos chunks indexados.
        """
        if not text or not text.strip():
            raise ValueError("Não é possível gerar embedding de uma query vazia.")
        return self.embed_texts([text])[0]

    def embed_chunks(
        self,
        chunks: Iterable[ChunkRecord],
        batch_size: int = settings.embedding_batch_size,
        show_progress: bool = False,
    ) -> List[ChunkRecord]:
        """
        Preenche o campo `embedding` de uma coleção de ChunkRecords.

        ChunkRecord é imutável (frozen=True), então retornamos novas instâncias
        em vez de mutar as originais.

        Chunks com texto vazio/em branco são ignorados (não vão para o índice)
        e contabilizados em `last_report.chunks_skipped`.

        Returns:
            Nova lista de ChunkRecords com embedding preenchido, na mesma ordem.
        """
        chunks = list(chunks)
        report = EmbeddingReport(chunks_total=len(chunks), model_max_tokens=0)

        if not chunks:
            self.last_report = report
            return []

        # Força o carregamento antes de medir, para report.model_max_tokens
        report.model_max_tokens = self.max_tokens

        valid = [chunk for chunk in chunks if chunk.text and chunk.text.strip()]
        report.chunks_skipped = len(chunks) - len(valid)
        if report.chunks_skipped:
            log.warning("%d chunk(s) sem texto foram ignorados.", report.chunks_skipped)

        if not valid:
            self.last_report = report
            return []

        log.info("Gerando embeddings para %d chunks (batch=%d)...", len(valid), batch_size)
        vectors = self.embed_texts(
            [chunk.text for chunk in valid], batch_size=batch_size, show_progress=show_progress
        )

        embedded: List[ChunkRecord] = []
        for chunk, vector in zip(valid, vectors):
            if len(vector) != self.expected_dimension:
                raise EmbeddingDimensionError(
                    f"Chunk {chunk.id}: embedding com {len(vector)} dimensões "
                    f"(esperado {self.expected_dimension})."
                )

            update = {"embedding": vector}

            if self.recount_tokens:
                token_count = self.count_tokens(chunk.text)
                update["token_count"] = token_count
                report.max_tokens_seen = max(report.max_tokens_seen, token_count)
                if token_count > report.model_max_tokens:
                    report.chunks_truncated += 1

            embedded.append(chunk.model_copy(update=update))

        report.chunks_embedded = len(embedded)
        self.last_report = report

        if report.chunks_truncated:
            log.warning(
                "%d de %d chunks excedem %d tokens e serão TRUNCADOS pelo modelo — "
                "o texto excedente não fica recuperável. Considere reduzir chunk_size.",
                report.chunks_truncated,
                report.chunks_embedded,
                report.model_max_tokens,
            )

        log.info("Embeddings concluídos: %s", report)
        return embedded

    # Alias mantido para compatibilidade com código que já chamava este nome.
    embed_chunks_batch = embed_chunks

    def embed_chunk(self, chunk: ChunkRecord) -> ChunkRecord:
        """Versão de conveniência para um único chunk (prefira embed_chunks)."""
        result = self.embed_chunks([chunk])
        if not result:
            raise ValueError(f"Chunk {chunk.id} não pôde ser embeddado (texto vazio?).")
        return result[0]


def embed_all_chunks(
    chunks: Iterable[ChunkRecord],
    model_name: str = EMBEDDING_MODEL,
    device: str = settings.embedding_device,
    batch_size: int = settings.embedding_batch_size,
) -> List[ChunkRecord]:
    """
    Atalho funcional: cria um serviço e embedda a lista inteira.

    Para múltiplas chamadas, prefira instanciar um único EmbeddingService e
    reutilizá-lo — assim o modelo é carregado apenas uma vez.
    """
    service = EmbeddingService(model_name=model_name, device=device)
    return service.embed_chunks(chunks, batch_size=batch_size, show_progress=True)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    print("=" * 70)
    print("SPACEBIO-010 — Embedding Pipeline (teste rápido, sem Neo4j)")
    print("=" * 70)

    demo_chunks = [
        ChunkRecord(
            id="demo_0",
            publication_id="demo_pub",
            text="Microgravity induces pelvic bone loss through osteoclastic activity.",
            section="abstract",
            position=0,
            token_count=10,
        ),
        ChunkRecord(
            id="demo_1",
            publication_id="demo_pub",
            text="Mice were selected and trained for the Bion-M 1 space mission.",
            section="methods",
            position=100,
            token_count=12,
        ),
    ]

    service = EmbeddingService()
    embedded = service.embed_chunks(demo_chunks)

    print(f"\nModelo: {service.model_name}")
    print(f"Dimensão: {service.dimension} (esperado {EMBEDDING_DIMENSION})")
    print(f"Limite de tokens: {service.max_tokens}")
    print(f"Relatório: {service.last_report}\n")

    for chunk in embedded:
        print(f"  {chunk.id}: dim={len(chunk.embedding)} tokens={chunk.token_count}")
        print(f"    primeiros valores: {[round(v, 4) for v in chunk.embedding[:5]]}")

    # Sanidade: com normalização L2, o produto interno de um vetor com ele
    # mesmo é 1.0 e chunks diferentes têm similaridade < 1.0.
    a, b = embedded[0].embedding, embedded[1].embedding
    self_sim = sum(x * x for x in a)
    cross_sim = sum(x * y for x, y in zip(a, b))
    print(f"\n  cos(a,a) = {self_sim:.4f} (esperado ~1.0)")
    print(f"  cos(a,b) = {cross_sim:.4f} (esperado < 1.0)")
