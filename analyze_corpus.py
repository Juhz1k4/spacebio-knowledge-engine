"""
SPACEBIO-012.5 (fase 1) — Diagnóstico estrutural do corpus

Mede a prevalência e o VOLUME do lixo estrutural nos textos processados, para
que as regras do cleaner.py sejam calibradas em dados e não em suposição.

O que este script NÃO faz: não altera nenhum arquivo. É só leitura e medição.

Modelo de zonas
---------------
Todo o corpus vem do PMC (594/594 registros), com uma estrutura estável:

    [HEADER]      revista, DOI, navegação PMC, autores, afiliações
    [BODY]        Abstract .. Discussion — a ciência
    [BACKMATTER]  Acknowledgments, References, rodapé do publisher

O script localiza as duas fronteiras por marcadores textuais, mede quantos
caracteres cada zona ocupa e reporta a cobertura da detecção — ou seja, em
quantos documentos cada fronteira foi encontrada com confiança.

Uso:
    python analyze_corpus.py                 # relatório completo
    python analyze_corpus.py --sample 50     # amostra rápida
    python analyze_corpus.py --csv saida.csv # métricas por documento
"""

from __future__ import annotations

import argparse
import re
import statistics
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd

from config import settings

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")


# --------------------------------------------------------------------- #
# Padrões observados no corpus (medidos, não supostos)
# --------------------------------------------------------------------- #

# Boilerplate de navegação/serviço do PMC. Frequência medida em 577 docs.
BOILERPLATE_LINES = {
    "Search in PMC": 1.00,
    "Search in PubMed": 1.00,
    "View in NLM Catalog": 1.00,
    "Add to search": 1.00,
    "PMC Copyright notice": 1.00,
    "Copyright and License information": 1.00,
    "Find articles by": 0.995,
    "Author information": 0.993,
    "PMC free article": 0.983,
    "Google Scholar": 0.984,
    "Open in a new tab": 0.967,
    "Article notes": 0.962,
    "Associated Data": 0.676,
    "Click here for file": None,
}

# Marcadores do fim do cabeçalho, em ordem de preferência.
HEADER_END_MARKERS = [
    re.compile(r"^Abstract$", re.I),
    re.compile(r"^PMC Copyright notice$", re.I),
    re.compile(r"^Copyright and License information$", re.I),
    re.compile(r"^Author information$", re.I),
]

# Início do backmatter. As duas grafias e as duas caixas importam:
# References 80,1% / REFERENCES 14,6% / Acknowledgments 47,3% / Acknowledgements 28,8%.
BACKMATTER_MARKERS = [
    re.compile(r"^references$", re.I),
    re.compile(r"^bibliography$", re.I),
    re.compile(r"^literature cited$", re.I),
    re.compile(r"^acknowledge?ments?$", re.I),
]

# Seções de fim de artigo — administrativas, não científicas.
ADMIN_SECTIONS = {
    "Acknowledgments": re.compile(r"^acknowledge?ments?$", re.I),
    "References": re.compile(r"^(references|bibliography|literature cited)$", re.I),
    "Funding": re.compile(r"^funding( statement)?$", re.I),
    "Competing interests": re.compile(r"^(competing|conflict of) interests?.*$", re.I),
    "Author contributions": re.compile(r"^author contributions?$", re.I),
    "Data availability": re.compile(r"^data availability.*$", re.I),
    "Supplementary": re.compile(r"^supplementary (materials?|information)$", re.I),
    "Footnotes": re.compile(r"^footnotes?$", re.I),
    "Contributor Information": re.compile(r"^contributor information$", re.I),
}

# Seções científicas — o que precisa ser PRESERVADO.
CONTENT_SECTIONS = {
    "Abstract": re.compile(r"^abstract$", re.I),
    "Introduction": re.compile(r"^introduction$", re.I),
    "Methods": re.compile(r"^(methods|materials and methods|methodology)$", re.I),
    "Results": re.compile(r"^results$", re.I),
    "Discussion": re.compile(r"^discussion$", re.I),
    "Conclusion": re.compile(r"^conclusions?$", re.I),
}

# Rodapé do publisher, sempre na última linha.
PUBLISHER_FOOTER = re.compile(r"^Articles from .+ are provided here courtesy of", re.I)

# Cabeçalho da citação: "PLoS One\n. 2014 Aug 18;9(8):e104830. doi:\n10.1371/..."
CITATION_HEADER = re.compile(r"^\.\s*\d{4}\s+\w{3}\s+\d+.*doi:", re.I)
DOI_PATTERN = re.compile(r"^10\.\d{4,9}/\S+$")

