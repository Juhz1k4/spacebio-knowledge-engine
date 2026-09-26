# SpaceBio — estado do projeto

**Para:** quem for continuar o desenvolvimento sem ter acompanhado as sessões
anteriores (humano ou agente).
**Atualizado em:** 26 de setembro de 2026.

Este documento é o contexto mínimo para retomar o trabalho sem quebrar o que
existe. Ele descreve **o que foi construído, por quê, e o que ainda falta** —
com os números que justificaram cada decisão, porque sem eles as escolhas
parecem arbitrárias e alguém "simplifica" e quebra o sistema.

O plano de trabalho está em `docs/FASE_FINAL.md`. Este arquivo diz onde
paramos dentro daquele plano.

---

## 1. O que o produto é

Um motor de conhecimento sobre biologia espacial, feito para o NASA Space Apps
Challenge 2025 (desafio *Build a Space Biology Knowledge Engine*). O usuário
pergunta em português ou inglês; o sistema responde **citando os trechos
literais dos artigos que sustentam cada afirmação** — ou diz que não há
evidência no acervo.

O diferencial não é "usa IA". É que **a resposta é verificada depois de
gerada**, em código. O campo `grounded` do contrato sai dessa verificação, não
da promessa do prompt.

### A regra que organiza todo o resto

> **Sem evidência, sem afirmação.** O LLM interpreta texto; o código afirma
> números. Nenhum número exibido ao usuário é gerado por LLM nem escrito à mão.

Ela vale para a API, para a interface e para o README.

---

## 2. Onde estamos

| | backend | frontend |
|---|---|---|
| repositório | `Juhz1k4/spacebio-knowledge-engine` | `Juhz1k4/spacebio-frontend` |
| `main` | `c3af4f5` · 22 commits | `069213d` · 21 commits |
| tamanho do `.git` | 13 MB | 921 KB |
| PRs abertos | **#4 — A1** | **#4 — A1** |

Etapas concluídas e mescladas em `main`: **Fases 1 e 2** (RAG com evidência,
ontologia, entity linking), **Etapa 3 inteira** (E3-00 a E3-08) e a **etapa F0**
(higiene do repositório).

Em revisão: **A1** (reposicionamento da página inicial), nos dois repositórios
— precisam ser revisados juntos, porque o frontend consome um endpoint criado
no backend.

### Estado do acervo

Medido na última vez em que o Neo4j esteve no ar:

```
493 publicações (das 608 do conjunto oficial do desafio)
45.947 chunks, todos em intfloat/multilingual-e5-small
979 entidades · 60.552 relações MENTIONS
488/493 (99,0%) com autores e ano vindos do Crossref
```

---

## 3. Arquitetura, na ordem em que os dados andam

```
CSV de metadados
   ↓  cleaner.py          9 regras, 4 salvaguardas → 594 linhas viram 493
   ↓  chunker.py          700/140 caracteres, IDs determinísticos (SHA256)
   ↓  embedder.py         multilingual-e5-small, prefixos passage:/query:
   ↓  graph_manager.py    Neo4j: índice vetorial HNSW + índice full-text
   ↓  ner.py              dicionário da ontologia (9 tipos de nó)
   ↓  entity_linking.py   NCBI Taxonomy, NCBI Gene, UniProt
   ↓  enrich_metadata.py  Crossref: autores, ano, volume, páginas

                         ↓ consulta ↓

   retrieval.py          busca híbrida: vetorial + léxico + entidades,
                         fundidos por RRF (k=60)
   aris.py               orquestra, aplica as TRÊS TRAVAS
   evidence.py           o contrato de resposta
   main.py               HTTP + segurança
```

### As três travas (o coração do projeto)

Estão em `aris.py`, e cada uma pega o que a anterior deixa passar:

| trava | o que verifica | quando |
|---|---|---|
| 1 | a melhor passagem atinge o limiar de similaridade | **antes** do LLM |
| 2 | cada `[n]` aponta para uma fonte que existe | depois do LLM |
| 3 | cada trecho entre aspas existe literalmente na passagem | depois do LLM |

