"""
Testes da SPACEBIO-023 — Incremental Graph Build

Verifica que reexecutar a ingestão e a extração:
  1. não refaz trabalho quando nada mudou;
  2. reprocessa exatamente o que mudou;
  3. remove chunks órfãos quando o texto de uma publicação muda;
  4. reanota quando a versão da ontologia muda;
  5. nunca apaga o grafo inteiro como efeito colateral.

O ponto 3 é o que separa build incremental de build preguiçoso: se o texto
muda, os chunk IDs mudam, e sem poda os chunks antigos permaneceriam no índice
vetorial como evidência de um texto que não existe mais — a Dra. Aris poderia
citá-los.

Pré-requisitos: Neo4j no ar e corpus ingerido.

Uso:
    python test_incremental.py
"""

from __future__ import annotations

import logging
import sys
from typing import List, Optional

from config import MissingCredentialError, settings
from graph_manager import Neo4jGraphManager
from ingest_corpus import content_fingerprint, load_clean_corpus
from ontology import ONTOLOGY_VERSION

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

log = logging.getLogger("test_incremental")

# Chunk sintético usado nos testes de escrita. O prefixo evita qualquer
# colisão com IDs reais do corpus, e permite limpar tudo ao final.
TEST_PREFIX = "__spacebio_test__"


class IncrementalTestFailure(AssertionError):
    """Falha de asserção nos testes de build incremental."""


def check(condition: bool, message: str) -> None:
    if condition:
        print(f"      [OK] {message}")
    else:
        print(f"      [FALHA] {message}")
        raise IncrementalTestFailure(message)


def section(step: str, title: str) -> None:
    print(f"\n[{step}] {title}")


# ---------------------------------------------------------------------- #


def test_fingerprint() -> None:
    """A impressão digital é estável e sensível a qualquer mudança."""
    text = "Mice exposed to microgravity showed bone loss."

    check(
        content_fingerprint(text) == content_fingerprint(text),
        "mesmo texto -> mesma impressão digital",
    )
    check(
        content_fingerprint(text) != content_fingerprint(text + " "),
        "um espaço a mais já muda a impressão digital",
    )
    check(
        content_fingerprint(text) != content_fingerprint(text.replace("Mice", "Rats")),
        "texto diferente -> impressão diferente",
    )
    check(len(content_fingerprint(text)) == 64, "sha256 em hexadecimal")
    check(content_fingerprint("") == content_fingerprint(""), "texto vazio é determinístico")


def test_publication_state(graph: Neo4jGraphManager) -> None:
    """O estado do grafo reflete o que foi ingerido."""
    state = graph.publication_state()
    check(len(state) > 0, f"{len(state)} publicações no grafo")

    with_hash = [s for s in state.values() if s["content_hash"]]
    check(
        len(with_hash) > 0,
        f"{len(with_hash)}/{len(state)} publicações têm content_hash gravado",
    )
    check(
        all(s["chunks"] >= 0 for s in state.values()),
        "toda publicação reporta contagem de chunks",
    )

    populated = [s for s in state.values() if s["chunks"] > 0]
    check(len(populated) > 0, f"{len(populated)} publicações com chunks")


def test_skip_unchanged(graph: Neo4jGraphManager) -> None:
    """Publicações inalteradas são identificadas sem tocar em embeddings."""
    state = graph.publication_state()
    publications = load_clean_corpus()

    unchanged = 0
    changed = 0
    missing_hash = 0

    for publication in publications:
        known = state.get(publication.title)
        if not known:
            continue
        if not known["content_hash"]:
            missing_hash += 1
            continue

        path = settings.resolve_corpus_path(
            publication.clean_path or publication.local_path
        )
        if not path.exists():
            continue

        fingerprint = content_fingerprint(path.read_text(encoding="utf-8"))
        if fingerprint == known["content_hash"] and known["chunks"] > 0:
            unchanged += 1
        else:
            changed += 1

    total = unchanged + changed + missing_hash
    print(f"      inalteradas={unchanged} mudadas={changed} sem hash={missing_hash}")
    check(total > 0, f"{total} publicações avaliadas")
    check(
        unchanged > 0 or missing_hash > 0,
        "o corpus estável é reconhecido como tal (ou aguarda o primeiro hash)",
    )


