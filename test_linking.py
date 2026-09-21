"""
Testes da SPACEBIO-020 — Entity Linking

Verifica que o linking:
  1. produz pares gene/proteína do MESMO organismo;
  2. usa o cache e não repete perguntas a bases públicas;
  3. é incremental — reexecutar não consulta nada;
  4. distingue "sem correspondência" de "erro de rede";
  5. nunca inventa identificador.

Por padrão NÃO faz chamadas externas: usa o cache já gravado e um cliente
falso. `--live` faz uma consulta real, para confirmar que o contrato das APIs
não mudou.

Uso:
    python test_linking.py
    python test_linking.py --live
"""

from __future__ import annotations

import json
import logging
import sys
from typing import Dict, List, Optional

from config import MissingCredentialError, settings
from entity_linking import (
    CACHE_FILE,
    STATUS_ERROR,
    STATUS_LINKED,
    STATUS_UNRESOLVED,
    ExternalLinker,
    LinkCache,
    LinkResult,
    fetch_pending,
)
from graph_manager import Neo4jGraphManager
from ontology import GENE, ORGANISM

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

log = logging.getLogger("test_linking")


class LinkingTestFailure(AssertionError):
    """Falha de asserção nos testes de entity linking."""


def check(condition: bool, message: str) -> None:
    if condition:
        print(f"      [OK] {message}")
    else:
        print(f"      [FALHA] {message}")
        raise LinkingTestFailure(message)


def section(step: str, title: str) -> None:
    print(f"\n[{step}] {title}")


class CountingLinker(ExternalLinker):
    """Linker que registra chamadas externas em vez de fazê-las."""

    def __init__(self, cache: LinkCache):
        super().__init__(cache)
        self.external_calls: List[str] = []

    def link_gene(self, symbol: str) -> LinkResult:
        self.external_calls.append(f"gene:{symbol}")
        result = LinkResult(key=f"{GENE}:{symbol}", canonical=symbol, label=GENE)
        result.status = STATUS_LINKED
        result.ncbi_gene_id = "999999"
        result.taxonomy_id = "9606"
        result.organism_matched = True
        return result

    def link_organism(self, canonical: str) -> LinkResult:
        self.external_calls.append(f"organism:{canonical}")
        result = LinkResult(key=f"{ORGANISM}:{canonical}", canonical=canonical, label=ORGANISM)
        result.status = STATUS_LINKED
        result.taxonomy_id = "9606"
        return result


# ---------------------------------------------------------------------- #


def test_cache_roundtrip(tmp_path) -> None:
    """O cache preserva os resultados entre execuções."""
    cache_file = tmp_path / "test_cache.json"
    cache = LinkCache(cache_file)
    check(len(cache) == 0, "cache novo começa vazio")

    result = LinkResult(key=f"{GENE}:TP53", canonical="TP53", label=GENE)
    result.status = STATUS_LINKED
    result.ncbi_gene_id = "7157"
    cache.put(result.key, result.__dict__.copy())
    cache.save()

    reloaded = LinkCache(cache_file)
    check(len(reloaded) == 1, "cache persistido e recarregado")
    stored = reloaded.get(f"{GENE}:TP53")
    check(stored is not None, "entrada recuperada por chave")
    check(stored["ncbi_gene_id"] == "7157", "identificador preservado")
    check(LinkResult(**stored).status == STATUS_LINKED, "resultado reconstruído do cache")

    cache_file.unlink()


def test_cache_prevents_external_calls(tmp_path) -> None:
    """Uma entidade em cache não gera nova consulta externa."""
    cache = LinkCache(tmp_path / "counting.json")
    linker = CountingLinker(cache)

    linker.resolve(f"{GENE}:TP53", "TP53", GENE)
    check(len(linker.external_calls) == 1, "primeira resolução consulta a base")

    linker.resolve(f"{GENE}:TP53", "TP53", GENE)
    check(len(linker.external_calls) == 1, "segunda resolução vem do cache, sem consultar")

    linker.resolve(f"{GENE}:BRCA1", "BRCA1", GENE)
    check(len(linker.external_calls) == 2, "entidade nova consulta a base")


