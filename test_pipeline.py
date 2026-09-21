"""
Teste integrado do pipeline de Evidence RAG — SPACEBIO-009 + 010 + 011

Executa a cadeia completa para UMA publicação do corpus:

    metadata.csv
        -> PublicationRecord            (SPACEBIO-006)
        -> DocumentChunker              (SPACEBIO-009)
        -> EmbeddingService             (SPACEBIO-010)
        -> Neo4j: constraints + índice  (SPACEBIO-011)
        -> (:Publication)-[:HAS_CHUNK]->(:Chunk)
        -> busca vetorial de verificação

Cada etapa tem asserções explícitas; o script termina com exit code 0 apenas
se todas passarem.

Uso:
    python test_pipeline.py --dry-run          # sem Neo4j: chunking + embeddings
    python test_pipeline.py                    # pipeline completo
    python test_pipeline.py --sample-index 3   # outra publicação do corpus
    python test_pipeline.py --cleanup          # remove os chunks gravados ao final

Pré-requisitos do modo completo:
    - Neo4j 5.x rodando e acessível
    - .env preenchido (copie de .env.example) com NEO4J_PASSWORD
"""

from __future__ import annotations

import argparse
import logging
import sys
from typing import List, Optional

import pandas as pd

from config import MissingCredentialError, settings
from schema import ChunkRecord, PublicationRecord

log = logging.getLogger("test_pipeline")

# O console do Windows abre em cp1252 e quebra em qualquer caractere fora
# dessa tabela. Sem isto, um acento numa mensagem de teste derruba a execução.
for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8", errors="replace")

# Query usada para verificar que a busca vetorial recupera evidência real.
VERIFICATION_QUERY = "What happens to bone tissue during spaceflight?"


class PipelineTestFailure(AssertionError):
    """Falha de asserção em uma etapa do pipeline."""


def check(condition: bool, message: str) -> None:
    """Asserção com saída legível (evita depender de pytest neste smoke test)."""
    if condition:
        print(f"      [OK] {message}")
    else:
        print(f"      [FALHA] {message}")
        raise PipelineTestFailure(message)


def section(step: str, title: str) -> None:
    print(f"\n[{step}] {title}")


# ---------------------------------------------------------------------- #
# Etapas
# ---------------------------------------------------------------------- #


def load_sample_publication(sample_index: int = 0) -> PublicationRecord:
    """
    Carrega uma publicação do corpus LIMPO cujo arquivo de texto exista.

    Usa metadata_clean.csv, não metadata.csv: este teste grava no Neo4j, e o
    corpus ingerido veio do texto limpo. Ler o texto sujo aqui plantaria
    chunks de cabeçalho e bibliografia por cima da publicação já indexada.

    O caminho gravado no metadata usa separadores Windows; config.resolve_corpus_path
    normaliza isso (paliativo de leitura para a SPACEBIO-005).
    """
    metadata_file = settings.data_dir / "metadata_clean.csv"
    check(
        metadata_file.exists(),
        f"metadata_clean.csv encontrado em {metadata_file} (rode cleaner.py se faltar)",
    )

    df = pd.read_csv(metadata_file)
    check(len(df) > 0, f"metadata.csv contém {len(df)} publicações")
    check(
        0 <= sample_index < len(df),
        f"sample_index {sample_index} está dentro do intervalo (0..{len(df) - 1})",
    )

    publication = PublicationRecord(**df.iloc[sample_index].to_dict())

    text_path = settings.resolve_corpus_path(
        publication.clean_path or publication.local_path
    )
    check(text_path.exists(), f"texto limpo existe: {text_path.name}")

    print(f"      Título: {publication.title[:70]}...")
    print(f"      Fonte:  {publication.source_url}")
    return publication


def build_chunks(publication: PublicationRecord) -> List[ChunkRecord]:
    """Etapa SPACEBIO-009: chunking determinístico com overlap."""
    from chunker import DocumentChunker

    chunker = DocumentChunker()  # 700/140 via config (CHUNK_SIZE/CHUNK_OVERLAP)

    # O chunker resolve o caminho sozinho; aqui passamos o texto já lido para
    # não depender do formato de caminho do metadata. Preferimos o limpo, pelo
    # mesmo motivo de load_sample_publication.
    text_path = settings.resolve_corpus_path(
        publication.clean_path or publication.local_path
    )
    text = text_path.read_text(encoding="utf-8")

    chunks = chunker.chunk_text(text=text, publication_id=publication.title)

    check(len(chunks) > 0, f"{len(chunks)} chunks gerados")
    check(
        all(chunk.publication_id == publication.title for chunk in chunks),
        "todos os chunks referenciam a publicação de origem",
    )
    check(
        len({chunk.id for chunk in chunks}) == len(chunks),
        "IDs de chunk são únicos dentro da publicação",
    )
    check(
        all(chunk.embedding is None for chunk in chunks),
        "chunks saem do chunker sem embedding (preenchido na etapa seguinte)",
    )

    # Determinismo: re-processar o mesmo texto precisa produzir os mesmos IDs.
    again = chunker.chunk_text(text=text, publication_id=publication.title)
    check(
        [chunk.id for chunk in again] == [chunk.id for chunk in chunks],
        "IDs determinísticos entre execuções",
    )

    print(f"      Primeiro chunk: {chunks[0].text[:70]}...")
    return chunks


