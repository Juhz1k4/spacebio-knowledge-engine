# -*- coding: utf-8 -*-
"""
E3-05 — cache do roteiro da demonstração

O que esta suíte protege é uma promessa operacional: na hora da apresentação,
as perguntas do roteiro respondem em milissegundos, sem tocar o grafo nem o
provedor de LLM — que é justamente o que pode falhar na hora errada (quota de
20 requisições por dia, fila do provedor com variação de 3s a 91s).

Duas garantias, e a segunda é a que costuma ser esquecida:

    1. ACERTO  -> resposta completa, rápida, sem RAG e sem LLM.
    2. RUÍDO   -> um cache desatualizado NÃO pode ser servido. Uma resposta
                  gerada sob outro prompt, outro modelo ou outra ontologia
                  mostraria um comportamento que o sistema não tem mais.
"""

import json
import sys
import time
from pathlib import Path
from tempfile import TemporaryDirectory

from demo_cache import (
    DEFAULT_SIMILARITY_THRESHOLD,
    DEMO_QUESTIONS,
    CacheEntry,
    DemoCache,
    mark_as_cached,
    prompt_fingerprint,
)
from intent import normalize as normalize_question

passed = failed = 0


def check(label, condition, detail=""):
    global passed, failed
    if condition:
        passed += 1
        print(f"  [OK]    {label}")
    else:
        failed += 1
        print(f"  [FALHA] {label}   {detail}")


MODELO = "intfloat/multilingual-e5-small"
ONTOLOGIA = "v1.0.0"
PROMPT = prompt_fingerprint()


def entrada(question, embedding=None, **overrides):
    base = dict(
        question=question,
        embedding=embedding or [1.0, 0.0, 0.0],
        answer={"answer": "resposta", "sources": [], "grounded": True, "warnings": []},
        embedding_model=MODELO,
        llm_model="gemini-flash-lite-latest",
        ontology_version=ONTOLOGIA,
        generated_at="2026-09-22T10:00:00+00:00",
        prompt_hash=PROMPT,
    )
    base.update(overrides)
    return CacheEntry(**base)


def cache_temporario(entries, threshold=DEFAULT_SIMILARITY_THRESHOLD):
    """Grava um cache num arquivo temporário e o carrega."""
    tmp = TemporaryDirectory()
    path = Path(tmp.name) / "demo_cache.json"
    path.write_text(
        json.dumps({"entries": [e.to_dict() for e in entries]}, ensure_ascii=False),
        encoding="utf-8",
    )
    cache = DemoCache(path=path, threshold=threshold)
    cache._tmp = tmp  # mantém o diretório vivo enquanto o cache existir
    return cache


print("=" * 78)
print("E3-05 — cache do roteiro")
print("=" * 78)

# ---------------------------------------------------------------------------
print("\n1. CASAMENTO EXATO (sem precisar de embedding)")
# ---------------------------------------------------------------------------

cache = cache_temporario([entrada("Como a microgravidade afeta a densidade óssea?")])

variantes_iguais = [
    ("idêntica", "Como a microgravidade afeta a densidade óssea?"),
    ("sem acento", "Como a microgravidade afeta a densidade ossea?"),
    ("caixa diferente", "COMO A MICROGRAVIDADE AFETA A DENSIDADE ÓSSEA?"),
    ("sem interrogação", "Como a microgravidade afeta a densidade óssea"),
    ("espaço extra", "  Como a microgravidade afeta   a densidade óssea? "),
    ("pontuação trocada", "Como a microgravidade afeta a densidade óssea!"),
]
for rotulo, pergunta in variantes_iguais:
    hit = cache.lookup_exact(pergunta, MODELO, ONTOLOGIA, PROMPT)
    check(f"{rotulo} -> acerto exato", hit is not None)

variantes_diferentes = [
    ("outra pergunta", "Como a radiação afeta a densidade óssea?"),
    ("qualificada", "Como a microgravidade afeta a densidade óssea dos astronautas?"),
    ("vazia", ""),
]
for rotulo, pergunta in variantes_diferentes:
    check(f"{rotulo} -> sem acerto exato",
          cache.lookup_exact(pergunta, MODELO, ONTOLOGIA, PROMPT) is None)

check("o índice exato tem uma chave por entrada", len(cache._exact) == 1)
check("a chave é a pergunta normalizada",
      normalize_question("Como a microgravidade afeta a densidade óssea?") in cache._exact)

# ---------------------------------------------------------------------------
print("\n2. INVALIDAÇÃO — o desatualizado NÃO é servido")
# ---------------------------------------------------------------------------

pergunta = "Como a microgravidade afeta a densidade óssea?"
cache = cache_temporario([entrada(pergunta)])

check("condições iguais -> serve",
      cache.lookup_exact(pergunta, MODELO, ONTOLOGIA, PROMPT) is not None)
check("modelo de embedding diferente -> NÃO serve",
      cache.lookup_exact(pergunta, "outro/modelo", ONTOLOGIA, PROMPT) is None)
check("ontologia diferente -> NÃO serve",
      cache.lookup_exact(pergunta, MODELO, "v2.0.0", PROMPT) is None)
check("prompt diferente -> NÃO serve (E3-05)",
      cache.lookup_exact(pergunta, MODELO, ONTOLOGIA, "outrohash1234567") is None)

# O mesmo vale para o caminho por similaridade.
check("similaridade: prompt diferente -> NÃO serve",
      cache.lookup([1.0, 0.0, 0.0], MODELO, ONTOLOGIA, "outrohash1234567") is None)
