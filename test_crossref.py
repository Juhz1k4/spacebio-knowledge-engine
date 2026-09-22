# -*- coding: utf-8 -*-
"""
E3-06 — metadados bibliográficos do Crossref

O que esta suíte protege: a conversão da resposta do Crossref no nosso
registro. É onde os dados reais são irregulares — autor sem nome próprio,
consórcio sem sobrenome, ano em três campos diferentes, título que vem como
lista de um elemento.

Nenhuma requisição de rede é feita aqui. A resposta do Crossref é usada como
fixture; o teste contra a API real está no relatório da E3-06, e o job
completo rodou uma vez sobre as 493 publicações.
"""

import json
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

from crossref import (
    STATUS_ENRICHED,
    STATUS_NOT_FOUND,
    CrossrefCache,
    CrossrefClient,
    PublicationMetadata,
    _extract_year,
    _first,
    _format_authors,
    _user_agent,
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


print("=" * 76)
print("E3-06 — Crossref")
print("=" * 76)

# ---------------------------------------------------------------------------
print("\n1. AUTORES — as três formas que o Crossref devolve")
# ---------------------------------------------------------------------------

check(
    "nome completo -> 'Sobrenome, Nome'",
    _format_authors([{"given": "Jane", "family": "Doe"}]) == ["Doe, Jane"],
)
check(
    "só sobrenome -> sem vírgula pendurada",
    _format_authors([{"family": "Doe"}]) == ["Doe"],
)
check(
    "consórcio (campo 'name') entra inteiro",
    _format_authors([{"name": "The ISS Consortium"}]) == ["The ISS Consortium"],
)
check(
    "ordem preservada (importa para 'et al.')",
    _format_authors(
        [
            {"given": "A", "family": "Primeiro"},
            {"given": "B", "family": "Segundo"},
        ]
    )
    == ["Primeiro, A", "Segundo, B"],
)
check("lista vazia -> []", _format_authors([]) == [])
check("None -> [] em vez de exceção", _format_authors(None) == [])
check("tipo inesperado -> []", _format_authors("Doe, Jane") == [])
check(
    "entrada sem nome nenhum é descartada",
    _format_authors([{"sequence": "first"}, {"family": "Doe"}]) == ["Doe"],
)
check(
    "espaços são aparados",
    _format_authors([{"given": "  Jane ", "family": " Doe "}]) == ["Doe, Jane"],
)

# ---------------------------------------------------------------------------
print("\n2. ANO — três campos, em ordem de confiabilidade")
# ---------------------------------------------------------------------------

check(
    "'issued' é preferido",
    _extract_year(
        {
            "issued": {"date-parts": [[2021, 8, 20]]},
            "published-print": {"date-parts": [[2022]]},
        }
    )
    == 2021,
)
check(
    "cai para 'published-print' quando issued falta",
    _extract_year({"published-print": {"date-parts": [[2019]]}}) == 2019,
)
check(
    "cai para 'published-online'",
    _extract_year({"published-online": {"date-parts": [[2018]]}}) == 2018,
)
check("sem nenhum campo -> None", _extract_year({}) is None)
check(
    "date-parts vazio -> None",
    _extract_year({"issued": {"date-parts": []}}) is None,
)
check(
    "date-parts com None dentro -> None, sem quebrar",
    _extract_year({"issued": {"date-parts": [[None]]}}) is None,
)
check(
    "ano como string ainda é convertido",
    _extract_year({"issued": {"date-parts": [["2020"]]}}) == 2020,
)

# ---------------------------------------------------------------------------
print("\n3. CAMPOS QUE VÊM COMO LISTA")
# ---------------------------------------------------------------------------

# `title` e `container-title` são SEMPRE listas na API, mesmo com um elemento.
# Ler como string devolveria o primeiro caractere.
check("lista -> primeiro elemento", _first(["Título do Artigo"]) == "Título do Artigo")
check("lista vazia -> None", _first([]) is None)
check("string continua funcionando", _first("Direto") == "Direto")
check("string vazia -> None", _first("   ") is None)
check("None -> None", _first(None) is None)

# ---------------------------------------------------------------------------
print("\n4. CONVERSÃO DA RESPOSTA COMPLETA")
# ---------------------------------------------------------------------------

RESPOSTA = {
    "DOI": "10.3390/ijms22169088",
    "title": ["Spaceflight Modulates the Expression of Key Oxidative Stress Genes"],
    "container-title": ["International Journal of Molecular Sciences"],
    "author": [
        {"given": "Akhilesh", "family": "Kumar"},
        {"given": "Candice G. T.", "family": "Tahimic"},
    ],
    "issued": {"date-parts": [[2021, 8, 20]]},
    "volume": "22",
    "issue": "16",
    "page": "9088",
    "publisher": "MDPI AG",
    "URL": "https://doi.org/10.3390/ijms22169088",
}

client = CrossrefClient(cache=CrossrefCache(path=Path("nao-existe-jamais.json")))
meta = client._parse("10.3390/ijms22169088", RESPOSTA)

check("status enriched", meta.status == STATUS_ENRICHED)
check("2 autores", len(meta.authors) == 2, f"-> {meta.authors}")
check("primeiro autor normalizado", meta.authors[0] == "Kumar, Akhilesh")
check("ano 2021", meta.year == 2021)
check("revista desembrulhada da lista",
      meta.container_title == "International Journal of Molecular Sciences")
check("volume 22", meta.volume == "22")
check("número 16", meta.issue == "16")
check("páginas 9088", meta.pages == "9088")
check("é utilizável para citação", meta.is_usable)

vazio = client._parse("10.0/x", {})
check("resposta vazia -> not_found", vazio.status == STATUS_NOT_FOUND)
check("not_found não é utilizável", not vazio.is_usable)

so_ano = PublicationMetadata(doi="x", status=STATUS_ENRICHED, year=2020)
check("só ano já é utilizável", so_ano.is_usable)
so_autor = PublicationMetadata(doi="x", status=STATUS_ENRICHED, authors=["Doe"])
check("só autor já é utilizável", so_autor.is_usable)
nada = PublicationMetadata(doi="x", status=STATUS_ENRICHED)
check("enriquecido sem autor nem ano NÃO é utilizável", not nada.is_usable)

# ---------------------------------------------------------------------------
print("\n5. CACHE EM DISCO")
# ---------------------------------------------------------------------------

with TemporaryDirectory() as tmp:
    caminho = Path(tmp) / "crossref.json"

    cache = CrossrefCache(path=caminho)
    check("arquivo ausente -> cache vazio", cache.entries == {})
    check("get num cache vazio -> None", cache.get("10.1/x") is None)

    cache.put("10.1/ABC", RESPOSTA)
    cache.save()
    check("save cria o arquivo", caminho.exists())

    recarregado = CrossrefCache(path=caminho)
    check("recarrega o que foi gravado", len(recarregado.entries) == 1)
    check(
        "a busca ignora a caixa do DOI",
        recarregado.get("10.1/abc") is not None and recarregado.get("10.1/ABC") is not None,
    )
    check("contador de acertos incrementa", recarregado.hits == 2)

    quebrado = Path(tmp) / "quebrado.json"
    quebrado.write_text("{ não é json", encoding="utf-8")
    corrompido = CrossrefCache(path=quebrado)
    check("JSON inválido -> cache vazio, sem exceção", corrompido.entries == {})

# ---------------------------------------------------------------------------
print("\n6. IDENTIFICAÇÃO AO CROSSREF")
# ---------------------------------------------------------------------------

import os

anterior = os.environ.pop("CROSSREF_MAILTO", None)
check("sem CROSSREF_MAILTO, não inventa mailto", "mailto:" not in _user_agent())
check("identifica o projeto", "SpaceBio" in _user_agent())

os.environ["CROSSREF_MAILTO"] = "contato@exemplo.org"
check("com a variável, entra no polite pool",
      "mailto:contato@exemplo.org" in _user_agent())
if anterior is None:
    os.environ.pop("CROSSREF_MAILTO", None)
else:
    os.environ["CROSSREF_MAILTO"] = anterior

# ---------------------------------------------------------------------------
print("\n7. COBERTURA REAL NO GRAFO")
# ---------------------------------------------------------------------------

try:
    from graph_manager import Neo4jGraphManager

    with Neo4jGraphManager() as graph:
        cobertura = graph.metadata_coverage()
    total = cobertura.get("total") or 0
    com_autores = cobertura.get("com_autores") or 0
    com_ano = cobertura.get("com_ano") or 0

    check(f"grafo tem publicações ({total})", total > 0)
    check(
        f"autores em {com_autores}/{total} ({com_autores / total:.1%})",
        com_autores / total > 0.95,
        "esperado >95%: 5 publicações não têm DOI",
    )
    check(
        f"ano em {com_ano}/{total} ({com_ano / total:.1%})",
        com_ano / total > 0.95,
    )
except Exception as error:  # noqa: BLE001
    print(f"  (pulado — Neo4j indisponível: {type(error).__name__})")

print("\n" + "=" * 76)
print(f"  {passed} verificações OK, {failed} falha(s)")
print("=" * 76)
sys.exit(1 if failed else 0)
