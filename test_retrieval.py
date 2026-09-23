"""
Testes da SPACEBIO-012 — Parameterized Retrieval Queries

Verifica que as consultas de recuperação:
  1. devolvem evidência (texto + procedência), não apenas títulos;
  2. respeitam filtros e limites;
  3. são imunes a injeção de Cypher e a apóstrofos;
  4. funcionam na cláusula SEARCH do Neo4j 2026.x.

Pré-requisitos: Neo4j no ar, .env preenchido e o corpus de teste já gravado
(rode `python test_pipeline.py` antes).

Uso:
    python test_retrieval.py
"""

from __future__ import annotations

import logging
import sys
from typing import List, Optional

from config import MissingCredentialError, settings
from graph_manager import build_driver
from retrieval import EvidencePassage, RetrievalRepository

log = logging.getLogger("test_retrieval")

# O console do Windows abre em cp1252 e quebra em qualquer caractere fora
# dessa tabela. Sem isto, um acento numa mensagem de teste derruba a execução.
for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8", errors="replace")

# Payloads de injeção. Todos NÃO destrutivos de propósito: se a defesa falhar,
# o pior resultado é uma query inválida ou um match indevido — nunca perda de
# dados. Um teste de segurança não pode ser mais perigoso que a falha.
INJECTION_PAYLOADS = [
    "bone' OR true OR 'x",           # tentativa clássica de tautologia
    "Parkinson's disease",            # apóstrofo legítimo — quebrava a versão antiga
    "') RETURN 1 AS pwned //",        # fecha o literal e injeta um RETURN
    'radiation" OR "1"="1',           # aspas duplas
    "micro`gravity",                  # backtick (identificador em Cypher)
    "bone\\' escaped",                # barra invertida
]


class RetrievalTestFailure(AssertionError):
    """Falha de asserção em um teste de recuperação."""


def check(condition: bool, message: str) -> None:
    if condition:
        print(f"      [OK] {message}")
    else:
        print(f"      [FALHA] {message}")
        raise RetrievalTestFailure(message)


def section(step: str, title: str) -> None:
    print(f"\n[{step}] {title}")


def legacy_interpolated_query(keywords: List[str]) -> str:
    """
    Reproduz a construção de query que existia em main.py, para comparação.

    NÃO É EXECUTADA em lugar nenhum — serve para mostrar, no output do teste,
    que Cypher ela produziria com um payload hostil.
    """
    clauses = [
        f"MATCH (p)-[:MENTIONS]->(e{i}) WHERE toLower(e{i}.name) CONTAINS '{keyword.lower()}'"
        for i, keyword in enumerate(keywords)
    ]
    return "\n".join(clauses) + "\nRETURN DISTINCT p.title AS title LIMIT 5"


# ---------------------------------------------------------------------- #
# Testes
# ---------------------------------------------------------------------- #


def test_corpus_present(repo: RetrievalRepository) -> str:
    """Confere que há corpus indexado e devolve o título da publicação de teste."""
    stats = repo.corpus_stats()
    print(f"      Corpus: {stats['publications']} publicações, {stats['chunks']} chunks")

    check(stats["chunks"] > 0, "há chunks indexados no grafo")
    check(
        stats["has_chunk_relations"] == stats["chunks"],
        "toda relação HAS_CHUNK corresponde a um chunk",
    )

    rows = repo._run("MATCH (p:Publication) RETURN p.title AS title LIMIT 1")
    check(bool(rows), "há ao menos uma publicação no grafo")
    return rows[0]["title"]


