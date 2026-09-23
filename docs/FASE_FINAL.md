# SpaceBio — Fase Final: fechamento do desafio e demonstração pública

**Para:** Claude Code / engenheiro responsável
**Repositórios:** `spacebio-knowledge-engine` (backend) · `spacebio-frontend` (frontend)
**Base:** branch `fase-3`, com a Etapa 3 concluída (E3-01 a E3-07)
**Local deste arquivo:** `docs/FASE_FINAL.md` no repositório do backend

---

## 0. COMO USAR ESTE ARQUIVO

Implemente **uma issue por vez, na ordem**, cada uma em branch própria (`final/<id>-descricao`)
com PR para `main`. Ao terminar cada issue: pare, reporte o que mudou, os testes adicionados e o
resultado da suíte.

Prompt de abertura sugerido:

> Leia CLAUDE.md e docs/FASE_FINAL.md. Implemente somente a issue F0-1. Não altere nada fora do
> escopo dela. Ao terminar, pare e me reporte.

### 0.1 Objetivo desta fase

Três entregas:

1. **Fechar o enunciado do desafio** naquilo que ainda falta: painel dinâmico, resumo da
   coleção, lacunas de conhecimento, distinção entre resultados e conclusões, ligação com os
   dados do OSDR.
2. **Publicar uma demonstração pública à prova de falha**, que qualquer pessoa abre no celular a
   partir de um link, sem backend, sem custo de LLM e sem risco de cair.
3. **Deixar os repositórios apresentáveis** para desenvolvedores que chegarem pelo lançamento.

### 0.2 O que o enunciado pedia e o que esta fase entrega

Trechos do enunciado oficial do NASA Space Apps Challenge 2025:

| Enunciado | Issue |
|---|---|
| "construir um **painel dinâmico**" / "busca e análise interativa dessa coleção" | A2, B3 |
| "**resumir** as publicações" | A2 (em números), **A7** (em conteúdo) |
| "explorem os **impactos e resultados** dos experimentos" | A4, **A7** |
| "identificar áreas de **progresso científico**" | A2 (linha do tempo — mede volume, não progresso) |
| "**lacunas de conhecimento**" | A3 |
| "informações práticas para os **planejadores da missão**" | **A8** |
| Públicos: cientistas · gestores · arquitetos de missão | Chat e A7 · A2 e A3 · A8 |
| "seções de Resultados podem fornecer informações demonstradas objetivamente, enquanto as seções de Conclusão podem ter uma abordagem mais voltada para o futuro" | A4 |
| "O Repositório de Dados Científicos Abertos da NASA (OSDR) contém os dados primários" | A5 |
| "áreas de **consenso ou desacordo**" | **A9**, versão limitada por amostra (opcional) |
| "representações visuais, de áudio ou outras" | Visuais: A2, A3. Áudio: **A10** |
| NASA Task Book | Segunda leva — `docs/SEGUNDA_LEVA_TASKBOOK.md` |
| NSLSL | Não atendido — opcional no enunciado; ver seção 7 |

**Sobre a cobertura:** o acervo tem **493 das 608** publicações do conjunto oficial. Esta fase
não amplia a cobertura. Em **toda** a interface, README e material público, o número é 493,
descrito como "493 das 608 publicações do conjunto do desafio". Nunca 608.

### 0.3 Modelo real do grafo (referência para todo o Cypher deste arquivo)

```text
(:Publication {title, doi, journal, authors, publication_year, crossref_status})
   -[:HAS_CHUNK]->
(:Chunk {id, text, section, embedding, ...})
   -[:MENTIONS {count}]->
(:Entity {key, name, ...})   com rótulo de tipo:
   Organism · Gene · ExperimentalCondition · BiologicalProcess · Tissue ·
   CellType · Mission · Spacecraft · Dataset

Atalhos derivados: (:Publication)-[:STUDIES]->(:Organism) e similares
```

Antes de escrever qualquer consulta, confirme em `graph_manager.py` e `ontology.py` os nomes
exatos de propriedades e rótulos. Onde este documento divergir do código, **o código vale**, e
esta especificação deve ser corrigida no mesmo PR.

### 0.4 Princípios que valem para toda issue

- **O LLM interpreta texto; o código afirma números.** Contagens, gráficos, lacunas e
  referências são calculados por código a partir do grafo. Nenhum número exibido ao usuário é
  gerado por LLM ou escrito à mão no código.
- **Sem evidência, sem afirmação** — vale também para a interface e o README.
- **Cálculo pesado é offline.** Panorama e lacunas são gerados por script e exportados em JSON.
  A interface só lê.
- **Um único frontend.** A demonstração estática não é um projeto separado (ver B1).

### 0.5 Ordem e estimativa (uma pessoa)

| Etapa | Issues | Estimativa |
|---|---|---|
| F0 — Higiene do repositório | F0-1 a F0-3 | 1 dia |
| A — Fechar o enunciado | A1 a A10 | 12–15 dias |
| B — Demonstração estática | B1 a B11 | 7–9 dias |
| C — Material de apresentação | C1 a C4 | 2–3 dias |

Total: **4 a 5 semanas**. Caminho mínimo para publicar (seção 6): cerca de 2 semanas e meia.

---

## ETAPA F0 — HIGIENE DO REPOSITÓRIO

Os repositórios são **públicos**. Esta etapa vem antes de qualquer outra coisa porque o
lançamento vai trazer gente olhando o histórico.

### F0-1 — Limpeza de arquivos rastreados indevidamente

