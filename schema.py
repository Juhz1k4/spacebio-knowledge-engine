"""
SpaceBio Canonical Publication Schema

This module defines the unified data model for all publication records in the SpaceBio system.
All ingestion, processing, RAG, and graph-building operations must use this schema to ensure
data consistency across the entire platform.

Usage:
    from schema import PublicationRecord
    import pandas as pd
    
    # Load from CSV
    df = pd.read_csv("data/metadata.csv")
    records = [PublicationRecord(**row) for _, row in df.iterrows()]
"""

from pydantic import BaseModel, Field, field_validator
from typing import Any, Optional, List


class PublicationRecord(BaseModel):
    """
    Canonical schema for publication metadata in SpaceBio.
    
    This is the single source of truth for publication data structure.
    All CSV imports, graph operations, and API responses must conform to this model.
    """
    
    title: str = Field(
        ...,
        description="Publication title",
        min_length=1,
        examples=["Microgravity induces pelvic bone loss through osteoclastic activity"]
    )
    
    source_url: str = Field(
        ...,
        description="URL to the original publication online",
        examples=["https://www.ncbi.nlm.nih.gov/pmc/articles/PMC3630201/"]
    )
    
    local_path: str = Field(
        ...,
        description="Relative path to processed text file in data directory",
        examples=["data/processed_text/Microgravity induces pelvic bone loss.txt"]
    )
    
    extraction_method: str = Field(
        ...,
        description="Method used to extract text from the publication source",
        examples=["HTML_EXTRACTION", "PDF_EXTRACTION", "TEXT_EXTRACTION"]
    )

    # --- Campos adicionados pela SPACEBIO-012.5 (Corpus Cleaning) ---
    # Recuperados do cabeçalho do PMC pelo cleaner (regra R7) ANTES de o
    # cabeçalho ser descartado. Medido no corpus: DOI presente em 99,1% dos
    # documentos, revista em 100%.
    #
    # Ambos são Optional e default None de propósito: o §15 do briefing proíbe
    # fabricar DOI, então a ausência precisa ser representável. Nunca preencher
    # com string vazia ou placeholder.

    doi: Optional[str] = Field(
        default=None,
        description="DOI extraído do cabeçalho do PMC. None quando ausente — nunca fabricar.",
        examples=["10.1371/journal.pone.0104830"]
    )

    journal: Optional[str] = Field(
        default=None,
        description="Nome da revista, extraído da primeira linha do documento",
        examples=["PLoS One", "NPJ Microgravity"]
    )

    clean_path: Optional[str] = Field(
        default=None,
        description="Caminho do texto limpo (data/clean_text/...). None se não limpo ainda.",
        examples=["data/clean_text/Mice in BionM 1 space mission.txt"]
    )

    cleaning_status: Optional[str] = Field(
        default=None,
        description="Resultado da limpeza: cleaned | flagged | no_boundary | excluded",
        examples=["cleaned", "flagged"]
    )

    @field_validator("doi", "journal", "clean_path", "cleaning_status", mode="before")
    @classmethod
    def _empty_to_none(cls, value: Any) -> Optional[str]:
        """
        Normaliza ausência para None.

        pandas devolve float('nan') para células vazias de CSV; sem isto, um
        metadata sem DOI quebraria a validação de Optional[str].
        """
        if value is None:
            return None
        if isinstance(value, float):  # NaN do pandas
            return None
        text = str(value).strip()
        return text if text else None

    class Config:
        """Pydantic model configuration"""
        frozen = True  # Immutable for data integrity in multi-stage pipelines
        json_schema_extra = {
            "example": {
                "title": "Microgravity induces pelvic bone loss through osteoclastic activity",
                "source_url": "https://www.ncbi.nlm.nih.gov/pmc/articles/PMC3630201/",
                "local_path": "data/processed_text/Microgravity induces pelvic bone loss.txt",
                "extraction_method": "HTML_EXTRACTION"
            }
        }
    
    def __str__(self) -> str:
        """Human-readable representation"""
        return f"{self.title} ({self.extraction_method})"
    
    def __repr__(self) -> str:
        """Developer-friendly representation"""
        return f"PublicationRecord(title={self.title[:50]}..., method={self.extraction_method})"


class ChunkRecord(BaseModel):
    """
    Canonical schema for text chunks extracted from publications in SpaceBio.
    
    Each chunk represents a semantically meaningful fragment of a publication,
    preserving traceability to the source document for citation and grounding.
    
    Chunks are created by SPACEBIO-009 (Document Chunking Pipeline) and will be
    embedded by SPACEBIO-010 (Embedding Pipeline) before storage in Neo4j.
    """
    
    id: str = Field(
        ...,
        description="Deterministic chunk identifier (SHA256-based)",
        examples=["pub_001_chunk_0_a3f7d2"]
    )
    
    publication_id: str = Field(
        ...,
        description="Reference to parent PublicationRecord via title or URL",
        examples=["Microgravity induces pelvic bone loss through osteoclastic activity"]
    )
    
    text: str = Field(
        ...,
        description="Actual text content of the chunk",
        min_length=1
    )
    
    embedding: Optional[List[float]] = Field(
        default=None,
        description="Dense vector embedding (populated during SPACEBIO-010)"
    )
    
    section: Optional[str] = Field(
        default="unknown",
        description="Section name (e.g., 'abstract', 'introduction') or paragraph number. None for HTML extractions."
    )
    
    page: Optional[int] = Field(
        default=None,
        description="Page number if available from PDF extraction. None for HTML sources."
    )
    
    position: int = Field(
        ...,
        description="Byte offset of chunk start in original document text"
    )
    
    token_count: int = Field(
        ...,
        description="Approximate word count (proxy for tokens). TODO: Replace with tokenizer-specific count during SPACEBIO-010."
    )
    
    class Config:
        """Pydantic model configuration"""
        frozen = True  # Immutable for pipeline integrity
    
    def __str__(self) -> str:
        """Human-readable representation"""
        return f"ChunkRecord(id={self.id}, tokens={self.token_count}, section={self.section})"
    
    def __repr__(self) -> str:
        """Developer-friendly representation"""
        return f"ChunkRecord(pub={self.publication_id[:40]}..., pos={self.position}, tokens={self.token_count})"