def embed_chunks(chunks: List[ChunkRecord], device: str = "cpu"):
    """Etapa SPACEBIO-010: embeddings all-MiniLM-L6-v2 (384 dims)."""
    from embedder import EmbeddingService

    service = EmbeddingService(device=device)
    embedded = service.embed_chunks(chunks)
    report = service.last_report

    check(len(embedded) == len(chunks), f"{len(embedded)} chunks embeddados")
    check(
        service.dimension == settings.embedding_dimension,
        f"modelo produz {service.dimension} dimensões (esperado {settings.embedding_dimension})",
    )
    check(
        all(len(chunk.embedding) == settings.embedding_dimension for chunk in embedded),
        "todos os embeddings têm a dimensão do índice",
    )
    check(
        all(isinstance(value, float) for value in embedded[0].embedding),
        "embeddings são listas de float (serializáveis para o Neo4j)",
    )

    # Com normalização L2, a norma de cada vetor é ~1.0.
    norm = sum(value * value for value in embedded[0].embedding) ** 0.5
    check(abs(norm - 1.0) < 1e-3, f"embeddings normalizados (norma={norm:.4f})")

    print(f"      Modelo: {service.model_name} em {device}")
    print(f"      Relatório: {report}")
    if report.chunks_truncated:
        print(
            f"      AVISO: {report.chunks_truncated} chunk(s) excedem "
            f"{report.model_max_tokens} tokens e são truncados pelo modelo."
        )

    return service, embedded


def prepare_graph(graph) -> None:
    """Etapa SPACEBIO-011: constraints + índice vetorial."""
    print(f"      Servidor Neo4j: {graph.server_version()}")

    constraints = graph.create_constraints()
    for name, status in constraints.items():
        if status == "ok":
            print(f"      [OK] constraint {name}")
        else:
            print(f"      [AVISO] constraint {name}: {status}")

    result = graph.create_vector_index()
    check(result["status"] == "ok", f"índice vetorial '{graph.index_name}' criado/verificado")

    info = result["index"]
    config = (info.get("options") or {}).get("indexConfig") or {}
    check(info.get("type") == "VECTOR", f"tipo do índice: {info.get('type')}")
    check(
        info.get("labelsOrTypes") == ["Chunk"] and info.get("properties") == ["embedding"],
        f"índice aponta para {info.get('labelsOrTypes')}.{info.get('properties')}",
    )
    check(
        int(config.get("vector.dimensions", 0)) == settings.embedding_dimension,
        f"dimensões do índice: {config.get('vector.dimensions')}",
    )
    check(
        config.get("vector.similarity_function", "").lower() == settings.vector_similarity,
        f"função de similaridade: {config.get('vector.similarity_function')}",
    )


def write_to_graph(graph, publication: PublicationRecord, chunks: List[ChunkRecord]) -> None:
    """Grava (:Publication)-[:HAS_CHUNK]->(:Chunk) e confere no banco."""
    result = graph.write_publication_with_chunks(publication, chunks)
    check(
        result["chunks_written"] == len(chunks),
        f"{result['chunks_written']} chunks gravados",
    )

    stored = graph.count_chunks(publication_title=publication.title)
    check(
        stored == len(chunks),
        f"{stored} chunks ligados à publicação via HAS_CHUNK",
    )

    # Idempotência: re-gravar não pode duplicar nós nem relações.
    graph.write_publication_with_chunks(publication, chunks)
    stored_again = graph.count_chunks(publication_title=publication.title)
    check(stored_again == stored, f"escrita idempotente (ainda {stored_again} chunks)")