- `__pycache__/main.cpython-313.pyc` continua rastreado na `fase-3`, apesar do `.gitignore`.
  Remover do índice: `git rm -r --cached __pycache__`.
- Procurar outros artefatos rastreados que o `.gitignore` já cobre:
  `git ls-files -i -c --exclude-standard` e remover do índice o que aparecer.
- Confirmar que o `.gitignore` do backend contém explicitamente:
  `CLAUDE.md`, `*.bundle`, `PR-*.md`, `docs/private/`, `.env`, `venv/`, `__pycache__/`,
  `.pytest_cache/`.

**Aceite:** `git ls-files | grep -E "pycache|\.pyc$|\.bundle$|^CLAUDE\.md$|\.env$"` não retorna nada.

### F0-2 — Varredura de conteúdo antes do lançamento

Procurar, em todos os arquivos rastreados **e no histórico** dos dois repositórios, por:

- credenciais: `password`, `secret`, `api_key`, `AIza`, `eyJ`, `sk-`, `NEO4J_PASSWORD=` com valor;
- afirmações sem lastro na interface: `500`, `1,2 mil`, `1.2k`, `imunidade`, `zero alucina`,
  `100%`;
- caminhos pessoais da máquina local (`C:\Users\`).

Comandos:

```bash
git grep -nE "AIza|eyJ|sk-[A-Za-z0-9]" $(git rev-list --all)
git grep -nE "C:\\\\Users" $(git rev-list --all)
```

Reportar o que for encontrado **antes** de alterar qualquer coisa. Se houver credencial no
histórico: não reescrever histórico por conta própria; parar e reportar, porque a correção é
rotacionar a chave, e a reescrita de histórico é decisão do mantenedor.

**Aceite:** relatório da varredura anexado ao PR.

### F0-3 — Merge da `fase-3` no `main`

Após F0-1 e F0-2, fazer merge dos PRs de `fase-3` nos dois repositórios. A partir daqui,
`main` é a base de todas as branches desta fase.

**Aceite:** `main` dos dois repositórios contém a Etapa 3; suíte verde em `main`.

---

## ETAPA A — FECHAR O ENUNCIADO

### A1 — Página inicial honesta e reposicionada

A página inicial atual (`Hero.tsx`, `Features.tsx`) ainda exibe "Mais de 500 Pesquisadores",
"1,2 mil Artigos Publicados", "24 horas por dia, 7 dias por semana" e o título "Rede Social para
Pesquisadores Espaciais". Nenhum desses números tem lastro, e o posicionamento como rede social
não corresponde ao que o produto faz.

**Backend — `GET /api/v1/stats`:**

```cypher
MATCH (p:Publication) WITH count(p) AS publications
MATCH (c:Chunk)       WITH publications, count(c) AS chunks
MATCH (e:Entity)      WITH publications, chunks, count(e) AS entities
MATCH (o:Organism)    WITH publications, chunks, entities, count(o) AS organisms
MATCH (d:Dataset)
RETURN publications, chunks, entities, organisms, count(d) AS datasets
```

Resposta com `generated_at`. Em cache de 10 minutos. O mesmo conteúdo é exportado para a
demonstração estática em B2.

**Frontend:**

- Título: **"O mapa da evidência em biologia espacial"**.
- Subtítulo: "Pergunte, e veja exatamente quais artigos da NASA sustentam cada resposta — ou
  descubra que nenhum sustenta."
- Três números, vindos de `stats`: **publicações do conjunto da NASA**, **trechos auditáveis**,
  **entidades biológicas vinculadas**. Abaixo, em texto menor: "493 das 608 publicações do
  conjunto do NASA Space Apps Challenge 2025".
- Remover o bloco "24 horas por dia" e qualquer contagem de usuários.
- `Features.tsx`: substituir "Conecte-se com Pesquisadores" e similares por quatro cartões:
  **Respostas com evidência**, **Recusa quando não há lastro**, **Panorama do acervo**,
  **Lacunas de conhecimento**.
- Botões: "Perguntar à Dra. Aris" e "Ver o panorama do acervo".
- Selo no topo: "NASA Space Apps Challenge 2025 · Build a Space Biology Knowledge Engine".
- Revisar textos traduzidos literalmente na navegação ("Alimentar", "Assistente de IA Testar",
  "configurações" em minúscula). Centralizar strings em `src/i18n/pt-BR.ts`.

**Aceite:** `grep -rnE "500|1,2 mil|24 horas|Rede Social" src/` não retorna texto de interface;
os números da página batem com uma consulta manual no Neo4j Browser.

### A2 — Panorama do acervo

A tela que responde ao "painel dinâmico" e ao "resumir as publicações" do enunciado. É a maior
lacuna entre o projeto e o que o desafio pediu.

**Backend — `scripts/export_overview.py`** gera `data/exports/overview.json`:

1. **Distribuição por tipo** — para cada rótulo de entidade, as 20 entidades mencionadas no
   **maior número de publicações distintas** (não de menções):

```cypher
MATCH (p:Publication)-[:HAS_CHUNK]->(:Chunk)-[:MENTIONS]->(e:Organism)
RETURN e.key AS key, e.name AS name, count(DISTINCT p) AS publications
ORDER BY publications DESC LIMIT 20
```

   Repetir para `ExperimentalCondition`, `Tissue`, `BiologicalProcess`, `Gene`, `Mission`.

2. **Linha do tempo** — publicações por ano, a partir de `p.publication_year`. Registrar à parte
   quantas publicações não têm ano (`crossref_status` sem dado). **Não imputar ano.**

3. **Linha do tempo por tema** — para as 8 condições experimentais mais frequentes, publicações
   por ano. É o que atende "áreas de progresso científico".

4. **Matriz organismo × condição** — as 10 principais de cada eixo, com contagem de publicações
   que mencionam os dois. Usada também em A3.

5. Metadados do arquivo: `generated_at`, total de publicações, versão do script.

Contar sempre **publicações distintas**. Uma publicação com quarenta menções a "microgravity"
conta uma vez.

**Endpoint:** `GET /api/v1/overview` serve o JSON exportado. Não recalcula a cada requisição.

**Frontend — rota `/panorama`, componente `Overview.tsx`:**

- Cabeçalho com os números de `stats` e a frase: "Um resumo das 493 publicações do acervo".
- **Gráficos de barra** das entidades principais, com abas por tipo (Organismos, Condições,
  Tecidos, Processos, Genes, Missões).
- **Linha do tempo** de publicações por ano, com nota "N publicações sem ano identificado".
- **Linha do tempo por tema**, com seletor de condições.
- **Clique em qualquer barra** leva à busca filtrada por aquela entidade: abre a Dra. Aris com a
  pergunta pré-preenchida "O que o acervo diz sobre <entidade>?" (no modo estático, leva ao
  catálogo filtrado — ver B3).
- Biblioteca de gráficos: `recharts`. Todos os gráficos com rótulo de eixo, número visível e
  alternativa em tabela para acessibilidade.
- **Responsivo a partir de 360 px de largura.** Em tela estreita, barras horizontais.

**Aceite:** os números de três barras escolhidas à mão batem com consulta manual; tela
utilizável em 375 px; teste Playwright abre a tela e troca de aba.

### A3 — Mapa de lacunas

Atende "lacunas de conhecimento". Não depende de nenhuma camada nova: usa as menções que já
existem no grafo.

**Conceito.** Uma lacuna interessante não é qualquer combinação ausente — a maioria é ausente
porque não faz sentido. É quando **duas coisas muito estudadas separadamente quase nunca aparecem
juntas**.

```text
N         = total de publicações
n_a       = publicações que mencionam A
n_b       = publicações que mencionam B
observado = publicações que mencionam A e B
esperado  = n_a × n_b / N
```

- **Lacuna:** `observado = 0` e `esperado ≥ 3`.
- **Sub-representado:** `observado ≥ 1` e `observado / esperado < 0,25`.
- Excluir entidades presentes em mais de 40% das publicações (genéricas demais).

**Backend — `scripts/export_gaps.py`** gera `data/exports/gaps.json` para três pares de eixos:
organismo × condição, condição × tecido, condição × processo biológico. Para cada eixo, as 15
entidades com mais publicações; para cada célula: `n_a`, `n_b`, `observed`, `expected`, `kind`
(`studied` | `underrepresented` | `gap`).

Consulta de coocorrência por publicação:

```cypher
MATCH (p:Publication)-[:HAS_CHUNK]->(:Chunk)-[:MENTIONS]->(a:Organism)
WHERE a.key IN $axis_a
WITH p, collect(DISTINCT a.key) AS as_
MATCH (p)-[:HAS_CHUNK]->(:Chunk)-[:MENTIONS]->(b:ExperimentalCondition)
WHERE b.key IN $axis_b
WITH p, as_, collect(DISTINCT b.key) AS bs
UNWIND as_ AS ak UNWIND bs AS bk
RETURN ak, bk, count(DISTINCT p) AS observed
```

**Endpoint:** `GET /api/v1/gaps?axes=organism-condition`.

**Frontend — rota `/lacunas`, componente `GapExplorer.tsx`:**

- Seletor dos três pares de eixos.
- **Mapa de calor**: intensidade = publicações; células de lacuna com borda tracejada e ícone;
  sub-representadas com borda pontilhada.
- Clique em célula estudada → lista das publicações.
- Clique em lacuna → painel lateral com texto gerado **por modelo de frase fixo**, sem LLM:
  > "*Radiação* aparece em 80 publicações e *tecido muscular* em 60. Nenhuma publicação do
  > acervo estuda os dois juntos — seriam esperadas cerca de 10."
- **Aviso fixo e obrigatório no topo:**
  > "Lacuna, aqui, significa ausência **neste acervo de 493 publicações da NASA**. O tema pode ter
  > sido estudado em outras bases."
- Em tela estreita: lista ordenada de lacunas no lugar do mapa de calor.

**Aceite:** 10 células conferidas à mão contra o Neo4j; teste unitário do cálculo de `expected`
e `kind` com dados sintéticos; o aviso aparece em todas as larguras de tela.

### A4 — Distinção entre resultados e conclusões

Atende diretamente a sugestão do enunciado sobre seções do texto. `Chunk.section` já existe.

**Backend:**
- Mapear os valores de `section` existentes para três categorias, em `evidence.py`:

| Categoria | Seções |
|---|---|
| `demonstrated` — "O que foi demonstrado" | Results, Findings |
| `interpreted` — "O que os autores interpretam ou propõem" | Discussion, Conclusion(s) |
| `context` — "Contexto" | Abstract, Introduction, Background, Methods, outras |

- Listar os valores distintos reais de `section` no grafo antes de fixar o mapeamento; seções não
  previstas vão para `context` e são registradas em log.
- Incluir `section_category` em cada fonte do contrato de evidência.

**Frontend (`EvidencePanel`, `SourceCard`):**
- Etiqueta por fonte: **Demonstrado**, **Interpretação dos autores** ou **Contexto**, com cores
  distintas e tooltip explicando a diferença.
- Filtro no painel: Todos · Demonstrado · Interpretação.
- Ordenação padrão: `demonstrated` antes de `interpreted` antes de `context`, mantendo a ordem
  de relevância dentro de cada grupo.

**Aceite:** teste com resposta que mistura seções confirma a etiqueta e a ordenação; o
mapeamento está documentado em `docs/RETRIEVAL_AUDIT.md`.

### A5 — Ligação com os dados do OSDR

Entidades `Dataset` (`GLDS-nnn`, `OSD-nnn`) já são extraídas (`ontology.py`, `ner.py`). Falta
torná-las úteis ao usuário.

- **Verificar o formato atual da URL pública** de um estudo no OSDR, conferindo em pelo menos três
  estudos reais. Não inventar o padrão.
- Verificar se um identificador `GLDS-n` corresponde ao `OSD-n` de mesmo número; se não houver
  garantia, gerar link somente para `OSD-` e, para `GLDS-`, link para a busca do OSDR.
- Função `dataset_url(accession) -> str | None` em `evidence.py`, testada.
- Frontend: componente `DatasetBadge.tsx` no `SourceCard` e na lista de publicações:
  "Dados do experimento: OSD-48 ↗".
- No panorama (A2): contador "N publicações com dados abertos no OSDR".

**Aceite:** 10 links conferidos manualmente abrindo a página certa.

### A6 — Navegação focada no produto

As telas de rede social (Feed, Chat, Profile, e o fluxo de login) não fazem parte da demonstração
e hoje sugerem funcionalidades que não existem.

- Criar flag `VITE_FEATURE_SOCIAL` (padrão `false`). Com a flag desligada, essas rotas e itens de
  menu não são registrados. **Não apagar o código.**
- Nova navegação: **Início · Perguntar à Dra. Aris · Temas · Panorama · Lacunas · Missão · Acervo · Como medimos · Sobre**. Em telas estreitas, agrupar em menu.
- Com a flag desligada, o cliente Supabase **não é inicializado** e nenhuma variável do Supabase é
  necessária no build.

**Aceite:** build com a flag desligada não contém a string da URL do Supabase no bundle
(`grep` no `dist/`).

### A7 — Sínteses temáticas

Atende "resumir as publicações" e "explorar os impactos e resultados dos experimentos". O
panorama (A2) resume o acervo em **números**; esta issue resume em **conteúdo**: o que o acervo
diz sobre cada grande tema, com evidência.

**Reaproveita a máquina que já existe.** O cache da demo (E3-05) já executa o pipeline completo
offline, valida as citações e congela o resultado. Aqui, o mesmo processo roda sobre **temas** em
vez de perguntas soltas.

**Backend — `scripts/generate_syntheses.py`:**

1. Arquivo de entrada versionado, `data/themes.json`, com **12 a 15 temas**, cada um com
   identificador, título, entidades-chave e a pergunta usada no pipeline. Sugestão de temas,
   confirmando antes que cada um tem volume no acervo (A2):
   perda óssea · atrofia muscular · sistema imunológico · sistema cardiovascular ·
   danos da radiação ao DNA · expressão gênica em voo · crescimento de plantas · microbiologia
   e biofilmes · sistema nervoso e comportamento · visão e olhos · pele e cicatrização ·
   reprodução e desenvolvimento.
2. Para cada tema, executar o **pipeline real** (recuperação + síntese), com número maior de
   fontes do que o chat (ex.: 12).
3. Prompt de síntese temática em `prompts.py`, com estrutura fixa:
   - **O que foi observado** — somente a partir de fontes `demonstrated` (A4);
   - **Como os autores interpretam** — a partir de fontes `interpreted`;
   - **Organismos e condições estudados** — lista, gerada **por código** a partir das entidades
     das fontes, não pelo LLM.
4. Validar todas as citações com o validador da E3-02. **Qualquer falha descarta a síntese
   daquele tema**; o tema não é publicado até ser regenerado com sucesso.
5. Se o pipeline recusar o tema por falta de evidência, registrar e **não** publicar o tema. Não
   forçar resposta.
6. Saída: `data/exports/syntheses.json`, com tema, texto, fontes, entidades, contagem de
   publicações (por código), data de geração, hash do prompt.

**Frontend — rota `/temas` e `/temas/:id`:**
- Índice com cartões por tema: título, número de publicações (do grafo), organismos principais.
- Página do tema: as seções da síntese; o `EvidencePanel` com todas as fontes; botão "Ver no
  panorama" e "Ver lacunas deste tema"; botão "Salvar" (B5).
- Rótulo fixo: "Síntese gerada pelo sistema a partir de N publicações do acervo, com citações
  verificadas. Gerada em <data>."

**Aceite:** todas as sínteses publicadas passam no validador de citações; toda contagem exibida
bate com o grafo; teste Playwright abre um tema e expande uma fonte.

### A8 — Visão para planejadores de missão

Atende "fornecer informações práticas para os planejadores da missão" e o terceiro público do
enunciado: "arquitetos de missões que buscam explorar a Lua e Marte de forma segura e eficiente".

Duas visões, ambas construídas sobre dados que já existem.

**1. Riscos para a exploração** — rota `/missao/riscos`.

Organiza as sínteses temáticas (A7) pelas perguntas que um planejador faz. Mapeamento em
`data/mission_risks.json`, versionado:

| Risco para a missão | Temas de A7 |
|---|---|
| Saúde musculoesquelética em missões longas | perda óssea, atrofia muscular |
| Exposição à radiação fora da órbita baixa | danos da radiação ao DNA |
| Resposta imune e infecção | sistema imunológico, microbiologia e biofilmes |
| Saúde cardiovascular | sistema cardiovascular |
| Produção de alimento e suporte à vida | crescimento de plantas |
| Desempenho cognitivo e comportamental | sistema nervoso e comportamento |

Para cada risco: resumo curto (primeira seção da síntese correspondente), número de publicações,
proporção de estudos em voo real frente a outros contextos **quando essa informação estiver
disponível nas entidades** (`Mission`, `Spacecraft`), e links para as sínteses e para as lacunas
relacionadas.

Os nomes dos riscos são **escolha editorial do projeto** para organizar o acervo. A página deve
dizer isso, e não apresentá-los como a classificação oficial de riscos da NASA.

**2. Missões e plataformas** — rota `/missao/plataformas`.

As entidades `Mission` e `Spacecraft` já existem no grafo. Para cada uma (com pelo menos 3
publicações): número de publicações, organismos estudados, condições estudadas, lista de
publicações. Tudo por consulta ao grafo:

```cypher
MATCH (p:Publication)-[:HAS_CHUNK]->(:Chunk)-[:MENTIONS]->(m:Mission)
WITH m, collect(DISTINCT p) AS pubs WHERE size(pubs) >= 3
UNWIND pubs AS p
OPTIONAL MATCH (p)-[:HAS_CHUNK]->(:Chunk)-[:MENTIONS]->(o:Organism)
RETURN m.name AS mission, size(pubs) AS publications,
       collect(DISTINCT o.name)[..10] AS organisms
ORDER BY publications DESC
```

Exportado para `data/exports/missions.json` em B2.

**Aceite:** contagens conferidas contra o grafo; a página de riscos traz a nota sobre a
classificação ser do projeto; teste Playwright navega de um risco até uma fonte.

### A9 — Evidência convergente e divergente, por amostra (opcional)

Atende, de forma **limitada e declarada**, "áreas de consenso ou desacordo". A versão completa —
classificar a postura de todas as evidências do acervo — exige uma camada de extração que não
cabe nesta fase. Esta é a versão intermediária defensável.

**Escopo:** apenas dentro das sínteses temáticas (A7), e apenas sobre as fontes que a própria
síntese recuperou.

- Durante `generate_syntheses.py`, para cada fonte `demonstrated`, pedir ao LLM, em saída JSON
  validada, se o trecho **reporta efeito**, **não reporta efeito** ou **é inconclusivo** em
  relação ao tema, com a frase literal que sustenta a classificação.
- A frase literal passa pelo validador da E3-02. Classificação sem frase válida é descartada.
- Na página do tema, bloco **"Nesta amostra de N estudos"**: quantos reportam efeito, quantos não
  reportam, quantos são inconclusivos — e a lista de cada grupo.

**Regras de apresentação, obrigatórias:**
- Título do bloco sempre com "nesta amostra".
- Texto fixo: "Classificação automática das fontes usadas nesta síntese, não de todo o acervo.
  Serve para indicar se há divergência, não para medir consenso."
- **Nunca** usar as palavras "consenso" ou "comprovado" nesta visão.
- Se a amostra tiver menos de 5 fontes `demonstrated`, o bloco não aparece.

**Aceite:** revisar manualmente 30 classificações e registrar a taxa de acerto em
`docs/RETRIEVAL_AUDIT.md`. **Se a taxa ficar abaixo de 80%, a issue não vai ao ar** e fica
registrada como tentativa.

### A10 — Ouvir as sínteses (áudio)

Atende a sugestão do enunciado de usar "representações visuais, de áudio ou outras", e melhora a
acessibilidade para pessoas com deficiência visual ou que preferem ouvir.

**Abordagem:** síntese de voz do próprio navegador (Web Speech API, `speechSynthesis`). Custo
zero, sem servidor, funciona na demonstração estática.

**O que é lido:**
- nas sínteses temáticas (A7): o título do tema, a seção "O que foi observado" e a seção "Como os
  autores interpretam";
- nas respostas da Dra. Aris: o texto da resposta.

**O que nunca é lido:** citações entre aspas, marcadores de referência (`[1]`, `[2]`), DOIs,
URLs, listas de entidades e o painel de evidências. Nomes de genes e trechos em inglês lidos por
uma voz em português ficam incompreensíveis.

**Implementação:**

1. Função pura `toSpeakableText(text: string): string[]` em `src/audio/speakable.ts`:
   - remove todo conteúdo entre aspas (retas e tipográficas);
   - remove marcadores `[n]`, DOIs, URLs e conteúdo entre parênteses que contenha apenas
     identificadores;
   - colapsa espaços e pontuação duplicada;
   - devolve o texto **dividido em frases ou parágrafos curtos**.

   A divisão é obrigatória: alguns navegadores interrompem a leitura de textos longos em uma
   única fala. Uma fala por trecho, enfileiradas, evita o problema.

2. Hook `useSpeech()` em `src/audio/useSpeech.ts`:
   - detecta suporte (`'speechSynthesis' in window`);
   - carrega as vozes de forma assíncrona (a lista pode chegar vazia na primeira chamada; ouvir
     o evento `voiceschanged`);
   - escolhe voz com idioma `pt-BR`, depois qualquer `pt-*`;
   - expõe `play`, `pause`, `resume`, `stop`, `rate`, `currentIndex`;
   - **cancela a fala ao desmontar o componente e ao trocar de rota**.

3. Componente `ListenButton.tsx`:
   - **só aparece se houver suporte e voz em português disponível**;
   - botões ouvir / pausar / parar e velocidade (0,85× · 1× · 1,25×);
   - destaca o parágrafo que está sendo lido (pelo índice do trecho, não pelo evento de limite de
     palavra, que é inconsistente entre navegadores);
   - nunca inicia sozinho — somente por clique (exigência dos navegadores móveis, e boa prática);
   - `aria-label` descritivo e operável por teclado.

4. Aviso discreto junto ao botão: "Voz do seu dispositivo. A qualidade varia conforme o aparelho."

**Fora do escopo:** áudio pré-gerado por serviço de voz (melhor qualidade e uniformidade entre
aparelhos, custo baixo). Pode ser uma evolução posterior, mantendo a mesma regra do que é lido.

**Testes:**
- unitários de `toSpeakableText`: texto com citação entre aspas, com `[1]`, com DOI, com URL,
  texto longo dividido em trechos;
- Playwright com `speechSynthesis` simulado: o botão aparece com voz `pt-BR`, some sem voz em
  português, e a fala é cancelada ao navegar para outra página.

**Aceite:** leitura funciona no Chrome (computador e Android) e no Safari do iPhone; nenhuma
citação ou marcador é lido; trocar de página interrompe a leitura.

---

## ETAPA B — DEMONSTRAÇÃO ESTÁTICA

**Objetivo:** um site que roda **sem backend**, hospedado de graça, carregando em menos de dois
segundos no celular, que não cai, não dorme e não depende de cota de API. As respostas exibidas
são respostas reais do sistema, geradas pelo pipeline completo e congeladas.

### B1 — Camada de acesso a dados com duas implementações

**Não criar um projeto separado para a demo.** Duas versões do frontend divergem em semanas.

```ts
// src/data/source.ts
export interface DataSource {
  ask(question: string): Promise<AskResult>;      // resposta + evidências, ou "not_in_demo"
  stats(): Promise<Stats>;
  overview(): Promise<Overview>;
  gaps(axes: GapAxes): Promise<Gaps>;
  catalog(): Promise<PublicationSummary[]>;
  evaluation(): Promise<EvaluationSummary | null>;
}
```

- `ApiDataSource` — chama o backend.
- `StaticDataSource` — lê JSON de `/data/*.json`.
- Seleção por `VITE_DATA_SOURCE=api|static` no build. **Nenhum componente importa `fetch` ou o
  cliente de API diretamente** — todos passam por `DataSource`.

Tipo de retorno de `ask` no modo estático quando a pergunta não está no roteiro:

```ts
{ status: "not_in_demo", suggestions: string[] }
```

**Aceite:** `grep -rn "fetch(" src/ --include=*.tsx` não retorna nada fora de `src/data/`;
`npm run build` funciona nos dois modos.

### B2 — Exportação dos dados estáticos

`scripts/export_static.py` (backend) gera, em `data/exports/static/`:

| Arquivo | Conteúdo | Tamanho alvo |
|---|---|---|
| `stats.json` | números de A1 | < 1 KB |
| `demo.json` | as perguntas do roteiro com resposta, fontes, `section_category`, datasets, bloco `retrieval` | < 500 KB |
| `overview.json` | A2 | < 200 KB |
| `gaps.json` | A3 | < 200 KB |
| `catalog.json` | por publicação: título, autores, ano, periódico, DOI, datasets, entidades principais, e o **primeiro trecho de seção Abstract** (se houver) | < 1,5 MB |
| `syntheses.json` | sínteses temáticas de A7, com fontes e, se houver, o bloco de amostra de A9 | < 600 KB |
| `missions.json` | riscos e plataformas de A8 | < 100 KB |
| `evaluation.json` | métricas medidas (ver B6) | < 10 KB |
| `manifest.json` | data de geração, commit do backend, hash do prompt, versões | < 1 KB |

Regras:

- `demo.json` é gerado a partir de `demo_cache.json`, que precisa conter **no mínimo 12
  perguntas**, incluindo: as dos chips; **pelo menos uma recusa** (pergunta sem resposta no
  acervo); uma pergunta operacional; e uma cuja resposta tenha fontes `demonstrated` e
  `interpreted` (para exibir A4).
- Antes de exportar, rodar o validador de citações (E3-02) em todas as respostas. Qualquer falha
  **interrompe** a exportação.
- Um script de frontend, `npm run sync-static`, copia os arquivos para `public/data/`.
- O `manifest.json` é exibido na página "Sobre" (data de geração dos dados).

**Aceite:** exportação reproduzível; tamanho total abaixo de 3,5 MB sem compressão.

### B3 — Catálogo pesquisável no navegador

Para o visitante não ficar preso às perguntas do roteiro.

- Rota `/acervo`, componente `Catalog.tsx`.
- Busca **por palavra** sobre título, autores, resumo e entidades, com `minisearch` (leve, sem
  dependências), índice construído no navegador a partir de `catalog.json`.
- Filtros: organismo, condição, ano, "com dados no OSDR".
- Cada item: título, autores, ano, periódico, DOI clicável, badges de datasets, entidades.
- Texto fixo no topo, no modo estático:
  > "Busca por palavras no catálogo. A versão completa usa busca semântica e responde com
  > evidência — veja como rodá-la no repositório."
- Os cliques do panorama (A2) chegam aqui já filtrados.

**Aceite:** busca responde em menos de 100 ms com as 493 publicações; filtros combináveis;
utilizável em 375 px.

### B4 — Perguntas fora do roteiro

No modo estático, ao receber uma pergunta que não está no roteiro, **não simular resposta**.

Mensagem:

> "Esta demonstração responde a um conjunto de perguntas pré-selecionadas, geradas pelo sistema
> real. Para perguntas livres, rode a versão completa a partir do repositório."

Seguida de:
- as perguntas disponíveis, como botões;
- um botão "Procurar no catálogo" que leva a `/acervo` com a busca preenchida com os termos da
  pergunta;
- link para o repositório.

Correspondência da pergunta com o roteiro: normalização de E3-05 (minúsculas, sem acento, sem
pontuação final). Não usar correspondência aproximada — responder uma pergunta parecida como se
fosse a pergunta feita seria uma resposta sem lastro.

O roteador de intenção (regras) é portado para TypeScript, para que perguntas operacionais
("quem é você?", "como te uso?") funcionem no modo estático. Reutilizar os casos de
`tests/fixtures/router_cases.jsonl` como teste do porte.

**Aceite:** teste Playwright com pergunta livre exibe a mensagem e as sugestões; os casos do
roteador passam na versão TypeScript com o mesmo resultado da versão Python.

### B5 — "Minhas evidências" no navegador

Versão individual e sem login das coleções.

- Botão **Salvar** em cada `SourceCard` e em cada resposta.
- Rota `/salvos`: lista de trechos e publicações salvos, com nota opcional por item.
- Armazenamento em `localStorage`, com leitura e escrita protegidas por `try/catch`; a tela
  funciona com armazenamento vazio ou indisponível.
- Exportação ABNT e BibTeX dos salvos, **reutilizando o código da E3-07** (gerado por código, sem
  LLM).
- Aviso discreto: "Salvo apenas neste navegador."

**Aceite:** salvar, recarregar a página e exportar funcionam; com `localStorage` bloqueado a tela
não quebra.

### B6 — Página "Como medimos"

Rota `/como-medimos`. Mostra ao visitante como a qualidade do sistema foi verificada.

Conteúdo, gerado a partir de `evaluation.json`:
- tamanho do conjunto de avaliação e sua composição;
- métricas de recuperação medidas;
- taxa de recusa correta e de recusa indevida;
- resultado do roteador (operacionais roteadas, científicas roteadas por engano);
- como o limiar de recusa foi escolhido;
- regra das citações verificadas por código.

**Regra rígida:** exibir **somente métricas que foram de fato medidas** e registradas pelos
scripts existentes (`calibrate_threshold.py`, testes do roteador, resultados de avaliação em
`docs/`). Métrica não medida não aparece — nem como "em breve". Se o conjunto de avaliação ainda
não existir, a página mostra apenas o que existe.

**Aceite:** cada número da página rastreia até um arquivo de resultado no repositório, citado no
`evaluation.json`.

### B7 — Página "Sobre"

Rota `/sobre`. Conteúdo:
- origem: NASA Space Apps Challenge 2025, desafio "Build a Space Biology Knowledge Engine", e a
  decisão de retomar o projeto depois do evento;
- o que é a demonstração estática e o que é a versão completa, com a data de geração dos dados
  (`manifest.json`);
- a arquitetura em um diagrama simples (Mermaid ou SVG);
- a cobertura: 493 das 608 publicações do conjunto oficial, e por quê;
- links para os dois repositórios;
- créditos às fontes de dados da NASA (lista de publicações, OSDR).

### B8 — Build, deploy e roteamento

- Build com `VITE_DATA_SOURCE=static` e `VITE_FEATURE_SOCIAL=false`.
- Hospedagem estática gratuita (Vercel, Netlify ou GitHub Pages). Configurar **fallback de SPA**
  (toda rota desconhecida serve `index.html`), senão links diretos como `/panorama` dão 404.
- Deploy automático a cada push em `main` via GitHub Actions ou integração da hospedagem, rodando
  lint, testes e Playwright antes.
- Domínio: usar o subdomínio gratuito da hospedagem nesta fase.

**Aceite:** acessar diretamente `/panorama`, `/lacunas`, `/acervo` pelo navegador funciona;
nenhuma chave ou variável sensível no `dist/`.

### B9 — Celular primeiro

A maior parte do tráfego vindo de redes sociais é pelo celular.

- Testar todas as rotas em 360 px, 375 px e 414 px.
- Painel de evidências em gaveta inferior no celular.
- Alvos de toque com no mínimo 44 px.
- Lighthouse no modo celular: Performance ≥ 85, Acessibilidade ≥ 90.

**Aceite:** relatório do Lighthouse anexado ao PR; Playwright roda a suíte também em viewport de
celular.

### B10 — Cartão de compartilhamento

Quando o link é colado numa rede social, a prévia (imagem, título, descrição) decide se alguém
clica.

- Tags `og:title`, `og:description`, `og:image`, `og:url` e `twitter:card` em `index.html`.
- Imagem `og-image.png`, 1200 × 630, com o título do produto, a frase de posicionamento e uma
  captura do painel de evidências.
- Título: "SpaceBio — o mapa da evidência em biologia espacial".
- Descrição: "493 publicações da NASA, respostas com trechos citados e verificados, e recusa
  quando não há evidência."
- `favicon` definido.

**Aceite:** a prévia aparece corretamente num validador de cartão de compartilhamento.

### B11 — Medição de visitas sem cookies (opcional)

Se for medir visitas, usar ferramenta **sem cookies e sem dados pessoais** (ex.: Plausible,
Umami ou a análise nativa da hospedagem), o que dispensa banner de consentimento. Registrar
apenas: páginas vistas, origem da visita, perguntas do roteiro mais clicadas. Nada de gravação de
sessão ou identificação de visitante.

---

## ETAPA C — MATERIAL DE APRESENTAÇÃO

### C1 — README do backend

O README atual é bom. Acrescentar no topo, antes do "Em 30 segundos":
- link para a demonstração e um GIF curto (pergunta → resposta → evidência → clique no DOI);
- três frases: o que é, o que faz de diferente (recusa + citação verificada), e os números;
- seção **"Decisões de engenharia"**, curta, com: por que recusar em vez de responder sempre;
  como o limiar foi calibrado; por que citações são verificadas por código; por que números nunca
  vêm do LLM; os prefixos do e5; o viés de medição do `localhost` no Windows. Esta seção é a que
  gera conversa técnica.
- seção **"Limitações conhecidas"**: 493 de 608; a demonstração é estática; o que ainda não
  existe.

### C2 — README do frontend

Substituir o README gerado pelo Lovable por um README do projeto: o que é, link da demo, como
rodar nos dois modos (`static` e `api`), estrutura de pastas, e link para o backend.

### C3 — Capturas e GIF

Gerar com Playwright, para serem reproduzíveis quando a interface mudar:
- `docs/img/hero.png` — página inicial;
- `docs/img/evidence.png` — resposta com o painel de evidências aberto;
- `docs/img/refusal.png` — a recusa;
- `docs/img/overview.png` — panorama;
- `docs/img/gaps.png` — lacunas;
- `docs/img/theme.png` — um tema;
- `docs/img/mission.png` — riscos para a missão;
- versões em viewport de celular das três primeiras.

Script: `npm run screenshots`.

### C4 — Roteiro de gravação do vídeo

Arquivo `docs/VIDEO.md` com o roteiro de 90 segundos, na ordem:

1. Página inicial (3 s).
2. Clique num chip; a resposta aparece com as fontes (15 s).
3. Abrir um `SourceCard`, mostrar a etiqueta "Demonstrado", clicar no DOI e ver o artigo real
   abrir (15 s).
4. Pergunta sem resposta no acervo; **a recusa** (15 s).
5. Um tema (A7): as seções "observado" e "interpretação", e o botão **Ouvir** por 3 segundos (10 s).
6. Panorama: trocar de aba, clicar numa barra (10 s).
7. Lacunas: clicar numa célula de lacuna e ler a frase (10 s).
8. Tela final com o link (5 s).

O trecho 4 é o mais importante do vídeo e não deve ser cortado.

---

## 6. CAMINHO MÍNIMO PARA PUBLICAR

Se o prazo apertar, este é o menor conjunto que sustenta um lançamento sem constrangimento:

**F0-1, F0-2, F0-3 → A1 → A2 → A4 → A6 → A7 → A10 → B1 → B2 → B4 → B7 → B8 → B9 → B10 → C1 → C3.**

A7 entra no caminho mínimo porque é ela que responde ao "resumir" do enunciado em conteúdo, e usa a máquina que já existe. A10 custa um dia, depende de A7 e aparece bem no vídeo.

Ficam para uma segunda publicação: A3 (lacunas), A5 (OSDR), A8 (missão), A9 (amostra), B3
(catálogo), B5 (salvos), B6 (como medimos), C2, C4. Cada uma delas é assunto para um novo post.

---

## 7. FORA DO ESCOPO DESTA FASE

| Item | Motivo |
|---|---|
| Ampliar o acervo para 608 | Decisão de manter as 493; documentado em toda a interface |
| Consenso sobre todo o acervo | A versão completa exige uma camada de extração de afirmações com verificação. Esta fase entrega apenas a versão por amostra (A9) |
| Perguntas livres na demonstração | Exigiria backend e LLM ao vivo, com custo e risco de indisponibilidade |
| Funcionalidades sociais | Mantidas atrás de flag desligada |
| Task Book | Especificado à parte, em `docs/SEGUNDA_LEVA_TASKBOOK.md`, para depois do lançamento |
| NSLSL | É principalmente um banco de registros bibliográficos; o texto completo da maioria dos artigos não pode ser ingerido. Criaria dois níveis de evidência e exigiria recalibrar a recusa. Só com demanda real |
| Modo Brainstorm | Retirado na Etapa 3; não reintroduzir |

---

## 8. DEFINIÇÃO DE PRONTO

Vale para toda issue desta fase:

- [ ] Branch própria e PR para `main`.
- [ ] Testes para a lógica nova; suíte inteira verde (backend, frontend e Playwright).
- [ ] Nenhum número exibido ao usuário escrito à mão no código.
- [ ] Nenhuma credencial no diff.
- [ ] Funciona em 375 px de largura.
- [ ] Funciona nos dois modos de dados (`api` e `static`), quando aplicável.
- [ ] Documentação atualizada.

## 9. CHECKLIST DE LANÇAMENTO

- [ ] F0-2 sem pendências; histórico limpo de credenciais
- [ ] Demonstração no ar, com todas as rotas acessíveis por link direto
- [ ] Todas as perguntas do roteiro respondem, incluindo a recusa
- [ ] Nenhum "500 pesquisadores", "608" ou "imunidade" em lugar nenhum
- [ ] Testado em celular real, não só no emulador
- [ ] Cartão de compartilhamento conferido
- [ ] READMEs atualizados, com GIF e link da demonstração
- [ ] Vídeo gravado seguindo o roteiro
- [ ] Um amigo abriu o link no celular, sem instrução nenhuma, e conseguiu usar
