"""
SpaceBio Document Chunking Pipeline (SPACEBIO-009)

This module implements sentence-aware chunking with configurable size and overlap.
Chunks preserve traceability to source publications for RAG citation and grounding.

Key Features:
- Deterministic chunk IDs (SHA256-based)
- Sentence-aware overflow handling (via NLTK)
- Configurable chunk size and overlap
- Handles missing metadata gracefully (page=None, section=None for HTML)
- Token count as word-count proxy (TODO: replace during SPACEBIO-010 with model-specific tokenizer)

Usage:
    from chunker import DocumentChunker
    
    chunker = DocumentChunker(chunk_size=1000, overlap=200)
    chunks = chunker.process_all()
    
    # Or process a single publication
    from schema import PublicationRecord
    pub = PublicationRecord(...)
    chunks = chunker.chunk_publication(pub)
"""

import os
import hashlib
from pathlib import Path
from typing import List, Optional
import pandas as pd
import nltk

from config import settings
from schema import PublicationRecord, ChunkRecord

# Download NLTK sentence tokenizer if not available
try:
    nltk.data.find("tokenizers/punkt_tab")
except LookupError:
    print("Downloading NLTK punkt_tab tokenizer...")
    nltk.download("punkt_tab", quiet=True)

from nltk.tokenize import sent_tokenize


class DocumentChunker:
    """
    Sentence-aware document chunking pipeline for SpaceBio.
    
    Attributes:
        chunk_size: Target size of each chunk in characters
        overlap: Number of characters to overlap between consecutive chunks
        data_dir: Path to data directory containing processed_text
    """
    
    def __init__(
        self,
        chunk_size: int = settings.chunk_size,
        overlap: int = settings.chunk_overlap,
        data_dir: str = "data"
    ):
        """
        Initialize the chunker.

        Args:
            chunk_size: Target chunk size in characters (default: 700, via CHUNK_SIZE)
            overlap: Overlap between chunks in characters (default: 140, via CHUNK_OVERLAP)
            data_dir: Root data directory path (default: "data")

        Nota sobre o padrão 700/140 (medido em 2026-09-02, SPACEBIO-010):
        o embedder all-MiniLM-L6-v2 processa no máximo 256 word-pieces e
        descarta o excedente SILENCIOSAMENTE. Com o padrão anterior de
        1000/200, 30,9% dos chunks estouravam esse limite — evidência que o
        RAG nunca conseguiria recuperar. Com 700/140 a taxa cai para 4,4%,
        ao custo de ~50% mais chunks.
        """
        if chunk_size <= 0 or overlap < 0:
            raise ValueError("chunk_size must be positive and overlap must be non-negative")
        if overlap >= chunk_size:
            raise ValueError("overlap must be less than chunk_size")
        
        self.chunk_size = chunk_size
        self.overlap = overlap
        self.data_dir = data_dir
        self.metadata_file = os.path.join(data_dir, "metadata.csv")
        self.processed_text_dir = os.path.join(data_dir, "processed_text")
    
    def _generate_chunk_id(self, publication_id: str, position: int, text: str) -> str:
        """
        Generate a deterministic chunk ID based on publication, position, and content.
        
        Args:
            publication_id: Title or identifier of publication
            position: Byte offset of chunk in document
            text: Chunk text content
        
        Returns:
            Deterministic SHA256-based chunk ID
        """
        content_hash = hashlib.sha256(text.encode()).hexdigest()[:6]
        base_id = publication_id.replace(" ", "_")[:30]
        return f"{base_id}_{position}_{content_hash}"
    
    def _count_tokens(self, text: str) -> int:
        """
        Approximate token count using word split.
        
        TODO: During SPACEBIO-010, replace this with model-specific tokenizer
        (tiktoken for GPT, transformers.PreTrainedTokenizer for HF models, etc.)
        
        Args:
            text: Text to count tokens for
        
        Returns:
            Approximate token count (word count proxy)
        """
        return len(text.split())
    
    def chunk_text(
        self,
        text: str,
        publication_id: str,
        section: Optional[str] = None,
        page: Optional[int] = None
    ) -> List[ChunkRecord]:
        """
        Split a publication text into chunks with overlap.
        
        Strategy:
        1. Split text into sentences using NLTK
        2. Group sentences until reaching chunk_size
        3. Apply overlap from previous chunk
        4. Generate deterministic IDs and metadata
        
        Handles missing metadata gracefully:
        - section defaults to "unknown" if None or empty
        - page remains None for HTML extractions (no page semantics)
        
        Args:
            text: Full publication text
            publication_id: Publication title or identifier
            section: Optional section name (e.g., "results"). None for HTML.
            page: Optional page number. None for HTML sources.
        
        Returns:
            List of ChunkRecord objects
        """
        if not text or not text.strip():
            return []
        
        chunks: List[ChunkRecord] = []
        sentences = sent_tokenize(text)
        
        if not sentences:
            return chunks
        
        current_chunk_text = ""
        current_position = 0
        chunk_index = 0
        overlap_buffer = ""  # Store last N characters for overlap
        
        for sentence in sentences:
            # Add sentence to current chunk
            candidate_chunk = current_chunk_text + sentence + " "
            
            # Check if adding this sentence exceeds chunk_size
            if len(candidate_chunk) > self.chunk_size and current_chunk_text.strip():
                # Save current chunk before exceeding limit
                final_chunk_text = current_chunk_text.strip()
                
                chunk_id = self._generate_chunk_id(
                    publication_id,
                    current_position,
                    final_chunk_text
                )
                
                chunk_record = ChunkRecord(
                    id=chunk_id,
                    publication_id=publication_id,
                    text=final_chunk_text,
                    embedding=None,  # Populated during SPACEBIO-010
                    section=section or "unknown",  # Default if None or empty
                    page=page,  # Remains None for HTML extractions
                    position=current_position,
                    token_count=self._count_tokens(final_chunk_text)
                )
                chunks.append(chunk_record)
                
                # Prepare next chunk with overlap from previous
                overlap_buffer = final_chunk_text[-self.overlap:] if len(final_chunk_text) > self.overlap else final_chunk_text
                current_chunk_text = overlap_buffer + " " + sentence + " "
                current_position += len(final_chunk_text) - len(overlap_buffer)
                chunk_index += 1
            else:
                current_chunk_text = candidate_chunk
        
        # Handle final chunk if there's remaining text
        if current_chunk_text.strip():
            final_chunk_text = current_chunk_text.strip()
            chunk_id = self._generate_chunk_id(
                publication_id,
                current_position,
                final_chunk_text
            )
            
            chunk_record = ChunkRecord(
                id=chunk_id,
                publication_id=publication_id,
                text=final_chunk_text,
                embedding=None,
                section=section or "unknown",
                page=page,
                position=current_position,
                token_count=self._count_tokens(final_chunk_text)
            )
            chunks.append(chunk_record)
        
        return chunks
    
    def chunk_publication(self, publication: PublicationRecord) -> List[ChunkRecord]:
        """
        Chunk a single publication by reading its text file.

        Prefere o texto LIMPO (SPACEBIO-012.5) quando o registro tem
        `clean_path` preenchido, caindo para `local_path` quando não tem.
        Sem essa preferência, a limpeza do corpus não teria efeito algum
        sobre o que chega ao índice vetorial.

        Handles file not found gracefully with empty return.

        Args:
            publication: PublicationRecord to chunk

        Returns:
            List of ChunkRecord objects, or empty list if file not found
        """
        file_path = publication.clean_path or publication.local_path

        if not os.path.exists(file_path):
            print(f"⚠ Warning: File not found for publication '{publication.title}': {file_path}")
            return []
        
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                text = f.read()
        except Exception as e:
            print(f"⚠ Warning: Error reading file {file_path}: {e}")
            return []
        
        # Extract section/page from extraction_method if available
        # For HTML extractions, section and page will be None (handled gracefully)
        section = None
        page = None
        
        return self.chunk_text(
            text=text,
            publication_id=publication.title,
            section=section,
            page=page
        )
    
    def process_all(self) -> List[ChunkRecord]:
        """
        Process all publications in metadata.csv and generate chunks.
        
        This is the main entry point for the chunking pipeline.
        
        Returns:
            List of all ChunkRecord objects from the entire corpus
        """
        if not os.path.exists(self.metadata_file):
            raise FileNotFoundError(f"Metadata file not found: {self.metadata_file}")
        
        print(f"Loading publications from {self.metadata_file}...")
        df = pd.read_csv(self.metadata_file)
        
        all_chunks: List[ChunkRecord] = []
        
        for idx, row in df.iterrows():
            try:
                publication = PublicationRecord(**row)
                chunks = self.chunk_publication(publication)
                all_chunks.extend(chunks)
                
                if (idx + 1) % 10 == 0:
                    print(f"✓ Processed {idx + 1}/{len(df)} publications ({len(all_chunks)} chunks so far)")
            
            except Exception as e:
                print(f"✗ Error processing publication at row {idx}: {e}")
                continue
        
        print(f"\n✓ Chunking complete: {len(all_chunks)} total chunks from {len(df)} publications")
        return all_chunks


