"""
SPACEBIO-011 — Neo4j Vector Index (e escrita de Chunks no grafo)

Cria e gerencia o índice vetorial nativo do Neo4j 5.x sobre os nós :Chunk,
e escreve o subgrafo de evidência:

    (:Publication)-[:HAS_CHUNK]->(:Chunk)

Índice criado:
    Nó:         Chunk
    Propriedade: embedding
    Dimensões:   384        (all-MiniLM-L6-v2 — SPACEBIO-010)
    Similaridade: cosine

Decisões de implementação:

1. TODA CYPHER É PARAMETRIZADA (Master Briefing §8.1)
   Nada de interpolar valores em query — nem top_k, nem filtros. A única
   exceção é o NOME do índice nos comandos DDL (CREATE/DROP INDEX), porque
   o Cypher não aceita parâmetro em identificador de esquema; por isso o
   nome é validado contra uma lista branca de caracteres antes do uso.

2. CREDENCIAIS VÊM DO AMBIENTE (Master Briefing §8.2)
   Sem senha hardcoded. Ver config.py e .env.example.

3. DRIVER COM CICLO DE VIDA EXPLÍCITO (Master Briefing §21)
   Um driver por manager, usável como context manager. Nada de criar driver
   dentro de loop.

4. ESCRITA EM LOTE COM UNWIND (Master Briefing §20)
   Um round-trip por lote de chunks, em transação gerenciada
   (session.execute_write), com MERGE idempotente. Nada de
   `MATCH (n) DETACH DELETE n`.

5. ESPERA O ÍNDICE FICAR ONLINE
   A população do índice vetorial é ASSÍNCRONA. Consultar logo após escrever
   retorna resultados vazios ou incompletos — por isso `await_index_online()`
   é chamada antes de qualquer busca de verificação.

6. COMPATIBILIDADE COM O GRAFO EXISTENTE
   build_graph.py cria (:Publication {title, source_url}). Para não duplicar
   nós, fazemos MERGE por `title` e apenas anexamos `id` e demais campos.

CLI:
    python graph_manager.py --status         # mostra índice/constraints/contagens
    python graph_manager.py --create-index   # cria constraints + índice vetorial
    python graph_manager.py --drop-index     # remove o índice (reset)
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Sequence

from neo4j import GraphDatabase

from config import settings
from schema import ChunkRecord, PublicationRecord

log = logging.getLogger(__name__)

# Constantes públicas (importadas por test_pipeline.py e pelo retriever)
INDEX_NAME = settings.vector_index_name
EMBEDDING_DIMENSION = settings.embedding_dimension
SIMILARITY_FUNCTION = settings.vector_similarity

# Índice léxico (SPACEBIO-013): BM25 sobre o texto do chunk. Cobre o que o
# vetorial denso não cobre — identificadores exatos como CDKN1a/p21 ou
# Bion-M 1, que medimos não aparecerem em nenhum top-10 semântico.
FULLTEXT_INDEX_NAME = "chunk_text_index"

_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# A cláusula SEARCH (Cypher 25) foi introduzida no Neo4j 2026.01 e substitui
# db.index.vector.queryNodes, deprecada em 2026.04.
SEARCH_CLAUSE_MIN_MAJOR = 2026


def validate_index_name(index_name: str) -> str:
    """
    Valida um nome de índice para uso em DDL e na subcláusula VECTOR INDEX.

    Nem `CREATE VECTOR INDEX` nem a subcláusula `VECTOR INDEX` da SEARCH
    aceitam parâmetro no nome do índice — é um identificador de esquema, não
    um valor. Como o nome precisa ser interpolado, ele passa por esta lista
    branca. O valor vem da configuração da aplicação, nunca de input de
    usuário.
    """
    if not _SAFE_IDENTIFIER.match(index_name):
        raise ValueError(
            f"Nome de índice inválido: {index_name!r}. "
            "Use apenas letras, dígitos e underscore."
        )
    return index_name


def build_driver(uri: Optional[str] = None, auth: Optional[tuple] = None, **kwargs):
    """
    Constrói o driver Neo4j com a política de notificações do projeto.

    Silencia a classe UNRECOGNIZED — avisos de "label/propriedade não existe",
    que o servidor emite o tempo todo enquanto o grafo está incompleto (ex.:
    :MENTIONS antes da Fase 2) e que afogam qualquer saída útil.

    DEPRECATION continua ligada de propósito: foi assim que descobrimos que
    db.index.vector.queryNodes tinha sido substituída por SEARCH.
    """
    kwargs.setdefault("notifications_disabled_classifications", ["UNRECOGNIZED"])
    return GraphDatabase.driver(
        uri or settings.neo4j_uri,
        auth=auth or settings.neo4j_auth(),
        **kwargs,
    )


def read_server_version(driver, database: str) -> str:
    """Lê a versão do kernel Neo4j (ex.: '5.26.0' ou '2026.07.1')."""
    with driver.session(database=database) as session:
        record = session.run(
            "CALL dbms.components() YIELD name, versions "
            "WHERE name = 'Neo4j Kernel' RETURN versions[0] AS version"
        ).single()
    return record["version"] if record else "unknown"


def supports_search_clause(version: str) -> bool:
    """
    Decide se o servidor entende a cláusula SEARCH a partir da sua versão.

    Versões calendário (2026.x) suportam; a linha 5.x usa o caminho legado
    com db.index.vector.queryNodes.
    """
    try:
        major = int(version.split(".")[0])
    except (ValueError, AttributeError):
        return False
    return major >= SEARCH_CLAUSE_MIN_MAJOR

# Constraints de unicidade (Master Briefing §20/§21).
# Publication é chaveada por `title` para permanecer compatível com o
# build_graph.py atual; `id` é gravado como campo para a migração futura
# a um identificador estável (evolução da SPACEBIO-006).
CONSTRAINTS = [
    (
        "publication_title_unique",
        "CREATE CONSTRAINT publication_title_unique IF NOT EXISTS "
        "FOR (p:Publication) REQUIRE p.title IS UNIQUE",
    ),
    (
        "chunk_id_unique",
        "CREATE CONSTRAINT chunk_id_unique IF NOT EXISTS "
        "FOR (c:Chunk) REQUIRE c.id IS UNIQUE",
    ),
    # SPACEBIO-021: a chave da entidade é "Label:canonical" numa única
    # propriedade. Chave composta exigiria node key, que é recurso Enterprise;
    # concatenar mantém a unicidade em qualquer edição do Neo4j.
    (
        "entity_key_unique",
        "CREATE CONSTRAINT entity_key_unique IF NOT EXISTS "
        "FOR (e:Entity) REQUIRE e.key IS UNIQUE",
    ),
]


class Neo4jGraphManager:
    """
    Gerencia índice vetorial, constraints e escrita de chunks no Neo4j.

    Uso:
        with Neo4jGraphManager() as graph:
            graph.create_constraints()
            graph.create_vector_index()
            graph.write_publication_with_chunks(pub, embedded_chunks)
            graph.await_index_online()
            hits = graph.vector_search(query_vector, top_k=5)
    """

    def __init__(
        self,
        uri: Optional[str] = None,
        user: Optional[str] = None,
        password: Optional[str] = None,
        database: Optional[str] = None,
        index_name: str = INDEX_NAME,
        dimension: int = EMBEDDING_DIMENSION,
        similarity: str = SIMILARITY_FUNCTION,
    ):
        """
        Args:
            uri/user/password/database: se omitidos, vêm do ambiente (config.py).
            index_name: nome do índice vetorial. Precisa ser um identificador
                simples ([A-Za-z_][A-Za-z0-9_]*) — validado aqui porque o
                Cypher não aceita parâmetro no nome do índice.
        """
        validate_index_name(index_name)

        self.uri = uri or settings.neo4j_uri
        if user is not None and password is not None:
            self.auth = (user, password)
        else:
            self.auth = settings.neo4j_auth()  # levanta erro claro se faltar senha
        self.database = database or settings.neo4j_database
        self.index_name = index_name
        self.dimension = dimension
        self.similarity = similarity

        self.driver = None
        self._supports_vector_procedure: Optional[bool] = None

    # ------------------------------------------------------------------ #
    # Ciclo de vida
    # ------------------------------------------------------------------ #

    def connect(self) -> None:
        """Abre o driver (idempotente) e valida a conectividade."""
        if self.driver is None:
            self.driver = build_driver(self.uri, self.auth)
            self.driver.verify_connectivity()
            log.info("Conectado ao Neo4j em %s (database=%s)", self.uri, self.database)

    def close(self) -> None:
        """Fecha o driver e libera o pool de conexões."""
        if self.driver is not None:
            self.driver.close()
            self.driver = None
            log.info("Conexão Neo4j encerrada.")

    def __enter__(self) -> "Neo4jGraphManager":
        self.connect()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def _session(self):
        self.connect()
        return self.driver.session(database=self.database)

    def server_version(self) -> str:
        """Retorna a versão do servidor Neo4j (ex.: '5.26.0' ou '2026.07.1')."""
        self.connect()
        return read_server_version(self.driver, self.database)

    # ------------------------------------------------------------------ #
    # Schema: constraints + índice vetorial
    # ------------------------------------------------------------------ #

    def create_constraints(self) -> Dict[str, str]:
        """
        Cria as constraints de unicidade usadas pelos MERGEs.

        Falhas são reportadas em vez de interromper: um banco legado com
        títulos duplicados rejeita a constraint, e isso é um dado de
        diagnóstico, não um motivo para abortar o pipeline.

        Returns:
            {nome_da_constraint: "ok" | "erro: ..."}
        """
        results: Dict[str, str] = {}
        with self._session() as session:
            for name, cypher in CONSTRAINTS:
                try:
                    session.run(cypher).consume()
                    results[name] = "ok"
                    log.info("Constraint garantida: %s", name)
                except Exception as exc:  # noqa: BLE001 — queremos reportar, não abortar
                    results[name] = f"erro: {exc}"
                    log.warning("Constraint %s não pôde ser criada: %s", name, exc)
        return results

    def create_vector_index(self) -> Dict[str, Any]:
        """
        Cria o índice vetorial nativo sobre (:Chunk).embedding.

        Cypher equivalente:
            CREATE VECTOR INDEX chunk_embedding_index IF NOT EXISTS
            FOR (c:Chunk) ON (c.embedding)
            OPTIONS { indexConfig: {
                `vector.dimensions`: 384,
                `vector.similarity_function`: 'cosine'
            }}

        É idempotente (IF NOT EXISTS). ATENÇÃO: se já existir um índice com
        esse nome e OUTRA dimensão, o Neo4j mantém o antigo silenciosamente —
        por isso conferimos a configuração depois de criar.

        Returns:
            {"status": "ok"|"dimension_mismatch"|"error", "index": {...}}
        """
        cypher = (
            f"CREATE VECTOR INDEX {self.index_name} IF NOT EXISTS "
            "FOR (c:Chunk) ON (c.embedding) "
            "OPTIONS { indexConfig: { "
            "`vector.dimensions`: $dimension, "
            "`vector.similarity_function`: $similarity "
            "}}"
        )

        log.info(
            "Criando índice vetorial '%s' (dim=%d, similarity=%s)...",
            self.index_name,
            self.dimension,
            self.similarity,
        )

        try:
            with self._session() as session:
                session.run(
                    cypher, dimension=self.dimension, similarity=self.similarity
                ).consume()
        except Exception as exc:  # noqa: BLE001
            log.error("Falha ao criar índice vetorial: %s", exc)
            return {"status": "error", "error": str(exc), "index_name": self.index_name}

        info = self.index_status()
        if info is None:
            return {"status": "error", "error": "índice não encontrado após criação"}

        configured = self._configured_dimension(info)
        if configured is not None and configured != self.dimension:
            log.error(
                "Índice '%s' já existia com %d dimensões (esperado %d). "
                "Rode --drop-index antes de recriar.",
                self.index_name,
                configured,
                self.dimension,
            )
            return {"status": "dimension_mismatch", "index": info, "expected": self.dimension}

        log.info("Índice vetorial pronto: %s (state=%s)", self.index_name, info.get("state"))
        return {"status": "ok", "index": info}

    def create_fulltext_index(self, index_name: str = FULLTEXT_INDEX_NAME) -> Dict[str, Any]:
        """
        Cria o índice full-text (Lucene/BM25) sobre (:Chunk).text.

        Cypher equivalente:
            CREATE FULLTEXT INDEX chunk_text_index IF NOT EXISTS
            FOR (c:Chunk) ON EACH [c.text]

        Diferente do índice vetorial, este não tem dimensão nem função de
        similaridade: o analisador padrão já faz tokenização e stemming em
        inglês, que é a língua de todo o corpus.
        """
        validate_index_name(index_name)
        cypher = (
            f"CREATE FULLTEXT INDEX {index_name} IF NOT EXISTS "
            "FOR (c:Chunk) ON EACH [c.text]"
        )

        log.info("Criando índice full-text '%s'...", index_name)
        try:
            with self._session() as session:
                session.run(cypher).consume()
        except Exception as exc:  # noqa: BLE001
            log.error("Falha ao criar índice full-text: %s", exc)
            return {"status": "error", "error": str(exc), "index_name": index_name}

        info = None
        with self._session() as session:
            for record in session.run(
                "SHOW INDEXES YIELD name, type, state, populationPercent, "
                "labelsOrTypes, properties"
            ):
                row = record.data()
                if row.get("name") == index_name:
                    info = row
                    break

        if info is None:
            return {"status": "error", "error": "índice não encontrado após criação"}

        log.info("Índice full-text pronto: %s (state=%s)", index_name, info.get("state"))
        return {"status": "ok", "index": info}

    @staticmethod
    def _configured_dimension(index_info: Dict[str, Any]) -> Optional[int]:
        """Extrai `vector.dimensions` do SHOW INDEXES, tolerando formatos."""
        options = index_info.get("options") or {}
        config = options.get("indexConfig") or {}
        value = config.get("vector.dimensions")
        return int(value) if value is not None else None

    def index_status(self) -> Optional[Dict[str, Any]]:
        """
        Retorna a linha do SHOW INDEXES referente ao índice vetorial.

        O filtro é feito em Python: comandos SHOW não aceitam parâmetros de
        forma consistente entre versões do Neo4j.
        """
        with self._session() as session:
            rows = [
                record.data()
                for record in session.run(
                    "SHOW INDEXES YIELD name, type, state, populationPercent, "
                    "labelsOrTypes, properties, options"
                )
            ]
        for row in rows:
            if row.get("name") == self.index_name:
                return row
        return None

    def await_index_online(self, timeout_seconds: int = 300) -> bool:
        """
        Bloqueia até o índice terminar de popular.

        A indexação vetorial é assíncrona: sem esta espera, uma busca logo
        após a escrita pode retornar zero resultados e parecer um bug de
        recuperação.

        Returns:
            True se o índice está ONLINE ao final.
        """
        try:
            with self._session() as session:
                session.run(
                    "CALL db.awaitIndex($name, $timeout)",
                    name=self.index_name,
                    timeout=timeout_seconds,
                ).consume()
        except Exception as exc:  # noqa: BLE001
            log.warning("db.awaitIndex falhou (%s); seguindo com checagem de estado.", exc)

        info = self.index_status()
        state = (info or {}).get("state")
        log.info("Estado do índice '%s': %s", self.index_name, state)
        return state == "ONLINE"

    def drop_vector_index(self) -> bool:
        """Remove o índice vetorial (útil para reset e troca de modelo)."""
        try:
            with self._session() as session:
                session.run(f"DROP INDEX {self.index_name} IF EXISTS").consume()
            log.info("Índice removido: %s", self.index_name)
            return True
        except Exception as exc:  # noqa: BLE001
            log.error("Falha ao remover índice: %s", exc)
            return False

    # ------------------------------------------------------------------ #
    # Escrita: Publication -[:HAS_CHUNK]-> Chunk
    # ------------------------------------------------------------------ #

    def supports_vector_property_procedure(self) -> bool:
        """
        Detecta se `db.create.setNodeVectorProperty` existe (Neo4j >= 5.13).

        É a forma recomendada de gravar vetores (armazenamento otimizado).
        Em versões sem a procedure, gravamos a lista diretamente — o índice
        também funciona, apenas sem a otimização.
        """
        if self._supports_vector_procedure is None:
            try:
                with self._session() as session:
                    record = session.run(
                        "SHOW PROCEDURES YIELD name "
                        "WHERE name = 'db.create.setNodeVectorProperty' "
                        "RETURN count(*) AS found"
                    ).single()
                self._supports_vector_procedure = bool(record and record["found"])
            except Exception as exc:  # noqa: BLE001
                log.warning("Não foi possível listar procedures (%s); usando SET direto.", exc)
                self._supports_vector_procedure = False
            log.info(
                "Gravação de vetores: %s",
                "db.create.setNodeVectorProperty"
                if self._supports_vector_procedure
                else "SET direto",
            )
        return self._supports_vector_procedure

    def _chunk_write_query(self) -> str:
        """Monta a query de escrita de chunks conforme o suporte do servidor."""
        assignments = [
            "c.publication_id = row.publication_id",
            "c.text = row.text",
            "c.section = row.section",
            "c.page = row.page",
            "c.position = row.position",
            "c.token_count = row.token_count",
            "c.embedding_model = $embedding_model",
            "c.updated_at = datetime()",
        ]

        if self.supports_vector_property_procedure():
            # Forma recomendada (Neo4j >= 5.13): armazenamento otimizado de vetor.
            embedding_step = (
                "WITH p, c, row\n"
                "        CALL db.create.setNodeVectorProperty(c, 'embedding', row.embedding)\n        "
            )
        else:
            assignments.insert(2, "c.embedding = row.embedding")
            embedding_step = ""

        set_clause = ",\n            ".join(assignments)

        return f"""
        MATCH (p:Publication {{title: $publication_title}})
        UNWIND $chunks AS row
        MERGE (c:Chunk {{id: row.id}})
        SET {set_clause}
        {embedding_step}MERGE (p)-[:HAS_CHUNK]->(c)
        RETURN count(c) AS written
        """

    @staticmethod
    def _chunk_payload(chunk: ChunkRecord) -> Dict[str, Any]:
        return {
            "id": chunk.id,
            "publication_id": chunk.publication_id,
            "text": chunk.text,
            "embedding": chunk.embedding,
            "section": chunk.section,
            "page": chunk.page,
            "position": chunk.position,
            "token_count": chunk.token_count,
        }

    def write_publication(
        self,
        publication: PublicationRecord,
        content_hash: Optional[str] = None,
        chunk_count: Optional[int] = None,
    ) -> None:
        """
        Cria ou atualiza o nó :Publication.

        MERGE por `title` para reaproveitar os nós já criados pelo
        build_graph.py em vez de duplicá-los.

        `content_hash` e `chunk_count` sustentam o build incremental
        (SPACEBIO-023): com eles, uma execução seguinte sabe que a publicação
        não mudou e pula chunking e embedding — a parte cara.
        """
        # doi e journal vêm da regra R7 do cleaner e são indispensáveis ao
        # contrato de evidência (§14): sem eles a citação da Dra. Aris fica
        # sem identificador persistente — e o §15 proíbe fabricar um.
        cypher = """
        MERGE (p:Publication {title: $title})
        SET p.id = $title,
            p.source_url = $source_url,
            p.local_path = $local_path,
            p.extraction_method = $extraction_method,
            p.doi = $doi,
            p.journal = $journal,
            p.content_hash = $content_hash,
            p.chunk_count = $chunk_count,
            p.updated_at = datetime()
        RETURN p.title AS title
        """

        def _write(tx):
            return tx.run(
                cypher,
                title=publication.title,
                source_url=publication.source_url,
                local_path=publication.local_path,
                extraction_method=publication.extraction_method,
                doi=publication.doi,
                journal=publication.journal,
                content_hash=content_hash,
                chunk_count=chunk_count,
            ).single()

        with self._session() as session:
            session.execute_write(_write)
        log.info("Publication gravada: %.60s...", publication.title)

    def write_chunks(
        self,
        publication: PublicationRecord,
        chunks: Sequence[ChunkRecord],
        batch_size: int = 500,
    ) -> int:
        """
        Grava os chunks e as relações (Publication)-[:HAS_CHUNK]->(Chunk).

        Args:
            publication: publicação-mãe (precisa já existir no grafo).
            chunks: chunks JÁ COM EMBEDDING (SPACEBIO-010).
            batch_size: chunks por transação.

        Returns:
            Número de chunks escritos.

        Raises:
            ValueError: se algum chunk vier sem embedding ou com dimensão
                diferente da configurada no índice.
        """
        chunks = list(chunks)
        if not chunks:
            log.warning("Nenhum chunk para gravar.")
            return 0

        for chunk in chunks:
            if chunk.embedding is None:
                raise ValueError(
                    f"Chunk {chunk.id} não possui embedding. "
                    "Rode o EmbeddingService (SPACEBIO-010) antes de gravar."
                )
            if len(chunk.embedding) != self.dimension:
                raise ValueError(
                    f"Chunk {chunk.id}: embedding com {len(chunk.embedding)} dimensões, "
                    f"mas o índice espera {self.dimension}."
                )

        query = self._chunk_write_query()
        written = 0

        with self._session() as session:
            for start in range(0, len(chunks), batch_size):
                batch = chunks[start : start + batch_size]
                payload = [self._chunk_payload(chunk) for chunk in batch]

                def _write(tx, payload=payload):
                    record = tx.run(
                        query,
                        publication_title=publication.title,
                        chunks=payload,
                        embedding_model=settings.embedding_model,
                    ).single()
                    return record["written"] if record else 0

                written += session.execute_write(_write)
                log.info("Lote gravado: %d/%d chunks", written, len(chunks))

        return written

    # ------------------------------------------------------------------ #
    # E3-06 — metadados bibliográficos
    # ------------------------------------------------------------------ #

    def fetch_publication_dois(
        self, only_missing: bool = True
    ) -> List[Dict[str, Any]]:
        """
        Lista as publicações a enriquecer com metadados do Crossref.

        `only_missing=True` devolve apenas o que ainda não foi tentado ou
        falhou por rede. Um DOI que o Crossref não tem (`not_found`) NÃO
        volta: repetir a consulta em toda execução gastaria requisição para
        confirmar uma ausência já conhecida.
        """
        condicao = (
            "WHERE p.crossref_status IS NULL OR p.crossref_status = 'error'"
            if only_missing
            else ""
        )
        cypher = f"""
        MATCH (p:Publication)
        {condicao}
        RETURN p.title AS title, p.doi AS doi, p.crossref_status AS status
        ORDER BY p.title
        """
        with self.driver.session(database=self.database) as session:
            return [dict(record) for record in session.run(cypher)]

    def write_publication_metadata(self, records: Sequence[Dict[str, Any]]) -> int:
        """
        Grava os metadados bibliográficos nos nós :Publication.

        Casado por `title`, que é a chave do MERGE em write_publication.

        `authors` é gravado como lista de strings "Sobrenome, Nome" e não como
        objetos: o Neo4j aceita arrays de primitivos, não de mapas. A forma
        canônica única também evita que o frontend precise conhecer o esquema
        do Crossref para montar uma citação.

        UNWIND em vez de uma escrita por publicação: 488 transações separadas
        levariam minutos, e a operação é idempotente — reexecutar sobrescreve
        com o mesmo valor.
        """
        cypher = """
        UNWIND $rows AS row
        MATCH (p:Publication {title: row.title})
        SET p.crossref_status = row.status,
            p.authors = row.authors,
            p.publication_year = row.year,
            p.volume = row.volume,
            p.issue = row.issue,
            p.pages = row.pages,
            p.publisher = row.publisher,
            p.crossref_title = row.crossref_title,
            p.crossref_container = row.container_title,
            p.crossref_checked_at = datetime()
        RETURN count(p) AS updated
        """
        with self.driver.session(database=self.database) as session:
            result = session.run(cypher, rows=list(records)).single()
            return result["updated"] if result else 0

    def metadata_coverage(self) -> Dict[str, Any]:
        """
        Quantas publicações têm metadados utilizáveis para citação.

        Serve ao relatório do job e à verificação de prontidão antes da
        demonstração — uma citação sem autor nem ano é o defeito que só
        aparece quando alguém clica em "Copiar ABNT" na frente da plateia.
        """
        cypher = """
        MATCH (p:Publication)
        RETURN count(p) AS total,
               count(p.doi) AS com_doi,
               sum(CASE WHEN p.crossref_status = 'enriched' THEN 1 ELSE 0 END) AS enriquecidas,
               sum(CASE WHEN p.authors IS NOT NULL AND size(p.authors) > 0 THEN 1 ELSE 0 END) AS com_autores,
               sum(CASE WHEN p.publication_year IS NOT NULL THEN 1 ELSE 0 END) AS com_ano
        """
        with self.driver.session(database=self.database) as session:
            record = session.run(cypher).single()
            return dict(record) if record else {}

    def write_publication_with_chunks(
        self,
        publication: PublicationRecord,
        chunks: Sequence[ChunkRecord],
        batch_size: int = 500,
        content_hash: Optional[str] = None,
        prune_orphans: bool = False,
    ) -> Dict[str, Any]:
        """
        Grava a publicação e seus chunks.

        Args:
            prune_orphans: remove chunks da publicação que não estão neste
                lote. Necessário quando o texto muda: o chunking produz IDs
                diferentes, e sem a poda os chunks da versão anterior ficariam
                no índice vetorial como evidência fantasma — recuperável pela
                Dra. Aris, mas correspondendo a um texto que não existe mais.
        """
        self.write_publication(
            publication, content_hash=content_hash, chunk_count=len(chunks)
        )
        written = self.write_chunks(publication, chunks, batch_size=batch_size)

        pruned = 0
        if prune_orphans:
            pruned = self.prune_chunks(publication.title, [c.id for c in chunks])

        return {
            "publication": publication.title,
            "chunks_written": written,
            "chunks_pruned": pruned,
            "embedding_dimension": self.dimension,
        }

    # ------------------------------------------------------------------ #
    # Build incremental (SPACEBIO-023)
    # ------------------------------------------------------------------ #

    def publication_state(self) -> Dict[str, Dict[str, Any]]:
        """
        Estado das publicações já no grafo: hash do conteúdo e nº de chunks.

        É o que permite pular publicações inalteradas sem tocar em disco nem
        no modelo de embeddings.
        """
        cypher = """
        MATCH (p:Publication)
        OPTIONAL MATCH (p)-[:HAS_CHUNK]->(c:Chunk)
        RETURN p.title AS title,
               p.content_hash AS content_hash,
               count(c) AS chunks
        """
        with self._session() as session:
            return {
                record["title"]: {
                    "content_hash": record["content_hash"],
                    "chunks": record["chunks"],
                }
                for record in session.run(cypher)
            }

    def prune_chunks(self, publication_title: str, keep_ids: Sequence[str]) -> int:
        """Remove os chunks da publicação que não estão em `keep_ids`."""
        cypher = """
        MATCH (:Publication {title: $title})-[:HAS_CHUNK]->(c:Chunk)
        WHERE NOT c.id IN $keep_ids
        WITH c, c.id AS removed
        DETACH DELETE c
        RETURN count(removed) AS pruned
        """
        with self._session() as session:
            record = session.execute_write(
                lambda tx: tx.run(
                    cypher, title=publication_title, keep_ids=list(keep_ids)
                ).single()
            )
        pruned = record["pruned"] if record else 0
        if pruned:
            log.info("Chunks órfãos removidos de '%.40s...': %d", publication_title, pruned)
        return pruned

    def count_chunks_pending_ner(self, ontology_version: str) -> int:
        """Quantos chunks aguardam anotação nesta versão da ontologia."""
        cypher = """
        MATCH (c:Chunk)
        WHERE c.ner_version IS NULL OR c.ner_version <> $version
        RETURN count(c) AS pending
        """
        with self._session() as session:
            record = session.run(cypher, version=ontology_version).single()
        return record["pending"] if record else 0

    def fetch_chunks_pending_ner(
        self, ontology_version: str, limit: int
    ) -> List[Dict[str, Any]]:
        """
        Página de chunks que precisam ser anotados.

        Não usa SKIP: a cada lote processado os chunks saem do conjunto
        pendente, então paginar por offset puxaria os mesmos registros duas
        vezes ou pularia outros. Buscar sempre o "próximo pendente" é correto
        e ainda deixa a extração retomável se for interrompida.
        """
        cypher = """
        MATCH (c:Chunk)
        WHERE c.ner_version IS NULL OR c.ner_version <> $version
        RETURN c.id AS id, c.text AS text
        ORDER BY c.id
        LIMIT $limit
        """
        with self._session() as session:
            return [
                record.data()
                for record in session.run(cypher, version=ontology_version, limit=limit)
            ]

    def mark_chunks_annotated(
        self, chunk_ids: Sequence[str], ontology_version: str
    ) -> int:
        """Marca chunks como anotados por esta versão da ontologia."""
        cypher = """
        UNWIND $chunk_ids AS chunk_id
        MATCH (c:Chunk {id: chunk_id})
        SET c.ner_version = $version, c.ner_at = datetime()
        RETURN count(c) AS marked
        """
        with self._session() as session:
            record = session.execute_write(
                lambda tx: tx.run(
                    cypher, chunk_ids=list(chunk_ids), version=ontology_version
                ).single()
            )
        return record["marked"] if record else 0

    def clear_mentions_for_chunks(self, chunk_ids: Sequence[str]) -> int:
        """
        Remove as menções destes chunks, preservando os nós de entidade.

        Necessário ao reanotar: sem apagar as menções antigas, uma entidade que
        deixou de ser reconhecida (um falso positivo corrigido no stoplist)
        continuaria ligada ao chunk.
        """
        cypher = """
        UNWIND $chunk_ids AS chunk_id
        MATCH (:Chunk {id: chunk_id})-[m:MENTIONS]->()
        DELETE m
        RETURN count(m) AS deleted
        """
        with self._session() as session:
            record = session.execute_write(
                lambda tx: tx.run(cypher, chunk_ids=list(chunk_ids)).single()
            )
        return record["deleted"] if record else 0

    def delete_orphan_entities(self) -> int:
        """
        Remove entidades que ficaram sem nenhuma menção.

        Complemento da reanotação: quando um falso positivo sai do stoplist,
        o nó dele fica no grafo sem nenhuma aresta. Não é errado, mas suja as
        estatísticas e o futuro Knowledge Explorer.
        """
        cypher = """
        MATCH (e:Entity)
        WHERE NOT (:Chunk)-[:MENTIONS]->(e)
        WITH e, e.key AS removed
        DETACH DELETE e
        RETURN count(removed) AS deleted
        """
        with self._session() as session:
            record = session.execute_write(lambda tx: tx.run(cypher).single())
        deleted = record["deleted"] if record else 0
        if deleted:
            log.info("Entidades órfãs removidas: %d", deleted)
        return deleted

    # ------------------------------------------------------------------ #
    # Leitura / verificação
    # ------------------------------------------------------------------ #

    # ------------------------------------------------------------------ #
    # Entidades (SPACEBIO-016 a 022)
    # ------------------------------------------------------------------ #

    def write_entities(self, entities: Sequence[Dict[str, Any]]) -> int:
        """
        Grava nós de entidade, agrupados por tipo.

        Cada nó recebe :Entity mais o label específico (:Gene, :Organism, ...).
        O label é interpolado na query porque Cypher não aceita parâmetro em
        label — por isso passa antes por lista branca contra ENTITY_LABELS.
        Nenhum valor vindo de texto extraído entra em query por concatenação.

        Args:
            entities: dicts com canonical, label e key.

        Returns:
            Quantidade de nós escritos.
        """
        from ontology import ENTITY_LABELS

        by_label: Dict[str, List[Dict[str, Any]]] = {}
        for entity in entities:
            label = entity["label"]
            if label not in ENTITY_LABELS:
                raise ValueError(f"Label fora da ontologia: {label!r}")
            by_label.setdefault(label, []).append(entity)

        written = 0
        with self._session() as session:
            for label, rows in by_label.items():
                cypher = f"""
                UNWIND $rows AS row
                MERGE (e:Entity {{key: row.key}})
                SET e:{label},
                    e.canonical = row.canonical,
                    e.label = row.label,
                    e.updated_at = datetime()
                RETURN count(e) AS written
                """
                record = session.execute_write(
                    lambda tx, cypher=cypher, rows=rows: tx.run(cypher, rows=rows).single()
                )
                written += record["written"] if record else 0

        log.info("Entidades gravadas: %d em %d tipos.", written, len(by_label))
        return written

    def write_mentions(
        self, mentions: Sequence[Dict[str, Any]], batch_size: int = 5000
    ) -> int:
        """
        Grava (:Chunk)-[:MENTIONS {count}]->(:Entity).

        A menção é do CHUNK, não da publicação: o retriever ranqueia chunks, e
        o sinal de entity overlap precisa da mesma granularidade. A visão por
        publicação sai de graça atravessando HAS_CHUNK.

        Args:
            mentions: dicts com chunk_id, entity_key e count.
        """
        cypher = """
        UNWIND $rows AS row
        MATCH (c:Chunk {id: row.chunk_id})
        MATCH (e:Entity {key: row.entity_key})
        MERGE (c)-[m:MENTIONS]->(e)
        SET m.count = row.count
        RETURN count(m) AS written
        """

        written = 0
        rows = list(mentions)
        with self._session() as session:
            for start in range(0, len(rows), batch_size):
                batch = rows[start : start + batch_size]
                record = session.execute_write(
                    lambda tx, batch=batch: tx.run(cypher, rows=batch).single()
                )
                written += record["written"] if record else 0

        log.info("Menções gravadas: %d.", written)
        return written

    def derive_publication_relations(
        self, min_mentions: int = 3
    ) -> Dict[str, int]:
        """
        Deriva as relações tipadas do §17 a partir das menções nos chunks.

        (:Publication)-[:STUDIES]->(:Organism) e companhia não precisam de
        extração própria: se um organismo aparece em vários chunks de uma
        publicação, ela o estuda. O limiar evita promover citação de passagem
        a objeto de estudo.
        """
        from ontology import DERIVED_RELATIONS

        results: Dict[str, int] = {}
        with self._session() as session:
            for label, relation in DERIVED_RELATIONS.items():
                cypher = f"""
                MATCH (p:Publication)-[:HAS_CHUNK]->(:Chunk)-[m:MENTIONS]->(e:{label})
                WITH p, e, sum(m.count) AS total
                WHERE total >= $min_mentions
                MERGE (p)-[r:{relation}]->(e)
                SET r.mentions = total
                RETURN count(r) AS created
                """
                record = session.execute_write(
                    lambda tx, cypher=cypher: tx.run(
                        cypher, min_mentions=int(min_mentions)
                    ).single()
                )
                results[relation] = record["created"] if record else 0
                log.info("Relação %s: %d criadas.", relation, results[relation])
        return results

    def clear_entities(self) -> int:
        """Remove todas as entidades e menções, preservando corpus e chunks."""
        cypher = """
        MATCH (e:Entity)
        WITH e, e.key AS deleted_key
        DETACH DELETE e
        RETURN count(deleted_key) AS deleted
        """
        with self._session() as session:
            record = session.execute_write(lambda tx: tx.run(cypher).single())
        deleted = record["deleted"] if record else 0
        log.info("Entidades removidas: %d.", deleted)
        return deleted

    def entity_stats(self) -> List[Dict[str, Any]]:
        """Contagem de entidades e menções por tipo."""
        cypher = """
        MATCH (e:Entity)
        OPTIONAL MATCH (:Chunk)-[m:MENTIONS]->(e)
        RETURN e.label AS label,
               count(DISTINCT e) AS entities,
               count(m) AS mentions
        ORDER BY mentions DESC
        """
        with self._session() as session:
            return [record.data() for record in session.run(cypher)]

    def count_chunks(self, publication_title: Optional[str] = None) -> int:
        """Conta chunks no grafo, opcionalmente de uma publicação específica."""
        if publication_title is None:
            cypher = "MATCH (c:Chunk) RETURN count(c) AS total"
            params: Dict[str, Any] = {}
        else:
            cypher = (
                "MATCH (:Publication {title: $title})-[:HAS_CHUNK]->(c:Chunk) "
                "RETURN count(c) AS total"
            )
            params = {"title": publication_title}

        with self._session() as session:
            record = session.run(cypher, **params).single()
        return record["total"] if record else 0

    def delete_publication_chunks(self, publication_title: str) -> int:
        """
        Remove os chunks de uma publicação (limpeza de teste).

        Só apaga :Chunk — a :Publication e as entidades extraídas pelo
        build_graph.py permanecem intactas.
        """
        cypher = """
        MATCH (:Publication {title: $title})-[:HAS_CHUNK]->(c:Chunk)
        WITH c, c.id AS deleted_id
        DETACH DELETE c
        RETURN count(deleted_id) AS deleted
        """
        with self._session() as session:
            record = session.execute_write(
                lambda tx: tx.run(cypher, title=publication_title).single()
            )
        deleted = record["deleted"] if record else 0
        log.info("Chunks removidos de '%.40s...': %d", publication_title, deleted)
        return deleted


def initialize_vector_index() -> Dict[str, Any]:
    """
    Atalho: garante constraints + índice vetorial usando o ambiente atual.

    Returns:
        Dict com o resultado da criação do índice.
    """
    with Neo4jGraphManager() as graph:
        graph.create_constraints()
        return graph.create_vector_index()


def _print_status(graph: Neo4jGraphManager) -> None:
    print(f"Servidor Neo4j:  {graph.server_version()}")
    print(f"Database:        {graph.database}")
    print(f"Índice:          {graph.index_name}")

    info = graph.index_status()
    if info is None:
        print("Status:          NÃO EXISTE (rode --create-index)")
    else:
        print(f"Status:          {info.get('state')} ({info.get('populationPercent')}%)")
        print(f"Tipo:            {info.get('type')}")
        print(f"Alvo:            {info.get('labelsOrTypes')} {info.get('properties')}")
        print(f"Config:          {(info.get('options') or {}).get('indexConfig')}")

    print(f"Chunks no grafo: {graph.count_chunks()}")


if __name__ == "__main__":
    import argparse

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    parser = argparse.ArgumentParser(description="SPACEBIO-011 — Neo4j Vector Index")
    parser.add_argument("--create-index", action="store_true", help="cria constraints + índice")
    parser.add_argument("--drop-index", action="store_true", help="remove o índice vetorial")
    parser.add_argument("--status", action="store_true", help="mostra o estado atual")
    args = parser.parse_args()

    if not any([args.create_index, args.drop_index, args.status]):
        parser.print_help()
        raise SystemExit(0)

    print("=" * 70)
    print("SPACEBIO-011 — Neo4j Vector Index")
    print("=" * 70)

    with Neo4jGraphManager() as manager:
        if args.drop_index:
            manager.drop_vector_index()

        if args.create_index:
            constraints = manager.create_constraints()
            for name, status in constraints.items():
                print(f"  constraint {name}: {status}")
            result = manager.create_vector_index()
            print(f"  índice: {result['status']}")

        if args.status or args.create_index:
            print()
            _print_status(manager)
