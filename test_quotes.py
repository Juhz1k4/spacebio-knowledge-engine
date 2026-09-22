# -*- coding: utf-8 -*-
"""
E3-02 — validação de citações literais

O que estes testes protegem, em uma frase: normalizar FORMA sem relaxar
CONTEÚDO. A metade fácil é aceitar a mesma frase escrita com aspas curvas; a
metade que importa é continuar reprovando a frase que teve uma palavra trocada.
"""

import sys

from evidence import (
    EvidenceSource,
    MIN_QUOTE_WORDS,
    normalize_for_match,
    validate_quotes,
)

PASSAGE_EN = (
    "Microgravity exposure leads to a rapid decline in bone mineral density, "
    "particularly in weight-bearing regions such as the femur and the lumbar "
    "spine. Astronauts' recovery after return to Earth remains incomplete."
)

PASSAGE_MULTILINE = (
    "Impaired  osteocyte\nautophagy, enlarged mitochondria are observed as well,\n"
    "and  associated   with decreased trabecular and cortical bone."
)


def source(index, passage, title="Artigo de teste"):
    return EvidenceSource(
        citation_index=index,
        chunk_id=f"c{index}",
        publication_id=f"p{index}",
        title=title,
        passage=passage,
        relevance=0.95,
    )


SOURCES = [source(1, PASSAGE_EN), source(2, PASSAGE_MULTILINE)]

passed = failed = 0


def check(label, condition, detail=""):
    global passed, failed
    if condition:
        passed += 1
        print(f"  [OK]    {label}")
    else:
        failed += 1
        print(f"  [FALHA] {label}   {detail}")


print("=" * 72)
print("E3-02 — validate_quotes")
print("=" * 72)

print("\n1. Normalização de forma (deve APROVAR)")

cases_ok = [
    ("aspas retas, texto idêntico",
     'Os dados mostram "a rapid decline in bone mineral density" nos ossos [1].'),
    ("aspas tipográficas",
     'Os dados mostram \u201ca rapid decline in bone mineral density\u201d nos ossos [1].'),
    ("caixa diferente no início",
     'Conforme o artigo, "A rapid decline in bone mineral density" ocorre [1].'),
    ("apóstrofo curvo vs reto",
     'O texto diz "Astronauts\u2019 recovery after return to Earth" [1].'),
    ("hífen vs travessão en",
     'Regiões "weight\u2013bearing regions such as the femur" sofrem mais [1].'),
    ("quebra de linha e espaço múltiplo na passagem",
     'Observou-se "enlarged mitochondria are observed as well" no estudo [2].'),
    ("elisão com reticências, em ordem",
     'O artigo relata "Microgravity exposure leads ... the lumbar spine" [1].'),
    ("elisão com caractere de reticências",
     'O artigo relata "Microgravity exposure leads \u2026 the lumbar spine" [1].'),
]

for label, answer in cases_ok:
    r = validate_quotes(answer, SOURCES)
    ok = r["examined"] == 1 and r["verified"] == 1 and r["unverified"] == 0
    check(label, ok, f"-> {r['verified']}/{r['examined']} verificadas")
    check(f"   {label}: aspas preservadas no texto",
          '"' in r["answer"] or "\u201c" in r["answer"] or "\u00ab" in r["answer"])

print("\n2. Conteúdo adulterado (deve REPROVAR)")

cases_fail = [
    ("citação traduzida para o português",
     'O artigo afirma que "um rápido declínio na densidade mineral óssea" ocorre [1].'),
    ("citação inventada do zero",
     'O estudo conclui que "bone loss is fully reversible within two weeks" [1].'),
    ("uma palavra trocada (rapid -> slow)",
     'Os dados mostram "a slow decline in bone mineral density" nos ossos [1].'),
    ("negação inserida",
     'O texto diz "Astronauts\u2019 recovery after return to Earth remains complete" [1].'),
    ("elisão fora de ordem",
     'O artigo diz "the lumbar spine ... Microgravity exposure leads" [1].'),
]

