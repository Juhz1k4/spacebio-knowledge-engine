"""
Roteador de intenção — perguntas sobre o sistema vs perguntas ao corpus

Intercepta perguntas operacionais ("quem é você", "como te uso", "ajuda")
antes do RAG e responde com um guia, sem buscar no índice vetorial nem
acionar o LLM.

POR QUE ISTO É NECESSÁRIO
-------------------------
"Quem é você?" não tem resposta no corpus — são 493 artigos de biologia
espacial, nenhum fala da Dra. Aris. Sem roteamento, a pergunta seguiria o
caminho normal e receberia a recusa por falta de evidência. Tecnicamente
correto, e péssimo: o usuário que tenta entender a ferramenta é recebido com
"não encontrei evidência suficiente", o que parece defeito.

O roteamento também protege a quota: perguntas operacionais são comuns nos
primeiros minutos de uso e não deveriam consumir requisições do LLM.

POR QUE A CLASSIFICAÇÃO É POR REGRA, NÃO POR MODELO
---------------------------------------------------
Usar o LLM para classificar intenção gastaria a requisição que estamos
tentando economizar. E o conjunto de perguntas operacionais é pequeno e
fechado — exatamente o caso em que regra explícita supera ML: é auditável,
instantânea e não alucina.

O RISCO DA CLASSIFICAÇÃO ERRADA É ASSIMÉTRICO
---------------------------------------------
Classificar uma pergunta científica como operacional é grave: o usuário
recebe um guia em vez da resposta que o corpus tinha. O contrário é leve:
recebe uma recusa educada.

Por isso os padrões exigem que a pergunta seja CURTA e casem a frase quase
inteira. "Como usar camundongos em experimentos de microgravidade?" contém
"como usar", mas tem 60 caracteres e termos do domínio — não é roteada.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional

# Perguntas operacionais raramente passam disto. O limite é a primeira
# barreira contra falso positivo: uma pergunta científica longa que por acaso
# contenha "como usar" não é capturada.
MAX_META_QUESTION_CHARS = 80

# Termos do domínio que vetam o roteamento. Se a pergunta menciona qualquer
# um, ela é científica mesmo que a forma pareça operacional.
DOMAIN_TERMS = {
    "microgravidade", "microgravity", "espacial", "space", "espaço",
    "radiação", "radiation", "gene", "genes", "genética", "osso", "ossos",
    "bone", "músculo", "muscle", "célula", "cell", "nasa", "iss", "astronauta",
    "astronaut", "camundongo", "camundongos", "rato", "ratos", "mice", "mouse",
    "planta", "plantas", "plant", "voo", "flight", "experimento", "experiment",
    "proteína", "protein", "dna", "rna", "corpo humano",
}


@dataclass(frozen=True)
class MetaIntent:
    """Uma intenção operacional reconhecida e a resposta que ela recebe."""

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

**Como ler a resposta.** Os números `[1]`, `[2]` no texto são citações: clique \
neles para ir ao trecho exato que sustenta aquela afirmação. Cada cartão de \
evidência mostra a passagem literal do artigo, a revista, o DOI e por qual \
caminho ele foi encontrado.

**Se eu recusar**, não é falha — significa que o corpus não cobre o tema. \
Os 493 artigos são de biologia espacial; perguntas fora disso não têm \
fundamento aqui."""

