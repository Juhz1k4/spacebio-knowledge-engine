# SpaceBio — Roadmap da Fase 3 (Product Experience)

> **Estado:** aprovado em 2026-09-21
> **Fases 0, 1 e 2:** concluídas — 360 asserções verdes em 7 suítes
> **Critério de saída (§39):** *o diferencial do sistema deve ser compreensível visualmente em menos de um minuto*

---

## 1. Por que esta fase é diferente das anteriores

As Fases 1 e 2 foram julgadas por testes. A Fase 3 será julgada por **pessoas olhando uma tela por 60 segundos**.

Isso inverte a economia das decisões. Uma consulta Cypher elegante não pontua; um cartão de evidência com DOI clicável, sim. O risco desta fase não é técnico — é ter a melhor engenharia da sala e não conseguir mostrá-la.

### O que temos que ninguém mais terá

| Ativo | Onde já está pronto |
|---|---|
| Recusa honesta por falta de evidência | `aris.py` — trava de limiar (0,72), antes do LLM |
| Citações verificadas, não prometidas | `evidence.py` — `verify_citations()` |
| Procedência até o DOI real | 488 DOIs no grafo, nenhum fabricado |
| Entidades ligadas a NCBI/UniProt | 548 de 605, com trava taxonômica |
| Recuperação por 3 canais | `retrieval.py` — semântico + BM25 + entidades, fundidos por RRF |

**A interface deve gastar seu espaço mostrando procedência, não conversa.** Pelo teste do §48, nossa vantagem é inteiramente **Evidence** e **Trust**.

---

## 2. Decisões travadas (não reabrir sem motivo novo)

### 2.1 Researcher Explorer está FORA do escopo

A SPACEBIO-029 do briefing original não é implementável e foi cortada.

**Motivo:** não existem nós `Researcher` no grafo. Os nomes de autor vivem no cabeçalho do PMC, que a regra **R3** do `cleaner.py` descarta. Reverter a R3 devolveria ao corpus 1,72 M de caracteres de licença, afiliação e navegação — degradando a recuperação que a Fase 1 construiu.

**Consequência aceita:** o produto não terá exploração por pesquisador nesta versão. O foco é 100% evidência científica.

### 2.2 Quota do Gemini: respostas pré-computadas

O free tier permite **20 requisições por dia**. Ativar billing não está no orçamento.

**Solução adotada:** cache de respostas para o roteiro da demonstração (SPACEBIO-015.1). Perguntas fora do roteiro continuam chamando a API ao vivo enquanto houver quota, e degradam para as passagens recuperadas quando ela acabar — comportamento que `_evidence_only_answer()` já implementa e que foi validado em produção quando a quota estourou durante os testes.

---

## 3. Estado do frontend (levantado em 2026-09-21)

```
Stack:   React 18.3 · TypeScript 5.8 · Vite 5.4 · Tailwind 3.4 · shadcn-ui
         react-router-dom 6.30 · TanStack Query 5.83 · Supabase 2.58

Páginas: Index · Feed · Chat · AIAssistant · Auth · Profile · Settings
         Dashboard · NotFound

Rotas:   /auth  /  /feed  /chat  /ai-assistant  /profile  /settings  *
```

Três fatos que definem o trabalho:

1. **`src/pages/AIAssistant.tsx:41`** chama `${VITE_API_URL}/chat` e usa **apenas `data.answer`**. Todo o contrato de evidência — `sources`, `entities`, `retrieval`, `grounded`, `warnings` — é descartado no cliente. O backend já entrega tudo; falta renderizar.
2. **`Dashboard.tsx` existe mas não está em `App.tsx`.** Nenhuma rota aponta para ele.
3. **Não há biblioteca de grafo instalada.** O Knowledge Explorer exige uma dependência nova.

`VITE_API_URL` já está configurado no `.env` do frontend, e o CORS foi resolvido na Fase 0.

---

## 4. Mapa de dependências

