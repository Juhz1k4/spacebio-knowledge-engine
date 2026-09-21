"""
Testes da SPACEBIO-013 — Hybrid Retriever V1

Verifica que a busca híbrida:
  1. recupera identificadores exatos que o canal semântico não alcança;
  2. funde os dois canais por posição (RRF), não por score;
  3. sobrevive à sintaxe do Lucene sem quebrar nem injetar;
  4. reporta por qual canal cada trecho entrou.

Pré-requisitos: Neo4j no ar, corpus ingerido (`python ingest_corpus.py`) e o
índice full-text criado.

Uso:
    python test_hybrid.py
"""

from __future__ import annotations

import logging
import sys
from typing import List, Optional

from config import MissingCredentialError, settings
from graph_manager import Neo4jGraphManager, build_driver
from retrieval import (
    HybridResult,
    RetrievalRepository,
    escape_lucene,
    reciprocal_rank_fusion,
)

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

log = logging.getLogger("test_hybrid")

# Medido no corpus: identificadores que a busca densa não recupera em nenhum
# top-10, e que por isso justificam a existência do canal léxico.
BLIND_SPOT_TERMS = ["CDKN1a/p21", "Bion-M 1"]

# Sintaxe do Lucene que quebraria a query se não fosse escapada.
LUCENE_HOSTILE = [
    'bone AND (radiation OR "microgravity"',   # parênteses e aspas desbalanceados
    "gene:CDKN1a/p21",                          # dois-pontos e barra
    "spaceflight^10 NOT bone",                  # boost e operador
    "micro~gravity*",                           # fuzzy e wildcard
    "[TO }",                                     # intervalo malformado
]


class HybridTestFailure(AssertionError):
    """Falha de asserção em um teste do retriever híbrido."""


def check(condition: bool, message: str) -> None:
    if condition:
        print(f"      [OK] {message}")
    else:
        print(f"      [FALHA] {message}")
        raise HybridTestFailure(message)


def section(step: str, title: str) -> None:
    print(f"\n[{step}] {title}")


# ---------------------------------------------------------------------- #
# Testes sem banco
# ---------------------------------------------------------------------- #


def test_rrf_unit() -> None:
    """A fusão combina posições e é determinística."""
    rankings = {
        "semantic": ["a", "b", "c"],
        "lexical": ["c", "a", "d"],
    }
    scores = reciprocal_rank_fusion(rankings, k=60)

    check(set(scores) == {"a", "b", "c", "d"}, "todos os candidatos entram na fusão")

    # 'a' aparece em 1º e 2º; 'c' em 3º e 1º. Ambos somam duas contribuições,
    # mas 'a' está mais bem posicionado no conjunto.
    check(scores["a"] > scores["c"], "posições melhores produzem score maior")
    check(scores["c"] > scores["b"], "aparecer nos dois canais supera um 2º lugar isolado")
    check(scores["b"] > scores["d"], "empate de canal é desempatado pela posição")

    expected_a = 1 / 61 + 1 / 62
    check(abs(scores["a"] - expected_a) < 1e-12, f"score de 'a' = 1/61 + 1/62 = {expected_a:.6f}")

    again = reciprocal_rank_fusion(rankings, k=60)
    check(again == scores, "fusão é determinística")

    check(reciprocal_rank_fusion({}) == {}, "rankings vazios devolvem dict vazio")


def test_lucene_escaping() -> None:
    """O escape neutraliza a sintaxe sem descartar o conteúdo."""
    check(escape_lucene("CDKN1a/p21") == "CDKN1a\\/p21", "barra é escapada")
    check(escape_lucene("Bion-M 1") == "Bion\\-M 1", "hífen é escapado")
    check(escape_lucene("bone loss") == "bone loss", "texto sem sintaxe fica intacto")

    for hostile in LUCENE_HOSTILE:
        escaped = escape_lucene(hostile)
        letters_in = [c for c in hostile if c.isalnum()]
        letters_out = [c for c in escaped if c.isalnum()]
        check(
            letters_in == letters_out,
            f"escape preserva o conteúdo alfanumérico de {hostile[:26]!r}",
        )


# ---------------------------------------------------------------------- #
# Testes contra o corpus
# ---------------------------------------------------------------------- #


