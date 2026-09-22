"""
SpaceBio — Configuração central (suporte a SPACEBIO-010 / SPACEBIO-011)

Centraliza credenciais e parâmetros do pipeline de Evidence RAG.

Regra do Master Briefing (§8.2): credenciais NUNCA ficam hardcoded no código.
Tudo vem de variáveis de ambiente, carregadas de um arquivo .env local
(que é ignorado pelo git; use .env.example como modelo).

Uso:
    from config import settings

    settings.neo4j_auth()          # (user, password) — levanta erro se faltar senha
    settings.resolve_corpus_path(p)  # normaliza caminhos Windows do metadata.csv
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

# Raiz do repositório backend (este arquivo mora na raiz)
PROJECT_ROOT = Path(__file__).resolve().parent

# Carrega .env da raiz do projeto, se existir. Variáveis já presentes no
# ambiente têm precedência (override=False) — importante para CI e Docker.
load_dotenv(PROJECT_ROOT / ".env", override=False)


class MissingCredentialError(RuntimeError):
    """Levantado quando uma credencial obrigatória não foi configurada."""


def _env(name: str, default: str) -> str:
    value = os.getenv(name)
    return value if value not in (None, "") else default


@dataclass(frozen=True)
class Settings:
    """Configuração imutável do pipeline, resolvida a partir do ambiente."""

    # --- Neo4j ---
    # 127.0.0.1, nao `localhost`: no Windows o resolver entrega ::1 (IPv6)
    # primeiro e o Neo4j escuta so em IPv4, entao cada conexao nova do pool
    # paga ~2 s de timeout antes de cair para IPv4. Medido: 2051 ms contra
    # 16 ms. Ver docs/RETRIEVAL_AUDIT.md.
    neo4j_uri: str = field(default_factory=lambda: _env("NEO4J_URI", "bolt://127.0.0.1:7687"))
    neo4j_user: str = field(default_factory=lambda: _env("NEO4J_USER", "neo4j"))
    neo4j_password: str | None = field(default_factory=lambda: os.getenv("NEO4J_PASSWORD"))
    neo4j_database: str = field(default_factory=lambda: _env("NEO4J_DATABASE", "neo4j"))

    # --- Embeddings (SPACEBIO-010) ---
    embedding_model: str = field(
        default_factory=lambda: _env("EMBEDDING_MODEL", "sentence-transformers/all-MiniLM-L6-v2")
    )
    embedding_dimension: int = field(
        default_factory=lambda: int(_env("EMBEDDING_DIMENSION", "384"))
    )
    embedding_device: str = field(default_factory=lambda: _env("EMBEDDING_DEVICE", "cpu"))
    embedding_batch_size: int = field(
        default_factory=lambda: int(_env("EMBEDDING_BATCH_SIZE", "32"))
    )

    # --- Índice vetorial (SPACEBIO-011) ---
    vector_index_name: str = field(
        default_factory=lambda: _env("VECTOR_INDEX_NAME", "chunk_embedding_index")
    )
    vector_similarity: str = field(default_factory=lambda: _env("VECTOR_SIMILARITY", "cosine"))

    # --- Chunking (SPACEBIO-009) ---
    # 700/140 medido em 2026-09-02: com 1000/200, 30,9% dos chunks excediam os
    # 256 word-pieces do all-MiniLM-L6-v2 e tinham o excedente descartado em
    # silêncio pelo modelo. Com 700/140 a taxa cai para 4,4%.
    # Trocar estes valores muda os IDs determinísticos de TODO o corpus e exige
    # re-chunk + re-embed + regravação no Neo4j.
    chunk_size: int = field(default_factory=lambda: int(_env("CHUNK_SIZE", "700")))
    chunk_overlap: int = field(default_factory=lambda: int(_env("CHUNK_OVERLAP", "140")))

    # --- Corpus ---
    data_dir: Path = field(default_factory=lambda: PROJECT_ROOT / _env("DATA_DIR", "data"))

    @property
    def metadata_file(self) -> Path:
        return self.data_dir / "metadata.csv"

    def neo4j_auth(self) -> tuple[str, str]:
        """
        Retorna (usuário, senha) do Neo4j.

        Raises:
            MissingCredentialError: se NEO4J_PASSWORD não estiver definida.
        """
        if not self.neo4j_password:
            raise MissingCredentialError(
                "NEO4J_PASSWORD não está definida.\n"
                "  1. Copie .env.example para .env\n"
                "  2. Preencha NEO4J_PASSWORD com a senha do seu Neo4j local\n"
                "  (ou exporte a variável no ambiente antes de rodar o script)."
            )
        return (self.neo4j_user, self.neo4j_password)

    def resolve_corpus_path(self, local_path: str | Path) -> Path:
        """
        Normaliza um caminho vindo do metadata.csv.

        O metadata atual guarda caminhos no formato Windows
        ("data\\processed_text\\arquivo.txt"), o que quebra em Linux/macOS.
        Esta função converte separadores e resolve o caminho relativo à raiz
        do projeto, tornando o pipeline independente do diretório de trabalho.

        NOTA: isto é um paliativo de leitura. A correção definitiva
        (normalizar o metadata na origem) é a SPACEBIO-005.
        """
        normalized = str(local_path).replace("\\", "/")
        path = Path(normalized)
        return path if path.is_absolute() else (PROJECT_ROOT / path)


settings = Settings()