```
                    ┌──────────────────────────┐
                    │ SPACEBIO-024             │
                    │ Dra. Aris UI V2          │  ← nada bloqueia; comece aqui
                    │ consome /api/v1/chat     │
                    └────────────┬─────────────┘
                                 │
                 ┌───────────────┼────────────────┐
                 ↓               ↓                ↓
      ┌────────────────┐ ┌──────────────┐ ┌──────────────┐
      │ SPACEBIO-025   │ │ SPACEBIO-026 │ │ SPACEBIO-027 │
      │ Source Cards   │ │ Sci. Search  │ │ Publication  │
      └───────┬────────┘ └──────────────┘ │ Explorer     │
              │                           └──────────────┘
              ↓
    ┌──────────────────┐
    │ SPACEBIO-028     │
    │ Knowledge        │  ← precisa de lib de grafo (decisão técnica)
    │ Explorer         │
    └──────────────────┘

   Independentes, podem correr em paralelo a qualquer momento:
      SPACEBIO-015.1  Demo Answer Cache      (backend, isolado)
      SPACEBIO-031    Remover Feed/Chat mock (frontend, isolado)
      SPACEBIO-030    Auth Route Cleanup     (frontend, isolado)
```

**Caminho crítico:** 024 → 025 → 028. Tudo o mais é paralelizável.

---

## 5. Issues

### SPACEBIO-024 — Dra. Aris UI V2 · **P0, caminho crítico**

**Context.** O backend expõe `POST /api/v1/chat` devolvendo o contrato do §14 completo. O cliente atual lê um único campo.

**Problem.** A tela atual é indistinguível de qualquer chatbot. Nada do que torna o sistema confiável — fontes, canais de recuperação, recusa honesta — chega ao usuário.

**Goal.** Migrar `AIAssistant.tsx` para `/api/v1/chat` e renderizar a resposta em quatro regiões, conforme o §25: `ANSWER` · `SCIENTIFIC EVIDENCE` · `RELATED ENTITIES` · `EXPLORE GRAPH`.

**Out of scope.** Estilo dos cartões de fonte (é a 025). Visualização do grafo (é a 028). Histórico persistente de conversa.

**Files.** `src/pages/AIAssistant.tsx`, `src/types/evidence.ts` (novo), `src/lib/api.ts` (novo).

**Constraints.**
- Tipar o contrato em TypeScript espelhando `evidence.py` — `EvidenceAnswer`, `EvidenceSource`, `RetrievalTrace`.
- Citações `[n]` no texto viram âncoras clicáveis que rolam até a fonte correspondente.
- `grounded: false` **deve** ser visualmente distinto. Uma recusa honesta é uma funcionalidade, não um erro: apresentá-la como falha destrói o valor que ela cria.
- `warnings` sempre visíveis quando não vazios.

**Acceptance criteria.**
1. Uma pergunta do domínio renderiza resposta, fontes numeradas e entidades.
2. Uma pergunta fora do domínio (ex.: *qual a melhor receita de pizza?*) mostra a recusa com destaque próprio, sem parecer erro de rede.
3. Clicar em `[2]` no texto leva à fonte 2.
4. `chunks_used / chunks_considered` e `top_score` visíveis, ainda que discretos.
5. Erro de rede continua distinguível de recusa por falta de evidência.

**Tests.** Componente renderiza os três estados a partir de fixtures: `grounded=true`, `grounded=false`, `warnings` não vazio.

---

### SPACEBIO-025 — Source Cards · **P0, caminho crítico**

**Context.** Cada item de `sources` traz título, URL, DOI, journal, passagem literal, relevância e os canais que a recuperaram.

**Problem.** Uma lista de links não comunica procedência. O júri precisa ver *o trecho* que sustenta a afirmação, não só onde ele mora.

**Goal.** Um componente de cartão que torne a cadeia de evidência legível de relance.

**Out of scope.** Navegação para a publicação inteira (é a 027).

**Files.** `src/components/SourceCard.tsx` (novo), `src/components/EvidenceList.tsx` (novo).

**Constraints.**
- Mostrar a **passagem literal**, não um resumo. É o ativo.
- DOI ausente renderiza como ausente — nunca inventado, nunca omitido silenciosamente (§15.4). 5 das 493 publicações não têm DOI.
- `page` é sempre `null` neste corpus (extração HTML). Não exibir campo de página.
- Exibir os canais (`semantic` / `lexical` / `entity`): explica *por que* o trecho foi recuperado.