def test_prune_orphans(graph: Neo4jGraphManager) -> None:
    """Chunks que saíram do texto são removidos, e só eles."""
    from schema import ChunkRecord, PublicationRecord

    title = f"{TEST_PREFIX} publicação de teste"
    publication = PublicationRecord(
        title=title,
        source_url="https://example.invalid/test",
        local_path="data/processed_text/__test__.txt",
        extraction_method="TEST",
    )

    def make_chunk(index: int) -> ChunkRecord:
        return ChunkRecord(
            id=f"{TEST_PREFIX}_chunk_{index}",
            publication_id=title,
            text=f"Texto de teste número {index} sobre microgravidade.",
            embedding=[0.01] * settings.embedding_dimension,
            section="unknown",
            position=index * 100,
            token_count=8,
        )

    try:
        # Primeira versão: 3 chunks.
        first = [make_chunk(i) for i in range(3)]
        graph.write_publication_with_chunks(publication, first, content_hash="hash-v1")
        check(graph.count_chunks(title) == 3, "3 chunks gravados na primeira versão")

        # Reescrita idêntica não deve remover nada.
        graph.write_publication_with_chunks(
            publication, first, content_hash="hash-v1", prune_orphans=True
        )
        check(graph.count_chunks(title) == 3, "reescrita idêntica preserva os 3 chunks")

        # Segunda versão: o texto mudou e só restam 2 chunks.
        second = [make_chunk(i) for i in (0, 1)]
        result = graph.write_publication_with_chunks(
            publication, second, content_hash="hash-v2", prune_orphans=True
        )
        check(result["chunks_pruned"] == 1, "1 chunk órfão removido")
        check(graph.count_chunks(title) == 2, "restam exatamente 2 chunks")

        remaining = graph._run(
            "MATCH (:Publication {title: $t})-[:HAS_CHUNK]->(c:Chunk) RETURN c.id AS id",
            t=title,
        ) if hasattr(graph, "_run") else None
        if remaining is not None:
            ids = {row["id"] for row in remaining}
            check(
                f"{TEST_PREFIX}_chunk_2" not in ids,
                "o chunk removido foi o que saiu do texto",
            )

    finally:
        # `with` não é opcional aqui: uma sessão aberta e nunca fechada segura
        # uma conexão do pool até o driver morrer.
        with graph._session() as session:
            session.run(
                "MATCH (p:Publication {title: $t}) "
                "OPTIONAL MATCH (p)-[:HAS_CHUNK]->(c:Chunk) "
                "DETACH DELETE p, c",
                t=title,
            ).consume()

    check(graph.count_chunks(title) == 0, "dados de teste removidos ao final")


def test_ner_versioning(graph: Neo4jGraphManager) -> None:
    """A versão da ontologia controla o que precisa ser reanotado."""
    pending = graph.count_chunks_pending_ner(ONTOLOGY_VERSION)
    total = graph.count_chunks()

    print(f"      ontologia {ONTOLOGY_VERSION}: {total - pending:,} anotados, {pending:,} pendentes")
    check(pending >= 0, "contagem de pendentes é válida")
    check(pending <= total, "pendentes nunca excede o total de chunks")

    # Uma versão que nunca existiu deixa tudo pendente — é o mecanismo que
    # força a reanotação quando o dicionário muda.
    all_pending = graph.count_chunks_pending_ner("versao-inexistente-9.9.9")
    check(
        all_pending == total,
        f"versão nova marca todos os {total:,} chunks como pendentes",
    )

    page = graph.fetch_chunks_pending_ner("versao-inexistente-9.9.9", 5)
    check(len(page) == min(5, total), f"paginação devolve {len(page)} chunks")
    check(all("id" in row and "text" in row for row in page), "página traz id e texto")


def test_no_destructive_side_effects(graph: Neo4jGraphManager) -> None:
    """Nenhuma operação incremental apaga o grafo."""
    before_chunks = graph.count_chunks()
    before_pubs = graph._run("MATCH (p:Publication) RETURN count(p) AS n")[0]["n"]

    # Operações de leitura do caminho incremental.
    graph.publication_state()
    graph.count_chunks_pending_ner(ONTOLOGY_VERSION)
    graph.fetch_chunks_pending_ner(ONTOLOGY_VERSION, 10)
    graph.delete_orphan_entities()

    check(graph.count_chunks() == before_chunks, f"chunks preservados ({before_chunks:,})")
    check(
        graph._run("MATCH (p:Publication) RETURN count(p) AS n")[0]["n"] == before_pubs,
        f"publicações preservadas ({before_pubs})",
    )


# ---------------------------------------------------------------------- #


def run() -> int:
    print("=" * 78)
    print("SPACEBIO-023 — Build incremental")
    print("=" * 78)

    section("1/6", "Impressão digital de conteúdo (unitário)")
    test_fingerprint()

    with Neo4jGraphManager() as graph:
        # `_run` é do RetrievalRepository; aqui damos ao manager um atalho
        # equivalente para as verificações.
        def _run(cypher: str, **params):
            with graph._session() as session:
                return [record.data() for record in session.run(cypher, **params)]

        graph._run = _run  # type: ignore[attr-defined]

        section("2/6", "Estado das publicações no grafo")
        test_publication_state(graph)

        section("3/6", "Detecção de publicações inalteradas")
        test_skip_unchanged(graph)

        section("4/6", "Poda de chunks órfãos")
        test_prune_orphans(graph)

        section("5/6", "Versionamento da anotação")
        test_ner_versioning(graph)

        section("6/6", "Ausência de efeitos destrutivos")
        test_no_destructive_side_effects(graph)

    print("\n" + "=" * 78)
    print("SPACEBIO-023 — todas as asserções passaram.")
    print("=" * 78)
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Testes do build incremental")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )

    try:
        return run()
    except IncrementalTestFailure as failure:
        print(f"\nTESTE REPROVADO: {failure}")
        return 1
    except MissingCredentialError as error:
        print(f"\nCONFIGURAÇÃO AUSENTE:\n{error}")
        return 2
    except Exception as error:  # noqa: BLE001
        print(f"\nERRO INESPERADO: {type(error).__name__}: {error}")
        import traceback

        traceback.print_exc()
        return 3


if __name__ == "__main__":
    sys.exit(main())
