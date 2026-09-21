"""
Testes da SPACEBIO-016/017/018/019 — Ontologia, NER e normalização

Verifica que o extrator:
  1. normaliza formas de superfície para o nó canônico (§19);
  2. reconhece identificadores estruturados (STS-107, GLDS-242, GSE12345);
  3. resiste aos falsos positivos que a auditoria do corpus revelou;
  4. não produz menções sobrepostas.

Os testes 3 são regressões de casos reais medidos no corpus, não hipóteses.

Uso:
    python test_ner.py
"""

from __future__ import annotations

import logging
import sys
from typing import List, Optional

from ner import EntityExtractor, summarize
from ontology import (
    BIOLOGICAL_PROCESS,
    CELL_TYPE,
    DATASET,
    ENTITY_LABELS,
    EXPERIMENTAL_CONDITION,
    GENE,
    MISSION,
    ORGANISM,
    TISSUE,
    DICTIONARY,
    ontology_summary,
)

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

log = logging.getLogger("test_ner")


class NerTestFailure(AssertionError):
    """Falha de asserção nos testes de extração."""


def check(condition: bool, message: str) -> None:
    if condition:
        print(f"      [OK] {message}")
    else:
        print(f"      [FALHA] {message}")
        raise NerTestFailure(message)


def section(step: str, title: str) -> None:
    print(f"\n[{step}] {title}")


def canonicals(extractor: EntityExtractor, text: str) -> set:
    return {(m.canonical, m.label) for m in extractor.extract(text)}


# ---------------------------------------------------------------------- #


def test_ontology_integrity() -> None:
    """A ontologia é consistente consigo mesma."""
    summary = ontology_summary()
    check(all(label in ENTITY_LABELS for label in summary), "todos os tipos são declarados")
    check(sum(summary.values()) == len(DICTIONARY), f"{len(DICTIONARY)} entidades no dicionário")

    keys = [(d.canonical, d.label) for d in DICTIONARY]
    check(len(keys) == len(set(keys)), "não há entidade canônica duplicada")

    for definition in DICTIONARY:
        check_forms = definition.all_forms()
        check(
            bool(check_forms),
            f"{definition.canonical} tem ao menos uma forma de busca",
        )


def test_normalization(extractor: EntityExtractor) -> None:
    """Formas de superfície diferentes convergem para o mesmo nó (§19)."""
    grupos = {
        ("Mus musculus", ORGANISM): ["mice", "mouse", "murine", "C57BL/6"],
        ("International Space Station", MISSION): ["ISS", "International Space Station", "space station"],
        ("Homo sapiens", ORGANISM): ["human", "astronauts", "crew member"],
        ("Hindlimb Unloading", EXPERIMENTAL_CONDITION): ["hindlimb unloading", "tail suspension", "HLU"],
        ("Bone Loss", BIOLOGICAL_PROCESS): ["bone loss", "osteopenia", "bone resorption"],
        ("Osteoblast", CELL_TYPE): ["osteoblast", "osteoblasts", "osteoblastic"],
    }
    for (canonical, label), formas in grupos.items():
        for forma in formas:
            found = canonicals(extractor, f"The study analyzed {forma} in detail.")
            check(
                (canonical, label) in found,
                f"{forma!r} -> {label}:{canonical}",
            )


def test_structured_patterns(extractor: EntityExtractor) -> None:
    """Identificadores estruturados são reconhecidos pela forma."""
    casos = {
        "Samples flew on STS-107 and STS-135.": [("STS-107", MISSION), ("STS-135", MISSION)],
        "Data available as GLDS-242 and OSD-379.": [("GLDS-242", DATASET), ("OSD-379", DATASET)],
        "Deposited under GSE123456.": [("GSE123456", DATASET)],
        "See BioProject PRJNA1234567.": [("PRJNA1234567", DATASET)],
    }
    for text, expected in casos.items():
        found = canonicals(extractor, text)
        for item in expected:
            check(item in found, f"{item[0]} reconhecido em {text[:34]!r}")


