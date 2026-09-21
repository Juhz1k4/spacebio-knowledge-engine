"""
SPACEBIO-017/019/022 — Extração de entidades sobre o corpus indexado

Lê os chunks já gravados no Neo4j, extrai entidades com o extrator
determinístico e grava (:Chunk)-[:MENTIONS]->(:Entity), depois derivando as
relações tipadas do §17 por agregação.

INCREMENTAL POR PADRÃO (SPACEBIO-023)
--------------------------------------
Cada chunk anotado guarda a versão da ontologia usada (`c.ner_version`). A
execução processa apenas o que está pendente:

    chunk novo                          -> anotado
    ontologia mudou (ONTOLOGY_VERSION)  -> reanotado
    nada mudou                          -> nenhum trabalho

A gravação é por página, então uma interrupção não perde o que já foi feito e
a próxima execução retoma de onde parou. `--reset` continua disponível para
apagar tudo, mas deixou de ser necessário no uso normal.

Lê os chunks do GRAFO, não dos arquivos: o texto indexado é o que a Dra. Aris
recupera, e é sobre ele que o entity overlap precisa ser calculado. Extrair do
arquivo criaria a possibilidade de divergência entre o que foi indexado e o
que foi anotado.

Uso:
    python extract_entities.py --reset     # limpa entidades e reextrai
    python extract_entities.py --limit 500 # amostra
    python extract_entities.py --dry-run   # mede sem escrever
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from collections import Counter
from typing import Dict, List, Optional

from config import MissingCredentialError
from graph_manager import Neo4jGraphManager
from ner import EntityExtractor
from ontology import (
    DERIVED_RELATION_MIN_MENTIONS,
    ONTOLOGY_VERSION,
    ontology_summary,
)

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

log = logging.getLogger("extract_entities")

CHUNK_PAGE_SIZE = 2000


def entity_key(canonical: str, label: str) -> str:
    """Chave única do nó de entidade."""
    return f"{label}:{canonical}"


def run(reset: bool, limit: Optional[int], dry_run: bool) -> int:
    started = time.time()
    extractor = EntityExtractor()

    print("=" * 78)
    print("SPACEBIO-017 — Extração de entidades" + ("  [DRY-RUN]" if dry_run else ""))
    print("=" * 78)

    summary = ontology_summary()
    print(f"\nOntologia {ONTOLOGY_VERSION}:")
    for label, count in summary.items():
        print(f"   {label:<24}{count:>4} entidades canônicas no dicionário")

    with Neo4jGraphManager() as graph:
        total_chunks = graph.count_chunks()

        if reset and not dry_run:
            removed = graph.clear_entities()
            print(f"\nReset: {removed} entidades removidas")

        if not dry_run:
            constraints = graph.create_constraints()
            for name, status in constraints.items():
                if status != "ok":
                    print(f"   [AVISO] constraint {name}: {status}")

        # SPACEBIO-023: só os chunks que ainda não foram anotados por ESTA
        # versão da ontologia. Trocar o stoplist e reexecutar reprocessa tudo;
        # reexecutar sem mudar nada não faz trabalho nenhum.
        pending = graph.count_chunks_pending_ner(ONTOLOGY_VERSION)
        target = min(pending, limit) if limit else pending

        print(f"\nChunks no grafo:  {total_chunks:,}")
        print(f"Já anotados:      {total_chunks - pending:,} (ontologia {ONTOLOGY_VERSION})")
        print(f"A processar:      {target:,}")

        if target == 0:
            print("\nNada a fazer — o grafo já está anotado nesta versão da ontologia.")
            print("Use --reset para reanotar tudo, ou incremente ONTOLOGY_VERSION.")
            return 0

        entities: Dict[str, Dict] = {}
        mentions: List[Dict] = []
        per_label: Counter = Counter()
        chunks_with_entities = 0
        processed = 0

        print(f"\n{'-' * 78}")
        while processed < target:
            page_size = min(CHUNK_PAGE_SIZE, target - processed)
            page = graph.fetch_chunks_pending_ner(ONTOLOGY_VERSION, page_size)
            if not page:
                break

            page_entities: Dict[str, Dict] = {}
            page_mentions: List[Dict] = []
            page_ids = [chunk["id"] for chunk in page]

            for chunk in page:
                counts = extractor.extract_counts(chunk["text"] or "")
                if counts:
                    chunks_with_entities += 1
                for (canonical, label), count in counts.items():
                    key = entity_key(canonical, label)
                    row = {"key": key, "canonical": canonical, "label": label}
                    page_entities.setdefault(key, row)
                    entities.setdefault(key, row)
                    mention = {"chunk_id": chunk["id"], "entity_key": key, "count": count}
                    page_mentions.append(mention)
                    mentions.append(mention)
                    per_label[label] += count

            # Grava por página: a extração fica retomável, e uma interrupção
            # não descarta o trabalho já feito. Marcar os chunks é o que os
            # tira do conjunto pendente e faz o laço avançar.
            if not dry_run:
                graph.clear_mentions_for_chunks(page_ids)
                if page_entities:
                    graph.write_entities(list(page_entities.values()))
                if page_mentions:
                    graph.write_mentions(page_mentions)
                graph.mark_chunks_annotated(page_ids, ONTOLOGY_VERSION)

            processed += len(page)
            elapsed = time.time() - started
            rate = processed / elapsed if elapsed else 0
            print(
                f"  {processed:>6,}/{target:,} chunks · "
                f"{len(entities):>4} entidades · {len(mentions):>7,} menções · "
                f"{rate:>6.0f} chunks/s"
            )

            if dry_run:  # sem marcação, a mesma página voltaria para sempre
                break

        print(f"{'-' * 78}")

        if not dry_run:
            orphans = graph.delete_orphan_entities()
            if orphans:
                print(f"\nEntidades órfãs removidas: {orphans}")

            print(f"\nDerivando relações tipadas (§17, min={DERIVED_RELATION_MIN_MENTIONS})...")
            derived = graph.derive_publication_relations(DERIVED_RELATION_MIN_MENTIONS)
            for relation, count in derived.items():
                print(f"   (Publication)-[:{relation}]->  {count:,}")

    elapsed = time.time() - started
    print(f"\n{'=' * 78}")
    print("EXTRAÇÃO CONCLUÍDA")
    print("=" * 78)
    print(f"   Chunks processados        {processed:,}")
    print(f"   Chunks com entidade       {chunks_with_entities:,} ({100*chunks_with_entities/max(processed,1):.1f}%)")
    print(f"   Entidades distintas       {len(entities)}")
    print(f"   Relações MENTIONS         {len(mentions):,}  (pares chunk-entidade)")
    print(f"   Ocorrências totais        {sum(per_label.values()):,}")
    print(f"   Tempo                     {elapsed:.0f}s")
    print("\n   OCORRÊNCIAS POR TIPO")
    for label, count in per_label.most_common():
        print(f"     {label:<24}{count:>9,}")
    print("=" * 78)
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Extração de entidades do corpus")
    parser.add_argument("--reset", action="store_true", help="apaga entidades antes")
    parser.add_argument("--limit", type=int, help="processar apenas N chunks")
    parser.add_argument("--dry-run", action="store_true", help="medir sem escrever")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )

    try:
        return run(reset=args.reset, limit=args.limit, dry_run=args.dry_run)
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
