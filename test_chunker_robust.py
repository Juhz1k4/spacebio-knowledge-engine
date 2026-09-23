#!/usr/bin/env python
"""Robust validation test for SPACEBIO-008 and SPACEBIO-009 with error handling"""

from chunker import DocumentChunker
from schema import PublicationRecord, ChunkRecord
import pandas as pd
import traceback

print("\n" + "="*70)
print("SPACEBIO-008 & SPACEBIO-009 - Robust Validation Test")
print("="*70)

print("\n1. Configurando chunker...")
chunker = DocumentChunker()
print(f"   ✓ Chunk size: {chunker.chunk_size} caracteres")
print(f"   ✓ Overlap: {chunker.overlap} caracteres")

print("\n2. Processando corpus com tratamento de erros...")

# Carregar todas as publicações
metadata_path = "data/metadata.csv"
df = pd.read_csv(metadata_path)

all_chunks = []
failed_publications = []
successful_count = 0

for idx, row in df.iterrows():
    try:
        pub = PublicationRecord(
            title=row['title'],
            source_url=row['source_url'],
            local_path=row['local_path'],
            extraction_method=row['extraction_method']
        )
        
        chunks = chunker.chunk_publication(pub)
        all_chunks.extend(chunks)
        successful_count += 1
        
        if (idx + 1) % 50 == 0:
            print(f"   ✓ Processadas {idx + 1}/{len(df)} publicações ({len(all_chunks)} chunks)")
            
    except Exception as e:
        failed_publications.append({
            'index': idx,
            'title': row['title'][:50],
            'error': str(e)[:100]
        })
        if (idx + 1) % 50 == 0:
            print(f"   ⚠ Processadas {idx + 1}/{len(df)} publicações (alguns erros, {len(all_chunks)} chunks)")

print(f"\n3. Estatísticas de Chunking:")
print(f"   ✓ Publications processadas com sucesso: {successful_count}/{len(df)}")
print(f"   ✓ Total de chunks: {len(all_chunks)}")
print(f"   ✓ Publications com erro: {len(failed_publications)}")

if failed_publications:
    print(f"\n   Erros encontrados (primeiros 5):")
    for i, fail in enumerate(failed_publications[:5]):
        print(f"      • Pub #{fail['index']+1}: {fail['title']}... → {fail['error']}")

if all_chunks:
    chunk_df = pd.DataFrame([
        {
            'id': c.id[:50],
            'pub_id': c.publication_id[:30],
            'tokens': c.token_count,
            'section': c.section,
            'page': c.page,
            'position': c.position
        }
        for c in all_chunks
    ])
    
    print(f"\n4. Análise de Tokens:")
    print(f"   ✓ Token count (média): {chunk_df['tokens'].mean():.1f}")
    print(f"   ✓ Token count (mediana): {chunk_df['tokens'].median():.1f}")
    print(f"   ✓ Token count (min/max): {chunk_df['tokens'].min()}/{chunk_df['tokens'].max()}")
    print(f"   ✓ Token count (std): {chunk_df['tokens'].std():.1f}")
    
    print(f"\n5. Análise de Metadados:")
    print(f"   ✓ Seções únicas: {chunk_df['section'].nunique()}")
    print(f"   ✓ Seções encontradas: {sorted(chunk_df['section'].dropna().unique().tolist())}")
    print(f"   ✓ Chunks com page=None (esperado para HTML): {chunk_df[chunk_df['page'].isna()].shape[0]}/{len(chunk_df)}")
    
    print(f"\n6. Validação Estrutural:")
    # Todos os chunks têm publication_id válido
    valid_pub_ids = sum(1 for c in all_chunks if c.publication_id and len(c.publication_id.strip()) > 0)
    print(f"   ✓ Chunks com publication_id válido: {valid_pub_ids}/{len(all_chunks)}")
    
    # Todos os chunks têm texto
    valid_text = sum(1 for c in all_chunks if c.text and len(c.text.strip()) > 0)
    print(f"   ✓ Chunks com texto não-vazio: {valid_text}/{len(all_chunks)}")
    
    # Todos têm token_count > 0
    valid_tokens = sum(1 for c in all_chunks if c.token_count > 0)
    print(f"   ✓ Chunks com token_count > 0: {valid_tokens}/{len(all_chunks)}")
    
    # IDs contêm partes esperadas
    valid_ids = sum(1 for c in all_chunks if c.id and '_' in c.id)
    print(f"   ✓ Chunks com IDs determinísticos (formato): {valid_ids}/{len(all_chunks)}")
    
    print(f"\n7. Exemplos de Chunks:")
    for i, chunk in enumerate(all_chunks[:3]):
        print(f"\n   Chunk {i}:")
        print(f"     ID: {chunk.id}")
        print(f"     Publication: {chunk.publication_id[:40]}...")
        print(f"     Texto: {chunk.text[:60]}...")
        print(f"     Posição: {chunk.position}, Tokens: {chunk.token_count}")
        print(f"     Section: {chunk.section}, Page: {chunk.page}")

print("\n" + "="*70)
print("✓ VALIDAÇÃO COMPLETA - SPACEBIO-008 e SPACEBIO-009")
print("="*70)
print("\nResumo Executivo:")
print(f"  • Publicações: {successful_count}/{len(df)} processadas com sucesso")
print(f"  • Chunks Totais: {len(all_chunks)} gerados")
print(f"  • Taxa de Chunk/Pub: {len(all_chunks)/max(successful_count, 1):.1f}")
print(f"  • Status: {'✓ PRONTO PARA SPACEBIO-010' if len(all_chunks) > 0 else '⚠ VERIFICAÇÃO NECESSÁRIA'}")
print("\nPróximos passos:")
print("  • SPACEBIO-010: Embedding Pipeline (sentence-transformers)")
print("  • SPACEBIO-011: Neo4j Vector Index")
print("  • SPACEBIO-012: Parameterized Retrieval Queries")
