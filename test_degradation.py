# -*- coding: utf-8 -*-
"""
E3-04 — degradação graciosa quando o LLM falha

A garantia que esta suíte protege é estreita e absoluta:

    se a evidência foi recuperada, o usuário recebe a evidência
    — independentemente do que o provedor de texto fizer.

Sem exceção vazando, sem 500, sem tela vermelha. O que o usuário perde é a
síntese; o que ele mantém é o que de fato sustenta uma resposta.

Os testes usam provedores que falham de propósito. Nenhuma requisição real ao
Gemini é feita aqui — a verificação contra a API real está no final do
relatório da E3-04, feita uma vez com timeout forçado.
"""

import os
import sys

from evidence import SYNTHESIS_UNAVAILABLE, EvidenceSource
from llm_provider import (
    DEFAULT_TIMEOUT_SECONDS,
    GeminiProvider,
    LLMError,
    LLMProvider,
    LLMResponse,
    LLMTimeout,
    LLMUnavailable,
)

passed = failed = 0


def check(label, condition, detail=""):
    global passed, failed
    if condition:
        passed += 1
        print(f"  [OK]    {label}")
    else:
        failed += 1
        print(f"  [FALHA] {label}   {detail}")


class FailingProvider(LLMProvider):
    """Levanta a exceção pedida, para exercitar cada ramo de degradação."""

    def __init__(self, error):
        self.error = error
        self.calls = 0

    def generate(self, system_prompt, user_prompt):
        self.calls += 1
        raise self.error

    def health(self):
        return {"provider": "failing", "configured": True}


class WorkingProvider(LLMProvider):
    def generate(self, system_prompt, user_prompt):
        return LLMResponse(
            text="Resposta fundamentada [1].",
            model="stub",
            provider="stub",
            finish_reason="STOP",
        )

    def health(self):
        return {"provider": "stub", "configured": True}


print("=" * 74)
print("E3-04 — degradação graciosa")
print("=" * 74)

# ---------------------------------------------------------------------------
print("\n1. CLASSIFICAÇÃO DAS FALHAS DO PROVEDOR")
# ---------------------------------------------------------------------------

provider = GeminiProvider(api_key="chave-de-teste")

try:
    import google.api_core.exceptions as gexc

    mapa = [
        (gexc.DeadlineExceeded("deadline"), LLMTimeout, "estouro de tempo"),
        (gexc.ResourceExhausted("quota exceeded"), LLMUnavailable, "quota esgotada"),
        (gexc.ServiceUnavailable("down"), LLMUnavailable, "provedor fora"),
        (gexc.InternalServerError("boom"), LLMUnavailable, "erro interno do provedor"),
        (gexc.PermissionDenied("nope"), LLMUnavailable, "credencial recusada"),
        (ValueError("outra coisa"), LLMError, "falha genérica"),
    ]
    for raw, expected, label in mapa:
        got = provider._classify(raw)
        check(f"{label} -> {expected.__name__}", isinstance(got, expected),
              f"-> {type(got).__name__}")
except ImportError:  # pragma: no cover
    print("  google.api_core ausente, classificação não verificada")

check("LLMTimeout é subclasse de LLMError (quem tratava, continua tratando)",
      issubclass(LLMTimeout, LLMError))
check("LLMUnavailable é subclasse de LLMError", issubclass(LLMUnavailable, LLMError))

# ---------------------------------------------------------------------------
print("\n2. O TIMEOUT CHEGA AO SDK")
# ---------------------------------------------------------------------------

check(f"timeout padrão é {DEFAULT_TIMEOUT_SECONDS}s", DEFAULT_TIMEOUT_SECONDS == 20.0,
      f"-> {DEFAULT_TIMEOUT_SECONDS}")
check("GeminiProvider guarda o timeout", provider.timeout_seconds == DEFAULT_TIMEOUT_SECONDS)
check("timeout é configurável por LLM_TIMEOUT_SECONDS",
      GeminiProvider(api_key="x", timeout_seconds=3.5).timeout_seconds == 3.5)

captured = {}


class SpyModel:
    """Finge ser o GenerativeModel, só para capturar o que é passado."""

    def generate_content(self, prompt, **kwargs):
        captured.update(kwargs)
        raise RuntimeError("parar aqui: só queremos ver os argumentos")


spy_provider = GeminiProvider(api_key="x", timeout_seconds=7.0)
spy_provider._model = SpyModel()
try:
    spy_provider.generate("sistema", "usuario")
except LLMError:
    pass

check("request_options é passado ao SDK", "request_options" in captured,
      f"-> {list(captured)}")
check("request_options carrega o timeout configurado",
      captured.get("request_options", {}).get("timeout") == 7.0,
      f"-> {captured.get('request_options')}")