def test_unsupported_label(tmp_path) -> None:
    """Tipos sem base externa são tratados sem erro."""
    cache = LinkCache(tmp_path / "unsupported.json")
    linker = CountingLinker(cache)

    result = linker.resolve("Mission:International Space Station", "International Space Station", "Mission")
    check(result.status == STATUS_UNRESOLVED, "Mission fica sem correspondência")
    check(result.ncbi_gene_id is None, "nenhum identificador é inventado")
    check("não tem base externa" in (result.note or ""), f"o motivo é registrado: {result.note}")
    check(len(linker.external_calls) == 0, "nenhuma consulta externa para tipo não suportado")


def test_network_error_not_cached(tmp_path) -> None:
    """Erro de rede não vira resultado permanente."""

    class FailingLinker(ExternalLinker):
        def link_gene(self, symbol: str) -> LinkResult:
            result = LinkResult(key=f"{GENE}:{symbol}", canonical=symbol, label=GENE)
            result.status = STATUS_ERROR
            result.note = "falha de rede"
            return result

    cache = LinkCache(tmp_path / "failing.json")
    linker = FailingLinker(cache)

    result = linker.resolve(f"{GENE}:FAKE1", "FAKE1", GENE)
    check(result.status == STATUS_ERROR, "erro de rede é sinalizado")
    check(
        cache.get(f"{GENE}:FAKE1") is None,
        "erro NÃO vai para o cache — a próxima execução tenta de novo",
    )


def test_graph_links(graph: Neo4jGraphManager) -> None:
    """Os links gravados no grafo são internamente consistentes."""
    rows = graph._run(
        """
        MATCH (e:Entity)
        WHERE e.link_status IS NOT NULL
        RETURN e.canonical AS canonical, e.label AS label, e.link_status AS status,
               e.ncbi_gene_id AS ncbi, e.uniprot_accession AS uniprot,
               e.taxonomy_id AS taxid, e.organism_matched AS organism_matched,
               e.source_organism AS organism, e.protein_name AS protein
        """
    )
    if not rows:
        print("      (nenhuma entidade linkada ainda — rode entity_linking.py)")
        return

    linked = [r for r in rows if r["status"] == STATUS_LINKED]
    unresolved = [r for r in rows if r["status"] == STATUS_UNRESOLVED]
    print(f"      {len(rows)} processadas · {len(linked)} resolvidas · {len(unresolved)} sem correspondência")

    check(
        all(r["status"] in (STATUS_LINKED, STATUS_UNRESOLVED) for r in rows),
        "todo status gravado é válido",
    )
    # Cada tipo resolve numa base diferente: gene em NCBI Gene e/ou UniProt,
    # organismo em NCBI Taxonomy. Exigir gene_id de um organismo reprovaria
    # um link perfeitamente correto — foi o que esta asserção fazia antes.
    def has_identifier(row: Dict) -> bool:
        return bool(row["ncbi"] or row["uniprot"] or row["taxid"])

    check(
        all(has_identifier(r) for r in linked),
        "toda entidade resolvida tem ao menos um identificador externo",
    )
    organisms = [r for r in linked if r["label"] == ORGANISM]
    check(
        all(r["taxid"] for r in organisms),
        f"os {len(organisms)} organismos resolvidos têm taxid do NCBI Taxonomy",
    )
    check(
        all(not has_identifier(r) for r in unresolved),
        "entidade sem correspondência não recebe identificador inventado",
    )

    # A correção que motivou a segunda rodada: gene e proteína do mesmo táxon.
    paired = [r for r in linked if r["ncbi"] and r["uniprot"]]
    if paired:
        matched = [r for r in paired if r["organism_matched"]]
        check(
            len(matched) == len(paired),
            f"os {len(paired)} pares gene/proteína foram restritos ao mesmo organismo",
        )
        check(
            all(r["taxid"] for r in paired),
            "todo par carrega o taxid usado na restrição",
        )
        print(f"      exemplo: {paired[0]['canonical']} -> NCBI {paired[0]['ncbi']} / "
              f"UniProt {paired[0]['uniprot']} [{paired[0]['organism']}]")