def test_semantic_search(repo: RetrievalRepository, embed) -> None:
    """Busca vetorial devolve evidência citável."""
    print(f"      Caminho: {'cláusula SEARCH' if repo.supports_search else 'queryNodes (legado)'}")

    vector = embed("How does microgravity affect bone tissue?")
    passages = repo.semantic_search(vector, top_k=3)

    check(len(passages) > 0, f"{len(passages)} trecho(s) recuperado(s)")
    check(len(passages) <= 3, "top_k é respeitado")
    check(
        all(isinstance(passage, EvidencePassage) for passage in passages),
        "resultados são EvidencePassage tipados",
    )
    check(all(passage.text.strip() for passage in passages), "todo trecho traz TEXTO")
    check(
        all(passage.source_url for passage in passages),
        "todo trecho traz a URL da fonte",
    )
    check(
        all(0.0 <= passage.score <= 1.0 for passage in passages),
        "scores de cosseno em [0, 1]",
    )
    scores = [passage.score for passage in passages]
    check(scores == sorted(scores, reverse=True), "resultados vêm ordenados por relevância")

    source = passages[0].to_source()
    # Conjunto EXATO de propósito: a asserção existe para pegar campo
    # acrescentado ou removido sem intenção. Quando o contrato muda de
    # verdade, atualizar esta linha é parte da mudança -- foi o que aconteceu
    # na E3-06, que acrescentou `citation`.
    check(
        set(source) == {
            "publication_id", "title", "url", "doi", "passage",
            "section", "page", "relevance", "citation",
        },
        "to_source() cumpre o contrato de evidência do §14",
    )
    citacao = source["citation"]
    check(
        set(citacao) == {"authors", "year", "journal", "volume", "issue", "pages"},
        "o bloco `citation` traz os campos que ABNT e BibTeX consomem (E3-06)",
    )
    check(
        isinstance(citacao["authors"], list),
        "autores sempre lista, mesmo quando a publicação não tem metadados",
    )
    # O §15.4 proíbe FABRICAR DOI, não tê-lo. Desde a regra R7 do cleaner, 488
    # das 493 publicações trazem o DOI real extraído do cabeçalho do PMC; as
    # outras 5 vêm com None, que é a representação honesta da ausência.
    check(
        source["doi"] is None or source["doi"].startswith("10."),
        f"DOI é real ou None, nunca fabricado (§15.4): {source['doi']}",
    )
    check(source["page"] is None, "page vem None — corpus HTML não tem paginação")


def test_search_filters(repo: RetrievalRepository, embed, known_title: str) -> None:
    """Filtro por publicação e validação de dimensão."""
    vector = embed("spaceflight experiment")

    filtered = repo.semantic_search(vector, top_k=3, publication_title=known_title)
    check(len(filtered) > 0, f"filtro por publicação existente devolve {len(filtered)} trecho(s)")
    check(
        all(passage.publication_title == known_title for passage in filtered),
        "todo resultado pertence à publicação filtrada",
    )

    missing = repo.semantic_search(
        vector, top_k=3, publication_title="Publicação Que Não Existe 12345"
    )
    check(missing == [], "filtro por publicação inexistente devolve lista vazia")

    try:
        repo.semantic_search([0.1, 0.2, 0.3], top_k=1)
        check(False, "embedding com dimensão errada deveria falhar")
    except ValueError:
        check(True, "embedding com dimensão errada levanta ValueError")

    try:
        repo.semantic_search(vector, top_k=0)
        check(False, "top_k=0 deveria falhar")
    except ValueError:
        check(True, "top_k inválido levanta ValueError")


def test_injection_resistance(repo: RetrievalRepository) -> None:
    """Payloads hostis viajam como dados, nunca como Cypher."""
    before = repo.corpus_stats()

    for payload in INJECTION_PAYLOADS:
        try:
            results = repo.publications_by_keywords([payload], limit=5)
        except Exception as error:  # noqa: BLE001 — qualquer erro aqui é falha do teste
            check(False, f"payload {payload!r} causou {type(error).__name__}: {error}")
            return
        check(
            isinstance(results, list),
            f"payload {payload!r} tratado como dado (retornou {len(results)} resultado(s))",
        )

    after = repo.corpus_stats()
    check(before == after, "nenhum payload alterou o conteúdo do grafo")

    # Mistura de payload com termo real, para exercitar o UNWIND com vários itens.
    mixed = repo.publications_by_keywords(["bone", "') RETURN 1 //", "mice"], limit=5)
    check(isinstance(mixed, list), "lista mista de keywords é processada sem erro")

    print("\n      Para comparação, o que a construção ANTIGA geraria com um payload:")
    print("      " + "-" * 62)
    for line in legacy_interpolated_query(["bone", "') RETURN 1 AS pwned //"]).splitlines():
        print(f"      | {line}")
    print("      " + "-" * 62)
    print("      (string apenas ilustrativa — nunca é executada por este teste)")


