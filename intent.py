# -*- coding: utf-8 -*-
"""
Roteador de intenção — perguntas sobre o SISTEMA vs perguntas ao ACERVO (E3-03)

Intercepta perguntas operacionais ("quem é você", "como funciona", "ajuda")
antes do RAG e responde com um guia estático: zero busca vetorial, zero token
de LLM. Tudo o mais segue para o RAG.

POR QUE ISTO EXISTE
-------------------
"Quem é você?" não tem resposta no corpus — são 493 artigos de biologia
espacial e nenhum fala da Dra. Aris. Sem roteamento, a pergunta seguiria o
caminho normal e receberia a recusa por falta de evidência: tecnicamente
correto e péssimo, porque quem tenta entender a ferramenta é recebido com
"não encontrei evidência suficiente", o que parece defeito.

A MÉTRICA É ASSIMÉTRICA, E ISSO GOVERNA TODO O DESENHO
------------------------------------------------------
Os dois erros não custam o mesmo:

    operacional tratada como científica  ->  uma chamada de LLM a mais
    científica tratada como operacional  ->  a evidência do acervo fica
                                             escondida atrás de um guia de
                                             ajuda que ninguém pediu

O segundo é inaceitável. Logo: recall de 100% para intenção científica, e
QUALQUER dúvida roteia para o RAG. Perder uma pergunta operacional de vez em
quando é o preço, e é barato.

POR QUE CASAR A FRASE INTEIRA, E NÃO O INÍCIO
---------------------------------------------
Esta é a correção central da E3-03. A versão anterior exigia que o padrão
casasse o INÍCIO da pergunta. Medido: 8 de 17 perguntas científicas eram
capturadas indevidamente —

    "me ajuda com RUNX2"                   -> guia de uso
    "como funciona a osteogênese"          -> guia de uso
    "quais artigos falam de osteoclastos"  -> descrição do corpus
    "o que você não sabe sobre telômeros"  -> lista de limitações

Todas começam como pergunta operacional e terminam com um assunto científico
grudado. O sinal que as distingue não é vocabulário — é FORMA: uma pergunta
operacional é uma frase fechada e autocontida. No instante em que sobra um
assunto depois dela, a pergunta é sobre o assunto.

Casar a frase inteira resolve os quatro casos acima sem precisar conhecer as
palavras "RUNX2", "osteogênese", "osteoclastos" ou "telômeros". É um guard
estrutural, e por isso não envelhece junto com o corpus.

POR QUE MESMO ASSIM EXISTE UM VOCABULÁRIO
-----------------------------------------
Cinto e suspensório. O guard de forma pega o caso geral; o vocabulário pega a
pergunta curta que POR ACASO tem forma operacional. Ele vem de
`data/domain_vocabulary.json`, gerado do próprio grafo por
`build_domain_vocabulary.py` — entidades extraídas do corpus mais termos em
português, que o grafo não tem porque os artigos são todos em inglês.

Lista escrita à mão foi exatamente o que falhou antes: cada termo faltante era
uma pergunta científica virando guia de ajuda.

POR QUE REGRA E NÃO MODELO
--------------------------
Usar o LLM para classificar gastaria a requisição que se está tentando
economizar. E o conjunto de perguntas operacionais é pequeno e fechado —
exatamente o caso em que regra explícita supera ML: é auditável, roda em
microssegundos e não alucina.
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import List, Optional, Set, Tuple

# Perguntas operacionais são curtas. O limite é a primeira barreira: uma
# pergunta científica longa que por acaso tenha forma operacional não passa.
MAX_META_QUESTION_CHARS = 80

VOCABULARY_PATH = Path(__file__).parent / "data" / "domain_vocabulary.json"

# Identificadores científicos: RUNX2, CDKN1A, p21, OSD-570, GLDS-104, IL6.
# Uma pergunta operacional não contém letra seguida de dígito.
IDENTIFIER_PATTERN = re.compile(r"\b[a-z]{1,10}[-_]?\d+[a-z0-9]*\b")

# Siglas do domínio em caixa alta, lidas no texto ORIGINAL: ISS, GCR, NASA, DNA.
ACRONYM_PATTERN = re.compile(r"\b[A-Z]{2,}\b")

# Saudação na frente e cortesia no fim não mudam a intenção da pergunta.
_GREETING = r"(?:(?:oi|ola|ei|hey|hi|hello|bom dia|boa tarde|boa noite|dra aris|aris)[\s,]+)?"
_COURTESY = r"(?:[\s,]*(?:por favor|pfv|pf|please|obrigado|obrigada))?"


def strip_accents(text: str) -> str:
    """'osteogênese' e 'osteogenese' precisam casar; acento não é intenção."""
    decomposed = unicodedata.normalize("NFD", text)
    return "".join(c for c in decomposed if unicodedata.category(c) != "Mn")


def normalize(text: str) -> str:
    """
    Reduz a pergunta à forma em que os padrões estão escritos.

    Minúsculas, sem acento, sem pontuação, espaços colapsados. A pontuação
    interna vira espaço para que "dra. aris" e "dra aris" coincidam.
    """
    text = strip_accents(text.lower())
    text = re.sub(r"[^\w\s]", " ", text)
    return " ".join(text.split())


@lru_cache(maxsize=1)
def _vocabulary() -> Tuple[Set[str], Tuple[str, ...]]:
    """
    Carrega o vocabulário de domínio, separando termos de uma palavra dos de
    várias — os primeiros são testados por interseção de conjuntos, os
    segundos por substring, que é mais caro.

    Ausência do arquivo NÃO é erro fatal: o guard de forma continua valendo e
    o sistema segue funcionando, só com uma rede a menos. Derrubar a API por
    causa de um cache regenerável seria trocar um problema pequeno por um
    grande.
    """
    try:
        payload = json.loads(VOCABULARY_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set(), ()

    single: Set[str] = set()
    multi: List[str] = []
    for term in payload.get("entity_terms", []) + payload.get("portuguese_terms", []):
        normalized = normalize(term)
        if not normalized:
            continue
        if " " in normalized:
            multi.append(normalized)
        else:
            single.add(normalized)
    return single, tuple(multi)


def mentions_domain(question: str) -> bool:
    """
    A pergunta carrega vocabulário ou estrutura do domínio científico?

    Generoso de propósito: um falso positivo aqui manda a pergunta para o RAG,
    que é o lado barato do erro.
    """
    normalized = normalize(question)
    if not normalized:
        return False

    # Identificador com dígito: RUNX2, p21, OSD-570.
    if IDENTIFIER_PATTERN.search(normalized):
        return True

    # Sigla em caixa alta. Ignorada quando a pergunta INTEIRA está em caixa
    # alta, senão "QUEM É VOCÊ" seria lida como termo técnico.
    letters = [c for c in question if c.isalpha()]
    if letters and not all(c.isupper() for c in letters):
        if ACRONYM_PATTERN.search(strip_accents(question)):
            return True

    single, multi = _vocabulary()
    tokens = set(normalized.split())
    if tokens & single:
        return True
    return any(term in normalized for term in multi)


@dataclass(frozen=True)
class MetaIntent:
    """Uma intenção operacional reconhecida e a resposta estática que recebe."""

    name: str
    patterns: List[re.Pattern]
    answer: str


IDENTITY_ANSWER = """Sou a Dra. Aris, assistente científica do SpaceBio.