Uma injeção de prompt bem-sucedida ainda precisa passar pela trava 2 para ser
apresentada como fundamentada, e pela 3 se tentar citar texto literal.

---

## 4. As decisões que parecem arbitrárias e não são

**Esta é a seção mais importante deste documento.** Cada número abaixo foi
medido. Mudá-los sem remedir quebra coisas que não dão erro — só pioram.

### Chunk 700/140

A 1000/200, **30,9%** dos chunks estouravam o limite de 256 tokens do encoder e
eram truncados **em silêncio**. A 700/140, 4,4%. O e5 aceita 512 tokens e hoje
trunca 0,7%; a folga foi mantida de propósito.

Alterar isto invalida os IDs determinísticos já gravados no grafo.

### Limiar de evidência 0,92

Recalibrado para o `multilingual-e5-small`, que comprime a faixa de scores:

```
perguntas do domínio      0,921 – 0,963
perguntas fora do domínio 0,865 – 0,916
```

A margem é **estreita**, e o limiar sozinho NÃO separa os dois grupos. Ele
barra o caso óbvio; as travas 2 e 3 fazem o resto. O valor antigo era 0,72, do
MiniLM, e **não se transporta** entre as escalas.

### `intfloat/multilingual-e5-small`, não MiniLM

Três de quatro perguntas em português pontuavam abaixo do limiar por **defeito
do encoder**, não por falta de evidência. O e5 é assimétrico: exige `passage: `
nos documentos e `query: ` nas perguntas (`embedder._prefixes_for`). Mantém 384
dimensões, então o índice não precisou ser recriado.

`paraphrase-multilingual-MiniLM-L12-v2` foi descartado: 128 tokens de limite →
86,8% de truncamento.

### `127.0.0.1`, nunca `localhost`

No Windows, `localhost` resolve `::1` antes de `127.0.0.1`, e os serviços
escutam só IPv4. Medido:

```
GET /api/v1/health     localhost 2073 ms  ·  127.0.0.1  37 ms
TCP cru na 7687        localhost 2051 ms  ·  127.0.0.1  16 ms
```

O Bolt era o mais afetado: **toda conexão nova do pool** pagava 2 segundos.

### `fullmatch` no roteador de intenção

A versão anterior casava o **início** da frase. Medido: **8 de 17** perguntas
científicas eram tratadas como operacionais ("me ajuda com RUNX2" recebia um
guia de uso). O sinal que separa os dois grupos não é vocabulário, é **forma**:
pergunta operacional é frase fechada; sobrou assunto, é sobre o assunto.

Resultado: recall científico **100%**, cobertura operacional **100%**.

### Timeout do LLM em 20 s

O free tier do Gemini devolveu respostas de comprimento semelhante em **3,2 s,
51 s e 91 s**. A variação é da fila do provedor, não do texto — esperar mais
não aumenta a chance, só prolonga uma tela travada.

Usa `request_options={"timeout": N}`, que **aborta** a requisição.
`asyncio.wait_for` não serviria: a chamada é bloqueante e o caminho é
síncrono; ele só faria parar de esperar, deixando a thread pendurada.

### Cache do roteiro em 7 ms

O acerto exato casa por texto normalizado **antes** de vetorizar. Obter o
embedding custaria dezenas de ms gastos antes de saber se há acerto. Medido:
mediana 7,0 ms, p95 26,4 ms, **0 chamadas** de RAG e de LLM.

Invalida por três eixos: modelo de embedding, versão da ontologia e **hash do
prompt**. O último provou seu valor: ao mudar o `SYSTEM_PROMPT` na E3-08, as
cinco respostas em cache foram recusadas automaticamente — sem isso a
demonstração teria servido respostas geradas sob regras que não existem mais.

### Metadados do Crossref ingeridos, não consultados ao vivo

Seis cartões por resposta seriam seis idas à rede por um dado **imutável**
(autor e ano de artigo publicado não mudam). Consequência verificada: **o
frontend faz zero requisições ao Crossref**.

