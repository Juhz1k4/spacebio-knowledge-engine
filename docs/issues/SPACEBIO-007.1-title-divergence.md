# SPACEBIO-007.1 — Divergência entre título do metadata e artigo baixado

> **Nota de numeração:** esta issue nasceu como SPACEBIO-013 e foi renumerada
> em 2026-09-02 para evitar colisão com "Hybrid Retriever V1" (§37 do Master
> Briefing). O ID 007.1 a coloca na família certa: pipeline de ingestão
> quebrado, Fase 0 — é uma extensão do SPACEBIO-007.

**Status:** aberta
**Prioridade:** alta (integridade de procedência)
**Descoberta em:** SPACEBIO-012.5, durante a limpeza do corpus
**Bloqueia:** confiança nas citações da Dra. Aris

---

## Context

O `data/metadata.csv` registra, para cada publicação, um `title` e uma
`source_url` do PMC. O texto processado em `data/processed_text/` deveria ser o
artigo correspondente a essa URL.

Ao extrair o título interno do documento (a linha seguinte a `Add to search`,
que é como o PMC apresenta o título do artigo) e compará-lo com o título do
metadata, **70 dos 576 documentos não correspondem**.

## Problem

Em 70 casos o arquivo contém um artigo **diferente** do que o metadata declara.
Não é variação de grafia nem truncamento: são artigos distintos.

Distribuição da similaridade (SequenceMatcher sobre títulos normalizados):

| Faixa | Documentos | Leitura |
|---|---:|---|
| < 0,10 | 1 | artigos sem nenhuma relação |
| 0,10 – 0,30 | 29 | artigos distintos, mesmo campo |
| 0,30 – 0,60 | 40 | distintos, algum vocabulário em comum |

Exemplos verificados:

```
sim=0.06
  metadata : Hindlimb Unloading: Rodent Analog for Microgravity
  documento: Single-cell RNA sequencing of the carotid artery and femoral artery
  url      : https://www.ncbi.nlm.nih.gov/pmc/articles/PMC11747068/

sim=0.13
  metadata : Innate immune responses of Drosophila melanogaster are altered by spaceflight
  documento: Spaceflight and simulated microgravity conditions increase virulence
  url      : https://www.ncbi.nlm.nih.gov/pmc/articles/PMC7000411/

sim=0.16
  metadata : Stem Cell Health and Tissue Regeneration in Microgravity
  documento: Microgravity and Cellular Biology: Insights into Cellular Responses
  url      : https://www.ncbi.nlm.nih.gov/pmc/articles/PMC11988870/
```

### Por que isso é grave

A Dra. Aris recupera um trecho, e cita o título que o metadata afirma. Com
esses 70 documentos no corpus, ela atribuiria uma passagem real ao **artigo
errado** — uma citação que parece bem fundamentada e não é. É a violação mais
direta possível do princípio "No evidence, no claim" (§15), porque a evidência
existe mas a procedência é falsa.

### Causa provável

Ainda não investigada. Hipóteses, em ordem de plausibilidade:

1. **Redirecionamento do PMC.** A URL foi resolvida para outro artigo (versão
   substituída, artigo retratado e republicado, link canônico alterado).
2. **Desalinhamento de índice no scraper.** O laço de ingestão escreveu o
   título da linha *i* junto com o texto da linha *j* — clássico de listas
   percorridas em paralelo.
3. **Lista de origem desatualizada.** O CSV de publicações da NASA aponta para
   URLs que mudaram de conteúdo desde a coleta.

A hipótese 2 é testável de graça: se o erro for de índice, os títulos trocados
devem aparecer em outras linhas do próprio metadata.

## Goal

Determinar a causa e restaurar a correspondência título ↔ documento para os 70
casos, seja corrigindo o metadata, seja re-baixando o artigo correto.

## Out of Scope

- Alterar as regras de limpeza (SPACEBIO-012.5 está fechada).
- Reescrever o pipeline de ingestão inteiro.
- Mexer no chunking, embeddings ou índice vetorial.

## Files likely affected

- `ingest.py`, `ingest_from_github.py` — os scrapers
- `data/metadata.csv` — a fonte da divergência
- `verify_ingestion.py` — deveria ter detectado isto
- `cleaner.py` — a regra R8, que hoje contorna o problema excluindo

## Implementation constraints

- Não apagar `data/processed_text/`: o texto baixado é evidência do bug.
- Qualquer correção de metadata precisa ser reproduzível por script, não manual.
- Se um artigo for re-baixado, registrar a data da nova coleta.

## Acceptance Criteria

1. A causa raiz está identificada e documentada.
2. Os 70 documentos estão resolvidos: metadata corrigido ou artigo re-baixado.
3. `verify_ingestion.py` passa a checar a correspondência título ↔ documento e
   falha quando ela quebra.
4. Rodar `cleaner.py` volta a aprovar esses documentos pela R8 (nenhum
   `excluded_title_mismatch` restante), ou o motivo de cada exceção está
   registrado.
5. Nenhuma regressão: os 493 documentos hoje aprovados continuam aprovados.

## Tests

- Teste de regressão com os 3 casos-exemplo acima, verificando que o título do
  metadata bate com o título interno do documento baixado.
- Teste da hipótese 2: procurar cada título "perdido" nas demais linhas do
  metadata; um número alto de reencontros confirma o desalinhamento de índice.

## Expected output

1. Relatório da causa raiz.
2. Metadata corrigido (ou textos re-baixados).
3. Checagem de correspondência incorporada à verificação de ingestão.
4. Registro de quantos dos 70 foram recuperados para o corpus.

---

## Situação atual (contorno)

A regra **R8** do `cleaner.py` exclui esses 70 documentos do corpus limpo,
usando o limiar `MIN_TITLE_SIMILARITY = 0.60`. O corpus caiu de 576 para 493
documentos.

É contenção, não correção: 70 artigos científicos legítimos estão fora do
alcance da Dra. Aris porque não sabemos a qual publicação atribuí-los. Resolver
esta issue os devolve ao corpus.

Todos os casos estão listados em `data/cleaning_report.csv`, com
`status = excluded_title_mismatch` e a coluna `title_similarity`.