def test_incremental_linking(graph: Neo4jGraphManager) -> None:
    """Entidades já linkadas não voltam para a fila."""
    pending = fetch_pending(graph, None)
    linked_count = graph._run(
        "MATCH (e:Entity) WHERE e.link_status IS NOT NULL RETURN count(e) AS n"
    )[0]["n"]

    print(f"      pendentes={len(pending)} já linkadas={linked_count}")
    check(
        all(row["label"] in (GENE, ORGANISM) for row in pending),
        "a fila só traz tipos que têm base externa",
    )

    keys_pending = {row["key"] for row in pending}
    keys_linked = {
        row["key"]
        for row in graph._run(
            "MATCH (e:Entity) WHERE e.link_status IS NOT NULL RETURN e.key AS key"
        )
    }
    check(
        not (keys_pending & keys_linked),
        "nenhuma entidade está simultaneamente pendente e linkada",
    )


def test_live_api() -> None:
    """Uma consulta real, para confirmar que o contrato das APIs não mudou."""
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        cache = LinkCache(Path(tmp) / "live.json")
        linker = ExternalLinker(cache)

        result = linker.link_gene("TP53")
        check(result.status == STATUS_LINKED, "TP53 resolvido")
        check(result.ncbi_gene_id == "7157", f"NCBI Gene ID correto: {result.ncbi_gene_id}")
        check(result.uniprot_accession == "P04637", f"UniProt correto: {result.uniprot_accession}")
        check(result.taxonomy_id == "9606", f"taxid humano: {result.taxonomy_id}")
        check(result.organism_matched, "UniProt foi restrito ao organismo do NCBI")
        print(f"      {result.official_symbol} | {result.protein_name}")

        organism = linker.link_organism("Mus musculus")
        check(organism.status == STATUS_LINKED, "Mus musculus resolvido")
        check(organism.taxonomy_id == "10090", f"taxid correto: {organism.taxonomy_id}")


# ---------------------------------------------------------------------- #


def run(live: bool) -> int:
    import tempfile
    from pathlib import Path

    print("=" * 78)
    print("SPACEBIO-020 — Entity Linking")
    print("=" * 78)

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)

        section("1/6", "Cache em disco (unitário)")
        test_cache_roundtrip(tmp_path)

        section("2/6", "O cache evita consultas repetidas")
        test_cache_prevents_external_calls(tmp_path)

        section("3/6", "Tipos sem base externa")
        test_unsupported_label(tmp_path)

        section("4/6", "Erro de rede não é cacheado")
        test_network_error_not_cached(tmp_path)

    with Neo4jGraphManager() as graph:
        def _run(cypher: str, **params):
            with graph._session() as session:
                return [record.data() for record in session.run(cypher, **params)]

        graph._run = _run  # type: ignore[attr-defined]

        section("5/6", "Consistência dos links no grafo")
        test_graph_links(graph)
        test_incremental_linking(graph)

    section("6/6", "Contrato das APIs externas")
    if live:
        test_live_api()
    else:
        print("      (pulado — use --live para consultar NCBI e UniProt)")

    print("\n" + "=" * 78)
    print("SPACEBIO-020 — todas as asserções passaram.")
    print("=" * 78)
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Testes de entity linking")
    parser.add_argument("--live", action="store_true", help="consultar as APIs reais")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )

    try:
        return run(live=args.live)
    except LinkingTestFailure as failure:
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