### SciSpaCy foi rejeitado

`pip install --dry-run` mostrou numpy 2.4.6 → 1.26.4. O scipy e o torch estão
compilados contra a ABI do numpy 2.x e quebrariam em runtime. O NER é por
dicionário da ontologia.

---

## 5. O que está pronto

### Fases 1 e 2 — o motor

Corpus limpo e curado (594 → 493; 101 descartes são erratas sem conteúdo
científico e arquivos cujo título baixado não corresponde ao registrado — ver
`docs/issues/SPACEBIO-007.1-title-divergence.md`). Busca híbrida com RRF.
Ontologia v1.0.0 com 9 tipos de nó. Entity linking contra NCBI e UniProt.
Build incremental: reingestão sem mudanças caiu de **37m03s para 3s**.

### Etapa 3

| issue | entrega |
|---|---|
| E3-01 | `127.0.0.1` em toda a configuração; `docs/RETRIEVAL_AUDIT.md` |
| E3-02 | validação de citações literais — pega a inventada e a traduzida pelo mesmo teste |
| E3-03 | roteador com métrica assimétrica; vocabulário gerado do grafo |
| E3-04 | degradação graciosa: 200 com a evidência, **nunca 500** |
| E3-05 | cache do roteiro, 5 perguntas, acerto sub-100 ms |
| E3-06 | metadados do Crossref, 488/493 |
| E3-07 | exportação ABNT e BibTeX, **gerada por código, sem LLM** |
| E3-08 | auditoria de segurança (OWASP LLM Top 10); `docs/SECURITY_AUDIT.md` |

### Etapa F0 — higiene

Índice limpo; histórico expurgado de um PDF de 35 MB (`.git` caiu de 48 MB para
13 MB); interface sem números inventados nem promessas sem lastro; plano
versionado.

### A1 — em revisão

`GET /api/v1/stats` conta no grafo; a página inicial lê dali. **A landing
estava atrás do login** e foi tornada pública — esse foi o achado que mudou o
escopo da issue.

---

## 6. O que falta

Do `docs/FASE_FINAL.md`, na ordem do caminho mínimo para publicar:

| issue | o que é | tamanho |
|---|---|---|
| **A2** | Panorama do acervo — gráficos, linha do tempo, matriz | grande |
| **A4** | distinguir Resultados de Conclusões (`Chunk.section` já existe) | médio |
| **A6** | flag `VITE_FEATURE_SOCIAL`, desliga as telas de rede social | pequeno |
| **A7** | sínteses temáticas — 12 a 15 temas, pipeline real, citações validadas | grande |
| **A10** | ouvir as sínteses (Web Speech API) | pequeno |
| **B1** | camada `DataSource` com duas implementações | médio |
| **B2** | exportação dos dados estáticos | médio |
| **B4** | perguntas fora do roteiro na demo estática | pequeno |
| **B7** | página "Sobre" | pequeno |
| **B8** | build, deploy e roteamento SPA | médio |
| **B9** | celular primeiro (Lighthouse ≥ 85 / ≥ 90) | médio |
| **B10** | cartão de compartilhamento (`og-image.png` 1200×630) | pequeno |
| **C1** | README do backend com "Decisões de engenharia" | pequeno |
| **C3** | capturas e GIF por Playwright | pequeno |

Segunda publicação: A3 (lacunas), A5 (OSDR), A8 (missão), A9 (amostra),
B3 (catálogo), B5 (salvos), B6 ("Como medimos"), C2, C4.

### Débitos conhecidos, registrados e não escondidos

- **Rate limit é por processo.** Com `--workers N`, cada worker tem o seu.
  Escalar exige Redis.
- **`X-Forwarded-For` é falsificável.** Só confiável atrás de um proxy que o
  reescreva.
- **Sem autenticação na API.** É demonstração pública sem dados de usuário.
- **Cota diária não é enforçada.** O rate limit contém rajadas, não o total do
  dia.