def test_false_positive_regressions(extractor: EntityExtractor) -> None:
    """Regressões de falsos positivos medidos no corpus real."""
    # "Mir": 7 de 8 ocorrências eram microRNA, não a estação.
    micro_rna = canonicals(extractor, "Expression of miR-125b, miR-21 and miR-16 was measured.")
    check(
        ("Mir", MISSION) not in micro_rna,
        "miR-125b/miR-21 não viram a estação Mir",
    )
    station = canonicals(extractor, "Experiments aboard the Mir Space Station.")
    check(("Mir", MISSION) in station, "'Mir Space Station' ainda é reconhecida")

    # Substrings: 'rat' dentro de 'temperature', 'ISS' dentro de 'tissue'.
    substrings = canonicals(
        extractor, "The temperature was generated during tissue analysis of the mission."
    )
    check(
        ("Rattus norvegicus", ORGANISM) not in substrings,
        "'temperature'/'generated' não produzem o organismo rato",
    )
    check(
        ("International Space Station", MISSION) not in substrings,
        "'tissue'/'mission' não produzem a ISS",
    )

    # Siglas que não são genes, apesar do contexto "protein"/"gene".
    nao_genes = canonicals(
        extractor,
        "ECM proteinases and AMR genes were studied alongside EV proteins and DNA repair.",
    )
    for sigla in ("ECM", "AMR", "EV", "DNA"):
        check((sigla, GENE) not in nao_genes, f"{sigla!r} não é classificado como gene")


def test_gene_extraction(extractor: EntityExtractor) -> None:
    """Genes vêm do dicionário curado e do padrão com contexto."""
    curated = canonicals(extractor, "Expression of p53 and CDKN1a/p21 increased.")
    check(("TP53", GENE) in curated, "'p53' -> TP53")
    check(("CDKN1A", GENE) in curated, "'CDKN1a/p21' -> CDKN1A")

    by_pattern = canonicals(extractor, "The RUNX2 gene and SOD1 protein were upregulated.")
    check(("RUNX2", GENE) in by_pattern, "RUNX2 reconhecido")
    check(("SOD1", GENE) in by_pattern, "SOD1 reconhecido")

    # Sem o contexto à direita, uma sigla solta não vira gene.
    bare = canonicals(extractor, "The XYZW was observed in the sample.")
    check(("XYZW", GENE) not in bare, "sigla sem contexto de gene não é extraída")


def test_no_overlaps(extractor: EntityExtractor) -> None:
    """Menções não se sobrepõem no texto."""
    text = (
        "Mice aboard the International Space Station during STS-135 showed bone loss "
        "and muscle atrophy under simulated microgravity."
    )
    mentions = extractor.extract(text)
    check(len(mentions) > 0, f"{len(mentions)} menções extraídas")

    ordered = sorted(mentions, key=lambda m: m.start)
    for previous, current in zip(ordered, ordered[1:]):
        check(
            previous.end <= current.start,
            f"{previous.surface!r} e {current.surface!r} não se sobrepõem",
        )

    found = {m.canonical for m in mentions}
    check("International Space Station" in found, "menção longa não foi fragmentada")
    check("Simulated Microgravity" in found, "'simulated microgravity' vence 'microgravity'")

    print(f"      distribuição: {summarize(mentions)}")


def test_counting(extractor: EntityExtractor) -> None:
    """Agregação por entidade conta ocorrências."""
    counts = extractor.extract_counts(
        "Mice were studied. The mice showed changes. Murine samples were collected."
    )
    key = ("Mus musculus", ORGANISM)
    check(key in counts, "organismo agregado")
    check(counts[key] == 3, f"três formas contadas como uma entidade: {counts[key]}")

    check(extractor.extract("") == [], "texto vazio devolve lista vazia")
    check(extractor.extract("   ") == [], "texto em branco devolve lista vazia")


# ---------------------------------------------------------------------- #


def run() -> int:
    print("=" * 78)
    print("SPACEBIO-016/017/018/019 — Ontologia, NER e normalização")
    print("=" * 78)

    extractor = EntityExtractor()

    section("1/7", "Integridade da ontologia")
    test_ontology_integrity()

    section("2/7", "Normalização de formas de superfície")
    test_normalization(extractor)

    section("3/7", "Identificadores estruturados")
    test_structured_patterns(extractor)

    section("4/7", "Regressões de falsos positivos")
    test_false_positive_regressions(extractor)

    section("5/7", "Extração de genes")
    test_gene_extraction(extractor)

    section("6/7", "Sobreposição de menções")
    test_no_overlaps(extractor)

    section("7/7", "Contagem e entradas vazias")
    test_counting(extractor)

    print("\n" + "=" * 78)
    print("SPACEBIO-016/017/018/019 — todas as asserções passaram.")
    print("=" * 78)
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Testes de NER e ontologia")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )

    try:
        return run()
    except NerTestFailure as failure:
        print(f"\nTESTE REPROVADO: {failure}")
        return 1
    except Exception as error:  # noqa: BLE001
        print(f"\nERRO INESPERADO: {type(error).__name__}: {error}")
        import traceback

        traceback.print_exc()
        return 3


if __name__ == "__main__":
    sys.exit(main())