for label, answer in cases_fail:
    r = validate_quotes(answer, SOURCES)
    ok = r["examined"] == 1 and r["verified"] == 0 and r["unverified"] == 1
    check(label, ok, f"-> {r['verified']}/{r['examined']} verificadas")

print("\n3. Tratamento da falha")

answer = 'O artigo afirma que "um rápido declínio na densidade mineral óssea" ocorre [1].'
r = validate_quotes(answer, SOURCES)
check("aspas removidas do trecho reprovado",
      '"um rápido declínio na densidade mineral óssea"' not in r["answer"])
check("texto do trecho preservado",
      "um rápido declínio na densidade mineral óssea" in r["answer"])
check("marcador [1] intacto", "[1]" in r["answer"])
check("registro no payload tem o trecho original",
      r["checks"][0].quote == "um rápido declínio na densidade mineral óssea")
check("registro marca verified=False", r["checks"][0].verified is False)
check("registro sem source_index", r["checks"][0].source_index is None)

r_ok = validate_quotes(
    'Os dados mostram "a rapid decline in bone mineral density" [1].', SOURCES
)
check("aprovada aponta a fonte correta", r_ok["checks"][0].source_index == 1,
      f"-> {r_ok['checks'][0].source_index}")

print("\n4. Limite de comprimento")

r = validate_quotes('A "microgravity" afeta o osso [1].', SOURCES)
check(f"termo curto (< {MIN_QUOTE_WORDS} palavras) não é examinado", r["examined"] == 0)
check("termo curto mantém as aspas", '"microgravity"' in r["answer"])

r = validate_quotes('O conceito de "bone mineral density loss" aparece [1].', SOURCES)
check("4 palavras é examinado", r["examined"] == 1)

print("\n5. Casos de contorno")

r = validate_quotes("Resposta sem nenhuma aspa [1].", SOURCES)
check("sem aspas -> nenhuma conferência", r["examined"] == 0 and r["checks"] == [])
check("sem aspas -> texto intacto", r["answer"] == "Resposta sem nenhuma aspa [1].")

r = validate_quotes('Diz "a rapid decline in bone mineral density" [1].', [])
check("sem fontes -> reprova em vez de aprovar por omissão",
      r["examined"] == 1 and r["verified"] == 0)

mixed = ('Primeiro "a rapid decline in bone mineral density" [1], '
         'depois "bone loss is fully reversible within weeks" [1].')
r = validate_quotes(mixed, SOURCES)
check("mistura: uma aprovada e uma reprovada",
      r["verified"] == 1 and r["unverified"] == 1, f"-> {r}")
check("mistura: só a reprovada perde as aspas",
      '"a rapid decline in bone mineral density"' in r["answer"]
      and '"bone loss is fully reversible within weeks"' not in r["answer"])

r = validate_quotes('Busca em duas fontes "enlarged mitochondria are observed as well" [2].',
                    SOURCES)
check("encontra na segunda fonte", r["checks"][0].source_index == 2)

print("\n6. normalize_for_match não altera conteúdo")

check("caixa some", normalize_for_match("Bone Loss Occurs") == "bone loss occurs")
check("travessões viram hífen",
      normalize_for_match("weight\u2013bearing") == normalize_for_match("weight-bearing"))
check("espaços colapsam",
      normalize_for_match("a  b\n\nc") == "a b c")
check("palavra diferente continua diferente",
      normalize_for_match("rapid decline") != normalize_for_match("slow decline"))
check("plural continua diferente",
      normalize_for_match("bone") != normalize_for_match("bones"))
check("acento continua significativo",
      normalize_for_match("ossea") != normalize_for_match("óssea"))

print("\n" + "=" * 72)
print(f"  {passed} verificações OK, {failed} falha(s)")
print("=" * 72)
sys.exit(1 if failed else 0)