# Marcadores de link de referência que sobram como linhas soltas.
REFERENCE_LINK_TOKENS = {"[", "]", "] [", "DOI", "PubMed", "PMC free article", "Google Scholar"}


@dataclass
class DocumentMetrics:
    """Métricas estruturais de um documento."""

    file_name: str
    chars_total: int
    lines_total: int

    header_end_line: Optional[int] = None
    header_marker: Optional[str] = None
    backmatter_start_line: Optional[int] = None
    backmatter_marker: Optional[str] = None

    chars_header: int = 0
    chars_body: int = 0
    chars_backmatter: int = 0

    boilerplate_lines: int = 0
    reference_link_lines: int = 0
    short_lines: int = 0
    has_publisher_footer: bool = False

    doi: Optional[str] = None
    journal: Optional[str] = None

    admin_sections: List[str] = field(default_factory=list)
    content_sections: List[str] = field(default_factory=list)

    @property
    def pct_header(self) -> float:
        return 100.0 * self.chars_header / self.chars_total if self.chars_total else 0.0

    @property
    def pct_body(self) -> float:
        return 100.0 * self.chars_body / self.chars_total if self.chars_total else 0.0

    @property
    def pct_backmatter(self) -> float:
        return 100.0 * self.chars_backmatter / self.chars_total if self.chars_total else 0.0

    @property
    def pct_removable(self) -> float:
        return self.pct_header + self.pct_backmatter


def analyze_document(path: Path) -> DocumentMetrics:
    """Mede a estrutura de um único documento."""
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    stripped = [line.strip() for line in lines]

    metrics = DocumentMetrics(
        file_name=path.name,
        chars_total=len(text),
        lines_total=len(lines),
    )

    # --- Cabeçalho de citação: revista e DOI ---
    # Formato: linha 0 = revista, linha 1 = ". <data>;<vol>. doi:", linha 2 = DOI.
    if stripped and stripped[0] and not stripped[0].startswith("."):
        metrics.journal = stripped[0][:80]
    for index, line in enumerate(stripped[:6]):
        if DOI_PATTERN.match(line):
            metrics.doi = line
            break

    # --- Fronteira 1: fim do cabeçalho ---
    # Procurada apenas nos primeiros 30% do documento; um "Abstract" citado no
    # meio do texto não pode ser confundido com o início do artigo.
    limit = max(int(len(lines) * 0.30), 1)
    for pattern in HEADER_END_MARKERS:
        for index in range(limit):
            if pattern.match(stripped[index]):
                metrics.header_end_line = index
                metrics.header_marker = pattern.pattern
                break
        if metrics.header_end_line is not None:
            break

    # --- Fronteira 2: início do backmatter ---
    # Procurada só depois de 30% do documento, pela mesma razão inversa.
    start = max(int(len(lines) * 0.30), 1)
    for pattern in BACKMATTER_MARKERS:
        for index in range(start, len(lines)):
            if pattern.match(stripped[index]):
                if metrics.backmatter_start_line is None or index < metrics.backmatter_start_line:
                    metrics.backmatter_start_line = index
                    metrics.backmatter_marker = pattern.pattern
                break

    # --- Volume de cada zona, em caracteres ---
    header_end = metrics.header_end_line if metrics.header_end_line is not None else 0
    backmatter_start = (
        metrics.backmatter_start_line
        if metrics.backmatter_start_line is not None
        else len(lines)
    )
    if backmatter_start < header_end:  # detecção incoerente: trata tudo como corpo
        header_end, backmatter_start = 0, len(lines)
        metrics.header_end_line = metrics.backmatter_start_line = None

    metrics.chars_header = sum(len(line) + 1 for line in lines[:header_end])
    metrics.chars_body = sum(len(line) + 1 for line in lines[header_end:backmatter_start])
    metrics.chars_backmatter = sum(len(line) + 1 for line in lines[backmatter_start:])

    # --- Contagens de ruído ---
    for line in stripped:
        if line in BOILERPLATE_LINES:
            metrics.boilerplate_lines += 1
        if line in REFERENCE_LINK_TOKENS:
            metrics.reference_link_lines += 1
        if 0 < len(line) <= 3:
            metrics.short_lines += 1

    metrics.has_publisher_footer = any(
        PUBLISHER_FOOTER.match(line) for line in stripped[-3:] if line
    )

    # --- Seções presentes ---
    unique = set(stripped)
    for name, pattern in ADMIN_SECTIONS.items():
        if any(pattern.match(line) for line in unique):
            metrics.admin_sections.append(name)
    for name, pattern in CONTENT_SECTIONS.items():
        if any(pattern.match(line) for line in unique):
            metrics.content_sections.append(name)

    return metrics


