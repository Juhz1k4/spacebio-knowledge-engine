"""
SPACEBIO-017 + 019 — Extração e normalização de entidades

Extrator determinístico: dicionário de domínio curado (SPACEBIO-018) e padrões
estruturados, com normalização para forma canônica (SPACEBIO-019).

POR QUE NÃO SCISPACY NESTA PRIMEIRA CAMADA
-------------------------------------------
O §18 do briefing sugere SciSpaCy + en_ner_bionlp13cg_md. Ao verificar a
instalação, o resolvedor do pip propôs rebaixar numpy de 2.4.6 para 1.26.4 e
compilar nmslib-metabrainz no Windows. scipy e torch deste ambiente foram
construídos contra a ABI do numpy 2.x — o downgrade quebraria o embedder, que
é o componente já validado de que todo o resto depende.

A decisão foi começar pela camada determinística, que:

  - cobre exatamente o que é ESPECÍFICO deste domínio (missões, condições,
    datasets, organismos-modelo) — categorias finitas, que ML resolveria pior;
  - é auditável: cada menção tem uma regra rastreável, não um peso opaco;
  - não tem custo de inferência, então roda sobre 45.947 chunks em minutos;
  - dá ao entity overlap do retriever o sinal que falta, hoje.

O NER estatístico continua necessário para genes e proteínas, que são um
vocabulário aberto. Fica para uma issue seguinte, em ambiente isolado.

NORMALIZAÇÃO (SPACEBIO-019)
---------------------------
A menção crua nunca vira nó. "ISS", "International Space Station" e "space
station" convergem para o nó `International Space Station`; "mice", "mouse" e
"murine" para `Mus musculus`. É o que o §19 exige: normalizar antes do MERGE.
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from dataclasses import dataclass
from typing import Dict, Iterable, List, Pattern, Sequence, Tuple

from ontology import (
    DICTIONARY,
    GENE,
    GENE_PATTERN,
    GENE_STOPLIST,
    PATTERN_RULES,
    EntityDefinition,
)

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class EntityMention:
    """Uma ocorrência de entidade num texto, já normalizada."""

    canonical: str
    label: str
    surface: str
    start: int
    end: int
    rule: str

    @property
    def key(self) -> Tuple[str, str]:
        """Chave do nó no grafo."""
        return (self.canonical, self.label)


def _compile_dictionary(
    definitions: Sequence[EntityDefinition],
) -> List[Tuple[Pattern, EntityDefinition]]:
    """
    Compila uma regex por entidade, com fronteira de palavra.

    A fronteira não é detalhe: medindo por substring, "rat" aparecia em 100%
    dos documentos (dentro de "temperature", "generated") e "ISS" em 97,6%
    (dentro de "tissue", "mission"). Com \\b, caem para 28% e 45% — que são os
    números reais.
    """
    compiled: List[Tuple[Pattern, EntityDefinition]] = []
    for definition in definitions:
        alternatives = []
        for form in definition.all_forms():
            escaped = re.escape(form)
            # Formas que começam/terminam em caractere não-alfanumérico (µg,
            # nf-κb) não aceitam \b naquele lado.
            left = r"\b" if form[0].isalnum() else ""
            right = r"\b" if form[-1].isalnum() else ""
            alternatives.append(f"{left}{escaped}{right}")
        pattern = re.compile("|".join(alternatives), re.IGNORECASE)
        compiled.append((pattern, definition))
    return compiled


class EntityExtractor:
    """
    Extrator de entidades por dicionário e padrão.

    Uso:
        extractor = EntityExtractor()
        mentions = extractor.extract(chunk_text)
        counts = extractor.count_by_entity(mentions)
    """

    def __init__(
        self,
        definitions: Sequence[EntityDefinition] = DICTIONARY,
        include_gene_pattern: bool = True,
    ):
        self._dictionary = _compile_dictionary(definitions)
        self.include_gene_pattern = include_gene_pattern

    # ------------------------------------------------------------------ #

    def extract(self, text: str) -> List[EntityMention]:
        """
        Extrai todas as menções de um texto, sem sobreposição.

        Quando dois padrões casam a mesma região, vence o mais longo — assim
        "International Space Station" não é fragmentado em "Space Station".
        """
        if not text or not text.strip():
            return []

        candidates: List[EntityMention] = []

        # --- Dicionário ---
        for pattern, definition in self._dictionary:
            for match in pattern.finditer(text):
                candidates.append(
                    EntityMention(
                        canonical=definition.canonical,
                        label=definition.label,
                        surface=match.group(0),
                        start=match.start(),
                        end=match.end(),
                        rule="dictionary",
                    )
                )

        # --- Padrões estruturados (STS-107, GLDS-242, GSE12345) ---
        for rule in PATTERN_RULES:
            for match in rule.pattern.finditer(text):
                candidates.append(
                    EntityMention(
                        canonical=rule.canonical_template.format(*match.groups()),
                        label=rule.label,
                        surface=match.group(0),
                        start=match.start(),
                        end=match.end(),
                        rule="pattern",
                    )
                )

        # --- Genes por nomenclatura + contexto ---
        if self.include_gene_pattern:
            for match in GENE_PATTERN.finditer(text):
                symbol = match.group(1)
                if symbol in GENE_STOPLIST:
                    continue
                candidates.append(
                    EntityMention(
                        canonical=symbol.upper(),
                        label=GENE,
                        surface=match.group(1),
                        start=match.start(1),
                        end=match.end(1),
                        rule="gene_pattern",
                    )
                )

        return self._resolve_overlaps(candidates)

    @staticmethod
    def _resolve_overlaps(mentions: List[EntityMention]) -> List[EntityMention]:
        """
        Remove menções sobrepostas, preferindo a mais longa.

        Sem isto, "hindlimb unloading" geraria também a menção "unloading" de
        outra entrada, e a contagem de evidência ficaria inflada.
        """
        if not mentions:
            return []

        ordered = sorted(mentions, key=lambda m: (m.start, -(m.end - m.start)))
        kept: List[EntityMention] = []
        last_end = -1

        for mention in ordered:
            if mention.start >= last_end:
                kept.append(mention)
                last_end = mention.end
        return kept

    # ------------------------------------------------------------------ #

    def count_by_entity(
        self, mentions: Iterable[EntityMention]
    ) -> Dict[Tuple[str, str], int]:
        """Agrega menções por (canonical, label)."""
        return Counter(mention.key for mention in mentions)

    def extract_counts(self, text: str) -> Dict[Tuple[str, str], int]:
        """Atalho: extrai e agrega em uma passada."""
        return self.count_by_entity(self.extract(text))


def summarize(mentions: Sequence[EntityMention]) -> Dict[str, int]:
    """Conta menções por tipo de entidade."""
    return dict(Counter(mention.label for mention in mentions))


if __name__ == "__main__":
    import argparse
    import sys

    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description="SPACEBIO-017 — extração de entidades")
    parser.add_argument("--sample", type=int, default=20, help="documentos a analisar")
    parser.add_argument("--text", help="extrair de um texto avulso em vez do corpus")
    args = parser.parse_args()

    extractor = EntityExtractor()

    if args.text:
        for mention in extractor.extract(args.text):
            print(f"  {mention.label:<22} {mention.canonical:<32} <- {mention.surface!r} ({mention.rule})")
        raise SystemExit(0)

    import pandas as pd

    from config import settings

    frame = pd.read_csv(settings.data_dir / "metadata_clean.csv").head(args.sample)
    totals: Counter = Counter()
    per_label: Counter = Counter()
    documents = 0

    for _, row in frame.iterrows():
        path = settings.resolve_corpus_path(row["clean_path"])
        if not path.exists():
            continue
        documents += 1
        for mention in extractor.extract(path.read_text(encoding="utf-8")):
            totals[mention.key] += 1
            per_label[mention.label] += 1

    print(f"{documents} documentos · {sum(per_label.values()):,} menções\n")
    print("POR TIPO")
    for label, count in per_label.most_common():
        print(f"  {label:<24}{count:>8,}")

    print("\nENTIDADES MAIS FREQUENTES")
    for (canonical, label), count in totals.most_common(20):
        print(f"  {count:>7,}  {label:<22} {canonical}")