check("similaridade: condições iguais -> serve",
      cache.lookup([1.0, 0.0, 0.0], MODELO, ONTOLOGIA, PROMPT) is not None)

# Cache anterior à E3-05, sem prompt_hash gravado.
legado = cache_temporario([entrada(pergunta, prompt_hash="")])
check("entrada legada (sem prompt_hash) continua servindo",
      legado.lookup_exact(pergunta, MODELO, ONTOLOGIA, PROMPT) is not None,
      "não derrubar um cache existente na véspera da demo")

check("prompt_fingerprint é estável entre chamadas",
      prompt_fingerprint() == prompt_fingerprint())
check("prompt_fingerprint tem 16 caracteres", len(prompt_fingerprint()) == 16)

# ---------------------------------------------------------------------------
print("\n3. SIMILARIDADE — mesma pergunta reescrita, não tema próximo")
# ---------------------------------------------------------------------------

alvo = [1.0, 0.0, 0.0]
cache = cache_temporario([entrada("pergunta âncora", embedding=alvo)])

casos = [
    ("idêntico (1.000)", [1.0, 0.0, 0.0], True),
    ("muito próximo (0.995)", [0.995, 0.0998, 0.0], True),
    ("próximo mas abaixo (0.97)", [0.97, 0.2431, 0.0], False),
    ("tema vizinho (0.90)", [0.90, 0.4359, 0.0], False),
    ("ortogonal (0.0)", [0.0, 1.0, 0.0], False),
]
for rotulo, vetor, esperado in casos:
    hit = cache.lookup(vetor, MODELO, ONTOLOGIA, PROMPT)
    check(f"{rotulo} -> {'HIT' if esperado else 'miss'}", (hit is not None) == esperado)

check(f"limiar é {DEFAULT_SIMILARITY_THRESHOLD}",
      DEFAULT_SIMILARITY_THRESHOLD == 0.98)
check("dimensão diferente é ignorada, não quebra",
      cache.lookup([1.0, 0.0], MODELO, ONTOLOGIA, PROMPT) is None)

# ---------------------------------------------------------------------------
print("\n4. HONESTIDADE — o acerto é sempre declarado")
# ---------------------------------------------------------------------------

e = entrada("q")
marcada = mark_as_cached(e.answer, e)
check("o aviso de cache é anexado",
      any("cache de demonstra" in w for w in marcada["warnings"]))
check("o aviso traz a data de geração",
      any("2026-09-22" in w for w in marcada["warnings"]))
check("o aviso traz o modelo usado",
      any("gemini-flash-lite-latest" in w for w in marcada["warnings"]))
check("a resposta original não é mutada", e.answer["warnings"] == [])

# ---------------------------------------------------------------------------
print("\n5. DEGRADAÇÃO SEGURA — cache ausente ou corrompido")
# ---------------------------------------------------------------------------

with TemporaryDirectory() as tmp:
    ausente = DemoCache(path=Path(tmp) / "nao-existe.json")
    check("arquivo ausente -> cache vazio", len(ausente) == 0)
    check("arquivo ausente -> lookup_exact devolve None",
          ausente.lookup_exact("q", MODELO, ONTOLOGIA, PROMPT) is None)
    check("arquivo ausente -> lookup devolve None",
          ausente.lookup([1.0], MODELO, ONTOLOGIA, PROMPT) is None)

    quebrado = Path(tmp) / "quebrado.json"
    quebrado.write_text("{ isto não é json válido", encoding="utf-8")
    corrompido = DemoCache(path=quebrado)
    check("JSON inválido -> cache vazio em vez de exceção", len(corrompido) == 0)
    check("JSON inválido -> lookup_exact não quebra",
          corrompido.lookup_exact("q", MODELO, ONTOLOGIA, PROMPT) is None)

# ---------------------------------------------------------------------------
print("\n6. O ROTEIRO REAL ESTÁ COMPLETO E VÁLIDO")
# ---------------------------------------------------------------------------

real = DemoCache()
check(f"o cache do projeto tem as {len(DEMO_QUESTIONS)} perguntas do roteiro",
      len(real) == len(DEMO_QUESTIONS), f"-> {len(real)}")

for pergunta in DEMO_QUESTIONS:
    hit = real.lookup_exact(pergunta, MODELO, ONTOLOGIA, PROMPT)
    check(f"acerto exato: {pergunta[:52]}", hit is not None)
    if hit:
        check(f"    resposta fundamentada", hit.answer.get("grounded") is True)
        check(f"    traz fontes", len(hit.answer.get("sources", [])) > 0)
        check(f"    status ok", hit.answer.get("status") == "ok",
              f"-> {hit.answer.get('status')}")

# ---------------------------------------------------------------------------
print("\n7. LATÊNCIA DO ACERTO")
# ---------------------------------------------------------------------------

real = DemoCache()
inicio = time.perf_counter()
for _ in range(200):
    for pergunta in DEMO_QUESTIONS:
        real.lookup_exact(pergunta, MODELO, ONTOLOGIA, PROMPT)
total = len(DEMO_QUESTIONS) * 200
por_consulta_us = (time.perf_counter() - inicio) * 1_000_000 / total
check(f"consulta ao índice em {por_consulta_us:.1f} µs",
      por_consulta_us < 1000, f"-> {por_consulta_us:.1f} µs")

print("\n" + "=" * 78)
print(f"  {passed} verificações OK, {failed} falha(s)")
print("=" * 78)
sys.exit(1 if failed else 0)
