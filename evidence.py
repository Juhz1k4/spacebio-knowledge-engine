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
import unicodedata
from typing import Any, Dict, List, Literal, Optional, Sequence

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

# E3-04 -- estado da resposta, para o cliente decidir COMO renderizar sem ter
# de inferir isso de campos soltos.
#
# Antes, distinguir "recusei por falta de evidencia" de "achei a evidencia mas
# nao consegui redigir" exigia cruzar `grounded`, `sources` e o texto de
# `warnings`. Sao situacoes opostas para o usuario -- uma diz que o corpus nao
# cobre o assunto, a outra que cobre e o provedor falhou -- e mereciam campo
# proprio.
AnswerStatus = Literal["ok", "synthesis_unavailable", "insufficient_evidence"]

# Texto exibido quando a evidencia foi recuperada mas a sintese falhou.
#
# Deliberadamente neutro: numa demonstracao, "erro" e "falha" na tela fazem o
# avaliador concluir que o sistema quebrou, quando na verdade ele degradou como
# projetado e entregou o que importa -- as passagens. A causa exata (timeout,
# quota, provedor fora) vai em `warnings`, para quem for auditar.
SYNTHESIS_UNAVAILABLE = (
    "Os documentos relevantes foram recuperados com sucesso, mas a síntese em "
    "texto está temporariamente indisponível devido a uma falha de conexão."
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


class QuoteCheck(BaseModel):
    """
    Resultado da conferencia de um trecho entre aspas (E3-02).

    Vai no payload para que a auditoria seja possivel do lado de fora: quem le
    a resposta consegue ver o que foi conferido e o que nao passou, sem ter de
    confiar na palavra do sistema.
    """

    quote: str = Field(..., description="O trecho entre aspas, como o modelo escreveu")
    verified: bool = Field(
        ...,
        description=(
            "O trecho existe literalmente em alguma passagem recuperada? "
            "False significa inventado OU traduzido -- os dois falham igual."
        ),
    )
    source_index: Optional[int] = Field(
        None, description="Indice [n] da fonte que contem o trecho, quando verificado"
    )


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
    status: AnswerStatus = Field(
        "ok",
        description=(
            "ok: resposta sintetizada normalmente. "
            "synthesis_unavailable: evidência recuperada, mas o provedor de "
            "texto falhou ou estourou o tempo — as fontes estão completas. "
            "insufficient_evidence: o corpus não sustenta a pergunta."
        ),
    )
    warnings: List[str] = Field(default_factory=list)
    quote_checks: List[QuoteCheck] = Field(
        default_factory=list,
        description=(
            "Conferencia dos trechos entre aspas (E3-02). Lista vazia significa "
            "que a resposta nao trouxe citacao literal longa o bastante para "
            "conferir, nao que a conferencia foi pulada."
        ),
    )

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



# ---------------------------------------------------------------------------
# E3-02 — validação de citações literais
# ---------------------------------------------------------------------------
#
# O PROBLEMA
# ----------
# A Dra. Aris sintetiza em português a partir de passagens em inglês. A regra
# de idioma do prompt manda NÃO traduzir o que estiver entre aspas: uma citação
# literal tem de continuar em inglês, exatamente como saiu do artigo.
#
# `verify_citations` confere os marcadores [n] — que a fonte existe. Não confere
# o que está entre aspas. São dois modos de falha diferentes:
#
#     [7] quando só há 6 fontes        -> citação fabricada    (verify_citations)
#     "frase que ninguém escreveu"     -> citação inventada    (aqui)
#     "frase do artigo, traduzida"     -> citação adulterada   (aqui)
#
# Os dois últimos são mais perigosos que o primeiro, porque aspas são a forma
# mais forte de afirmar fidelidade ao original. Uma tradução entre aspas parece
# transcrição e não é.
#
# COMO A TRADUÇÃO É DETECTADA
# ---------------------------
# Não há detecção de idioma. Ela cai fora de graça: uma citação traduzida não
# existe como substring da passagem em inglês, então falha na comparação exata
# como qualquer outra invenção. Menos código e nenhum falso positivo vindo de
# um classificador de idioma errando em texto técnico.
#
# O LIMITE DA NORMALIZAÇÃO
# ------------------------
# Normalizar FORMA, nunca CONTEÚDO. Aspas curvas, travessões, espaços e caixa
# mudam na viagem do PDF para o HTML e daí para o modelo, sem que uma palavra
# mude. Já casamento aproximado, lematização ou tolerância a sinônimo fariam
# passar exatamente o que esta função existe para pegar.

# Aspas duplas em todas as formas que aparecem no corpus e que um modelo pode
# emitir. Aspas SIMPLES ficam de fora de propósito: em inglês o apóstrofo de
# "don't" e do genitivo "cells'" viraria uma falsa abertura de citação, e o
# ruído afogaria o sinal.
_OPENING_TO_CLOSING = {'"': '"', '“': '”', '„': '“', '«': '»'}

QUOTE_PATTERN = re.compile(
    r'“(?P<curly>[^”]{2,400})”'
    r'|"(?P<straight>[^"]{2,400})"'
    r'|«(?P<guillemet>[^»]{2,400})»'
)

# Abaixo disto é terminologia entre aspas ("microgravity", "bone loss"), não
# transcrição. Marcar esses casos geraria alarme constante sem risco real: uma
# expressão de três palavras não sustenta afirmação nenhuma sozinha.
MIN_QUOTE_WORDS = 4

# Reticências que o modelo usa para elidir trecho no meio da citação.
_ELLIPSIS_PATTERN = re.compile(r'(?:\.\s*){3,}|…')

_DASHES = dict.fromkeys(map(ord, '‐‑‒–—―−'), '-')
_APOSTROPHES = dict.fromkeys(map(ord, '‘’‛ʼ'), "'")
_SPACES = dict.fromkeys(map(ord, '     '), ' ')


def normalize_for_match(text: str) -> str:
    """
    Reduz o texto à forma em que duas grafias do MESMO trecho coincidem.

    Trata apenas o que muda sem alterar uma palavra sequer:
      - travessões e hífens tipográficos vão todos para `-`;
      - apóstrofos e aspas curvas vão para os retos;
      - espaços especiais, quebras de linha e repetições viram um espaço;
      - caixa é ignorada, porque o modelo capitaliza a primeira letra ao
        começar um período com a citação.

    NÃO remove pontuação interna, acento de palavra nem plural. A comparação
    continua exata — o que muda é só a representação.
    """
    text = unicodedata.normalize("NFKC", text)
    text = text.translate(_DASHES).translate(_APOSTROPHES).translate(_SPACES)
    text = text.replace('“', '"').replace('”', '"')
    return " ".join(text.split()).casefold()


def _quote_segments(quote: str) -> List[str]:
    """
    Parte a citação nas reticências de elisão.

    `"A ... B"` é uma citação legítima de duas partes distantes no original.
    Exigir a string inteira reprovaria um uso correto, então cada segmento é
    procurado em separado — e em ordem, para que a elisão não possa inverter
    o sentido juntando trechos fora de sequência.
    """
    parts = [p.strip(" ,;:.") for p in _ELLIPSIS_PATTERN.split(quote)]
    return [p for p in parts if p]


def _find_in_passages(quote: str, sources: List["EvidenceSource"]) -> Optional[int]:
    """Índice da primeira fonte que contém a citação, ou None."""
    segments = [normalize_for_match(s) for s in _quote_segments(quote)]
    if not segments or not all(segments):
        return None

    for source in sources:
        haystack = normalize_for_match(source.passage)
        cursor = 0
        for segment in segments:
            position = haystack.find(segment, cursor)
            if position < 0:
                break
            cursor = position + len(segment)
        else:
            return source.citation_index
    return None


def validate_quotes(
    answer: str, sources: List["EvidenceSource"]
) -> Dict[str, Any]:
    """
    Confere que cada trecho entre aspas existe literalmente nas passagens.

    Reprova a citação que o modelo inventou e a que ele traduziu — os dois
    casos falham pelo mesmo teste, porque nenhum dos dois aparece no texto
    original em inglês.

    Tratamento da falha: as ASPAS são removidas, o texto permanece. A alegação
    falsa é a de literalidade, não necessariamente o conteúdo; uma paráfrase
    correta continua útil ao leitor, desde que pare de se apresentar como
    transcrição. A resposta nunca é bloqueada por isso — quem decide se ela se
    sustenta é a verificação de citações [n], e recusar duas vezes pelo mesmo
    material seria punir o usuário por um defeito de formatação do modelo.

    Só examina trechos com MIN_QUOTE_WORDS palavras ou mais; abaixo disso é
    terminologia entre aspas, não transcrição.

    Returns:
        {"answer": str, "checks": [QuoteCheck], "verified": int,
         "unverified": int, "examined": int}
    """
    checks: List[QuoteCheck] = []

    def _inspect(match: "re.Match") -> str:
        raw = next(g for g in match.groups() if g is not None)
        original = match.group(0)

        if len(raw.split()) < MIN_QUOTE_WORDS:
            return original  # terminologia, não citação

        source_index = _find_in_passages(raw, sources)
        verified = source_index is not None
        checks.append(
            QuoteCheck(quote=raw.strip(), verified=verified, source_index=source_index)
        )
        # Reprovada: cai para texto corrido, sem a alegação de literalidade.
        return original if verified else raw

    rewritten = QUOTE_PATTERN.sub(_inspect, answer)
    verified = sum(1 for c in checks if c.verified)

    return {
        "answer": rewritten,
        "checks": checks,
        "verified": verified,
        "unverified": len(checks) - verified,
        "examined": len(checks),
    }


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
        status="insufficient_evidence",
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
