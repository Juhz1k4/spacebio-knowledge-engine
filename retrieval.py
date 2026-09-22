"""
SPACEBIO-012 — Parameterized Retrieval Queries

Biblioteca única de consultas de recuperação do SpaceBio. Todo acesso de
LEITURA ao grafo passa por aqui; o graph_manager cuida de schema e escrita.

O problema que esta issue resolve (Master Briefing §8.1):

    # antes, em main.py — input do usuário concatenado na Cypher
    match_clauses.append(
        f"MATCH (p)-[:MENTIONS]->(e{i}) WHERE toLower(e{i}.name) CONTAINS '{keyword.lower()}'"
    )

Isso permitia injeção de Cypher, quebrava com apóstrofos (`O'Brien`, `Parkinson's`)
e, por gerar um MATCH por keyword, exigia que a publicação casasse com TODAS
elas — o "AND implícito" do §9.3, que derruba o recall.

Aqui, nenhum valor entra em query por concatenação. A única interpolação que
resta é o NOME DO ÍNDICE na subcláusula `VECTOR INDEX`, porque o Cypher não
aceita parâmetro em identificador de esquema; ele vem da configuração da
aplicação e passa por lista branca em `graph_manager.validate_index_name`.

Compatibilidade de servidor:
    Neo4j >= 2026.01 → cláusula SEARCH (Cypher 25)
    Neo4j 5.x        → CALL db.index.vector.queryNodes (deprecada em 2026.04)

A escolha é automática, pela versão reportada pelo servidor.

Uso:
    from neo4j import GraphDatabase
    from config import settings
    from embedder import EmbeddingService
    from retrieval import RetrievalRepository

    driver = GraphDatabase.driver(settings.neo4j_uri, auth=settings.neo4j_auth())
    repo = RetrievalRepository(driver)
    service = EmbeddingService()

    passages = repo.semantic_search(service.embed_query("bone loss in microgravity"))
    for passage in passages:
        print(passage.score, passage.text[:80], passage.source_url)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence

from config import settings
from graph_manager import (
    FULLTEXT_INDEX_NAME,
    read_server_version,
    supports_search_clause,
    validate_index_name,
)

log = logging.getLogger(__name__)

# Ao filtrar por publicação depois da busca vetorial, pedimos mais vizinhos do
# que o necessário para não devolver menos que top_k. Ver semantic_search.
OVERFETCH_FACTOR = 10
MAX_OVERFETCH = 500

# --------------------------------------------------------------------- #
# Fusão híbrida (SPACEBIO-013)
# --------------------------------------------------------------------- #

# Constante da Reciprocal Rank Fusion. 60 é o valor do paper original
# (Cormack et al., 2009) e o padrão de facto: amortece o peso das primeiras
# posições o bastante para que um bom resultado do canal B não seja esmagado
# pelo 1º lugar do canal A.
RRF_K = 60

# Quantos candidatos pedir a cada canal antes de fundir. Precisa ser bem maior
# que o top_k final para que a fusão tenha material com que trabalhar.
CANDIDATE_POOL = 50

# Caracteres com significado sintático no Lucene. Sem escapá-los, uma busca
# por "CDKN1a/p21" vira uma expressão inválida e a query inteira falha.
LUCENE_SPECIAL = r'+-&|!(){}[]^"~*?:\/'


def escape_lucene(query: str) -> str:
    """
    Escapa a sintaxe do Lucene para tratar a query como texto literal.

    Os termos que mais interessam ao canal léxico são justamente os que
    carregam pontuação especial — `CDKN1a/p21`, `Bion-M 1`, `OSD-570`. Sem
    escape, a barra e o hífen são operadores e a busca quebra ou muda de
    sentido.
    """
    escaped = []
    for char in query:
        if char in LUCENE_SPECIAL:
            escaped.append("\\")
        escaped.append(char)
    return "".join(escaped)


def reciprocal_rank_fusion(
    rankings: Dict[str, List[str]], k: int = RRF_K
) -> Dict[str, float]:
    """
    Funde múltiplos rankings pela posição, não pelo score.

    score(d) = Σ 1 / (k + rank(d))

    Por que não somar os scores diretamente: similaridade de cosseno vive em
    [0,1] e BM25 é ilimitado e dependente da query. Normalizá-los exigiria uma
    escala arbitrária que muda a cada busca. A RRF ignora as magnitudes e
    combina só a ordem — que é o que os dois canais concordam em significar.

    Args:
        rankings: {nome_do_canal: [id em ordem de relevância]}

    Returns:
        {id: score fundido}
    """
    scores: Dict[str, float] = {}
    for ranked_ids in rankings.values():
        for position, identifier in enumerate(ranked_ids, start=1):
            scores[identifier] = scores.get(identifier, 0.0) + 1.0 / (k + position)
    return scores


@dataclass(frozen=True)
class EvidencePassage:
    """
    Um trecho recuperado, com a procedência necessária para citá-lo.

    É a unidade de evidência do §14 do briefing: nunca só o título da
    publicação, sempre o texto que sustenta a afirmação.
    """

    chunk_id: str
    publication_title: str
    text: str
    section: Optional[str]
    page: Optional[int]
    source_url: Optional[str]
    score: float
    doi: Optional[str] = None
    journal: Optional[str] = None

    # E3-06 -- metadados bibliográficos vindos do Crossref, gravados no grafo
    # por `enrich_metadata.py`. Chegam junto da passagem porque a alternativa
    # seria o frontend consultar a rede uma vez por cartão de fonte, somando
    # seis idas externas ao caminho de leitura de cada resposta.
    #
    # Todos opcionais: 5 das 493 publicações não têm DOI e portanto não têm
    # registro no Crossref. A citação delas cai na forma simplificada.
    authors: Optional[List[str]] = None
    publication_year: Optional[int] = None
    volume: Optional[str] = None
    issue: Optional[str] = None
    pages: Optional[str] = None

    def to_source(self) -> Dict[str, Any]:
        """
        Serializa no formato de `sources` do Scientific Evidence Contract (§14).

        `doi` vem do grafo (regra R7 do cleaner) e é None nas 5 publicações
        cujo cabeçalho do PMC não trazia um — o §15 proíbe fabricar. `page` é
        sempre None: o corpus é HTML e não tem paginação.
        """
        return {
            "publication_id": self.publication_title,
            "title": self.publication_title,
            "url": self.source_url,
            "doi": self.doi,
            "passage": self.text,
            "section": self.section,
            "page": self.page,
            "relevance": round(self.score, 4),
            # Agrupado, e não sete campos soltos no topo: o bloco inteiro é
            # o que a formatação de citação consome, e mantê-lo junto deixa
            # claro que ele vem de uma origem só (Crossref) e pode faltar
            # inteiro.
            "citation": {
                "authors": self.authors or [],
                "year": self.publication_year,
                "journal": self.journal,
                "volume": self.volume,
                "issue": self.issue,
                "pages": self.pages,
            },
        }


@dataclass(frozen=True)
class HybridResult:
    """
    Um trecho recuperado pela busca híbrida, com a proveniência do ranking.

    Guardar a posição em cada canal não é diagnóstico opcional: é o que
    permite à resposta dizer se o trecho veio por correspondência de termo
    (o pesquisador citou um gene) ou por proximidade semântica (ele descreveu
    um conceito) — e é o dado que a avaliação da Fase 4 vai precisar.
    """

    passage: EvidencePassage
    score: float
    semantic_rank: Optional[int] = None
    lexical_rank: Optional[int] = None
    entity_rank: Optional[int] = None

    @property
    def matched_channels(self) -> List[str]:
        channels = []
        if self.semantic_rank is not None:
            channels.append("semantic")
        if self.lexical_rank is not None:
            channels.append("lexical")
        if self.entity_rank is not None:
            channels.append("entity")
        return channels

    def to_source(self) -> Dict[str, Any]:
        """Contrato de evidência (§14), acrescido de como o trecho foi achado."""
        source = self.passage.to_source()
        source["relevance"] = round(self.score, 6)
        source["retrieval"] = {
            "channels": self.matched_channels,
            "semantic_rank": self.semantic_rank,
            "lexical_rank": self.lexical_rank,
            "entity_rank": self.entity_rank,
        }
        return source


def _passage_from_record(record: Dict[str, Any]) -> EvidencePassage:
    return EvidencePassage(
        chunk_id=record["chunk_id"],
        publication_title=record["publication_title"],
        text=record["text"],
        section=record.get("section"),
        page=record.get("page"),
        source_url=record.get("source_url"),
        score=float(record["score"]),
        doi=record.get("doi"),
        journal=record.get("journal"),
        authors=record.get("authors"),
        publication_year=record.get("publication_year"),
        volume=record.get("volume"),
        issue=record.get("issue"),
        pages=record.get("pages"),
    )


class RetrievalRepository:
    """
    Consultas de recuperação parametrizadas.

    Recebe um driver já construído em vez de criar o seu — o driver tem
    ciclo de vida de aplicação (§21), não de request.
    """

    def __init__(
        self,
        driver,
        database: Optional[str] = None,
        index_name: Optional[str] = None,
        dimension: Optional[int] = None,
        fulltext_index_name: Optional[str] = None,
    ):
        self.driver = driver
        self.database = database or settings.neo4j_database
        self.index_name = validate_index_name(index_name or settings.vector_index_name)
        self.fulltext_index_name = validate_index_name(
            fulltext_index_name or FULLTEXT_INDEX_NAME
        )
        self.dimension = dimension or settings.embedding_dimension
        self._supports_search: Optional[bool] = None
        self._entity_extractor = None

    # ------------------------------------------------------------------ #
    # Capacidades do servidor
    # ------------------------------------------------------------------ #

    @property
    def supports_search(self) -> bool:
        """True se o servidor entende a cláusula SEARCH (Neo4j >= 2026.01)."""
        if self._supports_search is None:
            version = read_server_version(self.driver, self.database)
            self._supports_search = supports_search_clause(version)
            log.info(
                "Neo4j %s — busca vetorial via %s.",
                version,
                "cláusula SEARCH" if self._supports_search else "db.index.vector.queryNodes",
            )
        return self._supports_search

    def _run(self, cypher: str, **params) -> List[Dict[str, Any]]:
        """Executa uma query de leitura em transação gerenciada."""
        with self.driver.session(database=self.database) as session:
            return session.execute_read(
                lambda tx: [record.data() for record in tx.run(cypher, **params)]
            )

    # ------------------------------------------------------------------ #
    # Busca semântica (vetorial)
    # ------------------------------------------------------------------ #

    # Busca exata dentro de uma publicação. Não usa o índice ANN: com 493
    # publicações e 46 mil chunks, pós-filtrar os vizinhos globais devolvia
    # ZERO resultados, porque os mais próximos do corpus inteiro quase nunca
    # pertencem à publicação pedida. Aqui a similaridade é calculada
    # diretamente sobre os chunks daquela publicação — mediana de 89 por
    # publicação, então é barato e, além de barato, exato.
    _SCOPED_SEARCH_CYPHER = """
    MATCH (p:Publication {title: $publication_title})-[:HAS_CHUNK]->(c:Chunk)
    WHERE c.embedding IS NOT NULL
    WITH p, c, vector.similarity.cosine(c.embedding, $embedding) AS score
    RETURN c.id          AS chunk_id,
           p.title       AS publication_title,
           c.text        AS text,
           c.section     AS section,
           c.page        AS page,
           p.source_url  AS source_url,
           p.doi         AS doi,
           p.journal     AS journal,
           p.authors AS authors,
           p.publication_year AS publication_year,
           p.volume AS volume,
           p.issue AS issue,
           p.pages AS pages,
           score
    ORDER BY score DESC
    LIMIT $top_k
    """

    def _semantic_search_cypher(self) -> str:
        """
        Monta a query de busca vetorial conforme a versão do servidor.

        O nome do índice é interpolado por exigência do Cypher (identificador
        de esquema não aceita parâmetro) e já foi validado no __init__.
        Todo o resto — vetor, top_k, filtro — é parâmetro.
        """
        projection = """
        MATCH (p:Publication)-[:HAS_CHUNK]->(c)
        WHERE $publication_title IS NULL OR p.title = $publication_title
        RETURN c.id          AS chunk_id,
               p.title       AS publication_title,
               c.text        AS text,
               c.section     AS section,
               c.page        AS page,
               p.source_url  AS source_url,
               p.doi         AS doi,
               p.journal     AS journal,
               p.authors AS authors,
               p.publication_year AS publication_year,
               p.volume AS volume,
               p.issue AS issue,
               p.pages AS pages,
               score
        ORDER BY score DESC
        LIMIT $top_k
        """

        if self.supports_search:
            return f"""
        MATCH (c:Chunk)
          SEARCH c IN (VECTOR INDEX {self.index_name} FOR $embedding LIMIT $neighbours)
          SCORE AS score
        {projection}
        """

        # Caminho legado (Neo4j 5.x): a procedure aceita o nome do índice como
        # parâmetro, então aqui nem isso é interpolado.
        return f"""
        CALL db.index.vector.queryNodes($index_name, $neighbours, $embedding)
        YIELD node AS c, score
        {projection}
        """

    def semantic_search(
        self,
        embedding: Sequence[float],
        top_k: int = 5,
        publication_title: Optional[str] = None,
    ) -> List[EvidencePassage]:
        """
        Recupera os trechos semanticamente mais próximos da pergunta.

        Args:
            embedding: vetor da pergunta, produzido por EmbeddingService.embed_query.
            top_k: quantos trechos devolver.
            publication_title: restringe a uma publicação específica.

        Dois caminhos, conforme o filtro:

        - sem `publication_title`, usa o índice vetorial (busca aproximada
          sobre o corpus inteiro, que é para isso que ele existe);
        - com `publication_title`, calcula a similaridade exata sobre os chunks
          daquela publicação. Pós-filtrar o resultado do índice não funciona em
          escala: com 46 mil chunks, os vizinhos mais próximos do corpus quase
          nunca pertencem à publicação pedida, e a busca voltava vazia.

        Raises:
            ValueError: se a dimensão do vetor não bater com a do índice.
        """
        if len(embedding) != self.dimension:
            raise ValueError(
                f"Embedding da query tem {len(embedding)} dimensões, "
                f"mas o índice espera {self.dimension}."
            )
        if top_k < 1:
            raise ValueError("top_k precisa ser >= 1.")

        if publication_title is not None:
            rows = self._run(
                self._SCOPED_SEARCH_CYPHER,
                embedding=list(embedding),
                top_k=int(top_k),
                publication_title=publication_title,
            )
            log.info("Busca semântica restrita: %d trecho(s).", len(rows))
            return [_passage_from_record(row) for row in rows]

        rows = self._run(
            self._semantic_search_cypher(),
            embedding=list(embedding),
            neighbours=int(top_k),
            top_k=int(top_k),
            publication_title=None,
            index_name=self.index_name,  # usado apenas no caminho legado
        )

        log.info("Busca semântica: %d trecho(s) para top_k=%d.", len(rows), top_k)
        return [_passage_from_record(row) for row in rows]

    # ------------------------------------------------------------------ #
    # Busca léxica (BM25) — SPACEBIO-013
    # ------------------------------------------------------------------ #

    def lexical_search(
        self,
        query: str,
        top_k: int = 5,
        publication_title: Optional[str] = None,
    ) -> List[EvidencePassage]:
        """
        Busca por correspondência de termos, via índice full-text (BM25).

        Cobre o ponto cego do vetorial denso. Medido neste corpus: `CDKN1a/p21`
        existe em 18 chunks e `Bion-M 1` em 19, e nenhum dos dois aparece em
        qualquer top-10 da busca semântica.

        O score do BM25 NÃO é comparável ao cosseno — é ilimitado e depende da
        query. Use hybrid_search para combinar os dois canais.
        """
        if not query or not query.strip():
            raise ValueError("Query léxica vazia.")
        if top_k < 1:
            raise ValueError("top_k precisa ser >= 1.")

        cypher = """
        CALL db.index.fulltext.queryNodes($index_name, $search_terms, {limit: $limit})
        YIELD node AS c, score
        MATCH (p:Publication)-[:HAS_CHUNK]->(c)
        WHERE $publication_title IS NULL OR p.title = $publication_title
        RETURN c.id          AS chunk_id,
               p.title       AS publication_title,
               c.text        AS text,
               c.section     AS section,
               c.page        AS page,
               p.source_url  AS source_url,
               p.doi         AS doi,
               p.journal     AS journal,
               p.authors AS authors,
               p.publication_year AS publication_year,
               p.volume AS volume,
               p.issue AS issue,
               p.pages AS pages,
               score
        ORDER BY score DESC
        LIMIT $top_k
        """

        limit = top_k if publication_title is None else min(top_k * OVERFETCH_FACTOR, MAX_OVERFETCH)
        rows = self._run(
            cypher,
            index_name=self.fulltext_index_name,
            # nome do parâmetro é search_terms, não query: `query` colide com o
            # primeiro argumento posicional de tx.run().
            search_terms=escape_lucene(query),
            limit=int(limit),
            top_k=int(top_k),
            publication_title=publication_title,
        )
        log.info("Busca léxica: %d trecho(s) para %r.", len(rows), query[:40])
        return [_passage_from_record(row) for row in rows]

    # ------------------------------------------------------------------ #
    # Busca híbrida — SPACEBIO-013
    # ------------------------------------------------------------------ #

    def hybrid_search(
        self,
        query: str,
        embedding: Sequence[float],
        top_k: int = 5,
        publication_title: Optional[str] = None,
        pool: int = CANDIDATE_POOL,
    ) -> List[HybridResult]:
        """
        Combina os canais semântico e léxico por Reciprocal Rank Fusion.

        Args:
            query: pergunta em texto, para o canal léxico.
            embedding: o mesmo texto vetorizado, para o canal semântico.
            top_k: quantos resultados devolver.
            publication_title: filtro opcional.
            pool: candidatos pedidos a cada canal antes da fusão.

        Returns:
            Lista de HybridResult ordenada pelo score fundido, com a posição
            que o trecho ocupava em cada canal — para que a resposta possa
            explicar POR QUE recuperou aquilo.

        Três canais entram na fusão quando disponíveis (§13):
        semântico (vetorial), léxico (BM25) e entidades (grafo). O canal de
        entidades só participa quando a pergunta menciona alguma entidade da
        ontologia — do contrário produziria um ranking degenerado.
        """
        if top_k < 1:
            raise ValueError("top_k precisa ser >= 1.")

        semantic = self.semantic_search(
            embedding, top_k=pool, publication_title=publication_title
        )
        lexical = self.lexical_search(
            query, top_k=pool, publication_title=publication_title
        )

        by_id: Dict[str, EvidencePassage] = {}
        for passage in semantic + lexical:
            by_id.setdefault(passage.chunk_id, passage)

        rankings = {
            "semantic": [p.chunk_id for p in semantic],
            "lexical": [p.chunk_id for p in lexical],
        }
        entity_ranking = self._entity_ranking(query, pool)
        if entity_ranking:
            rankings["entity"] = entity_ranking
            # O canal de entidades devolve só identificadores — ele ranqueia
            # atravessando o grafo, não recuperando texto. Os chunks que ele
            # trouxe e os outros canais não precisam ter a passagem carregada,
            # senão a fusão não teria o que devolver.
            missing = [cid for cid in entity_ranking if cid not in by_id]
            for passage in self.passages_by_ids(missing):
                by_id[passage.chunk_id] = passage
            # Um id sem passagem (chunk removido entre as duas queries) sai do
            # ranking em vez de derrubar a busca.
            rankings["entity"] = [cid for cid in entity_ranking if cid in by_id]

        fused = reciprocal_rank_fusion(rankings)

        semantic_positions = {cid: i for i, cid in enumerate(rankings["semantic"], 1)}
        lexical_positions = {cid: i for i, cid in enumerate(rankings["lexical"], 1)}
        entity_positions = {
            cid: i for i, cid in enumerate(rankings.get("entity", []), 1)
        }

        results = [
            HybridResult(
                passage=by_id[chunk_id],
                score=score,
                semantic_rank=semantic_positions.get(chunk_id),
                lexical_rank=lexical_positions.get(chunk_id),
                entity_rank=entity_positions.get(chunk_id),
            )
            for chunk_id, score in fused.items()
        ]
        results.sort(key=lambda item: item.score, reverse=True)

        log.info(
            "Busca híbrida: %d candidatos (%d semânticos, %d léxicos) -> top %d.",
            len(results),
            len(semantic),
            len(lexical),
            top_k,
        )
        return results[:top_k]

    @property
    def entity_extractor(self):
        """Extrator compartilhado — compila ~70 regex, não vale refazer por query."""
        if self._entity_extractor is None:
            from ner import EntityExtractor

            self._entity_extractor = EntityExtractor()
        return self._entity_extractor

    def _entity_ranking(self, query: str, pool: int) -> List[str]:
        """
        Terceiro canal do §13.2: sobreposição de entidades (SPACEBIO-016+).

        Extrai as entidades da pergunta com a mesma ontologia usada para
        anotar o corpus, e ranqueia os chunks por quantas dessas entidades
        eles mencionam. É o único dos três canais que atravessa o GRAFO:
        semântico compara vetores, léxico compara termos, este compara
        conceitos normalizados.

        O valor prático é a normalização. Uma pergunta sobre "mice" alcança
        chunks que só dizem "murine" ou "C57BL/6", porque os três convergiram
        para `Organism:Mus musculus` na anotação. Nem o vetor nem o BM25 fazem
        essa ponte de forma confiável.

        Devolve lista vazia quando a pergunta não menciona entidade conhecida
        ou o grafo ainda não foi anotado — e aí o canal não entra na fusão,
        em vez de contribuir com um ranking degenerado.
        """
        mentions = self.entity_extractor.extract(query)
        if not mentions:
            return []

        keys = sorted({f"{m.label}:{m.canonical}" for m in mentions})

        cypher = """
        UNWIND $entity_keys AS entity_key
        MATCH (e:Entity {key: entity_key})<-[m:MENTIONS]-(c:Chunk)
        WITH c, count(DISTINCT entity_key) AS matched, sum(m.count) AS occurrences
        RETURN c.id AS chunk_id
        ORDER BY matched DESC, occurrences DESC, c.id ASC
        LIMIT $limit
        """
        rows = self._run(cypher, entity_keys=keys, limit=int(pool))
        log.info(
            "Canal de entidades: %d entidade(s) na pergunta -> %d chunk(s).",
            len(keys),
            len(rows),
        )
        return [row["chunk_id"] for row in rows]

    def passages_by_ids(self, chunk_ids: Sequence[str]) -> List[EvidencePassage]:
        """
        Carrega passagens por identificador.

        `score` vem 0.0: estes chunks não foram ranqueados por similaridade, e
        atribuir qualquer outro valor fingiria uma relevância que não foi
        medida. A relevância deles é a posição no ranking de entidades, que a
        RRF já considera.
        """
        if not chunk_ids:
            return []

        cypher = """
        MATCH (p:Publication)-[:HAS_CHUNK]->(c:Chunk)
        WHERE c.id IN $chunk_ids
        RETURN c.id          AS chunk_id,
               p.title       AS publication_title,
               c.text        AS text,
               c.section     AS section,
               c.page        AS page,
               p.source_url  AS source_url,
               p.doi         AS doi,
               p.journal     AS journal,
               p.authors AS authors,
               p.publication_year AS publication_year,
               p.volume AS volume,
               p.issue AS issue,
               p.pages AS pages,
               0.0           AS score
        """
        rows = self._run(cypher, chunk_ids=list(chunk_ids))
        return [_passage_from_record(row) for row in rows]

    def entities_in_question(self, query: str) -> List[Dict[str, str]]:
        """As entidades reconhecidas na pergunta — para o contrato §14."""
        return [
            {"canonical": mention.canonical, "label": mention.label, "surface": mention.surface}
            for mention in self.entity_extractor.extract(query)
        ]

    def entities_for_chunks(self, chunk_ids: Sequence[str]) -> List[Dict[str, Any]]:
        """
        Entidades mencionadas por um conjunto de chunks.

        Alimenta o campo `entities` do contrato de evidência, que estava vazio
        desde a SPACEBIO-014 esperando esta fase.
        """
        if not chunk_ids:
            return []

        cypher = """
        MATCH (c:Chunk)-[m:MENTIONS]->(e:Entity)
        WHERE c.id IN $chunk_ids
        WITH e, sum(m.count) AS occurrences, count(DISTINCT c) AS chunks
        RETURN e.canonical AS canonical, e.label AS label,
               occurrences, chunks
        ORDER BY chunks DESC, occurrences DESC
        """
        return self._run(cypher, chunk_ids=list(chunk_ids))

    def publication_concentration(
        self, results: Sequence["HybridResult"]
    ) -> List[Dict[str, Any]]:
        """
        Agrega os resultados por publicação — um sinal de grafo real.

        Uma publicação com vários trechos entre os melhores é mais provável de
        responder à pergunta que outra com um único acerto isolado. Usa a
        relação HAS_CHUNK, que é o que o grafo oferece hoje.
        """
        by_publication: Dict[str, Dict[str, Any]] = {}
        for item in results:
            title = item.passage.publication_title
            entry = by_publication.setdefault(
                title,
                {
                    "publication_title": title,
                    "source_url": item.passage.source_url,
                    "chunks": 0,
                    "score": 0.0,
                    "best_rank": None,
                },
            )
            entry["chunks"] += 1
            entry["score"] += item.score

        ranked = sorted(by_publication.values(), key=lambda e: e["score"], reverse=True)
        for position, entry in enumerate(ranked, start=1):
            entry["best_rank"] = position
        return ranked

    # ------------------------------------------------------------------ #
    # Busca por entidades mencionadas
    # ------------------------------------------------------------------ #

    def publications_by_keywords(
        self,
        keywords: Sequence[str],
        limit: int = 5,
        min_matches: int = 1,
    ) -> List[Dict[str, Any]]:
        """
        Publicações que mencionam entidades correspondentes às keywords.

        Substitui a construção dinâmica de MATCHes do main.py. Duas mudanças
        de comportamento, ambas deliberadas:

        1. As keywords viajam como UM parâmetro (lista), eliminando a injeção
           de Cypher e o problema com apóstrofos.
        2. O casamento passa a ser OR com ranking por número de keywords
           distintas atendidas, em vez do AND implícito que o §9.3 aponta como
           destruidor de recall. Use `min_matches` para exigir mais rigor.

        NOTA: depende de (:Publication)-[:MENTIONS]->(:Entity), que só será
        populado de forma útil na Fase 2 (SPACEBIO-017 em diante). Hoje
        retorna lista vazia — o que é honesto, não um erro.
        """
        cleaned = [keyword.strip() for keyword in keywords if keyword and keyword.strip()]
        if not cleaned:
            return []

        cypher = """
        UNWIND $keywords AS keyword
        MATCH (p:Publication)-[:MENTIONS]->(e)
        WHERE toLower(e.name) CONTAINS toLower(keyword)
        WITH p,
             count(DISTINCT keyword) AS matched_keywords,
             collect(DISTINCT e.name)[0..10] AS entities
        WHERE matched_keywords >= $min_matches
        RETURN p.title      AS title,
               p.source_url AS source_url,
               matched_keywords,
               entities
        ORDER BY matched_keywords DESC, title ASC
        LIMIT $limit
        """

        rows = self._run(
            cypher,
            keywords=cleaned,
            min_matches=int(min_matches),
            limit=int(limit),
        )
        log.info("Busca por keywords: %d publicação(ões) para %d termo(s).", len(rows), len(cleaned))
        return rows

    # ------------------------------------------------------------------ #
    # Lookups auxiliares
    # ------------------------------------------------------------------ #

    def publication_by_title(self, title: str) -> Optional[Dict[str, Any]]:
        """Metadados de uma publicação e quantos chunks ela possui."""
        cypher = """
        MATCH (p:Publication {title: $title})
        OPTIONAL MATCH (p)-[:HAS_CHUNK]->(c:Chunk)
        RETURN p.title            AS title,
               p.source_url       AS source_url,
               p.extraction_method AS extraction_method,
               count(c)           AS chunk_count
        """
        rows = self._run(cypher, title=title)
        return rows[0] if rows else None

    def chunks_for_publication(self, title: str, limit: int = 50) -> List[EvidencePassage]:
        """
        Chunks de uma publicação, na ordem original do documento.

        `score` vem 0.0: não houve busca por relevância, é leitura sequencial.
        """
        cypher = """
        MATCH (p:Publication {title: $title})-[:HAS_CHUNK]->(c:Chunk)
        RETURN c.id         AS chunk_id,
               p.title      AS publication_title,
               c.text       AS text,
               c.section    AS section,
               c.page       AS page,
               p.source_url AS source_url,
               p.doi        AS doi,
               p.journal    AS journal,
               p.authors AS authors,
               p.publication_year AS publication_year,
               p.volume AS volume,
               p.issue AS issue,
               p.pages AS pages,
               0.0          AS score
        ORDER BY c.position ASC
        LIMIT $limit
        """
        rows = self._run(cypher, title=title, limit=int(limit))
        return [_passage_from_record(row) for row in rows]

    def corpus_stats(self) -> Dict[str, Any]:
        """Contagens do grafo — útil para /health e para diagnosticar recall."""
        cypher = """
        MATCH (p:Publication)
        WITH count(p) AS publications
        MATCH (c:Chunk)
        WITH publications, count(c) AS chunks
        OPTIONAL MATCH (:Publication)-[r:HAS_CHUNK]->(:Chunk)
        RETURN publications, chunks, count(r) AS has_chunk_relations
        """
        rows = self._run(cypher)
        return rows[0] if rows else {"publications": 0, "chunks": 0, "has_chunk_relations": 0}


if __name__ == "__main__":
    import argparse

    import sys

    from graph_manager import build_driver

    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    parser = argparse.ArgumentParser(description="SPACEBIO-012 — busca no corpus")
    parser.add_argument("question", nargs="?", default="How does microgravity affect bone density?")
    parser.add_argument("--top-k", type=int, default=5)
    args = parser.parse_args()

    from embedder import EmbeddingService

    driver = build_driver()
    try:
        repo = RetrievalRepository(driver)
        stats = repo.corpus_stats()
        print(f"Corpus indexado: {stats['publications']} publicações, {stats['chunks']} chunks")
        print(f'Pergunta: "{args.question}"\n')

        vector = EmbeddingService().embed_query(args.question)
        for position, passage in enumerate(repo.semantic_search(vector, top_k=args.top_k), 1):
            print(f"{position}. score={passage.score:.4f}  [{passage.section}]")
            print(f"   {passage.publication_title[:70]}")
            print(f"   {passage.text[:150].strip()}...")
            print(f"   {passage.source_url}\n")
    finally:
        driver.close()