**Acceptance criteria.**
1. O cartão mostra: título, journal, DOI (ou "não disponível"), passagem, relevância, canais.
2. DOI é link para `https://doi.org/<doi>`; URL é link para o PMC.
3. Fontes não citadas pela resposta são visualmente secundárias às citadas.
4. Passagem longa trunca com expansão, sem quebrar o layout.

**Tests.** Fixtures com DOI presente, DOI ausente, passagem longa, e fonte não citada.

---

### SPACEBIO-015.1 — Demo Answer Cache · **P0, paralelo**

**Context.** 20 requisições/dia no free tier do Gemini. Ensaios de véspera consomem a quota da apresentação.

**Problem.** Uma demonstração ao vivo que falha por quota é pior que nenhuma demonstração.

**Goal.** Cache em disco de respostas da Dra. Aris para um roteiro de perguntas definido.

**Out of scope.** Cache de recuperação (já é rápida). Invalidação automática.

**Files.** `aris.py`, `data/demo_answers.json` (novo), `scripts/build_demo_cache.py` (novo).

**Constraints.**
- A chave é a pergunta normalizada mais `ONTOLOGY_VERSION` e o modelo — um cache que sobrevive a mudança de corpus serve resposta obsoleta.
- Cache **não** entra no caminho quando a quota está disponível, salvo com flag explícita `--demo-mode`.
- O contrato devolvido do cache é idêntico ao ao vivo, inclusive `grounded` e `warnings`.
- Respostas cacheadas ficam marcadas, para nunca serem confundidas com geração ao vivo em avaliação.

**Acceptance criteria.**
1. `python scripts/build_demo_cache.py` popula o cache a partir de uma lista de perguntas.
2. Com `--demo-mode`, uma pergunta do roteiro responde sem tocar na API.
3. Pergunta fora do roteiro, em demo-mode, degrada para as passagens recuperadas.
4. O cache registra quando foi gerado e com qual modelo.

**Tests.** Acerto de cache, erro de cache, e invalidação por mudança de `ONTOLOGY_VERSION`.

---

### SPACEBIO-031 — Remover Feed e Chat do MVP · **P1, paralelo**

**Context.** `Feed.tsx` e `Chat.tsx` são mock. O §27 recomenda removê-los: *funcionalidade mock em grande destaque enfraquece credibilidade*.

**Goal.** Tirar as duas telas do caminho principal do produto.

**Constraints.** Remover as rotas e os itens de menu; **não apagar** os componentes — a decisão é de escopo do MVP, não de descarte de código.

**Acceptance criteria.**
1. `/feed` e `/chat` não acessíveis pela navegação.
2. `AppSidebar.tsx` sem os itens correspondentes.
3. Nenhuma tela alcançável a partir da home exibe dado fabricado.

---

### SPACEBIO-028 — Knowledge Explorer · **P1**

**Context.** O grafo tem 982 entidades, 60.552 menções e 4 relações tipadas. É o que separa o projeto de um chatbot.

**Problem.** Hoje o grafo só é visível por Cypher.

**Goal.** Visualização interativa a partir de uma entidade da resposta.

**Out of scope.** Edição do grafo. Visualizar o corpus inteiro de uma vez — 45.947 chunks travam qualquer canvas.

**Files.** `src/pages/KnowledgeExplorer.tsx` (novo), `src/components/GraphCanvas.tsx` (novo), endpoint `GET /api/v1/graph` (novo, em `main.py`).

**Constraints.**
- **Decisão técnica pendente:** `cytoscape.js` ou `react-force-graph`. Nenhuma está instalada. Critério: peso do bundle e suporte a rótulos legíveis.
- O backend deve devolver vizinhança **limitada** (1–2 saltos, teto de nós), nunca o grafo inteiro.
- Query parametrizada, como todo o resto (§8.1).
- Entidades com link externo mostram NCBI/UniProt.

**Acceptance criteria.**
1. Clicar em `Mus musculus` numa resposta abre o explorador centrado nessa entidade.
2. O grafo mostra tipo por cor e permite expandir um nó.
3. Clicar numa publicação leva às suas evidências.
4. Uma entidade linkada exibe o identificador externo com link.
5. A vizinhança carrega em menos de 2 s para as entidades mais conectadas (`Spaceflight`, com 26.684 menções, é o pior caso).

