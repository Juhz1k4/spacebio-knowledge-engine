# SpaceBio — Knowledge Engine

Motor de conhecimento para literatura de biologia espacial. Transforma 493
publicações científicas do PubMed Central num grafo consultável e numa
assistente que responde **citando evidência verificada** — ou recusa
responder quando o corpus não sustenta a pergunta.

NASA Space Apps Challenge 2025 · *Build a Space Biology Knowledge Engine*

---

## Em 30 segundos

```bash
# 1. ambiente
python -m venv venv
./venv/Scripts/python -m pip install -r requirements.txt
./venv/Scripts/python -m spacy download en_core_web_sm

# 2. configuração
cp .env.example .env        # preencha NEO4J_PASSWORD e GOOGLE_API_KEY

# 3. suba o Neo4j 5.x+ e rode o pipeline
./venv/Scripts/python cleaner.py            # limpa o corpus      (~2 min)
./venv/Scripts/python ingest_corpus.py      # chunks + embeddings (~37 min)
./venv/Scripts/python extract_entities.py   # entidades           (~2 min)
./venv/Scripts/python entity_linking.py     # NCBI/UniProt        (~21 min)

# 4. API
./venv/Scripts/python -m uvicorn main:app --reload
```

Reexecutar qualquer etapa é barato: o pipeline é **incremental** e pula o que
não mudou (ingestão completa sem mudanças: 3 segundos).

---

## O que o sistema faz de diferente

A maior parte dos sistemas de RAG promete grounding no prompt. Este **verifica**:

| Garantia | Como é obtida |
|---|---|
| Não responde sem evidência | Limiar de similaridade 0,72 **antes** de chamar o LLM |
| Citações apontam para fontes reais | `verify_citations()` confere cada `[n]` depois |
| `chunks_used` é medido, não estimado | Contagem das citações verificadas |
| Nenhum DOI inventado | DOI vem do grafo; ausente é `None`, nunca fabricado |
| Recupera termo exato E conceito | Três canais: vetorial + BM25 + entidades |

O limiar foi calibrado medindo: perguntas do domínio pontuam 0,89–0,92 e
perguntas fora dele 0,59–0,66. Não é zero porque, com vetores normalizados,
qualquer pergunta pontua ~0,6 — sem limiar, o modelo receberia "evidência"
para qualquer coisa.

---

## Arquitetura

```
  data/processed_text/        corpus bruto (594 registros, 38 M chars)
            │
            ▼  cleaner.py         R1-R9 + salvaguardas S1-S4
  data/clean_text/            corpus curado (493 docs, −33,5%)
            │
            ▼  ingest_corpus.py   chunker.py + embedder.py
       ┌────────────────────────────────────┐
       │  Neo4j                             │
       │    (:Publication)-[:HAS_CHUNK]->   │
       │        (:Chunk {embedding})        │
       │    (:Chunk)-[:MENTIONS]->(:Entity) │
       │    índice vetorial + full-text     │
       └────────────────────────────────────┘
            │                    ▲
            │                    │  extract_entities.py + entity_linking.py
            ▼  retrieval.py
    busca híbrida (RRF de 3 canais)
            │
            ▼  aris.py  +  llm_provider.py
    EvidenceAnswer (evidence.py)
            │
            ▼  main.py
    POST /api/v1/chat
```

### Onde está cada coisa

| Arquivo | Responsabilidade |
|---|---|
| `config.py` | Credenciais e parâmetros, tudo do ambiente |
| `schema.py` | Contrato de dados: `PublicationRecord`, `ChunkRecord` |
| `cleaner.py` | Limpeza do corpus (R1–R9) e salvaguardas (S1–S4) |
| `chunker.py` | Fatiamento determinístico, 700/140 |
| `embedder.py` | Embeddings all-MiniLM-L6-v2, 384 dims |
| `graph_manager.py` | Schema, índices e **escrita** no Neo4j |
| `retrieval.py` | **Leitura** do grafo e busca híbrida |
| `ontology.py` | Tipos de entidade e dicionário de domínio |
| `ner.py` | Extração e normalização de entidades |
| `entity_linking.py` | Identificadores NCBI Taxonomy / NCBI Gene / UniProt |
| `evidence.py` | Contrato de resposta e verificação de citações |
| `prompts.py` | As 8 regras de grounding da Dra. Aris |
| `llm_provider.py` | Abstração de LLM (Gemini, Echo para testes) |
| `aris.py` | Orquestração e as duas travas |
| `main.py` | API HTTP |

**Divisão que vale conhecer:** `graph_manager.py` escreve, `retrieval.py` lê.
Toda consulta de leitura passa pelo repositório; nenhuma Cypher solta no resto
do código.

---

## Decisões que parecem estranhas e não são

Estão documentadas no próprio código, mas as principais:

**`chunk_size=700`, não 1000.** O all-MiniLM-L6-v2 corta em 256 word-pieces e
descarta o resto **em silêncio**. Com 1000 caracteres, 30,9% dos chunks
perdiam texto — evidência que o RAG nunca recuperaria. Com 700, 4,4%.

**O NER é determinístico, não SciSpaCy.** Instalar scispacy rebaixaria numpy de
2.4.6 para 1.26.4, e scipy e torch foram compilados contra a ABI do numpy 2.x.
Quebraria o embedder. Ver o docstring de `ner.py`.

**Filtro por publicação não usa o índice vetorial.** Com 45.947 chunks, os
vizinhos mais próximos do corpus quase nunca pertencem a uma publicação
específica, e a busca voltava vazia. Filtro estreito → varredura exata do
subconjunto; sem filtro → índice ANN.

**Fusão por RRF, não por soma de scores.** Cosseno vive em [0,1] e BM25 é
ilimitado. A RRF combina posições, não magnitudes.

**83 publicações foram descartadas de propósito.** 11 erratas e 70 cujo título
do metadata não corresponde ao artigo baixado. É preferível um corpus menor e
confiável a um que faça a assistente citar a fonte errada. Ver
`docs/issues/SPACEBIO-007.1-title-divergence.md`.

---

## Testes

```bash
for t in test_ner test_linking test_incremental test_pipeline \
         test_retrieval test_hybrid test_aris; do
    ./venv/Scripts/python $t.py
done
```

360 asserções. Exigem Neo4j no ar e corpus ingerido, exceto `test_ner.py`
(puramente unitário). `test_aris.py` e `test_linking.py` aceitam `--live` para
chamar as APIs externas de verdade.

Os testes de falso positivo em `test_ner.py` são **regressões de casos reais**
medidos no corpus, não hipóteses: `miR-125b` confundido com a estação Mir,
`ISS` casando dentro de "tissue", `ECM` classificado como gene.

---

## Armadilhas conhecidas

| Sintoma | Causa |
|---|---|
| `ServiceUnavailable` em 7687 | Neo4j não está rodando |
| Dra. Aris devolve só passagens | Quota do Gemini esgotada (20/dia no free tier) |
| Extração não processa nada | Tudo já anotado nesta `ONTOLOGY_VERSION` |
| Mudou o dicionário e nada mudou | Incremente `ONTOLOGY_VERSION` em `ontology.py` |
| `build_graph.py` bloqueia | Correto — é obsoleto, ver docstring |

---

## Documentação

- `docs/ROADMAP-FASE-3.md` — próximas issues, dependências e critérios de aceite
- `docs/issues/SPACEBIO-007.1-title-divergence.md` — divergência de procedência
- `SpaceBio_Master_Briefing_V2_Completo.md` (raiz do workspace) — visão e arquitetura-alvo

As referências a `§N` no código apontam para as seções do Master Briefing.
