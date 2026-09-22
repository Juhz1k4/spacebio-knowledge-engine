# -*- coding: utf-8 -*-
"""
SPACEBIO E3-06 — job de enriquecimento bibliográfico

Percorre as publicações do grafo, busca o registro completo no Crossref a
partir do DOI e grava autores, ano, volume, número e páginas nos nós.

POR QUE ISTO É UM JOB E NÃO PARTE DO CAMINHO DE LEITURA
--------------------------------------------------------
Autor e ano de um artigo publicado são imutáveis. Consultá-los a cada
pergunta somaria seis idas à rede por resposta (uma por cartão de fonte) para
obter um dado que nunca muda -- e faria a apresentação depender de o Crossref
estar no ar naquele minuto. Ver o cabeçalho de `crossref.py`.

INCREMENTAL POR PADRÃO
----------------------
Só processa o que ainda não foi tentado ou falhou por rede. Um DOI que o
Crossref não tem fica marcado `not_found` e não é reconsultado: repetir a
requisição em toda execução gastaria chamadas para confirmar uma ausência já
conhecida.

USO
    python enrich_metadata.py                 # só o que falta
    python enrich_metadata.py --all           # reprocessa tudo
    python enrich_metadata.py --limit 10      # amostra, para conferir antes
    python enrich_metadata.py --dry-run       # consulta sem gravar
    python enrich_metadata.py --report        # só a cobertura atual
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from typing import Any, Dict, List, Optional

from config import MissingCredentialError
from crossref import (
    STATUS_ENRICHED,
    STATUS_NO_DOI,
    STATUS_NOT_FOUND,
    CrossrefClient,
    PublicationMetadata,
)
from graph_manager import Neo4jGraphManager

log = logging.getLogger(__name__)

# Gravar de 50 em 50 em vez de tudo no fim. Um job de 488 publicações que
# falhe aos 400 não pode perder o trabalho já feito -- e a rede é justamente
# a parte que falha.
BATCH_SIZE = 50


def _to_row(title: str, metadata: PublicationMetadata) -> Dict[str, Any]:
    """
    Converte o registro do Crossref na linha que o Cypher grava.

    Campos ausentes viram None explicitamente, e não são omitidos: o SET do
    Cypher precisa do valor para limpar um dado antigo quando o registro for
    reprocessado e o campo tiver sumido do Crossref.
    """
    return {
        "title": title,
        "status": metadata.status,
        "authors": metadata.authors,
        "year": metadata.year,
        "volume": metadata.volume,
        "issue": metadata.issue,
        "pages": metadata.pages,
        "publisher": metadata.publisher,
        "crossref_title": metadata.title,
        "container_title": metadata.container_title,
    }


def _print_coverage(graph: Neo4jGraphManager) -> None:
    """Relatório de quantas publicações conseguem virar citação completa."""
    cobertura = graph.metadata_coverage()
    total = cobertura.get("total") or 0
    if not total:
        print("  Nenhuma publicação no grafo.")
        return

    def linha(rotulo: str, valor: Optional[int]) -> None:
        valor = valor or 0
        print(f"    {rotulo:24} {valor:4} / {total}  ({valor / total:5.1%})")

    print("\n  COBERTURA DE METADADOS")
    linha("com DOI", cobertura.get("com_doi"))
    linha("enriquecidas", cobertura.get("enriquecidas"))
    linha("com autores", cobertura.get("com_autores"))
    linha("com ano", cobertura.get("com_ano"))


def run(limit: Optional[int], dry_run: bool, refetch_all: bool) -> int:
    client = CrossrefClient()

    # O Neo4jGraphManager abre e fecha o proprio driver como context manager,
    # igual aos demais jobs do projeto (ingest_corpus, extract_entities).
    with Neo4jGraphManager() as graph:
        pendentes = graph.fetch_publication_dois(only_missing=not refetch_all)
        if limit:
            pendentes = pendentes[:limit]

        if not pendentes:
            print("Nada a fazer: todas as publicações já foram processadas.")
            _print_coverage(graph)
            return 0

        print(f"Enriquecendo {len(pendentes)} publicação(ões) via Crossref...")
        if dry_run:
            print("  (dry-run: nada será gravado)\n")

        contagem = {STATUS_ENRICHED: 0, STATUS_NOT_FOUND: 0, STATUS_NO_DOI: 0, "error": 0}
        lote: List[Dict[str, Any]] = []
        gravadas = 0
        inicio = time.time()

        for posicao, publicacao in enumerate(pendentes, start=1):
            titulo = publicacao["title"]
            doi = publicacao.get("doi")

            if not doi:
                # As 5 publicações sem DOI. Marcadas para que a próxima
                # execução não as reconsidere.
                metadata = PublicationMetadata(doi="", status=STATUS_NO_DOI)
            else:
                metadata = client.fetch(doi)

            contagem[metadata.status] = contagem.get(metadata.status, 0) + 1
            lote.append(_to_row(titulo, metadata))

            if posicao % 25 == 0 or posicao == len(pendentes):
                decorrido = time.time() - inicio
                print(
                    f"  [{posicao:>4}/{len(pendentes)}] "
                    f"{contagem[STATUS_ENRICHED]:>4} enriquecidas · "
                    f"{contagem[STATUS_NOT_FOUND]:>3} sem registro · "
                    f"{contagem['error']:>3} erro · "
                    f"{client.requests_made:>4} req · {decorrido:>5.0f}s"
                )

            if not dry_run and len(lote) >= BATCH_SIZE:
                gravadas += graph.write_publication_metadata(lote)
                client.cache.save()
                lote = []

        if not dry_run and lote:
            gravadas += graph.write_publication_metadata(lote)

        client.cache.save()

        print(f"\n  Requisições ao Crossref  {client.requests_made}")
        print(f"  Servidas do cache        {client.cache.hits}")
        print(f"  Publicações gravadas     {gravadas}")
        print(f"  Tempo                    {time.time() - inicio:.0f}s")

        if contagem[STATUS_NOT_FOUND] or contagem[STATUS_NO_DOI]:
            print(
                f"\n  {contagem[STATUS_NOT_FOUND]} sem registro no Crossref e "
                f"{contagem[STATUS_NO_DOI]} sem DOI."
            )
            print("  Essas citações caem na forma simplificada (título + link).")

        if not dry_run:
            _print_coverage(graph)
        return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, help="processar no máximo N publicações")
    parser.add_argument("--dry-run", action="store_true", help="consultar sem gravar")
    parser.add_argument(
        "--all",
        dest="refetch_all",
        action="store_true",
        help="reprocessar tudo, inclusive o que já foi enriquecido",
    )
    parser.add_argument(
        "--report", action="store_true", help="apenas imprimir a cobertura atual"
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")

    try:
        if args.report:
            with Neo4jGraphManager() as graph:
                _print_coverage(graph)
            return 0

        return run(args.limit, args.dry_run, args.refetch_all)

    except MissingCredentialError as error:
        print(f"\nCONFIGURAÇÃO AUSENTE:\n{error}")
        return 2
    except KeyboardInterrupt:
        # O job grava em lotes, então interromper perde no máximo os últimos
        # BATCH_SIZE registros -- e reexecutar retoma de onde parou.
        print("\nInterrompido. Rode de novo para continuar de onde parou.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