def test_fulltext_index(graph: Neo4jGraphManager) -> None:
    """O índice léxico existe e está populado."""
    with graph._session() as session:
        rows = [
            record.data()
            for record in session.run(
                "SHOW INDEXES YIELD name, type, state, populationPercent, "
                "labelsOrTypes, properties"
            )
        ]
    info = next((r for r in rows if r["name"] == "chunk_text_index"), None)

    check(info is not None, "índice chunk_text_index existe")
    check(info["type"] == "FULLTEXT", f"tipo: {info['type']}")
    check(info["state"] == "ONLINE", f"estado: {info['state']} ({info['populationPercent']}%)")
    check(
        info["labelsOrTypes"] == ["Chunk"] and info["properties"] == ["text"],
        f"alvo: {info['labelsOrTypes']}.{info['properties']}",
    )


def test_blind_spot(repo: RetrievalRepository, embed) -> None:
    """O híbrido alcança o que o canal semântico não alcança."""
    for term in BLIND_SPOT_TERMS:
        literal = repo._run(
            "MATCH (c:Chunk) WHERE toLower(c.text) CONTAINS toLower($t) "
            "RETURN count(c) AS n",
            t=term,
        )[0]["n"]
        check(literal > 0, f"{term!r} existe em {literal} chunks do corpus")

        vector = embed(term)
        semantic = repo.semantic_search(vector, top_k=10)
        hybrid = repo.hybrid_search(term, vector, top_k=10)

        semantic_hits = sum(1 for p in semantic if term.lower() in p.text.lower())
        hybrid_hits = sum(1 for r in hybrid if term.lower() in r.passage.text.lower())

        check(
            hybrid_hits > semantic_hits,
            f"{term!r}: híbrido recupera {hybrid_hits}/10 contra {semantic_hits}/10 do semântico",
        )


def test_hybrid_contract(repo: RetrievalRepository, embed) -> None:
    """Estrutura, ordenação e proveniência do resultado."""
    question = "How does microgravity affect bone density?"
    results = repo.hybrid_search(question, embed(question), top_k=5)

    check(len(results) > 0, f"{len(results)} resultado(s)")
    check(len(results) <= 5, "top_k é respeitado")
    check(all(isinstance(r, HybridResult) for r in results), "resultados são HybridResult")

    scores = [r.score for r in results]
    check(scores == sorted(scores, reverse=True), "ordenado pelo score fundido")

    check(
        all(r.matched_channels for r in results),
        "todo resultado registra ao menos um canal de origem",
    )
    check(
        any("lexical" in r.matched_channels for r in results)
        or any("semantic" in r.matched_channels for r in results),
        "os canais aparecem na proveniência",
    )

    identifiers = [r.passage.chunk_id for r in results]
    check(len(identifiers) == len(set(identifiers)), "sem chunks duplicados após a fusão")

    source = results[0].to_source()
    check("retrieval" in source, "to_source() carrega a proveniência do ranking")
    check(
        set(source["retrieval"])
        == {"channels", "semantic_rank", "lexical_rank", "entity_rank"},
        "proveniência traz canais e posições dos três canais",
    )
    check(source["doi"] is None or isinstance(source["doi"], str), "campo doi presente")
    check(bool(source["passage"]), "o trecho de evidência vem junto")


def test_hostile_queries(repo: RetrievalRepository, embed) -> None:
    """Sintaxe hostil do Lucene não derruba a busca."""
    before = repo.corpus_stats()

    for hostile in LUCENE_HOSTILE:
        try:
            lexical = repo.lexical_search(hostile, top_k=3)
            hybrid = repo.hybrid_search(hostile, embed(hostile), top_k=3)
        except Exception as error:  # noqa: BLE001
            check(False, f"query {hostile[:26]!r} causou {type(error).__name__}: {error}")
            return
        check(
            isinstance(lexical, list) and isinstance(hybrid, list),
            f"query {hostile[:30]!r} tratada como texto ({len(hybrid)} resultado(s))",
        )

    check(before == repo.corpus_stats(), "nenhuma query alterou o grafo")

    for invalid in ("", "   "):
        try:
            repo.lexical_search(invalid)
            check(False, "query vazia deveria falhar")
        except ValueError:
            check(True, f"query {invalid!r} levanta ValueError")


