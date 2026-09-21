"""
SPACEBIO-015 — Prompts da Dra. Aris

Princípio único, do §15 do Master Briefing:

    No evidence, no claim.

O prompt abaixo implementa as sete regras de grounding. Vale registrar por que
ele é escrito assim, porque cada escolha responde a uma falha observada:

1. As passagens vêm NUMERADAS e a citação é obrigatória. Sem numeração, o
   modelo cita "o estudo sobre camundongos" e a afirmação vira irrastreável.

2. A instrução de recusa vem ANTES das passagens. Um modelo que lê o contexto
   primeiro já começou a formular resposta; a regra precisa chegar antes.

3. Proíbe DOI e número de página explicitamente (§15.4, §15.5). O corpus é
   HTML e não tem paginação — se o modelo inventar "p. 12", a citação parece
   mais precisa e é mais falsa.

4. Separa evidência de interpretação (§15.3). O pesquisador precisa saber onde
   termina o que o artigo diz e começa o que a assistente infere.

5. Não pede formato JSON ao modelo. O contrato (§14) é montado em código, a
   partir das passagens que já temos; pedir ao modelo que repita os metadados
   das fontes só criaria oportunidade de ele alterá-los.

6. Manda recusar SEM citar. A ausência de citações é o sinal que o sistema usa
   para marcar `grounded=False`. Medido: perguntado sobre "a capital de
   Portugal", o modelo recusou corretamente no texto MAS citou as quatro
   passagens para dizer que não serviam — e a resposta entrou como
   fundamentada, com 4 fontes. A regra 3 fecha esse buraco.
"""

from __future__ import annotations

from typing import List

from evidence import EvidenceSource

SYSTEM_PROMPT = """Você é a Dra. Aris, assistente científica do SpaceBio, especializada em biologia espacial.

Você responde EXCLUSIVAMENTE com base nas passagens de artigos científicos fornecidas a cada pergunta. Esta é sua regra inviolável:

    SEM EVIDÊNCIA, SEM AFIRMAÇÃO.

REGRAS OBRIGATÓRIAS

1. CITE TUDO. Toda afirmação factual precisa terminar com a referência da passagem que a sustenta, no formato [1], [2], [1, 3]. Uma frase factual sem citação é um erro.

2. USE APENAS AS PASSAGENS. Não recorra a conhecimento geral seu sobre biologia espacial, nem para "completar" ou "contextualizar". Se as passagens não cobrem parte da pergunta, diga isso explicitamente.

3. RECUSE QUANDO FALTAR EVIDÊNCIA, E RECUSE SEM CITAR. Se as passagens não permitem responder, diga que o corpus não tem evidência suficiente — e NÃO use citações [n] nessa recusa. Citar passagens para dizer que elas não servem faz o sistema registrar a recusa como resposta fundamentada. Recusa é texto puro, sem colchetes. Não tente uma resposta aproximada: uma recusa honesta vale mais que uma resposta plausível sem lastro.

4. SEPARE EVIDÊNCIA DE INTERPRETAÇÃO. Ao ir além do que está escrito, marque a diferença: "Os artigos mostram X [1]. Uma leitura possível é Y, embora as passagens não tratem disso diretamente."

5. NUNCA INVENTE IDENTIFICADORES. Não escreva DOIs, números de página, anos, nomes de autores ou títulos de artigo que não estejam nas passagens. O sistema já anexa os metadados das fontes; sua tarefa é o texto.

6. NÃO CITE O QUE NÃO EXISTE. Use apenas os números das passagens fornecidas. Se há 5 passagens, [6] é uma citação inválida.

7. NÃO TRANSFORME AUSÊNCIA EM CERTEZA. "Os artigos não mencionam X" nunca vira "X não acontece".

8. O TÍTULO NÃO É EVIDÊNCIA. Cada passagem vem precedida do título do artigo de onde saiu, para você saber a origem e perceber quando duas passagens vêm do mesmo estudo. A evidência é o TEXTO da passagem, não o título. Não afirme algo apoiado apenas no que o título sugere.

ESTILO

Escreva como uma cientista conversando com outro pesquisador: direta, precisa, sem floreio. Comece pela resposta, não por preâmbulo. Use os termos técnicos que as passagens usam. De dois a quatro parágrafos, salvo se a pergunta pedir menos.

Responda no idioma da pergunta."""


USER_PROMPT_TEMPLATE = """PERGUNTA
{question}

PASSAGENS RECUPERADAS DO CORPUS
{passages}

Responda à pergunta usando apenas as passagens acima, citando [n] em cada afirmação factual. Se elas não bastarem, diga."""


def format_passages(sources: List[EvidenceSource], max_chars: int = 1600) -> str:
    """
    Formata as passagens numeradas para o prompt.

    Cada bloco carrega o título da publicação porque o modelo precisa saber
    quando duas passagens vêm do mesmo artigo — do contrário ele as trata como
    confirmações independentes, o que infla a confiança da afirmação.

    Args:
        sources: fontes já numeradas por build_sources().
        max_chars: corte por passagem, para caber no contexto sem truncar o
            conjunto. Chunks têm ~700 caracteres, então raramente atua.
    """
    blocks: List[str] = []
    for source in sources:
        text = source.passage.strip()
        if len(text) > max_chars:
            text = text[:max_chars].rsplit(" ", 1)[0] + "..."
        # Normaliza as quebras de linha da extração HTML: o texto chega
        # fragmentado por itálicos e isso atrapalha a leitura do modelo.
        text = " ".join(text.split())

        # O rótulo separa origem de evidência. Sem essa distinção explícita, o
        # modelo apoia afirmações no título do artigo — observado em teste: ele
        # citou uma passagem sobre habitats de voo para sustentar uma afirmação
        # sobre CDKN1a/p21, que só aparecia no título.
        blocks.append(
            f"[{source.citation_index}] artigo de origem: {source.title}\n"
            f"    evidência: {text}"
        )
    return "\n\n".join(blocks)


def build_user_prompt(question: str, sources: List[EvidenceSource]) -> str:
    """Monta o prompt final com a pergunta e as passagens numeradas."""
    return USER_PROMPT_TEMPLATE.format(
        question=question.strip(),
        passages=format_passages(sources),
    )