---

### SPACEBIO-026 — Scientific Search · **P2**

**Goal.** Expor a busca híbrida crua, sem LLM no caminho.

**Context que a justifica.** `CDKN1a/p21` existe em 18 chunks e a busca semântica pura o encontra em **0 de 10** tentativas; a híbrida recupera. Demonstrar isso leva 5 segundos e prova o valor dos três canais.

**Files.** `src/pages/Search.tsx` (novo), endpoint `POST /api/v1/search` (novo).

**Acceptance criteria.**
1. Busca por `CDKN1a/p21` retorna trechos contendo o termo.
2. Cada resultado indica os canais que o recuperaram.
3. Filtro por publicação funciona (usa a busca exata restrita, não o índice ANN).
4. Sem chamada ao LLM — a busca não consome quota.

---

### SPACEBIO-030 — Auth Route Cleanup · **P2, paralelo**

**Goal.** Conectar `ProtectedRoute` e `Dashboard`, hoje órfãos.

**Acceptance criteria.**
1. `Dashboard` acessível por rota.
2. Rotas privadas redirecionam para `/auth` quando não autenticado.
3. A Dra. Aris permanece acessível conforme a política de acesso definida.

---

### SPACEBIO-027 — Publication Explorer · **P3**

**Goal.** Página de publicação com metadados e chunks em ordem.

**Context.** `repository.chunks_for_publication()` já existe e devolve na ordem do documento.

**Acceptance criteria.**
1. Mostra título, journal, DOI, URL e nº de chunks.
2. Lista as entidades que a publicação estuda (`STUDIES`, `EXPOSED_TO`, `INVESTIGATES`, `USES_DATASET`).
3. Permite ler as passagens em sequência.

---

### SPACEBIO-029 — Researcher Explorer · **CANCELADA**

Ver §2.1. Reabrir exigiria uma issue de ingestão para extrair autores do cabeçalho do PMC antes da regra R3 — trabalho de pipeline, não de interface.

---

## 6. Sequência recomendada

| Ordem | Issue | Por quê nesta posição |
|---|---|---|
| 1 | **024** | Desbloqueia tudo; o backend já está pronto, é só consumir |
| 2 | **025** | Transforma a tela em demonstração de procedência |
| 3 | **015.1** | Precisa estar pronto antes dos ensaios, não na véspera |
| 4 | **031** | Barato e elimina risco de credibilidade |
| 5 | **028** | O diferencial visual; depende de decisão de biblioteca |
| 6 | **026** | Alto impacto por baixo custo, reusa componentes da 025 |
| 7 | **030** | Higiene |
| 8 | **027** | Se houver tempo |

**Se o tempo acabar após a 025**, o produto já é demonstrável e defensável: uma assistente que cita evidência verificada e recusa responder sem ela. As issues 4 a 8 ampliam, não sustentam.

---

## 7. Riscos

| Risco | Probabilidade | Mitigação |
|---|---|---|
| Quota do Gemini acabar na apresentação | **Alta** | SPACEBIO-015.1, concluída antes dos ensaios |
| Knowledge Explorer consumir todo o tempo | Média | É P1, não P0; a 025 já basta para demonstrar |
| Nó muito conectado travar o canvas | Média | Teto de nós no backend; `Spaceflight` é o caso de teste |
| Neo4j indisponível na hora | Baixa | Já aconteceu uma vez nesta sessão; ter o banco subido e verificado antes |
| Demonstrar com dado fabricado | Baixa | SPACEBIO-031 remove as telas mock |

---

## 8. Definition of Done da Fase 3

Um avaliador que nunca viu o projeto deve, em menos de um minuto e sem explicação verbal:

1. fazer uma pergunta científica e receber resposta com fontes;
2. ver o **trecho literal** que sustenta cada afirmação;
3. identificar o DOI e chegar ao artigo original;
4. ver a assistente **recusar** uma pergunta fora do corpus — e entender que isso é rigor, não defeito.

Os itens 1 a 3 são entregues pelas issues 024 e 025. O item 4 já funciona no backend e depende apenas de ser exibido com o destaque adequado.
