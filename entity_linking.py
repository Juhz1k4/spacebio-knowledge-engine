"""
SPACEBIO-020 — Entity Linking contra NCBI e UniProt

Conecta as entidades extraídas a identificadores persistentes de bases
externas, resolvendo o problema que o §19 aponta: NER sozinho não é Knowledge
Graph. Um nó `Gene:TP53` só vira conhecimento quando aponta para
NCBI Gene 7157 e UniProt P04637.

    Organism -> NCBI Taxonomy   (taxid)
    Gene     -> NCBI Gene       (Entrez ID, símbolo oficial, descrição)
             -> UniProt         (accession da proteína, nome recomendado)

O ELO ENTRE GENE E PROTEIN
--------------------------
A ontologia V1 deixou `Protein` de fora justamente por não ser separável de
`Gene` sem uma base externa. O UniProt resolve isso: TP53 (gene) mapeia para
P04637 (Cellular tumor antigen p53, a proteína). O accession fica no nó de
gene como ponte, sem inventar um tipo de nó que a extração não sabe produzir.

O LINKING TAMBÉM É UM FILTRO DE QUALIDADE
------------------------------------------
Um símbolo que não resolve em nenhuma das duas bases provavelmente não é gene.
A auditoria manual da Fase 2 pegou ECM, AMR e EV; esta etapa faz a mesma
triagem de forma sistemática e verificável, marcando `link_status='unresolved'`
sem apagar nada — quem decide o que fazer com eles é uma issue posterior.

RESPEITO ÀS BASES PÚBLICAS
---------------------------
São serviços gratuitos mantidos com dinheiro público. O cliente aqui:
  - respeita o limite de 3 req/s do NCBI (sem API key);
  - identifica-se por User-Agent, como as duas bases pedem;
  - guarda cache em disco, para nunca repetir a mesma pergunta;
  - é incremental, então uma reexecução não consulta nada.

Uso:
    python entity_linking.py --dry-run       # mostra o que seria consultado
    python entity_linking.py --limit 20      # amostra
    python entity_linking.py                 # todas as pendentes
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

from config import MissingCredentialError, settings
from graph_manager import Neo4jGraphManager
from ontology import GENE, ORGANISM

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

log = logging.getLogger("entity_linking")

NCBI_EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
UNIPROT_API = "https://rest.uniprot.org/uniprotkb/search"

# O NCBI permite 3 requisições por segundo sem API key. Ficamos abaixo disso.
NCBI_DELAY = 0.40
UNIPROT_DELAY = 0.20
REQUEST_TIMEOUT = 25

# As duas bases pedem que clientes automatizados se identifiquem.
USER_AGENT = "SpaceBio-KnowledgeEngine/1.0 (NASA Space Apps 2025; entity linking)"

CACHE_FILE = "entity_links.json"

STATUS_LINKED = "linked"
STATUS_UNRESOLVED = "unresolved"
STATUS_ERROR = "error"


@dataclass
class LinkResult:
    """O que as bases externas disseram sobre uma entidade."""

    key: str
    canonical: str
    label: str
    status: str = STATUS_UNRESOLVED

    taxonomy_id: Optional[str] = None
    organism_matched: bool = False
    """
    O UniProt foi consultado restrito ao organismo que o NCBI encontrou?

    Quando False, gene e proteína podem vir de espécies diferentes e o par
    não deve ser tratado como equivalência.
    """
    ncbi_gene_id: Optional[str] = None
    official_symbol: Optional[str] = None
    gene_description: Optional[str] = None
    source_organism: Optional[str] = None
    uniprot_accession: Optional[str] = None
    uniprot_id: Optional[str] = None
    protein_name: Optional[str] = None

    note: Optional[str] = None

    def to_graph_row(self) -> Dict[str, Any]:
        return {
            "key": self.key,
            "link_status": self.status,
            "organism_matched": self.organism_matched,
            "taxonomy_id": self.taxonomy_id,
            "ncbi_gene_id": self.ncbi_gene_id,
            "official_symbol": self.official_symbol,
            "gene_description": self.gene_description,
            "source_organism": self.source_organism,
            "uniprot_accession": self.uniprot_accession,
            "uniprot_id": self.uniprot_id,
            "protein_name": self.protein_name,
        }


class LinkCache:
    """
    Cache em disco das respostas das bases externas.

    Não é otimização acessória: sem ele, cada execução repetiria centenas de
    perguntas idênticas a serviços públicos gratuitos.
    """

    def __init__(self, path: Path):
        self.path = path
        self._data: Dict[str, Dict] = {}
        if path.exists():
            try:
                self._data = json.loads(path.read_text(encoding="utf-8"))
            except Exception as error:  # noqa: BLE001
                log.warning("Cache ilegível (%s); recomeçando vazio.", error)

    def get(self, key: str) -> Optional[Dict]:
        return self._data.get(key)

    def put(self, key: str, value: Dict) -> None:
        self._data[key] = value

    def save(self) -> None:
        self.path.write_text(
            json.dumps(self._data, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def __len__(self) -> int:
        return len(self._data)


class ExternalLinker:
    """Cliente das APIs do NCBI e do UniProt."""

    def __init__(self, cache: LinkCache, api_key: Optional[str] = None):
        self.cache = cache
        self.api_key = api_key
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT})
        self.requests_made = 0

    # ------------------------------------------------------------------ #

    def _get(self, url: str, params: Dict[str, Any], delay: float) -> Optional[Dict]:
        """GET com rate limiting e falha graciosa."""
        if self.api_key and NCBI_EUTILS in url:
            params = {**params, "api_key": self.api_key}
        try:
            response = self.session.get(url, params=params, timeout=REQUEST_TIMEOUT)
            self.requests_made += 1
            time.sleep(delay)
            if response.status_code != 200:
                log.warning("HTTP %s em %s", response.status_code, url)
                return None
            return response.json()
        except Exception as error:  # noqa: BLE001 — uma falha de rede não derruba o lote
            log.warning("Falha ao consultar %s: %s", url, error)
            time.sleep(delay)
            return None

    # ------------------------------------------------------------------ #

    def link_organism(self, canonical: str) -> LinkResult:
        """Resolve um organismo contra o NCBI Taxonomy."""
        result = LinkResult(key=f"{ORGANISM}:{canonical}", canonical=canonical, label=ORGANISM)

        payload = self._get(
            f"{NCBI_EUTILS}/esearch.fcgi",
            {
                "db": "taxonomy",
                "term": f"{canonical}[Scientific Name]",
                "retmode": "json",
                "retmax": 1,
            },
            NCBI_DELAY,
        )
        if payload is None:
            result.status = STATUS_ERROR
            result.note = "falha de rede"
            return result

        ids = payload.get("esearchresult", {}).get("idlist") or []
        if not ids:
            result.note = "nome científico não encontrado no NCBI Taxonomy"
            return result

        result.taxonomy_id = ids[0]
        result.status = STATUS_LINKED
        return result

    def link_gene(self, symbol: str) -> LinkResult:
        """
        Resolve um símbolo de gene contra NCBI Gene e UniProt.

        Busca primeiro em humano, que domina a literatura biomédica, e depois
        sem restrição de organismo. O organismo efetivamente encontrado fica
        registrado — não fingimos saber de qual espécie o corpus falava.
        """
        result = LinkResult(key=f"{GENE}:{symbol}", canonical=symbol, label=GENE)

        gene_id = None
        for term in (f"{symbol}[sym] AND human[orgn]", f"{symbol}[sym]"):
            payload = self._get(
                f"{NCBI_EUTILS}/esearch.fcgi",
                {"db": "gene", "term": term, "retmode": "json", "retmax": 1},
                NCBI_DELAY,
            )
            if payload is None:
                result.status = STATUS_ERROR
                result.note = "falha de rede"
                return result
            ids = payload.get("esearchresult", {}).get("idlist") or []
            if ids:
                gene_id = ids[0]
                break

        taxid = None
        if gene_id:
            summary = self._get(
                f"{NCBI_EUTILS}/esummary.fcgi",
                {"db": "gene", "id": gene_id, "retmode": "json"},
                NCBI_DELAY,
            )
            entry = (summary or {}).get("result", {}).get(gene_id, {})
            organism = entry.get("organism") or {}
            result.ncbi_gene_id = gene_id
            result.official_symbol = entry.get("name")
            result.gene_description = (entry.get("description") or "")[:200] or None
            result.source_organism = organism.get("scientificname")
            taxid = organism.get("taxid")
            if taxid:
                result.taxonomy_id = str(taxid)
            result.status = STATUS_LINKED

        # UniProt: a ponte para a proteína, RESTRITA ao organismo que o NCBI
        # encontrou. Sem essa restrição o mesmo símbolo casa em espécies
        # diferentes e o par vira ficção: medido, "ACE2" devolvia o gene humano
        # (NCBI 59272) ao lado da proteína bHLH74 de Arabidopsis, e "AHR" o
        # receptor humano ao lado de uma aldeído-redutase de E. coli.
        query = f"gene_exact:{symbol} AND reviewed:true"
        if taxid:
            query += f" AND organism_id:{taxid}"

        uniprot = self._get(
            UNIPROT_API,
            {
                "query": query,
                "fields": "accession,id,protein_name,gene_primary,organism_name",
                "format": "json",
                "size": 1,
            },
            UNIPROT_DELAY,
        )
        entries = (uniprot or {}).get("results") or []
        if entries:
            entry = entries[0]
            result.uniprot_accession = entry.get("primaryAccession")
            result.uniprot_id = entry.get("uniProtkbId")
            name = (
                entry.get("proteinDescription", {})
                .get("recommendedName", {})
                .get("fullName", {})
                .get("value")
            )
            result.protein_name = (name or "")[:200] or None
            result.organism_matched = bool(taxid)
            if not result.source_organism:
                result.source_organism = (entry.get("organism") or {}).get("scientificName")
            result.status = STATUS_LINKED

        if result.status != STATUS_LINKED:
            result.note = "símbolo não encontrado em NCBI Gene nem UniProt"
        return result

    # ------------------------------------------------------------------ #

    def resolve(self, key: str, canonical: str, label: str) -> LinkResult:
        """Resolve uma entidade, usando o cache quando possível."""
        cached = self.cache.get(key)
        if cached is not None:
            return LinkResult(**cached)

        if label == ORGANISM:
            result = self.link_organism(canonical)
        elif label == GENE:
            result = self.link_gene(canonical)
        else:
            result = LinkResult(key=key, canonical=canonical, label=label)
            result.note = f"tipo {label} não tem base externa nesta versão"

        # Erro de rede não vai para o cache: seria fixar uma falha transitória.
        if result.status != STATUS_ERROR:
            self.cache.put(key, result.__dict__.copy())
        return result


# --------------------------------------------------------------------- #


def fetch_pending(graph: Neo4jGraphManager, limit: Optional[int]) -> List[Dict]:
    """Entidades linkáveis que ainda não foram resolvidas."""
    cypher = """
    MATCH (e:Entity)
    WHERE e.label IN $labels AND e.link_status IS NULL
    RETURN e.key AS key, e.canonical AS canonical, e.label AS label
    ORDER BY e.label, e.canonical
    """
    if limit:
        cypher += " LIMIT $limit"
    with graph._session() as session:
        return [
            record.data()
            for record in session.run(cypher, labels=[GENE, ORGANISM], limit=limit)
        ]


def write_links(graph: Neo4jGraphManager, results: List[LinkResult]) -> int:
    """Grava os identificadores externos nos nós de entidade."""
    cypher = """
    UNWIND $rows AS row
    MATCH (e:Entity {key: row.key})
    SET e.link_status = row.link_status,
        e.organism_matched = row.organism_matched,
        e.taxonomy_id = row.taxonomy_id,
        e.ncbi_gene_id = row.ncbi_gene_id,
        e.official_symbol = row.official_symbol,
        e.gene_description = row.gene_description,
        e.source_organism = row.source_organism,
        e.uniprot_accession = row.uniprot_accession,
        e.uniprot_id = row.uniprot_id,
        e.protein_name = row.protein_name,
        e.linked_at = datetime()
    RETURN count(e) AS written
    """
    rows = [r.to_graph_row() for r in results]
    with graph._session() as session:
        record = session.execute_write(lambda tx: tx.run(cypher, rows=rows).single())
    return record["written"] if record else 0


def run(limit: Optional[int], dry_run: bool, api_key: Optional[str]) -> int:
    started = time.time()

    print("=" * 78)
    print("SPACEBIO-020 — Entity Linking" + ("  [DRY-RUN]" if dry_run else ""))
    print("=" * 78)

    cache = LinkCache(settings.data_dir / CACHE_FILE)
    print(f"\nCache: {len(cache)} entidades já resolvidas em {settings.data_dir / CACHE_FILE}")

    with Neo4jGraphManager() as graph:
        pending = fetch_pending(graph, limit)
        by_label: Dict[str, int] = {}
        for row in pending:
            by_label[row["label"]] = by_label.get(row["label"], 0) + 1

        print(f"Entidades pendentes: {len(pending)}")
        for label, count in sorted(by_label.items()):
            print(f"   {label:<12}{count:>5}")

        if not pending:
            print("\nNada a fazer — todas as entidades linkáveis já foram resolvidas.")
            return 0

        if dry_run:
            print("\nDRY-RUN: nenhuma consulta externa será feita. Amostra:")
            for row in pending[:15]:
                print(f"   {row['label']:<10} {row['canonical']}")
            return 0

        linker = ExternalLinker(cache, api_key=api_key)
        results: List[LinkResult] = []
        linked = unresolved = errors = 0

        print(f"\n{'-' * 78}")
        for position, row in enumerate(pending, start=1):
            result = linker.resolve(row["key"], row["canonical"], row["label"])
            results.append(result)

            if result.status == STATUS_LINKED:
                linked += 1
            elif result.status == STATUS_ERROR:
                errors += 1
            else:
                unresolved += 1

            if position % 25 == 0 or position == len(pending):
                elapsed = time.time() - started
                print(
                    f"  {position:>4}/{len(pending)}  "
                    f"resolvidas={linked:>4} sem correspondência={unresolved:>4} erros={errors:>3}  "
                    f"{linker.requests_made:>4} requisições  {elapsed:>5.0f}s"
                )
                cache.save()

        cache.save()
        print(f"{'-' * 78}")

        # Erros de rede ficam sem link_status, para a próxima execução tentar.
        gravaveis = [r for r in results if r.status != STATUS_ERROR]
        written = write_links(graph, gravaveis) if gravaveis else 0
        print(f"\n{written} entidades atualizadas no grafo")

    elapsed = time.time() - started
    print(f"\n{'=' * 78}")
    print("ENTITY LINKING CONCLUÍDO")
    print("=" * 78)
    print(f"   Resolvidas             {linked}")
    print(f"   Sem correspondência    {unresolved}")
    print(f"   Erros de rede          {errors}  (ficam pendentes para a próxima execução)")
    print(f"   Requisições externas   {linker.requests_made}")
    print(f"   Tempo                  {elapsed:.0f}s")
    print("=" * 78)
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="SPACEBIO-020 — entity linking")
    parser.add_argument("--limit", type=int, help="resolver apenas N entidades")
    parser.add_argument("--dry-run", action="store_true", help="listar sem consultar")
    parser.add_argument("--api-key", help="NCBI API key (eleva o limite para 10 req/s)")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )

    import os

    try:
        return run(
            limit=args.limit,
            dry_run=args.dry_run,
            api_key=args.api_key or os.getenv("NCBI_API_KEY"),
        )
    except MissingCredentialError as error:
        print(f"\nCONFIGURAÇÃO AUSENTE:\n{error}")
        return 2
    except KeyboardInterrupt:
        print("\nInterrompido. O cache foi salvo; a próxima execução retoma daqui.")
        return 130
    except Exception as error:  # noqa: BLE001
        print(f"\nERRO: {type(error).__name__}: {error}")
        import traceback

        traceback.print_exc()
        return 3


if __name__ == "__main__":
    sys.exit(main())