def percentiles(values: List[float]) -> Dict[str, float]:
    if not values:
        return {"mediana": 0.0, "p10": 0.0, "p90": 0.0, "media": 0.0}
    ordered = sorted(values)
    return {
        "mediana": statistics.median(ordered),
        "p10": ordered[int(len(ordered) * 0.10)],
        "p90": ordered[int(len(ordered) * 0.90)],
        "media": statistics.mean(ordered),
    }


def print_report(metrics: List[DocumentMetrics], duplicate_info: Dict[str, int]) -> None:
    total = len(metrics)
    bar = "=" * 78

    print(bar)
    print("SPACEBIO-012.5 — Diagnóstico estrutural do corpus")
    print(bar)

    # ---------------------------------------------------------------- #
    print(f"\n1. COBERTURA\n{'-' * 78}")
    print(f"   Documentos analisados          : {total}")
    print(f"   Linhas no metadata.csv         : {duplicate_info['rows']}")
    print(f"   Arquivos únicos                : {duplicate_info['unique_files']}")
    print(
        f"   Linhas duplicadas (mesmo arquivo, título diferente): "
        f"{duplicate_info['duplicate_rows']}"
    )

    chars = sum(m.chars_total for m in metrics)
    print(f"   Volume total                   : {chars / 1_000_000:.1f} M caracteres")

    # ---------------------------------------------------------------- #
    print(f"\n2. DETECÇÃO DE FRONTEIRAS\n{'-' * 78}")
    header_ok = sum(1 for m in metrics if m.header_end_line is not None)
    back_ok = sum(1 for m in metrics if m.backmatter_start_line is not None)
    both = sum(
        1 for m in metrics if m.header_end_line is not None and m.backmatter_start_line is not None
    )
    print(f"   Fim do cabeçalho detectado     : {header_ok:>4} ({100*header_ok/total:>5.1f}%)")
    print(f"   Início do backmatter detectado : {back_ok:>4} ({100*back_ok/total:>5.1f}%)")
    print(f"   Ambas as fronteiras            : {both:>4} ({100*both/total:>5.1f}%)")
    print(
        f"   Rodapé do publisher            : "
        f"{sum(1 for m in metrics if m.has_publisher_footer):>4} "
        f"({100*sum(1 for m in metrics if m.has_publisher_footer)/total:>5.1f}%)"
    )

    # ---------------------------------------------------------------- #
    print(f"\n3. VOLUME POR ZONA (% dos caracteres do documento)\n{'-' * 78}")
    print(f"   {'zona':<16}{'mediana':>10}{'média':>10}{'p10':>10}{'p90':>10}")
    for label, values in [
        ("cabeçalho", [m.pct_header for m in metrics]),
        ("corpo (ciência)", [m.pct_body for m in metrics]),
        ("backmatter", [m.pct_backmatter for m in metrics]),
        ("REMOVÍVEL", [m.pct_removable for m in metrics]),
    ]:
        stats = percentiles(values)
        print(
            f"   {label:<16}{stats['mediana']:>9.1f}%{stats['media']:>9.1f}%"
            f"{stats['p10']:>9.1f}%{stats['p90']:>9.1f}%"
        )

    removable_chars = sum(m.chars_header + m.chars_backmatter for m in metrics)
    print(
        f"\n   Total removível no corpus      : "
        f"{removable_chars / 1_000_000:.1f} M de {chars / 1_000_000:.1f} M caracteres "
        f"({100 * removable_chars / chars:.1f}%)"
    )

    # ---------------------------------------------------------------- #
    print(f"\n4. SEÇÕES ADMINISTRATIVAS (candidatas a remoção)\n{'-' * 78}")
    for name in ADMIN_SECTIONS:
        count = sum(1 for m in metrics if name in m.admin_sections)
        print(f"   {name:<26}{count:>5} ({100*count/total:>5.1f}%)")

    print(f"\n5. SEÇÕES CIENTÍFICAS (precisam ser PRESERVADAS)\n{'-' * 78}")
    for name in CONTENT_SECTIONS:
        count = sum(1 for m in metrics if name in m.content_sections)
        print(f"   {name:<26}{count:>5} ({100*count/total:>5.1f}%)")

    # ---------------------------------------------------------------- #
    print(f"\n6. RUÍDO DE LINHA\n{'-' * 78}")
    for label, values in [
        ("linhas boilerplate", [float(m.boilerplate_lines) for m in metrics]),
        ("linhas de link de ref.", [float(m.reference_link_lines) for m in metrics]),
        ("linhas <= 3 chars", [float(m.short_lines) for m in metrics]),
    ]:
        stats = percentiles(values)
        print(f"   {label:<26} mediana={stats['mediana']:>7.0f}  p90={stats['p90']:>7.0f}")

    frag = [100.0 * m.short_lines / m.lines_total for m in metrics if m.lines_total]
    stats = percentiles(frag)
    print(
        f"   fragmentação (linhas <=3 chars / total): "
        f"mediana={stats['mediana']:.1f}%  p90={stats['p90']:.1f}%"
    )

    # ---------------------------------------------------------------- #
    print(f"\n7. METADADOS RECUPERÁVEIS DO CABEÇALHO\n{'-' * 78}")
    with_doi = sum(1 for m in metrics if m.doi)
    with_journal = sum(1 for m in metrics if m.journal)
    print(f"   DOI extraível                  : {with_doi:>5} ({100*with_doi/total:>5.1f}%)")
    print(f"   Revista extraível              : {with_journal:>5} ({100*with_journal/total:>5.1f}%)")
    print("   (o schema atual não possui campo doi — ver §7.1 do briefing)")

    # ---------------------------------------------------------------- #
    print(f"\n8. IMPACTO ESTIMADO NO PIPELINE\n{'-' * 78}")
    chunk_size = settings.chunk_size
    overlap = settings.chunk_overlap
    stride = chunk_size - overlap
    chunks_now = sum(max(1, m.chars_total // stride) for m in metrics)
    chunks_clean = sum(max(1, m.chars_body // stride) for m in metrics if m.chars_body)
    print(f"   Chunking atual ({chunk_size}/{overlap}):")
    print(f"     chunks sem limpeza           : ~{chunks_now:,}")
    print(f"     chunks após limpeza          : ~{chunks_clean:,}")
    print(
        f"     redução                      : ~{chunks_now - chunks_clean:,} "
        f"({100 * (chunks_now - chunks_clean) / chunks_now:.1f}%)"
    )

    # ---------------------------------------------------------------- #
    print(f"\n9. DOCUMENTOS QUE EXIGEM ATENÇÃO\n{'-' * 78}")
    tiny = [m for m in metrics if m.chars_total < 2000]
    no_body = [m for m in metrics if m.pct_body < 30]
    aggressive = [m for m in metrics if m.pct_removable > 70]
    no_boundary = [
        m for m in metrics if m.header_end_line is None and m.backmatter_start_line is None
    ]
    print(f"   Textos < 2 KB (extração falha?): {len(tiny)}")
    print(f"   Corpo < 30% do documento       : {len(no_body)}")
    print(f"   Removível > 70% do documento   : {len(aggressive)}")
    print(f"   Nenhuma fronteira detectada    : {len(no_boundary)}")
    for m in (tiny + aggressive)[:5]:
        print(f"     - {m.file_name[:52]:<54} {m.chars_total:>7} chars, corpo {m.pct_body:.0f}%")

    print(f"\n{bar}")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Diagnóstico estrutural do corpus SpaceBio")
    parser.add_argument("--sample", type=int, help="analisar apenas N documentos")
    parser.add_argument("--csv", help="gravar métricas por documento neste caminho")
    args = parser.parse_args(argv)

    frame = pd.read_csv(settings.metadata_file)
    duplicate_info = {
        "rows": len(frame),
        "unique_files": frame["local_path"].nunique(),
        "duplicate_rows": len(frame) - frame["local_path"].nunique(),
    }

    unique = frame.drop_duplicates("local_path")
    paths = [settings.resolve_corpus_path(p) for p in unique["local_path"]]
    paths = [p for p in paths if p.exists()]
    if args.sample:
        paths = paths[: args.sample]

    print(f"Analisando {len(paths)} documentos...\n")
    metrics = [analyze_document(path) for path in paths]

    print_report(metrics, duplicate_info)

    if args.csv:
        rows = []
        for m in metrics:
            row = asdict(m)
            row["admin_sections"] = "|".join(m.admin_sections)
            row["content_sections"] = "|".join(m.content_sections)
            row.update(
                pct_header=round(m.pct_header, 2),
                pct_body=round(m.pct_body, 2),
                pct_backmatter=round(m.pct_backmatter, 2),
            )
            rows.append(row)
        pd.DataFrame(rows).to_csv(args.csv, index=False, encoding="utf-8")
        print(f"\nMétricas por documento gravadas em {args.csv}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