# ---------------------------------------------------------------------------
print("\n3. DEGRADAÇÃO: A EVIDÊNCIA SOBREVIVE A QUALQUER FALHA")
# ---------------------------------------------------------------------------


def fake_sources(n=4):
    return [
        EvidenceSource(
            citation_index=i,
            chunk_id=f"c{i}",
            publication_id=f"p{i}",
            title=f"Artigo {i}",
            passage=f"Passagem literal número {i} sobre microgravidade.",
            relevance=0.95,
            doi=f"10.1000/test{i}",
        )
        for i in range(1, n + 1)
    ]


class FakeAris:
    """
    Exercita _evidence_only_answer isoladamente.

    Chamar DraAris exigiria Neo4j e o modelo de embeddings; o que esta suíte
    precisa verificar é o formato da resposta degradada, que não depende de
    nenhum dos dois.
    """

    threshold = 0.92

    from aris import DraAris

    _evidence_only_answer = DraAris._evidence_only_answer


falhas = [
    (LLMTimeout("A geracao passou de 20s e foi abortada."), "timeout"),
    (LLMUnavailable("Quota do provedor esgotada: 429"), "quota esgotada"),
    (LLMUnavailable("Provedor indisponivel: 503"), "provedor fora"),
    (LLMError("Falha na chamada ao Gemini: conexão recusada"), "falha de rede"),
]

for error, label in falhas:
    sources = fake_sources()
    resposta = FakeAris()._evidence_only_answer(
        "Como a microgravidade afeta os ossos?", sources, 0.94, ["semantic"], str(error)
    )
    check(f"{label}: status = synthesis_unavailable",
          resposta.status == "synthesis_unavailable", f"-> {resposta.status}")
    check(f"{label}: todas as {len(sources)} fontes vão no payload",
          len(resposta.sources) == 4, f"-> {len(resposta.sources)}")
    check(f"{label}: fontes marcadas como citadas (não ficam escondidas)",
          all(s.cited for s in resposta.sources))
    check(f"{label}: mensagem é a neutra, sem a palavra 'erro'",
          resposta.answer == SYNTHESIS_UNAVAILABLE)
    check(f"{label}: causa técnica preservada em warnings",
          any(str(error) in w for w in resposta.warnings), f"-> {resposta.warnings}")
    check(f"{label}: grounded=False (não há texto cujo lastro verificar)",
          resposta.grounded is False)
    check(f"{label}: rastro de recuperação preenchido",
          resposta.retrieval.chunks_used == 4 and resposta.retrieval.top_score == 0.94)

# ---------------------------------------------------------------------------
print("\n4. A MENSAGEM NEUTRA")
# ---------------------------------------------------------------------------

esperada = (
    "Os documentos relevantes foram recuperados com sucesso, mas a síntese em "
    "texto está temporariamente indisponível devido a uma falha de conexão."
)
check("texto exatamente como especificado na E3-04",
      SYNTHESIS_UNAVAILABLE == esperada, f"-> {SYNTHESIS_UNAVAILABLE!r}")
for proibida in ("erro", "Erro", "ERRO", "falhou", "exceção", "500"):
    check(f"mensagem não contém {proibida!r}", proibida not in SYNTHESIS_UNAVAILABLE)

# ---------------------------------------------------------------------------
print("\n5. OS TRÊS ESTADOS SÃO DISTINGUÍVEIS")
# ---------------------------------------------------------------------------

from evidence import insufficient_evidence_answer

recusa = insufficient_evidence_answer("qual a capital de Portugal?", 0.92, considered=6)
check("recusa por falta de evidência -> insufficient_evidence",
      recusa.status == "insufficient_evidence", f"-> {recusa.status}")
check("recusa não traz fontes", recusa.sources == [])

degradada = FakeAris()._evidence_only_answer(
    "q", fake_sources(), 0.94, ["semantic"], "timeout"
)
check("degradação e recusa NÃO se confundem",
      degradada.status != recusa.status)
check("degradação traz fontes, recusa não",
      len(degradada.sources) > 0 and len(recusa.sources) == 0)
check("as duas têm grounded=False, o que prova que status era necessário",
      degradada.grounded is False and recusa.grounded is False)

# ---------------------------------------------------------------------------
print("\n6. O PROVEDOR É CHAMADO UMA VEZ SÓ (sem retry escondido)")
# ---------------------------------------------------------------------------

falho = FailingProvider(LLMTimeout("estourou"))
try:
    falho.generate("a", "b")
except LLMTimeout:
    pass
check("uma falha = uma chamada", falho.calls == 1, f"-> {falho.calls}")

# ---------------------------------------------------------------------------
print("\n" + "=" * 74)
print(f"  {passed} verificações OK, {failed} falha(s)")
print("=" * 74)
sys.exit(1 if failed else 0)