Respondo perguntas sobre biologia espacial usando **exclusivamente** um corpus de \
493 artigos científicos revisados por pares, extraídos do PubMed Central. Cada \
afirmação que eu faço vem acompanhada do trecho do artigo que a sustenta, com DOI \
e link para a fonte original.

O que me diferencia de um assistente de IA comum: **quando o corpus não tem \
evidência para a sua pergunta, eu digo isso em vez de arriscar uma resposta**. \
Prefiro recusar a inventar."""

USAGE_ANSWER = """Faça perguntas científicas sobre biologia espacial — em português \
ou inglês, tanto faz. Alguns exemplos que funcionam bem:

• *Como a microgravidade afeta a densidade óssea?*
• *Quais genes respondem à radiação espacial?*
• *O que acontece com o sistema imune em voo espacial?*
• *Quais experimentos foram feitos com Arabidopsis na ISS?*

Termos soltos também funcionam: *RUNX2*, *atrofia muscular*, *OSD-570*.

**Como ler a resposta.** Os números `[1]`, `[2]` no texto são citações: clique \
neles para ir ao trecho exato que sustenta aquela afirmação. Cada cartão de \
evidência mostra a passagem literal do artigo, a revista, o DOI e por qual \
caminho ele foi encontrado.

**Se eu recusar**, não é falha — significa que o corpus não cobre o tema. \
Os 493 artigos são de biologia espacial; perguntas fora disso não têm \
fundamento aqui."""

CORPUS_ANSWER = """Meu corpus tem **493 publicações científicas** de biologia \
espacial, todas do PubMed Central, divididas em 45.947 trechos indexados.

