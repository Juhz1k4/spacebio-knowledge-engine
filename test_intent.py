# -*- coding: utf-8 -*-
"""
E3-03 — roteador de intenção, com métrica assimétrica

O critério NÃO é acurácia. Os dois erros custam coisas diferentes:

    operacional -> RAG    uma chamada de LLM a mais       tolerável
    científica  -> meta   evidência do acervo escondida   inaceitável

Então a suíte tem dois níveis. A taxa de científicas capturadas precisa ser
ZERO e falha o teste. A taxa de operacionais roteadas é uma meta (>=90%), e
uma perda individual é registrada mas não derruba a suíte — porque relaxar um
padrão para recuperar uma operacional é exatamente o movimento que reintroduz
falso positivo científico.
"""

import json
import sys
import time
from pathlib import Path

from intent import (
    MAX_META_QUESTION_CHARS,
    META_INTENTS,
    classify,
    mentions_domain,
    normalize,
)

FIXTURE = Path(__file__).parent / "tests" / "fixtures" / "router_cases.jsonl"

# Meta para o lado tolerável do erro. Não é critério de falha: ver docstring.
OPERATIONAL_RECALL_TARGET = 0.90

passed = failed = 0


def check(label, condition, detail=""):
    global passed, failed
    if condition:
        passed += 1
        print(f"  [OK]    {label}")
    else:
        failed += 1
        print(f"  [FALHA] {label}   {detail}")


def load_cases():
    cases = []
    for line in FIXTURE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            cases.append(json.loads(line))
    return cases


print("=" * 74)
print("E3-03 — roteador de intenção")
print("=" * 74)

cases = load_cases()
operational = [c for c in cases if c["intent"] is not None]
scientific = [c for c in cases if c["intent"] is None]

print(f"\nFixture: {len(cases)} casos "
      f"({len(operational)} operacionais, {len(scientific)} científicos)")

# ---------------------------------------------------------------------------
print("\n1. RECALL CIENTÍFICO — nenhuma pode ser capturada (critério de falha)")
# ---------------------------------------------------------------------------

capturadas = []
for case in scientific:
    result = classify(case["q"])
    if result is not None:
        capturadas.append((case["q"], result.name, case.get("note")))

check(
    f"0 de {len(scientific)} perguntas científicas roteadas para meta",
    not capturadas,
    f"-> {len(capturadas)} capturadas",
)
for question, intent, note in capturadas:
    print(f"          {question!r} -> {intent}   ({note})")

# ---------------------------------------------------------------------------
print("\n2. COBERTURA OPERACIONAL — meta, não critério de falha")
# ---------------------------------------------------------------------------

roteadas = 0
perdidas = []
intencao_errada = []
for case in operational:
    result = classify(case["q"])
    if result is None:
        perdidas.append((case["q"], case.get("note")))
    else:
        roteadas += 1
        if result.name != case["intent"]:
            intencao_errada.append((case["q"], case["intent"], result.name))

recall = roteadas / len(operational) if operational else 0.0
print(f"  {roteadas}/{len(operational)} roteadas = {recall:.1%} "
      f"(meta {OPERATIONAL_RECALL_TARGET:.0%})")
if perdidas:
    print("  perdidas (vão ao RAG, custam uma chamada de LLM):")
    for question, note in perdidas:
        print(f"      {question!r}   ({note})")
if recall < OPERATIONAL_RECALL_TARGET:
    print(f"  AVISO: abaixo da meta. Não falha a suíte — corrigir relaxando "
          f"padrão reintroduz falso positivo científico.")

check(
    "operacionais roteadas caem na intenção certa",
    not intencao_errada,
    f"-> {intencao_errada}",
)

# ---------------------------------------------------------------------------
print("\n3. TERMOS ISOLADOS E BUSCAS CURTAS (exigência explícita da E3-03)")
# ---------------------------------------------------------------------------