CORPUS_ANSWER = """Meu corpus tem **493 publicações científicas** de biologia \
espacial, todas do PubMed Central, divididas em 45.947 trechos indexados.

São artigos revisados por pares sobre efeitos do voo espacial e da \
microgravidade em organismos: perda óssea, atrofia muscular, expressão gênica, \
resposta imune, danos por radiação, crescimento de plantas no espaço, \
microbiologia em ambiente espacial.

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

META_INTENTS: List[MetaIntent] = [
    MetaIntent(
        name="identity",
        patterns=[
            re.compile(r"^\s*(quem|que)\s+(é|e|es)\s+(você|voce|vc|tu)\b", re.I),
            re.compile(r"^\s*(o\s+)?que\s+(você|voce|vc)\s+(é|e)\b", re.I),
            re.compile(r"^\s*who\s+are\s+you\b", re.I),
            re.compile(r"^\s*(qual|quem)\s+(é|e)\s+(o\s+)?seu\s+nome\b", re.I),
            re.compile(r"^\s*(se\s+)?apresent[ea]", re.I),
        ],
        answer=IDENTITY_ANSWER,
    ),
    MetaIntent(
        name="usage",
        patterns=[
            re.compile(r"^\s*como\s+(te\s+)?(us[ao]|utiliz[ao]|funciona)", re.I),
            re.compile(r"^\s*como\s+(eu\s+)?(devo|posso)\s+(te\s+)?(us|pergunt|falar)", re.I),
            re.compile(r"^\s*(me\s+)?ajud[ae]\b", re.I),
            re.compile(r"^\s*(help|ajuda)\s*[?!.]*\s*$", re.I),
            re.compile(r"^\s*how\s+(do\s+i\s+)?use\s+(you|this)", re.I),
            re.compile(r"^\s*o\s+que\s+(você|voce|vc)\s+(faz|pode\s+fazer)", re.I),
            re.compile(r"^\s*what\s+can\s+you\s+do", re.I),
            re.compile(r"^\s*(quais?|que)\s+(tipos?\s+de\s+)?pergunt", re.I),
        ],
        answer=USAGE_ANSWER,
    ),
    MetaIntent(
        name="corpus",
        patterns=[
            re.compile(r"^\s*(quais|que|quantos)\s+(artigos|publicaç|estudos|dados)", re.I),
            re.compile(r"^\s*(qual|que)\s+(é\s+)?(a\s+)?sua\s+(base|fonte)", re.I),
            re.compile(r"^\s*(de\s+)?onde\s+(vem|vêm|vêem)\s+(as\s+)?(suas\s+)?(informa|resposta|dados)", re.I),
            re.compile(r"^\s*what\s+(data|papers|articles)\s+do\s+you", re.I),
            re.compile(r"^\s*(o\s+que\s+)?(há|tem|existe)\s+(no\s+)?seu\s+(corpus|acervo)", re.I),
        ],
        answer=CORPUS_ANSWER,
    ),
    MetaIntent(
        name="limitations",
        patterns=[
            re.compile(r"^\s*(quais|que)\s+(são\s+)?(suas\s+)?limitaç", re.I),
            # "não" e "nao": quem digita rápido não acentua, e a pergunta é a
            # mesma. Vale para todos os padrões — acentuação não é sinal de
            # intenção diferente.
            re.compile(r"^\s*(o\s+que\s+)?(você|voce|vc)\s+n[ãa]o\s+(sabe|pode|consegue)", re.I),
            re.compile(r"^\s*what\s+(are\s+your\s+)?limitations", re.I),
            re.compile(r"^\s*(você|voce|vc)\s+(pode|consegue)\s+errar", re.I),
        ],
        answer=LIMITATIONS_ANSWER,
    ),
]


def mentions_domain(question: str) -> bool:
    """A pergunta usa vocabulário do domínio científico?"""
    lowered = question.lower()
    return any(
        re.search(rf"\b{re.escape(term)}\b", lowered) for term in DOMAIN_TERMS
    )


def classify(question: str) -> Optional[MetaIntent]:
    """
    Identifica uma pergunta operacional, ou devolve None.

    Três guardas contra capturar pergunta científica por engano:
      1. a pergunta precisa ser curta;
      2. não pode mencionar termo do domínio;
      3. o padrão precisa casar o INÍCIO da frase, não um trecho qualquer.
    """
    if not question or not question.strip():
        return None

    text = question.strip()
    if len(text) > MAX_META_QUESTION_CHARS:
        return None
    if mentions_domain(text):
        return None

    for intent in META_INTENTS:
        if any(pattern.search(text) for pattern in intent.patterns):
            return intent
    return None