**Temas que eu cubro:** perda óssea e osteoporose em microgravidade, atrofia \
muscular, expressão gênica e transcriptômica, resposta imune, danos por \
radiação cósmica, crescimento de plantas no espaço, microbiologia de ambientes \
fechados e fisiologia cardiovascular em voo.

São artigos revisados por pares sobre os efeitos do voo espacial e da \
microgravidade em organismos — de *Arabidopsis* e *Bacillus subtilis* a \
camundongos e astronautas.

O acervo passou por curadoria: 83 documentos foram **descartados** por não \
serem confiáveis — erratas sem conteúdo científico e artigos cujo título \
registrado não correspondia ao texto baixado. Prefiro um corpus menor e \
íntegro a um grande e duvidoso."""

LIMITATIONS_ANSWER = """Minhas limitações, com franqueza:

**Só sei o que está no corpus.** 493 artigos de biologia espacial. Não sei \
nada sobre outros assuntos, e não tenho acesso à internet nem a artigos \
publicados fora desse acervo.

**Não substituo a leitura do artigo original.** Recupero trechos e sintetizo, \
mas o contexto completo — métodos, limitações do estudo, discussão — está no \
artigo. Sempre forneço o DOI para você conferir.

**Posso não encontrar algo que existe.** Se uma pergunta usar termos muito \
diferentes dos artigos, a busca pode falhar. Vale reformular.