isolados = [
    "RUNX2", "CDKN1A", "p21", "OSD-570", "GLDS-104", "IL6", "TP53",
    "microgravidade ossos", "radiação celular", "atrofia muscular",
    "perda óssea", "Arabidopsis", "osteoclastos", "telômeros", "autofagia",
    "ISS", "DNA", "RNA", "NASA",
]
for termo in isolados:
    check(f"{termo!r} vai ao RAG", classify(termo) is None,
          f"-> {getattr(classify(termo), 'name', None)}")

# ---------------------------------------------------------------------------
print("\n4. FORMA OPERACIONAL COM ASSUNTO GRUDADO (a regressão da E3-03)")
# ---------------------------------------------------------------------------

grudados = [
    "me ajuda com RUNX2",
    "como funciona a osteogênese",
    "quais artigos falam de osteoclastos",
    "o que você não sabe sobre telômeros",
    "ajuda sobre Arabidopsis",
    "como usar dados de RNAseq",
    "me ajude a entender a atrofia muscular",
    "como funciona a autofagia",
    "quais temas de radiação você cobre",
    "me dá um exemplo de experimento na ISS",
]
for pergunta in grudados:
    check(f"{pergunta!r} vai ao RAG", classify(pergunta) is None,
          f"-> {getattr(classify(pergunta), 'name', None)}")

# ---------------------------------------------------------------------------
print("\n5. GUARDS INDIVIDUAIS")
# ---------------------------------------------------------------------------

longa = "quem é você e o que você faz exatamente neste sistema todo, me explique"
check(f"pergunta acima de {MAX_META_QUESTION_CHARS} chars não é roteada",
      classify("x" * (MAX_META_QUESTION_CHARS + 1)) is None)
check("guard de comprimento não corta frases operacionais normais",
      len("quais são as suas limitações?") < MAX_META_QUESTION_CHARS)

check("mentions_domain pega identificador com dígito", mentions_domain("RUNX2"))
check("mentions_domain pega sigla em caixa alta", mentions_domain("o que é a ISS"))
check("mentions_domain pega termo em português", mentions_domain("fale de autofagia"))
check("mentions_domain pega entidade do grafo", mentions_domain("fale de Arabidopsis"))
check("mentions_domain NÃO dispara em pergunta operacional",
      not mentions_domain("quem é você"))
check("mentions_domain NÃO dispara com a pergunta toda em caixa alta",
      not mentions_domain("QUEM É VOCÊ"))

check("normalize remove acento", normalize("osteogênese") == "osteogenese")
check("normalize remove pontuação", normalize("dra. aris?") == "dra aris")
check("normalize colapsa espaço", normalize("  a   b  ") == "a b")

# ---------------------------------------------------------------------------
print("\n6. ENTRADAS DEGENERADAS")
# ---------------------------------------------------------------------------

for entrada, rotulo in [("", "vazia"), ("   ", "só espaços"), ("?", "só pontuação"),
                        ("!!!", "só pontuação"), ("\n\t", "só whitespace")]:
    check(f"entrada {rotulo} não quebra e vai ao RAG", classify(entrada) is None)

check("None-safe: string vazia não levanta", classify("") is None)

# ---------------------------------------------------------------------------
print("\n7. CUSTO ZERO DAS RESPOSTAS OPERACIONAIS")
# ---------------------------------------------------------------------------

t0 = time.perf_counter()
for _ in range(1000):
    classify("quem é você?")
elapsed_us = (time.perf_counter() - t0) * 1000
check(f"1000 classificações em {elapsed_us:.1f} ms (sem I/O, sem LLM)",
      elapsed_us < 2000, f"-> {elapsed_us:.1f} ms")

for intent in META_INTENTS:
    check(f"intenção {intent.name!r} tem resposta estática não vazia",
          bool(intent.answer and intent.answer.strip()))

# ---------------------------------------------------------------------------
print("\n" + "=" * 74)
print(f"  {passed} verificações OK, {failed} falha(s)")
print(f"  recall científico: {(len(scientific) - len(capturadas)) / len(scientific):.1%} "
      f"(exigido 100%)")
print(f"  cobertura operacional: {recall:.1%} (meta {OPERATIONAL_RECALL_TARGET:.0%})")
print("=" * 74)
sys.exit(1 if failed else 0)