def test_keyword_search_contract(repo: RetrievalRepository) -> None:
    """Busca por entidades: contrato e comportamento com o grafo atual."""
    empty_cases = {
        "lista vazia": [],
        "strings em branco": ["", "   "],
        "None filtrado": ["", None],  # type: ignore[list-item]
    }
    for label, keywords in empty_cases.items():
        check(repo.publications_by_keywords(keywords) == [], f"{label} -> lista vazia")

    results = repo.publications_by_keywords(["bone", "microgravity"], limit=5)
    check(isinstance(results, list), "busca por keywords reais executa sem erro")

    if results:
        check(
            all("matched_keywords" in row for row in results),
            "resultados trazem a contagem de keywords atendidas (ranking OR)",
        )
        counts = [row["matched_keywords"] for row in results]
        check(counts == sorted(counts, reverse=True), "ordenado por keywords atendidas")
    else:
        print(
            "      [NOTA] 0 resultados: o grafo ainda não tem relações MENTIONS.\n"
            "             Esperado até a Fase 2 (SPACEBIO-017+). A query está\n"
            "             correta e parametrizada; falta o dado."
        )


def test_lookups(repo: RetrievalRepository, known_title: str) -> None:
    """Lookups auxiliares de publicação e chunks."""
    publication = repo.publication_by_title(known_title)
    check(publication is not None, "publication_by_title encontra a publicação")
    check(publication["chunk_count"] > 0, f"publicação tem {publication['chunk_count']} chunks")

    missing = repo.publication_by_title("Não Existe 98765")
    check(missing is None, "publication_by_title devolve None para título inexistente")

    apostrophe = repo.publication_by_title("O'Brien's Study: 100% \"real\"")
    check(apostrophe is None, "título com apóstrofos e aspas não quebra a query")

    chunks = repo.chunks_for_publication(known_title, limit=5)
    check(len(chunks) > 0, f"chunks_for_publication devolve {len(chunks)} trecho(s)")
    check(len(chunks) <= 5, "limite é respeitado")
    check(
        all(passage.publication_title == known_title for passage in chunks),
        "todos os chunks pertencem à publicação pedida",
    )


# ---------------------------------------------------------------------- #
# Orquestração
# ---------------------------------------------------------------------- #


def run() -> int:
    print("=" * 78)
    print("SPACEBIO-012 — Testes de recuperação parametrizada")
    print("=" * 78)

    driver = build_driver()
    try:
        repo = RetrievalRepository(driver)

        section("1/6", "Corpus indexado")
        known_title = test_corpus_present(repo)

        section("2/6", "Busca semântica devolve evidência")
        from embedder import EmbeddingService

        service = EmbeddingService()
        test_semantic_search(repo, service.embed_query)

        section("3/6", "Filtros e validação de entrada")
        test_search_filters(repo, service.embed_query, known_title)

        section("4/6", "Resistência a injeção de Cypher")
        test_injection_resistance(repo)

        section("5/6", "Busca por keywords (contrato)")
        test_keyword_search_contract(repo)

        section("6/6", "Lookups auxiliares")
        test_lookups(repo, known_title)

    finally:
        driver.close()

    print("\n" + "=" * 78)
    print("SPACEBIO-012 — todas as asserções passaram.")
    print("=" * 78)
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Testes da SPACEBIO-012")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )

    try:
        return run()
    except RetrievalTestFailure as failure:
        print(f"\nTESTE REPROVADO: {failure}")
        return 1
    except MissingCredentialError as error:
        print(f"\nCONFIGURAÇÃO AUSENTE:\n{error}")
        return 2
    except Exception as error:  # noqa: BLE001
        print(f"\nERRO INESPERADO: {type(error).__name__}: {error}")
        import traceback

        traceback.print_exc()
        return 3


if __name__ == "__main__":
    sys.exit(main())