- **Sem `pip-audit` nem `npm audit` no CI.** Próxima lacuna a fechar.
- **`X-XSS-Protection: 1; mode=block`** foi implementado como a issue pediu,
  mas o OWASP hoje recomenda `0` — o XSS Auditor saiu do Chrome em 2019. A
  proteção real vem da CSP e de o frontend não usar `dangerouslySetInnerHTML`.
- **493 de 608.** Decisão mantida; descrita assim em toda a interface.

---

## 7. Armadilhas — leia antes de mexer

**Comentário em HTML não é removido no build.** Um bloco de 27 linhas no
`index.html` viajava para o navegador de todo visitante. Vale para
`index.html`, não para `.tsx`.

**`TestClient` não atravessa a camada de protocolo.** Um teste do cabeçalho
`Server` passava enquanto o servidor real anunciava a versão. Cabeçalho de
servidor exige HTTP de verdade.

**`git show branch:arquivo` quebra no Git Bash do Windows.** O MSYS converte
`branch:arquivo` em caminho. Use `MSYS_NO_PATHCONV=1`.

**Um arquivo já no índice continua rastreado** mesmo depois de entrar no
`.gitignore`. Confira com `git ls-files -i -c --exclude-standard`.

**O `.env` do frontend pode não existir.** Sem `VITE_SUPABASE_URL`, o cliente
Supabase era construído no import e apagava a aplicação inteira — `#root` com
0 caracteres, sem mensagem. Hoje a construção é preguiçosa (A1), mas a
configuração continua necessária para as telas com conta.

**Não confie na primeira medição de latência.** A primeira requisição depois de
um restart paga o carregamento tardio do modelo de embeddings (~25 s). Aqueça
antes de medir.

**O corpus é entrada não confiável.** São artigos de terceiros. Título e texto
das passagens passam por `neutralize_markers` como qualquer entrada.

**Números no código são proibidos na interface.** A única exceção documentada é
`PUBLICACOES_NO_CONJUNTO_OFICIAL = 608`, em `i18n/pt-BR.ts`, porque não existe
no nosso grafo.

---

## 8. Como rodar

```bash
# Backend — precisa do Neo4j no ar (Neo4j Desktop, porta 7687)
cd spacebio-knowledge-engine
venv\Scripts\python.exe -m uvicorn main:app --host 0.0.0.0 --port 8000 --no-server-header

# Frontend
cd spacebio-frontend
npx vite --host 0.0.0.0 --port 8080
```

`--no-server-header` não é opcional em ambiente exposto: sem ele o uvicorn
anuncia a própria versão, e o middleware não alcança a camada de protocolo.

### Suíte

```bash
cd spacebio-knowledge-engine
for t in test_*.py; do venv\Scripts\python.exe $t; done
```

Última execução completa (Neo4j no ar): **770 verificações em 14 arquivos**,
zero falhas. Com a A1, somam-se 26 de `test_stats.py`.

Cinco suítes exigem Neo4j: `test_aris`, `test_hybrid`, `test_incremental`,
`test_linking`, `test_retrieval`. Com o banco fora, elas não rodam — e isso
**não é falha**, é dependência.

### Regeneração

```bash
python enrich_metadata.py          # Crossref (~3 min, incremental)
python build_domain_vocabulary.py  # vocabulário do roteador, a partir do grafo
python demo_cache.py --build       # cache do roteiro (incremental; gasta quota)
python demo_cache.py --refresh-metadata   # só metadados, sem chamar o LLM
```

---

## 9. Como trabalhar neste projeto

As regras de processo estão em `CLAUDE.md`, na raiz do workspace (não
versionado). Em resumo: branch e PR para tudo; **merge no `main` só com
aprovação explícita**; nunca `push --force`, reescrita de histórico ou
exclusão de branch sem perguntar; sempre merge commit.

E a regra de comentários, que é o que mantém este projeto legível: comentar o
**porquê** da decisão e **o número medido**, não o que a linha faz.