if __name__ == "__main__":
    # Quick test with a single sample file
    import sys
    
    print("=" * 70)
    print("SpaceBio Document Chunker (SPACEBIO-009) - Quick Test")
    print("=" * 70)
    
    try:
        chunker = DocumentChunker()

        # Test with one publication
        print("\nLoading metadata...")
        df = pd.read_csv(chunker.metadata_file)
        
        if len(df) > 0:
            first_pub_data = df.iloc[0]
            first_pub = PublicationRecord(**first_pub_data)
            
            print(f"\nTesting with publication: {first_pub.title[:60]}...")
            print(f"File: {first_pub.local_path}")
            
            chunks = chunker.chunk_publication(first_pub)
            
            print(f"\n✓ Generated {len(chunks)} chunks")
            print("\nFirst 3 chunks:")
            for i, chunk in enumerate(chunks[:3]):
                print(f"\n  Chunk {i}:")
                print(f"    ID: {chunk.id}")
                print(f"    Tokens: {chunk.token_count}")
                print(f"    Position: {chunk.position}")
                print(f"    Section: {chunk.section}")
                print(f"    Page: {chunk.page}")
                print(f"    Text preview: {chunk.text[:80]}...")
        
        else:
            print("✗ No publications found in metadata.csv")
    
    except Exception as e:
        print(f"\n✗ Error during test: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