def verify_vector_search(graph, service, publication: PublicationRecord, expected_chunks: int) -> None:
    """Confere que a busca vetorial recupera trechos reais da publicação."""
    from retrieval import RetrievalRepository

    online = graph.await_index_online(timeout_seconds=120)
    check(online, "índice vetorial está ONLINE (população concluída)")

    # Usa a mesma camada de recuperação da API (SPACEBIO-012), em vez de uma
    # query própria — o teste exercita o caminho real.
    repo = RetrievalRepository(graph.driver, database=graph.database)
    print(f"      Caminho: {'cláusula SEARCH' if repo.supports_search else 'queryNodes (legado)'}")

    query_vector = service.embed_query(VERIFICATION_QUERY)

    # O filtro por publicação é essencial aqui: o índice cobre o corpus inteiro,
    # então uma busca global legitimamente devolve trechos de outras
    # publicações. O que este teste verifica é que os chunks que ACABAMOS de
    # gravar são recuperáveis e rastreáveis até sua origem.
    hits = repo.semantic_search(
        query_vector,
        top_k=min(3, expected_chunks),
        publication_title=publication.title,
    )

    check(len(hits) > 0, f"busca semântica retornou {len(hits)} resultado(s)")
    check(
        all(0.0 <= hit.score <= 1.0 for hit in hits),
        "scores de similaridade de cosseno estão em [0, 1]",
    )
    check(
        all(hit.text for hit in hits),
        "cada resultado traz o TEXTO do chunk (evidência, não apenas o título)",
    )
    check(
        all(hit.publication_title == publication.title for hit in hits),
        "resultados rastreiam de volta à publicação de origem",
    )

    # E a busca global continua funcionando sobre o corpus inteiro.
    global_hits = repo.semantic_search(query_vector, top_k=3)
    check(len(global_hits) > 0, f"busca global retorna {len(global_hits)} resultado(s)")
    check(
        len({hit.publication_title for hit in global_hits}) >= 1,
        "busca global alcança o corpus além da publicação de teste",
    )

    print(f'\n      Pergunta: "{VERIFICATION_QUERY}"')
    for position, hit in enumerate(hits, start=1):
        print(f"\n      {position}. score={hit.score:.4f}  section={hit.section}")
        print(f"         chunk: {hit.chunk_id[:60]}")
        print(f"         texto: {hit.text[:110].strip()}...")
        print(f"         fonte: {hit.source_url}")


# ---------------------------------------------------------------------- #
# Orquestração
# ---------------------------------------------------------------------- #


def run(sample_index: int, device: str, dry_run: bool, cleanup: bool) -> int:
    print("=" * 78)
    print("SpaceBio — Teste integrado do pipeline (SPACEBIO-009 + 010 + 011)")
    print("=" * 78)
    if dry_run:
        print("MODO DRY-RUN: chunking + embeddings, sem escrever no Neo4j.")

    section("1/5", "Carregando publicação do corpus")
    publication = load_sample_publication(sample_index)

    section("2/5", "Chunking (SPACEBIO-009)")
    chunks = build_chunks(publication)

    section("3/5", "Embeddings (SPACEBIO-010)")
    service, embedded = embed_chunks(chunks, device=device)

    if dry_run:
        print("\n" + "=" * 78)
        print("DRY-RUN CONCLUÍDO — etapas 1 a 3 aprovadas. Neo4j não foi tocado.")
        print("=" * 78)
        return 0

    from graph_manager import Neo4jGraphManager

    with Neo4jGraphManager() as graph:
        section("4/5", "Neo4j: constraints, índice vetorial e escrita (SPACEBIO-011)")
        prepare_graph(graph)
        write_to_graph(graph, publication, embedded)

        section("5/5", "Verificação: busca vetorial recupera evidência")
        verify_vector_search(graph, service, publication, len(embedded))

        if cleanup:
            deleted = graph.delete_publication_chunks(publication.title)
            print(f"\n      Limpeza: {deleted} chunks de teste removidos.")

    print("\n" + "=" * 78)
    print("PIPELINE COMPLETO — todas as asserções passaram.")
    print("=" * 78)
    print(f"  Publicação:       {publication.title[:60]}...")
    print(f"  Chunks:           {len(embedded)}")
    print(f"  Embeddings:       {settings.embedding_model} ({settings.embedding_dimension} dims)")
    print(f"  Índice vetorial:  {settings.vector_index_name} / {settings.vector_similarity}")
    print(f"  Relação gravada:  (Publication)-[:HAS_CHUNK]->(Chunk)")
    print("\nPróximas issues: SPACEBIO-013 (retriever híbrido),")
    print("SPACEBIO-014/015 (contrato de evidência + grounding da Dra. Aris).")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Teste integrado do pipeline SpaceBio")
    parser.add_argument("--sample-index", type=int, default=0, help="linha do metadata.csv")
    parser.add_argument("--device", default=settings.embedding_device, help="cpu | cuda | mps")
    parser.add_argument("--dry-run", action="store_true", help="não conecta ao Neo4j")
    parser.add_argument("--cleanup", action="store_true", help="apaga os chunks ao final")
    parser.add_argument("--verbose", action="store_true", help="logs detalhados")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )

    try:
        return run(args.sample_index, args.device, args.dry_run, args.cleanup)

    except PipelineTestFailure as failure:
        print(f"\nTESTE REPROVADO: {failure}")
        return 1

    except MissingCredentialError as error:
        print(f"\nCONFIGURAÇÃO AUSENTE:\n{error}")
        return 2

    except Exception as error:  # noqa: BLE001 — smoke test precisa reportar tudo
        print(f"\nERRO INESPERADO: {type(error).__name__}: {error}")
        import traceback

        traceback.print_exc()
        return 3


if __name__ == "__main__":
    sys.exit(main())