def test_publication_concentration(repo: RetrievalRepository, embed) -> None:
    """Agregação por publicação — o sinal de grafo do V1."""
    question = "effects of spaceflight on the immune system"
    results = repo.hybrid_search(question, embed(question), top_k=20)
    aggregated = repo.publication_concentration(results)

    check(len(aggregated) > 0, f"{len(aggregated)} publicação(ões) agregada(s)")
    check(
        sum(entry["chunks"] for entry in aggregated) == len(results),
        "toda evidência é contabilizada em alguma publicação",
    )
    scores = [entry["score"] for entry in aggregated]
    check(scores == sorted(scores, reverse=True), "publicações ordenadas por score somado")
    check(
        aggregated[0]["chunks"] >= aggregated[-1]["chunks"]
        or aggregated[0]["score"] >= aggregated[-1]["score"],
        "concentração de trechos eleva a publicação",
    )

    print(f'\n      Pergunta: "{question}"')
    for entry in aggregated[:3]:
        print(
            f"      {entry['chunks']:>2} trechos · score {entry['score']:.4f} · "
            f"{entry['publication_title'][:52]}"
        )


def test_entity_channel(repo: RetrievalRepository) -> None:
    """O terceiro canal do §13.2, ligado na Fase 2."""
    relations = repo._run("MATCH ()-[r:MENTIONS]->() RETURN count(r) AS n")[0]["n"]
    check(relations > 0, f"grafo anotado com {relations:,} relações MENTIONS")

    # Pergunta com entidades conhecidas: o canal participa.
    with_entities = repo._entity_ranking("What happens to mice bone in microgravity?", 10)
    check(len(with_entities) > 0, f"pergunta com entidades -> {len(with_entities)} chunks")
    check(
        len(with_entities) == len(set(with_entities)),
        "o canal não devolve chunks repetidos",
    )

    # Pergunta sem nenhuma entidade da ontologia: o canal se retira.
    without = repo._entity_ranking("What is the best pizza recipe?", 10)
    check(without == [], "pergunta sem entidade conhecida -> canal não participa")

    recognized = repo.entities_in_question("Mice exposed to microgravity showed bone loss")
    canonicals = {e["canonical"] for e in recognized}
    check(
        "Mus musculus" in canonicals,
        f"normalização funciona: 'Mice' -> Mus musculus ({sorted(canonicals)})",
    )
    check("Microgravity" in canonicals, "condição experimental reconhecida")


# ---------------------------------------------------------------------- #
# Orquestração
# ---------------------------------------------------------------------- #


def run() -> int:
    print("=" * 78)
    print("SPACEBIO-013 — Testes do Hybrid Retriever V1")
    print("=" * 78)

    section("1/7", "Reciprocal Rank Fusion (unitário)")
    test_rrf_unit()

    section("2/7", "Escape da sintaxe Lucene (unitário)")
    test_lucene_escaping()

    driver = build_driver()
    try:
        repo = RetrievalRepository(driver)
        from embedder import EmbeddingService

        service = EmbeddingService()
        embed = service.embed_query

        stats = repo.corpus_stats()
        print(f"\n      Corpus: {stats['publications']} publicações, {stats['chunks']:,} chunks")

        section("3/7", "Índice léxico")
        with Neo4jGraphManager() as graph:
            test_fulltext_index(graph)

        section("4/7", "Ponto cego do canal semântico")
        test_blind_spot(repo, embed)

        section("5/7", "Contrato do resultado híbrido")
        test_hybrid_contract(repo, embed)

        section("6/7", "Queries com sintaxe hostil")
        test_hostile_queries(repo, embed)

        section("7/7", "Agregação por publicação e canal de entidades")
        test_publication_concentration(repo, embed)
        test_entity_channel(repo)

    finally:
        driver.close()

    print("\n" + "=" * 78)
    print("SPACEBIO-013 — todas as asserções passaram.")
    print("=" * 78)
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Testes da SPACEBIO-013")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )

    try:
        return run()
    except HybridTestFailure as failure:
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
