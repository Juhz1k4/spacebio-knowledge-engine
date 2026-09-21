"""
SPACEBIO-014 — Scientific Evidence Contract

Define o formato único de resposta da Dra. Aris: uma afirmação sempre
acompanhada dos trechos que a sustentam, com procedência verificável.

Formato do §14 do Master Briefing:

    {
      "answer": "...",
      "sources": [{publication_id, title, url, doi, passage, section, page, relevance}],
      "entities": [],
      "related_topics": [],
      "retrieval": {"chunks_considered": 20, "chunks_used": 5}
    }

Três campos foram acrescentados ao contrato original, e a razão é a mesma nos
três: o §15 exige que a assistente DECLARE quando não tem evidência, e isso
precisa ser legível por máquina, não só por humano.

    grounded   a resposta se sustenta em evidência recuperada?
    warnings   o que o sistema notou e o leitor precisa saber
    citations  quais fontes o texto realmente citou, verificado

`chunks_used` não é estimado: é o número de fontes que a resposta de fato
citou, medido por `verify_citations`. Uma citação a uma fonte inexistente é
detectada e reportada, nunca silenciada.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Sequence

from pydantic import BaseModel, Field

from retrieval import HybridResult

# Mensagem padrão quando o corpus não sustenta uma resposta. É deliberadamente
# explícita: o §9.2 identifica o fallback silencioso para conhecimento
# paramétrico como a principal erosão de confiança do sistema atual.
INSUFFICIENT_EVIDENCE = (
    "Não encontrei evidência suficiente no corpus do SpaceBio para responder a "
    "essa pergunta. Prefiro dizer isso a arriscar uma resposta que eu não "
    "consiga sustentar com os artigos que temos indexados."
)

# Citações no texto têm a forma [1], [2], [1, 3] ou [1][2].
CITATION_PATTERN = re.compile(r"\[(\d+(?:\s*,\s*\d+)*)\]")


class EvidenceSource(BaseModel):
    """Uma fonte citável — trecho real, com rastro até a publicação."""

    citation_index: int = Field(
        ...,
        description="O número usado no texto da resposta: [1], [2], ...",
        ge=1,
    )
    publication_id: str = Field(..., description="Identificador da publicação (título)")
    title: str
    url: Optional[str] = Field(default=None, description="URL do artigo no PMC")
    doi: Optional[str] = Field(
        default=None,
        description="DOI real ou None. Nunca fabricado (§15.4).",
    )
    journal: Optional[str] = None
    passage: str = Field(..., description="O trecho literal que sustenta a afirmação")
    section: Optional[str] = None
    page: Optional[int] = Field(
        default=None,
        description="Sempre None neste corpus: extração HTML não tem páginas (§15.5).",
    )
    relevance: float
    chunk_id: str
    channels: List[str] = Field(
        default_factory=list,
        description="Canais que recuperaram o trecho: semantic, lexical",
    )
    cited: bool = Field(
        default=False,
        description="A resposta realmente citou esta fonte? Verificado, não presumido.",
    )


class RetrievalTrace(BaseModel):
    """
    Como a evidência foi obtida.

    Existe para tornar a recuperação auditável: sem isto, não há como
    distinguir "o corpus não tem" de "a busca falhou".
    """

    query: str
    chunks_considered: int = Field(..., description="Trechos avaliados pelo retriever")
    chunks_used: int = Field(..., description="Trechos efetivamente citados na resposta")
    top_score: Optional[float] = Field(
        default=None, description="Similaridade da melhor passagem (cosseno)"
    )
    evidence_threshold: float = Field(
        ..., description="Limiar abaixo do qual a resposta é recusada"
    )
    channels: List[str] = Field(default_factory=list)


class EvidenceAnswer(BaseModel):
    """A resposta completa da Dra. Aris, no contrato do §14."""

    answer: str
    sources: List[EvidenceSource] = Field(default_factory=list)
    entities: List[Dict[str, Any]] = Field(
        default_factory=list,
        description=(
            "Entidades da ontologia (§16) mencionadas nas passagens citadas, "
            "com canonical, label e em quantos trechos aparecem."
        ),
    )
    related_topics: List[str] = Field(default_factory=list)
    retrieval: RetrievalTrace
    grounded: bool = Field(
        ...,
        description="A resposta está sustentada em evidência do corpus?",
    )
    warnings: List[str] = Field(default_factory=list)

    @property
    def cited_sources(self) -> List[EvidenceSource]:
        return [source for source in self.sources if source.cited]


def build_sources(results: Sequence[HybridResult]) -> List[EvidenceSource]:
    """
    Converte o resultado do retriever híbrido em fontes numeradas.

    A numeração é 1-based e estável: é o índice que o prompt apresenta ao
    modelo e que o modelo usa para citar.
    """
    sources: List[EvidenceSource] = []
    for position, result in enumerate(results, start=1):
        passage = result.passage
        sources.append(
            EvidenceSource(
                citation_index=position,
                publication_id=passage.publication_title,
                title=passage.publication_title,
                url=passage.source_url,
                doi=passage.doi,
                journal=passage.journal,
                passage=passage.text,
                section=passage.section,
                page=passage.page,
                relevance=round(result.score, 6),
                chunk_id=passage.chunk_id,
                channels=result.matched_channels,
            )
        )
    return sources


def extract_citations(answer: str) -> List[int]:
    """
    Extrai os índices citados no texto, em ordem de aparição e sem repetir.

    Aceita `[1]`, `[2]`, `[1, 3]` e sequências como `[1][2]`.
    """
    found: List[int] = []
    for match in CITATION_PATTERN.finditer(answer):
        for part in match.group(1).split(","):
            index = int(part.strip())
            if index not in found:
                found.append(index)
    return found


def verify_citations(
    answer: str, sources: List[EvidenceSource]
) -> Dict[str, Any]:
    """
    Confere que as citações do texto apontam para fontes que existem.

    Esta é a diferença entre pedir grounding a um modelo e VERIFICAR que ele
    obedeceu. Um índice fora do intervalo é uma citação fabricada — o modo de
    falha mais perigoso de um sistema de evidência, porque parece rigor.

    Efeito colateral: marca `cited=True` nas fontes efetivamente usadas.

    Returns:
        {"cited": [...], "invalid": [...], "used": int}
    """
    citations = extract_citations(answer)
    valid_range = range(1, len(sources) + 1)

    cited = [index for index in citations if index in valid_range]
    invalid = [index for index in citations if index not in valid_range]

    for source in sources:
        source.cited = source.citation_index in cited

    return {"cited": cited, "invalid": invalid, "used": len(cited)}


def insufficient_evidence_answer(
    query: str,
    threshold: float,
    considered: int = 0,
    top_score: Optional[float] = None,
    reason: Optional[str] = None,
) -> EvidenceAnswer:
    """
    Constrói a resposta para quando o corpus não sustenta a pergunta.

    Devolve o contrato completo, não uma exceção: o cliente recebe a mesma
    estrutura de sempre, com `grounded=False` e `sources` vazio. A ausência de
    evidência é um resultado legítimo, não um erro.
    """
    warnings = [reason] if reason else []
    if top_score is not None and top_score < threshold:
        warnings.append(
            f"Melhor similaridade {top_score:.3f} abaixo do limiar {threshold:.2f}."
        )

    return EvidenceAnswer(
        answer=INSUFFICIENT_EVIDENCE,
        sources=[],
        retrieval=RetrievalTrace(
            query=query,
            chunks_considered=considered,
            chunks_used=0,
            top_score=round(top_score, 4) if top_score is not None else None,
            evidence_threshold=threshold,
            channels=[],
        ),
        grounded=False,
        warnings=warnings,
    )
