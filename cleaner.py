"""
SPACEBIO-012.5 (fase 2) — Corpus Cleaning

Remove lixo estrutural dos textos do PMC antes do chunking, preservando
integralmente o conteúdo científico.

As regras abaixo foram calibradas em medição, não em suposição: ver
`analyze_corpus.py` e o relatório de diagnóstico dos 577 documentos.

REGRAS
------
R1  Excluir documentos não-científicos (erratas, correções, retratações),
    detectados pelo título INTERNO do PMC — o título do metadata.csv guarda o
    nome do artigo original e não denuncia a errata.
R2  Deduplicar o metadata por local_path (594 linhas → 577 arquivos).
R3  Cortar o cabeçalho até o marcador de início de conteúdo.
R4  Cortar o backmatter a partir de References/Acknowledgments.
R5  Remover seções administrativas nomeadas onde quer que apareçam — 53,2% dos
    documentos têm ao menos uma DENTRO da zona de corpo.
R6  Remover boilerplate de linha (navegação do PMC, tokens de link).
R7  Extrair DOI e revista ANTES do corte de cabeçalho, para o metadata.
R8  Excluir documentos cuja procedência não fecha: o título do metadata não
    corresponde ao artigo baixado (similaridade < 0,60). São erro de ingestão
    da Fase 0 — ver docs/issues/SPACEBIO-007.1-title-divergence.md.
R9  Manter uma única cópia por DOI, descartando as demais. Vence o registro
    com melhor procedência (maior similaridade de título) e, no empate, o
    texto mais longo.

R8 e R9 servem ao princípio "No evidence, no claim" (§15): é preferível um
corpus menor e confiável a um corpus grande em que a Dra. Aris possa atribuir
um trecho ao artigo errado.

SALVAGUARDAS
------------
S1  Se o texto limpo ficar < 40% do original ou < 3.000 caracteres, os cortes
    agressivos são revertidos: aplica-se apenas R6 e o documento é marcado
    como `flagged` para revisão manual.
S2  Sem fronteira detectada, aplica-se apenas R6 (`no_boundary`).
S3  O original nunca é sobrescrito. A saída vai para data/clean_text/.
S4  Cada execução emite data/cleaning_report.csv com o efeito por regra.

Uso:
    python cleaner.py                  # limpa o corpus inteiro
    python cleaner.py --dry-run        # mede sem escrever nada
    python cleaner.py --sample 20      # amostra rápida
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

import pandas as pd

from config import settings

# Os padrões vivem em analyze_corpus.py porque foi lá que foram MEDIDOS.
# Importar em vez de copiar evita que diagnóstico e limpeza divirjam.
from analyze_corpus import (
    ADMIN_SECTIONS,
    BACKMATTER_MARKERS,
    BOILERPLATE_LINES,
    CONTENT_SECTIONS,
    DOI_PATTERN,
    HEADER_END_MARKERS,
    PUBLISHER_FOOTER,
    REFERENCE_LINK_TOKENS,
)

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")


# --------------------------------------------------------------------- #
# Parâmetros das salvaguardas (S1)
# --------------------------------------------------------------------- #

# Calibrados na distribuição real: o p01 do corpo é 42,8% e o menor corpo
# absoluto tem 1.192 caracteres. Estes limiares disparam em ~5 documentos.
MIN_BODY_RATIO = 0.40
MIN_BODY_CHARS = 3000

# A busca de fronteiras é restrita às pontas do documento para que um
# "Abstract" citado no meio do texto não seja confundido com o início do artigo.
BOUNDARY_SEARCH_MARGIN = 0.30

# R1 — títulos que denunciam um documento sem conteúdo científico próprio.
# O prefixo opcional importa: 2 dos 11 casos do corpus são "Author Correction:",
# que um `^correction` deixaria passar.
ERRATA_PATTERN = re.compile(
    r"^\s*((author|publisher|editorial)\s+)?"
    r"(correction|corrigend(um|a)|erratum|errata|retraction|withdrawal)\b",
    re.I,
)

# O título interno do PMC começa na linha seguinte a este marcador.
INTERNAL_TITLE_ANCHOR = "Add to search"

# ...mas pode ocupar VÁRIAS linhas: a extração HTML quebrou cada trecho em
# itálico (nomes de espécies, genes) em linha própria, então "Test of
# Arabidopsis Space Transcriptome" chega como "Test of" / "Arabidopsis" /
# "Space Transcriptome". O título termina onde começa a lista de autores, e
# esta é reconhecível: o PMC repete o nome de cada autor em duas linhas
# consecutivas idênticas.
MAX_TITLE_LINES = 12

# Todos os títulos de seção conhecidos: usados por R5 para saber onde uma
# seção administrativa TERMINA.
ALL_SECTION_PATTERNS = list(ADMIN_SECTIONS.values()) + list(CONTENT_SECTIONS.values())

# Linhas de serviço do PMC removidas por R6 (casamento por linha inteira).
BOILERPLATE_EXACT: Set[str] = set(BOILERPLATE_LINES) | set(REFERENCE_LINK_TOKENS)

# R8 — abaixo deste limiar, o documento não é confiavelmente o artigo que o
# metadata declara. 0,60 separa variação de grafia/truncamento (que fica acima)
# de artigo genuinamente diferente (que fica bem abaixo, tipicamente < 0,20).
MIN_TITLE_SIMILARITY = 0.60

STATUS_CLEANED = "cleaned"
STATUS_FLAGGED = "flagged"
STATUS_NO_BOUNDARY = "no_boundary"
STATUS_EXCLUDED = "excluded"
STATUS_TITLE_MISMATCH = "excluded_title_mismatch"
STATUS_DUPLICATE_DOI = "excluded_duplicate_doi"

EXCLUDED_STATUSES = {
    STATUS_EXCLUDED,
    STATUS_TITLE_MISMATCH,
    STATUS_DUPLICATE_DOI,
}


@dataclass
class CleaningResult:
    """O que aconteceu com um documento."""

    file_name: str
    status: str

    chars_before: int = 0
    chars_after: int = 0

    # Efeito isolado de cada regra, em caracteres
    removed_header: int = 0
    removed_backmatter: int = 0
    removed_sections: int = 0
    removed_boilerplate: int = 0

    header_marker: Optional[str] = None
    backmatter_marker: Optional[str] = None
    sections_removed: List[str] = field(default_factory=list)

    doi: Optional[str] = None
    journal: Optional[str] = None
    internal_title: Optional[str] = None

    # Sinalização de procedência (S4). Nenhuma regra age sobre isto.
    title_similarity: Optional[float] = None

    note: Optional[str] = None

    @property
    def pct_removed(self) -> float:
        if not self.chars_before:
            return 0.0
        return 100.0 * (self.chars_before - self.chars_after) / self.chars_before

    @property
    def body_ratio(self) -> float:
        if not self.chars_before:
            return 0.0
        return self.chars_after / self.chars_before


# --------------------------------------------------------------------- #
# Extração de metadados (R7)
# --------------------------------------------------------------------- #


def extract_header_metadata(lines: List[str]) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """
    Lê DOI, revista e título interno do cabeçalho — antes de qualquer corte.

    Estrutura observada em 100% do corpus:
        linha 0  <Revista>
        linha 1  . <data>;<volume>(<n>):<páginas>. doi:
        linha 2  <DOI>
        ...
        "Add to search"
        <título interno do artigo>

    Returns:
        (doi, journal, internal_title) — cada um None se ausente.
    """
    journal = None
    if lines and lines[0].strip() and not lines[0].strip().startswith("."):
        journal = lines[0].strip()[:200]

    doi = None
    for line in lines[:6]:
        candidate = line.strip()
        if DOI_PATTERN.match(candidate):
            doi = candidate
            break

    internal_title = _rebuild_internal_title(lines)
    return doi, journal, internal_title


def _rebuild_internal_title(lines: List[str]) -> Optional[str]:
    """
    Remonta o título interno, que pode vir partido em várias linhas.

    Para ao encontrar o início da lista de autores — duas linhas consecutivas
    idênticas, que é como o PMC apresenta cada autor — ou um número solto de
    afiliação.

    Sem esta remontagem, 23,6% dos títulos ficariam truncados em fragmentos
    como "Test of" ou "In situ"; com ela, a concordância com o metadata sobe
    de 63,3% para 83,9%.
    """
    stripped = [line.strip() for line in lines]
    try:
        index = stripped.index(INTERNAL_TITLE_ANCHOR) + 1
    except ValueError:
        return None

    parts: List[str] = []
    while index < len(stripped) and len(parts) < MAX_TITLE_LINES:
        line = stripped[index]
        if not line:
            index += 1
            continue
        # Início dos autores: nome repetido em duas linhas seguidas.
        if parts and index + 1 < len(stripped) and line == stripped[index + 1]:
            break
        # Marcador de afiliação.
        if parts and re.fullmatch(r"\d{1,3}", line):
            break
        parts.append(line)
        index += 1

    return " ".join(parts)[:300] if parts else None


def title_similarity(metadata_title: str, internal_title: Optional[str]) -> Optional[float]:
    """
    Similaridade entre o título do metadata e o do documento (0..1).

    Serve só para SINALIZAR no relatório. Nenhuma regra age sobre este valor:
    uma divergência indica erro de ingestão (a URL do PMC não corresponde ao
    título registrado), que é problema de outra issue.
    """
    if not internal_title:
        return None

    def normalize(value: str) -> str:
        return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", "", str(value).lower())).strip()

    return round(
        SequenceMatcher(None, normalize(metadata_title)[:90], normalize(internal_title)[:90]).ratio(),
        3,
    )


def is_non_scientific(internal_title: Optional[str]) -> bool:
    """R1 — o documento é uma errata/correção/retratação?"""
    return bool(internal_title and ERRATA_PATTERN.match(internal_title))


# --------------------------------------------------------------------- #
# Localização de fronteiras (R3, R4)
# --------------------------------------------------------------------- #


def find_header_end(stripped: List[str]) -> Tuple[Optional[int], Optional[str]]:
    """R3 — índice da primeira linha de conteúdo real."""
    limit = max(int(len(stripped) * BOUNDARY_SEARCH_MARGIN), 1)
    for pattern in HEADER_END_MARKERS:
        for index in range(min(limit, len(stripped))):
            if pattern.match(stripped[index]):
                return index, pattern.pattern
    return None, None


def find_backmatter_start(stripped: List[str]) -> Tuple[Optional[int], Optional[str]]:
    """R4 — índice onde começa o backmatter."""
    start = max(int(len(stripped) * BOUNDARY_SEARCH_MARGIN), 1)
    best_index: Optional[int] = None
    best_pattern: Optional[str] = None
    for pattern in BACKMATTER_MARKERS:
        for index in range(start, len(stripped)):
            if pattern.match(stripped[index]):
                if best_index is None or index < best_index:
                    best_index, best_pattern = index, pattern.pattern
                break
    return best_index, best_pattern


# --------------------------------------------------------------------- #
# Remoção de seções administrativas (R5)
# --------------------------------------------------------------------- #


def find_admin_section_ranges(
    stripped: List[str], start: int, end: int
) -> List[Tuple[int, int, str]]:
    """
    R5 — localiza seções administrativas no intervalo [start, end).

    Cada seção vai do seu título até o PRÓXIMO título de seção conhecido —
    administrativo ou científico. Parar num título científico é o que impede
    que um "Funding" no meio do artigo leve embora a Discussão que vem depois.

    Returns:
        Lista de (início, fim, nome_da_seção), sem sobreposição.
    """
    ranges: List[Tuple[int, int, str]] = []

    index = start
    while index < end:
        line = stripped[index]
        matched_name = None
        for name, pattern in ADMIN_SECTIONS.items():
            if pattern.match(line):
                matched_name = name
                break

        if matched_name is None:
            index += 1
            continue

        # Avança até o próximo título de seção conhecido.
        section_end = end
        for lookahead in range(index + 1, end):
            if any(pattern.match(stripped[lookahead]) for pattern in ALL_SECTION_PATTERNS):
                section_end = lookahead
                break

        ranges.append((index, section_end, matched_name))
        index = section_end

    return ranges


# --------------------------------------------------------------------- #
# Limpeza de um documento
# --------------------------------------------------------------------- #


def _chars(lines: List[str], indices: Set[int]) -> int:
    """Soma de caracteres (com o \\n) das linhas nos índices dados."""
    return sum(len(lines[i]) + 1 for i in indices if 0 <= i < len(lines))


def clean_text(text: str, file_name: str = "") -> Tuple[str, CleaningResult]:
    """
    Aplica R3–R7 a um texto e devolve (texto_limpo, relatório).

    R1 e R2 operam no nível do corpus, não do documento; ver clean_corpus.
    """
    lines = text.splitlines()
    stripped = [line.strip() for line in lines]

    doi, journal, internal_title = extract_header_metadata(lines)  # R7 — antes de cortar

    result = CleaningResult(
        file_name=file_name,
        status=STATUS_CLEANED,
        chars_before=len(text),
        doi=doi,
        journal=journal,
        internal_title=internal_title,
    )

    # --- R6: boilerplate de linha (sempre aplicado, é o mínimo seguro) ---
    boilerplate: Set[int] = {
        index for index, line in enumerate(stripped) if line in BOILERPLATE_EXACT
    }
    # Rodapé do publisher, sempre nas últimas linhas.
    for index in range(max(0, len(stripped) - 3), len(stripped)):
        if PUBLISHER_FOOTER.match(stripped[index]):
            boilerplate.add(index)

    # --- R3 / R4: fronteiras ---
    header_end, header_marker = find_header_end(stripped)
    backmatter_start, backmatter_marker = find_backmatter_start(stripped)
    result.header_marker = header_marker
    result.backmatter_marker = backmatter_marker

    # Detecção incoerente (backmatter antes do cabeçalho) é tratada como ausente.
    if (
        header_end is not None
        and backmatter_start is not None
        and backmatter_start <= header_end
    ):
        header_end = backmatter_start = None
        result.note = "fronteiras incoerentes; tratadas como ausentes"

    # S2 — sem nenhuma fronteira, só R6.
    if header_end is None and backmatter_start is None:
        keep = [line for index, line in enumerate(lines) if index not in boilerplate]
        cleaned = "\n".join(keep).strip() + "\n"
        result.status = STATUS_NO_BOUNDARY
        result.removed_boilerplate = _chars(lines, boilerplate)
        result.chars_after = len(cleaned)
        return cleaned, result

    body_start = header_end if header_end is not None else 0
    body_end = backmatter_start if backmatter_start is not None else len(lines)

    header_indices = set(range(0, body_start))
    backmatter_indices = set(range(body_end, len(lines)))

    # --- R5: seções administrativas dentro do corpo ---
    section_indices: Set[int] = set()
    section_names: List[str] = []
    for section_start, section_end, name in find_admin_section_ranges(
        stripped, body_start, body_end
    ):
        section_indices.update(range(section_start, section_end))
        section_names.append(name)

    removed = header_indices | backmatter_indices | section_indices | boilerplate
    keep = [line for index, line in enumerate(lines) if index not in removed]
    cleaned = "\n".join(keep).strip() + "\n"

    # --- S1: a rede de segurança ---
    if len(cleaned) < MIN_BODY_CHARS or len(cleaned) < MIN_BODY_RATIO * len(text):
        conservative = [line for index, line in enumerate(lines) if index not in boilerplate]
        cleaned = "\n".join(conservative).strip() + "\n"
        result.status = STATUS_FLAGGED

        # Quanto teria sobrado se o corte agressivo tivesse sido mantido.
        # Serve só para a mensagem de revisão — por isso o guarda explícito
        # contra divisão por zero, em vez do idiom `len(text) and ...`, que
        # era compacto e ilegível.
        chars_removed = _chars(lines, removed)
        if len(text) > 0:
            would_remain = 100.0 * (len(text) - chars_removed) / len(text)
        else:
            would_remain = 0.0

        result.note = (
            f"S1: corte agressivo revertido "
            f"(restariam {would_remain:.0f}% do original); apenas R6 aplicado"
        )
        result.removed_boilerplate = _chars(lines, boilerplate)
        result.chars_after = len(cleaned)
        return cleaned, result

    result.removed_header = _chars(lines, header_indices - boilerplate)
    result.removed_backmatter = _chars(lines, backmatter_indices - boilerplate)
    result.removed_sections = _chars(lines, section_indices - boilerplate - backmatter_indices)
    result.removed_boilerplate = _chars(lines, boilerplate)
    result.sections_removed = section_names
    result.chars_after = len(cleaned)

    return cleaned, result


# --------------------------------------------------------------------- #
# Limpeza do corpus
# --------------------------------------------------------------------- #


def clean_corpus(
    output_dir: Path,
    sample: Optional[int] = None,
    dry_run: bool = False,
) -> Tuple[List[CleaningResult], pd.DataFrame]:
    """
    Executa a limpeza sobre todo o corpus.

    Returns:
        (resultados por documento, metadata limpo)
    """
    frame = pd.read_csv(settings.metadata_file)
    rows_before = len(frame)

    # --- R2: deduplicar por arquivo, mantendo o título mais completo ---
    # A chave é o caminho NORMALIZADO em minúsculas: o corpus tem um par de
    # registros que difere só na caixa ("...A roadmap..." / "...a roadmap..."),
    # e no Windows os dois apontam para o mesmo arquivo — sem normalizar, o
    # segundo sobrescreveria o primeiro em silêncio.
    frame["_title_len"] = frame["title"].astype(str).str.len()
    frame["_key"] = (
        frame["local_path"].astype(str).str.replace("\\", "/", regex=False).str.lower()
    )
    frame = (
        frame.sort_values("_title_len", ascending=False)
        .drop_duplicates("_key", keep="first")
        .drop(columns=["_title_len", "_key"])
        .sort_index()
    )
    duplicates_dropped = rows_before - len(frame)

    if sample:
        frame = frame.head(sample)

    # ---------------- Passada 1: limpar cada documento ---------------- #
    # Os filtros de corpus (R8, R9) dependem do DOI e do título interno de
    # TODOS os documentos, então a escrita fica para a segunda passada.
    results: List[CleaningResult] = []
    pending: List[Tuple[CleaningResult, Dict, str]] = []  # (resultado, linha, texto limpo)

    for _, row in frame.iterrows():
        source = settings.resolve_corpus_path(row["local_path"])
        if not source.exists():
            results.append(
                CleaningResult(
                    file_name=Path(str(row["local_path"])).name,
                    status=STATUS_EXCLUDED,
                    note="arquivo não encontrado",
                )
            )
            continue

        text = source.read_text(encoding="utf-8")
        cleaned, result = clean_text(text, file_name=source.name)
        result.title_similarity = title_similarity(row["title"], result.internal_title)

        # --- R1: erratas são excluídas do corpus, não limpas ---
        if is_non_scientific(result.internal_title):
            result.status = STATUS_EXCLUDED
            result.chars_after = 0
            result.note = f"documento não-científico: {result.internal_title[:60]}"
            results.append(result)
            continue

        # --- R8: procedência não confiável ---
        if (
            result.title_similarity is not None
            and result.title_similarity < MIN_TITLE_SIMILARITY
        ):
            result.status = STATUS_TITLE_MISMATCH
            result.chars_after = 0
            result.note = (
                f"título divergente (sim={result.title_similarity:.2f}): "
                f"metadata diz '{str(row['title'])[:40]}', "
                f"documento é '{(result.internal_title or '')[:40]}'"
            )
            results.append(result)
            continue

        results.append(result)
        pending.append((result, row.to_dict(), cleaned))

    # ---------------- R9: uma única cópia por DOI ---------------- #
    # Vence a melhor procedência; no empate, o texto mais longo.
    best_by_doi: Dict[str, CleaningResult] = {}
    for result, _, _ in pending:
        if not result.doi:
            continue
        current = best_by_doi.get(result.doi)
        if current is None:
            best_by_doi[result.doi] = result
            continue
        candidate_rank = (result.title_similarity or 0.0, result.chars_after)
        current_rank = (current.title_similarity or 0.0, current.chars_after)
        if candidate_rank > current_rank:
            best_by_doi[result.doi] = result

    # ---------------- Passada 2: escrever os aprovados ---------------- #
    if not dry_run:
        # O diretório é saída derivada: recriá-lo evita deixar para trás
        # arquivos de documentos que agora foram excluídos. O corpus original
        # nunca é tocado (S3).
        if output_dir.exists():
            for stale in output_dir.glob("*.txt"):
                stale.unlink()
        output_dir.mkdir(parents=True, exist_ok=True)

    metadata_rows: List[Dict] = []
    for result, row, cleaned in pending:
        if result.doi and best_by_doi.get(result.doi) is not result:
            winner = best_by_doi[result.doi]
            result.status = STATUS_DUPLICATE_DOI
            result.chars_after = 0
            result.note = f"DOI {result.doi} já representado por '{winner.file_name[:44]}'"
            continue

        file_name = Path(str(row["local_path"]).replace("\\", "/")).name
        if not dry_run:
            (output_dir / file_name).write_text(cleaned, encoding="utf-8")

        metadata_rows.append(
            {
                "title": row["title"],
                "source_url": row["source_url"],
                "local_path": row["local_path"],
                "extraction_method": row["extraction_method"],
                "doi": result.doi or "",
                "journal": result.journal or "",
                "clean_path": str(Path("data") / output_dir.name / file_name).replace("\\", "/"),
                "cleaning_status": result.status,
                "title_similarity": result.title_similarity
                if result.title_similarity is not None
                else "",
            }
        )

    metadata = pd.DataFrame(metadata_rows)

    # Verificação da R9: depois do filtro, nenhum DOI pode repetir.
    if not metadata.empty:
        has_doi = metadata["doi"].astype(str).str.strip() != ""
        remaining = int((metadata["doi"].duplicated(keep=False) & has_doi).sum())
        metadata.attrs["duplicate_doi_remaining"] = remaining

    metadata.attrs["duplicates_dropped"] = duplicates_dropped
    return results, metadata


# --------------------------------------------------------------------- #
# Relatório
# --------------------------------------------------------------------- #


def build_report(results: List[CleaningResult]) -> pd.DataFrame:
    """S4 — relatório por documento."""
    return pd.DataFrame(
        [
            {
                "file_name": r.file_name,
                "status": r.status,
                "chars_before": r.chars_before,
                "chars_after": r.chars_after,
                "pct_removed": round(r.pct_removed, 2),
                "removed_header": r.removed_header,
                "removed_backmatter": r.removed_backmatter,
                "removed_sections": r.removed_sections,
                "removed_boilerplate": r.removed_boilerplate,
                "sections_removed": "|".join(r.sections_removed),
                "doi": r.doi or "",
                "journal": r.journal or "",
                "internal_title": r.internal_title or "",
                "title_similarity": r.title_similarity if r.title_similarity is not None else "",
                "title_mismatch": (
                    r.title_similarity is not None and r.title_similarity < 0.60
                ),
                "note": r.note or "",
            }
            for r in results
        ]
    )


def print_summary(
    results: List[CleaningResult],
    metadata: pd.DataFrame,
    output_dir: Path,
    dry_run: bool,
) -> None:
    bar = "=" * 78
    total = len(results)
    kept = [r for r in results if r.status not in EXCLUDED_STATUSES]

    print(f"\n{bar}")
    print("SPACEBIO-012.5 — Limpeza do corpus" + ("  [DRY-RUN]" if dry_run else ""))
    print(bar)

    by_status: Dict[str, int] = {}
    for r in results:
        by_status[r.status] = by_status.get(r.status, 0) + 1

    print(f"\n1. RESULTADO POR DOCUMENTO\n{'-' * 78}")
    labels = {
        STATUS_CLEANED: "limpos (todas as regras)",
        STATUS_FLAGGED: "S1 disparou — só R6, revisar",
        STATUS_NO_BOUNDARY: "S2 sem fronteira — só R6",
        STATUS_EXCLUDED: "R1 excluídos (errata / ausente)",
        STATUS_TITLE_MISMATCH: "R8 excluídos (título divergente)",
        STATUS_DUPLICATE_DOI: "R9 excluídos (DOI duplicado)",
    }
    for status, label in labels.items():
        count = by_status.get(status, 0)
        print(f"   {label:<34}{count:>5} ({100*count/total:>5.1f}%)")
    print(f"   {'TOTAL':<34}{total:>5}")

    print(f"\n2. VOLUME\n{'-' * 78}")
    before = sum(r.chars_before for r in kept)
    after = sum(r.chars_after for r in kept)
    print(f"   Antes                             {before/1e6:>8.1f} M caracteres")
    print(f"   Depois                            {after/1e6:>8.1f} M caracteres")
    print(f"   Removido                          {(before-after)/1e6:>8.1f} M ({100*(before-after)/before:.1f}%)")

    print(f"\n3. CONTRIBUIÇÃO DE CADA REGRA\n{'-' * 78}")
    contributions = [
        ("R3 cabeçalho", sum(r.removed_header for r in results)),
        ("R4 backmatter", sum(r.removed_backmatter for r in results)),
        ("R5 seções nomeadas", sum(r.removed_sections for r in results)),
        ("R6 boilerplate de linha", sum(r.removed_boilerplate for r in results)),
    ]
    removed_total = sum(value for _, value in contributions) or 1
    for label, value in contributions:
        print(
            f"   {label:<28}{value/1e6:>7.2f} M  ({100*value/removed_total:>5.1f}% do removido)"
        )

    print(f"\n4. R7 — METADADOS RECUPERADOS\n{'-' * 78}")
    with_doi = sum(1 for r in kept if r.doi)
    with_journal = sum(1 for r in kept if r.journal)
    print(f"   DOI extraído                      {with_doi:>5} / {len(kept)} ({100*with_doi/len(kept):.1f}%)")
    print(f"   Revista extraída                  {with_journal:>5} / {len(kept)} ({100*with_journal/len(kept):.1f}%)")

    remaining = metadata.attrs.get("duplicate_doi_remaining", 0)
    print(
        f"   DOIs repetidos após R9            {remaining:>5} "
        f"{'(esperado: 0)' if remaining == 0 else '<-- FALHA DA R9'}"
    )

    mismatch = [r for r in results if r.status == STATUS_TITLE_MISMATCH]
    duplicates = [r for r in results if r.status == STATUS_DUPLICATE_DOI]

    if mismatch:
        print(f"\n5. R8 — PROCEDÊNCIA NÃO CONFIÁVEL ({len(mismatch)} excluídos)\n{'-' * 78}")
        print("   O título do metadata não corresponde ao artigo baixado.")
        print("   Causa provável no scraper — ver docs/issues/SPACEBIO-007.1-title-divergence.md")
        for r in sorted(mismatch, key=lambda x: x.title_similarity or 0)[:5]:
            print(f"   [{r.title_similarity:.2f}] {(r.internal_title or '')[:62]}")
        if len(mismatch) > 5:
            print(f"   ... e mais {len(mismatch) - 5}")

    if duplicates:
        print(f"\n6. R9 — CÓPIAS DESCARTADAS ({len(duplicates)})\n{'-' * 78}")
        for r in duplicates[:5]:
            print(f"   {r.doi}  {r.file_name[:46]}")
        if len(duplicates) > 5:
            print(f"   ... e mais {len(duplicates) - 5}")

    excluded = [r for r in results if r.status == STATUS_EXCLUDED]
    if excluded:
        print(f"\n7. R1 — DOCUMENTOS EXCLUÍDOS ({len(excluded)})\n{'-' * 78}")
        for r in excluded:
            print(f"   - {(r.internal_title or r.note or '')[:70]}")

    flagged = [r for r in results if r.status in (STATUS_FLAGGED, STATUS_NO_BOUNDARY)]
    if flagged:
        print(f"\n8. PARA REVISÃO MANUAL ({len(flagged)})\n{'-' * 78}")
        for r in flagged[:12]:
            print(f"   - [{r.status}] {r.file_name[:48]:<50} {r.chars_before:>7} chars")
        if len(flagged) > 12:
            print(f"   ... e mais {len(flagged) - 12}")

    print(f"\n9. SAÍDA\n{'-' * 78}")
    if dry_run:
        print("   DRY-RUN: nenhum arquivo foi escrito.")
    else:
        print(f"   Textos limpos                     {output_dir}")
        print(f"   Metadata atualizado               {settings.data_dir / 'metadata_clean.csv'}")
        print(f"   Relatório                         {settings.data_dir / 'cleaning_report.csv'}")
    print(f"   Original preservado (S3)          {settings.data_dir / 'processed_text'}")
    print(f"\n{bar}")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="SPACEBIO-012.5 — limpeza do corpus")
    parser.add_argument("--output-dir", default="clean_text", help="subdiretório em data/")
    parser.add_argument("--sample", type=int, help="processar apenas N documentos")
    parser.add_argument("--dry-run", action="store_true", help="medir sem escrever")
    args = parser.parse_args(argv)

    output_dir = settings.data_dir / args.output_dir

    print(f"Limpando corpus a partir de {settings.metadata_file}...")
    results, metadata = clean_corpus(output_dir, sample=args.sample, dry_run=args.dry_run)

    report = build_report(results)
    if not args.dry_run:
        metadata.to_csv(settings.data_dir / "metadata_clean.csv", index=False, encoding="utf-8")
        report.to_csv(settings.data_dir / "cleaning_report.csv", index=False, encoding="utf-8")

    print(f"\nR2 — linhas duplicadas removidas do metadata: {metadata.attrs.get('duplicates_dropped', 0)}")
    print_summary(results, metadata, output_dir, args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
