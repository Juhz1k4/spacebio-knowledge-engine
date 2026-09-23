"""
Ingestão do corpus limpo no Neo4j — chunking + embeddings + grafo

Percorre o corpus aprovado (data/metadata_clean.csv), gera chunks, calcula
embeddings e grava (:Publication)-[:HAS_CHUNK]->(:Chunk) com o vetor de 384
dimensões, populando o índice vetorial.

Encadeia as issues já entregues:
    SPACEBIO-009  DocumentChunker (700/140)
    SPACEBIO-010  EmbeddingService (all-MiniLM-L6-v2)
    SPACEBIO-011  índice vetorial + escrita em lote
    SPACEBIO-012.5 corpus limpo e filtrado por qualidade

Características:
- Um único modelo e um único driver para toda a execução.
- Progresso a cada publicação, com estimativa de tempo restante.
- Idempotente: MERGE por chunk_id determinístico, pode ser re-executado.
- Uma publicação que falhe não derruba a ingestão; o erro é registrado.

Uso:
    python ingest_corpus.py --reset      # apaga os Chunks antigos e reingere
    python ingest_corpus.py              # incremental (MERGE por cima)
    python ingest_corpus.py --limit 10   # amostra
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import sys
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import pandas as pd

from config import MissingCredentialError, settings
from schema import PublicationRecord  # noqa: F401  (usado por load_clean_corpus)

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

log = logging.getLogger("ingest_corpus")


@dataclass
class IngestionStats:
    publications_total: int = 0
    publications_ok: int = 0
    publications_skipped: int = 0
    publications_failed: int = 0
    publications_empty: int = 0
    chunks_generated: int = 0
    chunks_written: int = 0
    chunks_pruned: int = 0
    chunks_truncated: int = 0
    seconds: float = 0.0
    failures: List[str] = field(default_factory=list)


def content_fingerprint(text: str, model: Optional[str] = None) -> str:
    """
    Impressão digital de uma publicação, para o build incremental.

    O chunking já produz IDs determinísticos, então comparar chunk a chunk
    detectaria mudanças — mas só depois de chunkar. O hash do documento
    inteiro decide antes disso, e é o que torna a reexecução barata: para uma
    publicação inalterada, o custo cai a uma leitura de arquivo.

    O MODELO ENTRA NO HASH, e isso não é detalhe. Trocar o modelo de
    embeddings não altera uma letra do texto — se o hash olhasse só para o
    texto, a troca de modelo seria silenciosamente ignorada pelo incremental e
    o índice ficaria com vetores de dois modelos misturados, que não são
    comparáveis entre si. Foi exatamente o risco ao migrar de
    all-MiniLM-L6-v2 para multilingual-e5-small.

    Args:
        model: nome do modelo de embeddings. Omitido, usa o configurado.
    """
    material = f"{model or settings.embedding_model}\n{text}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def format_duration(seconds: float) -> str:
    minutes, secs = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h{minutes:02d}m{secs:02d}s"
    if minutes:
        return f"{minutes}m{secs:02d}s"
    return f"{secs}s"


def load_clean_corpus(
    limit: Optional[int] = None,
    title_contains: Optional[str] = None,
) -> List[PublicationRecord]:
    """
    Carrega o metadata do corpus limpo, validando contra o schema.

    `title_contains` permite reingerir publicações específicas — por exemplo
    depois de corrigir um título no metadata, já que o título é a chave do
    nó :Publication.
    """
    metadata_file = settings.data_dir / "metadata_clean.csv"
    if not metadata_file.exists():
        raise FileNotFoundError(
            f"{metadata_file} não existe. Rode `python cleaner.py` antes de ingerir."
        )

    frame = pd.read_csv(metadata_file)
    if title_contains:
        frame = frame[
            frame["title"].astype(str).str.contains(title_contains, case=False, regex=False)
        ]
        if frame.empty:
            raise ValueError(f"Nenhuma publicação com título contendo {title_contains!r}.")
    if limit:
        frame = frame.head(limit)

    records: List[PublicationRecord] = []
    for _, row in frame.iterrows():
        records.append(PublicationRecord(**row.to_dict()))
    return records


def backfill_hashes(graph, publications) -> Dict[str, int]:
    """
    Grava content_hash nas publicações já ingeridas, sem reprocessar nada.

    Migração para o esquema do build incremental. Um grafo povoado antes da
    SPACEBIO-023 tem embeddings corretos e nenhum hash; recalcular tudo só
    para preencher esse campo custaria a ingestão inteira — aqui custa uma
    leitura de arquivo por publicação.

    Só grava onde o número de chunks no grafo bate com o número que o chunking
    produz agora. Se não bater, a publicação mudou desde a ingestão e precisa
    passar pelo caminho normal — marcar como "inalterada" seria mentir para as
    execuções seguintes.
    """
    from chunker import DocumentChunker

    chunker = DocumentChunker()
    state = graph.publication_state()
    stats = {"updated": 0, "skipped": 0, "mismatched": 0, "missing": 0}

    cypher = """
    UNWIND $rows AS row
    MATCH (p:Publication {title: row.title})
    SET p.content_hash = row.content_hash, p.chunk_count = row.chunk_count
    RETURN count(p) AS updated
    """
    rows = []

    for publication in publications:
        known = state.get(publication.title)
        if not known or known["chunks"] == 0:
            stats["missing"] += 1
            continue
        if known["content_hash"]:
            stats["skipped"] += 1
            continue

        path = settings.resolve_corpus_path(
            publication.clean_path or publication.local_path
        )
        if not path.exists():
            stats["missing"] += 1
            continue

        text = path.read_text(encoding="utf-8")
        expected = len(chunker.chunk_text(text, publication_id=publication.title))
        if expected != known["chunks"]:
            stats["mismatched"] += 1
            continue

        rows.append(
            {
                "title": publication.title,
                "content_hash": content_fingerprint(text),
                "chunk_count": expected,
            }
        )

    if rows:
        with graph._session() as session:
            record = session.execute_write(
                lambda tx: tx.run(cypher, rows=rows).single()
            )
        stats["updated"] = record["updated"] if record else 0

    return stats


def reset_chunks(graph) -> int:
    """
    Apaga todos os :Chunk e suas relações.

    Remove apenas Chunks — Publications, entidades e o que o build_graph.py
    tiver criado permanecem. Não é o `MATCH (n) DETACH DELETE n` que o §20
    do briefing manda eliminar.
    """
    cypher = """
    MATCH (c:Chunk)
    WITH c, c.id AS deleted_id
    DETACH DELETE c
    RETURN count(deleted_id) AS deleted
    """
    with graph._session() as session:
        record = session.execute_write(lambda tx: tx.run(cypher).single())
    return record["deleted"] if record else 0


def ingest(
    reset: bool = False,
    limit: Optional[int] = None,
    device: str = "cpu",
    progress_every: int = 10,
    title_contains: Optional[str] = None,
    force: bool = False,
) -> IngestionStats:
    from chunker import DocumentChunker
    from embedder import EmbeddingService
    from graph_manager import Neo4jGraphManager

    stats = IngestionStats()
    started = time.time()

    print("=" * 78)
    print("SpaceBio — Ingestão do corpus limpo")
    print("=" * 78)

    publications = load_clean_corpus(limit=limit, title_contains=title_contains)
    stats.publications_total = len(publications)
    print(f"\nCorpus: {len(publications)} publicações aprovadas")

    chunker = DocumentChunker()
    print(f"Chunking: {chunker.chunk_size} caracteres, overlap {chunker.overlap}")

    # O EmbeddingService carrega o modelo de forma preguiçosa, e é importante
    # não estragar isso: ler `service.dimension` aqui dispararia o
    # carregamento (~15s) mesmo numa execução incremental que não vai
    # embeddar nada. A dimensão é impressa depois, se houver trabalho.
    service = EmbeddingService(device=device)
    print(f"Embeddings: {service.model_name} em {device} (carregado sob demanda)\n")

    with Neo4jGraphManager() as graph:
        print(f"Neo4j {graph.server_version()} · database {graph.database}")

        constraints = graph.create_constraints()
        for name, status in constraints.items():
            if status != "ok":
                print(f"  [AVISO] constraint {name}: {status}")

        index_result = graph.create_vector_index()
        if index_result["status"] != "ok":
            raise RuntimeError(f"Índice vetorial indisponível: {index_result}")
        print(f"Índice vetorial: {graph.index_name} pronto")

        if reset:
            deleted = reset_chunks(graph)
            print(f"Reset: {deleted} chunks antigos removidos")

        # SPACEBIO-023: estado atual do grafo, para decidir o que reprocessar.
        state: Dict[str, Dict] = {} if reset else graph.publication_state()
        if state:
            print(f"Modo incremental: {len(state)} publicações já no grafo")
        if force:
            print("--force: reprocessando tudo, inclusive o inalterado")

        print(f"\n{'-' * 78}")

        for position, publication in enumerate(publications, start=1):
            try:
                source = settings.resolve_corpus_path(
                    publication.clean_path or publication.local_path
                )
                if not source.exists():
                    stats.publications_empty += 1
                    log.warning("Arquivo ausente: %s", publication.title[:60])
                    continue

                text = source.read_text(encoding="utf-8")
                fingerprint = content_fingerprint(text)

                # --- A decisão incremental ---
                # Pula quando o conteúdo não mudou E os chunks estão lá. A
                # segunda condição importa: um hash igual com zero chunks
                # significa que a escrita anterior falhou no meio.
                known = state.get(publication.title)
                if (
                    not force
                    and known
                    and known["content_hash"] == fingerprint
                    and known["chunks"] > 0
                ):
                    stats.publications_skipped += 1
                    continue

                chunks = chunker.chunk_publication(publication)
                if not chunks:
                    stats.publications_empty += 1
                    log.warning("Sem chunks: %s", publication.title[:60])
                    continue

                if stats.publications_ok == 0:
                    print(
                        f"      (modelo: {service.dimension} dims, "
                        f"limite {service.max_tokens} tokens)"
                    )
                embedded = service.embed_chunks(chunks)
                stats.chunks_generated += len(embedded)
                stats.chunks_truncated += service.last_report.chunks_truncated

                # Se a publicação já existia com outro conteúdo, os chunks da
                # versão anterior precisam sair — os IDs mudaram e eles
                # ficariam no índice como evidência de um texto inexistente.
                result = graph.write_publication_with_chunks(
                    publication,
                    embedded,
                    content_hash=fingerprint,
                    prune_orphans=known is not None,
                )
                stats.chunks_written += result["chunks_written"]
                stats.chunks_pruned += result["chunks_pruned"]
                stats.publications_ok += 1

            except Exception as error:  # noqa: BLE001 — uma falha não derruba a ingestão
                stats.publications_failed += 1
                stats.failures.append(f"{publication.title[:50]}: {error}")
                log.error("Falhou em '%s': %s", publication.title[:50], error)

            if position % progress_every == 0 or position == len(publications):
                elapsed = time.time() - started
                rate = position / elapsed if elapsed else 0
                remaining = (len(publications) - position) / rate if rate else 0
                print(
                    f"  {position:>4}/{len(publications)}  "
                    f"novos={stats.publications_ok:>4} inalterados={stats.publications_skipped:>4}  "
                    f"chunks={stats.chunks_written:>6}  "
                    f"{rate:.2f} pub/s  "
                    f"restam ~{format_duration(remaining)}"
                )

        print(f"{'-' * 78}")
        print("\nAguardando o índice vetorial popular...")
        online = graph.await_index_online(timeout_seconds=900)
        print(f"Índice ONLINE: {online}")

        total_chunks = graph.count_chunks()
        stats.seconds = time.time() - started

    print(f"\n{'=' * 78}")
    print("INGESTÃO CONCLUÍDA")
    print("=" * 78)
    print(f"  Publicações processadas   {stats.publications_ok}/{stats.publications_total}")
    if stats.publications_skipped:
        print(f"  Inalteradas (puladas)     {stats.publications_skipped}")
    if stats.chunks_pruned:
        print(f"  Chunks órfãos removidos   {stats.chunks_pruned}")
    if stats.publications_empty:
        print(f"  Sem chunks                {stats.publications_empty}")
    if stats.publications_failed:
        print(f"  Falharam                  {stats.publications_failed}")
    print(f"  Chunks gravados           {stats.chunks_written:,}")
    print(f"  Chunks no grafo (total)   {total_chunks:,}")
    print(f"  Chunks truncados (>256t)  {stats.chunks_truncated:,} "
          f"({100*stats.chunks_truncated/max(stats.chunks_generated,1):.1f}%)")
    print(f"  Tempo                     {format_duration(stats.seconds)}")
    if stats.chunks_written:
        print(f"  Média                     {stats.chunks_written/stats.publications_ok:.0f} chunks/publicação")

    if stats.failures:
        print(f"\n  FALHAS ({len(stats.failures)}):")
        for failure in stats.failures[:10]:
            print(f"    - {failure[:100]}")

    print("=" * 78)
    return stats


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Ingestão do corpus limpo no Neo4j")
    parser.add_argument(
        "--reset",
        action="store_true",
        help="DESTRUTIVO: apaga todos os Chunks antes de ingerir",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="reprocessa publicações inalteradas (sem apagar nada antes)",
    )
    parser.add_argument(
        "--backfill-hashes",
        action="store_true",
        help="grava content_hash em publicações já ingeridas, sem reprocessar",
    )
    parser.add_argument("--limit", type=int, help="processar apenas N publicações")
    parser.add_argument(
        "--title-contains",
        help="reingerir apenas publicações cujo título contenha este trecho",
    )
    parser.add_argument("--device", default=settings.embedding_device, help="cpu | cuda | mps")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.ERROR,
        format="%(levelname)s %(name)s: %(message)s",
    )

    try:
        if args.backfill_hashes:
            from graph_manager import Neo4jGraphManager

            with Neo4jGraphManager() as graph:
                publications = load_clean_corpus(
                    limit=args.limit, title_contains=args.title_contains
                )
                print(f"Backfill de content_hash em {len(publications)} publicações...")
                stats = backfill_hashes(graph, publications)
            print(f"   atualizadas   {stats['updated']}")
            print(f"   já tinham     {stats['skipped']}")
            print(f"   divergentes   {stats['mismatched']}  (chunk_count != grafo; precisam reingerir)")
            print(f"   ausentes      {stats['missing']}")
            return 0

        ingest(
            reset=args.reset,
            limit=args.limit,
            device=args.device,
            title_contains=args.title_contains,
            force=args.force,
        )
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