**Não faço juízo sobre a qualidade dos estudos.** Se dois artigos discordam, \
mostro os dois."""


def _full(*alternatives: str) -> List[re.Pattern]:
    """
    Compila cada alternativa como padrão de frase INTEIRA.

    `fullmatch` é o que implementa a regra central: o padrão precisa dar conta
    de tudo o que foi digitado. Sobrou assunto? Não é pergunta operacional.
    Saudação e cortesia são toleradas nas bordas porque não mudam a intenção.
    """
    return [
        re.compile(_GREETING + "(?:" + alternative + ")" + _COURTESY)
        for alternative in alternatives
    ]


META_INTENTS: List[MetaIntent] = [
    MetaIntent(
        name="identity",
        patterns=_full(
            r"(?:quem|que|qual) (?:e|eh|es|sao) (?:voce|vc|tu|voces)",
            r"quem (?:e|eh) (?:a |o )?(?:dra?\s*)?aris",
            r"o que (?:voce|vc) (?:e|eh)",
            r"(?:who|what) (?:are|is) you",
            r"qual (?:e )?(?:o )?seu nome",
            r"como (?:voce|vc) se chama",
            r"(?:se )?apresent(?:e|e se|a se|acao)",
            r"fale (?:sobre |de )?(?:voce|vc|si)",
        ),
        answer=IDENTITY_ANSWER,
    ),
    MetaIntent(
        name="usage",
        patterns=_full(
            r"(?:me )?ajud(?:a|e|ar)(?: ai| aqui)?",
            r"(?:preciso de )?ajuda",
            r"help",
            r"socorro",
            r"como (?:voce|vc|isso|isto|tudo|o sistema|essa ferramenta) funciona",
            r"como funciona(?: isso| isto| o sistema| voce| vc| essa ferramenta| tudo)?",
            r"como (?:eu )?(?:te |voce |vc )?us(?:o|ar)(?: isso| isto| o sistema| voce| vc)?",
            r"como (?:eu )?(?:posso|devo) (?:te |voce |vc )?(?:usar|perguntar|falar|pesquisar)",
            r"como (?:eu )?(?:posso|devo) (?:fazer|comecar)",
            r"como (?:perguntar|pesquisar|buscar|procurar)",
            r"o que (?:voce|vc) (?:faz|pode fazer|consegue fazer|sabe fazer)",
            r"o que (?:da|de) para (?:fazer|perguntar)(?: aqui)?",
            r"what can you do",
            r"how (?:do i )?(?:use|work with) (?:you|this|it)",
            r"(?:quais|que|quantas) (?:tipos? de )?pergunt(?:a|as)"
            r"(?: (?:eu )?(?:posso|devo) fazer)?",
            r"que (?:tipo|tipos) de (?:coisa|coisas|duvida|duvidas)",
            r"por onde (?:eu )?come(?:co|car)",
            r"(?:me )?(?:da|de) (?:um )?exemplos?",
            r"(?:quais|que) (?:sao )?(?:os )?exemplos",
        ),
        answer=USAGE_ANSWER,
    ),
    MetaIntent(
        name="corpus",
        patterns=_full(
            r"(?:quais|que|quantos|quantas) "
            r"(?:artigos|publicacoes|estudos|papers|dados|fontes)"
            r"(?: (?:voce|vc) (?:tem|usa|consulta|indexa|leu))?",
            r"qual (?:e )?(?:a )?sua (?:base|fonte|origem)(?: de dados)?",
            r"quais (?:sao )?(?:as )?suas (?:fontes|bases|referencias)",
            r"(?:de )?onde vem (?:as |os )?(?:suas |seus )?"
            r"(?:informacoes|respostas|dados|fontes)",
            r"(?:o que )?(?:tem|ha|existe) no seu (?:corpus|acervo|banco|indice)",
            r"qual (?:e )?(?:o )?seu (?:corpus|acervo|banco|escopo)",
            r"(?:quais|que) (?:temas|assuntos|topicos|areas|materias) (?:voce|vc) "
            r"(?:cobre|aborda|responde|trata|conhece)",
            r"(?:quais|que) (?:sao )?(?:os )?(?:seus )?(?:temas|assuntos|topicos)",
            r"sobre o que (?:voce|vc) (?:responde|fala|sabe|escreve)",
            r"what (?:data|papers|articles|sources) do you (?:have|use)",
            r"(?:qual|que) (?:e )?(?:o )?(?:tamanho|escopo) do (?:corpus|acervo)",
        ),
        answer=CORPUS_ANSWER,
    ),
    MetaIntent(
        name="limitations",
        patterns=_full(
            r"(?:quais|que) (?:sao )?(?:as )?(?:suas )?limitacoes",
            r"(?:quais|que) (?:sao )?(?:os )?(?:seus )?limites",
            r"suas limitacoes",
            r"o que (?:voce|vc) nao (?:sabe|pode|consegue)(?: fazer| responder)?",
            r"(?:voce|vc) (?:pode|consegue) errar",
            r"(?:voce|vc) (?:erra|alucina|inventa)",
            r"what (?:are your )?limitations",
            r"(?:quais|que) (?:sao )?(?:os )?(?:seus )?(?:defeitos|problemas)",
        ),
        answer=LIMITATIONS_ANSWER,
    ),
]


def classify(question: str) -> Optional[MetaIntent]:
    """
    Identifica uma pergunta operacional, ou devolve None para seguir ao RAG.

    Três guards, todos com o mesmo viés: na dúvida, None.

      1. COMPRIMENTO  — acima de MAX_META_QUESTION_CHARS não é operacional.
      2. DOMÍNIO      — vocabulário do grafo, termos em português, ou um
                        identificador/sigla no texto. Qualquer um veta.
      3. FORMA        — o padrão precisa casar a frase INTEIRA. Sobrou
                        assunto, a pergunta é sobre o assunto.

    Devolver None é sempre seguro: custa uma chamada de LLM. Devolver uma
    intenção por engano esconde o acervo, e é o erro que não se aceita.
    """
    if not question or not question.strip():
        return None

    text = question.strip()
    if len(text) > MAX_META_QUESTION_CHARS:
        return None
    if mentions_domain(text):
        return None

    normalized = normalize(text)
    if not normalized:
        return None

    for intent in META_INTENTS:
        if any(pattern.fullmatch(normalized) for pattern in intent.patterns):
            return intent
    return None
