# -*- coding: utf-8 -*-
"""
Gera o vocabulário de domínio usado pelo roteador de intenção (E3-03).

POR QUE NÃO UMA LISTA ESCRITA À MÃO
-----------------------------------
A versão anterior do `intent.py` trazia ~40 termos digitados manualmente.
Medido: 8 de 17 perguntas científicas eram classificadas como operacionais,
porque usavam vocabulário fora da lista — "osteogênese", "autofagia",
"telômeros", "RUNX2". Uma lista escrita à mão está sempre desatualizada em
relação ao corpus, e cada termo faltante é uma pergunta científica que recebe
um guia de ajuda no lugar da evidência.

O grafo já sabe o vocabulário: 979 entidades extraídas do próprio corpus.
Usá-las remove a manutenção manual e faz o guard crescer junto com o acervo.

POR QUE SER GENEROSO AQUI É SEGURO
----------------------------------
A assimetria da E3-03 trabalha a favor: um termo deste arquivo casando por
engano com uma pergunta operacional custa UMA chamada de LLM. Um termo
faltante esconde evidência do acervo. Na dúvida, incluir.

O limite é não engolir as próprias perguntas operacionais: uma entidade
chamada "help" quebraria o roteamento inteiro. Daí a lista de exclusão.

USO
    python build_domain_vocabulary.py          # regenera data/domain_vocabulary.json
    python build_domain_vocabulary.py --show   # imprime uma amostra
"""

from __future__ import annotations

import argparse
import json
import sys
import unicodedata
from pathlib import Path
from typing import Dict, List, Set

from config import settings
from graph_manager import build_driver

VOCABULARY_PATH = Path(__file__).parent / "data" / "domain_vocabulary.json"

# Termos que NUNCA entram, mesmo que apareçam como entidade: colidem com o
# vocabulário das perguntas operacionais e quebrariam o roteamento.
BLOCKLIST = {
    "help", "you", "your", "name", "data", "base", "use", "using", "work",
    "works", "what", "who", "how", "can", "do", "does", "the", "and", "for",
    "ajuda", "voce", "nome", "base", "dados", "uso", "usar", "faz", "fazer",
    "tema", "temas", "assunto", "assuntos", "limite", "limites",
}

# Vocabulário científico em português, que o grafo não tem porque o corpus é
# todo em inglês. Complementa as entidades; não as substitui.
PORTUGUESE_DOMAIN_TERMS = {
    # áreas e processos
    "microgravidade", "gravidade", "hipogravidade", "espacial", "espaço",
    "radiacao", "radiação", "osteogenese", "osteogênese", "osteoporose",
    "autofagia", "apoptose", "atrofia", "hipertrofia", "metabolismo",
    "expressao", "expressão", "transcricao", "transcrição", "traducao",
    "mutacao", "mutação", "epigenetica", "epigenética", "metilacao",
    "inflamacao", "inflamação", "imunidade", "imune", "homeostase",
    "diferenciacao", "diferenciação", "proliferacao", "proliferação",
    "senescencia", "senescência", "estresse", "oxidativo", "mitocondrial",
    # estruturas
    "osso", "ossos", "ossea", "óssea", "osseo", "ósseo", "musculo", "músculo",
    "muscular", "celula", "célula", "celulas", "células", "celular",
    "tecido", "tecidos", "cartilagem", "tendao", "tendão", "medula",
    "telomero", "telômero", "telomeros", "telômeros", "cromossomo",
    "proteina", "proteína", "proteinas", "proteínas", "enzima", "enzimas",
    "gene", "genes", "genetica", "genética", "genoma", "genomica",
    "dna", "rna", "rnaseq", "transcriptoma", "proteoma", "microbioma",
    "osteoclasto", "osteoclastos", "osteoblasto", "osteoblastos",
    "osteocito", "osteócito", "osteocitos", "osteócitos",
    "neuronio", "neurônio", "neuronios", "neurônios", "cardiaco", "cardíaco",
    "vascular", "endotelial", "epitelial", "fibroblasto", "linfocito",
    # organismos e sujeitos
    "camundongo", "camundongos", "rato", "ratos", "roedor", "roedores",
    "astronauta", "astronautas", "tripulacao", "tripulação", "humano",
    "planta", "plantas", "semente", "sementes", "raiz", "raizes", "raízes",
    "bacteria", "bactéria", "bacterias", "bactérias", "fungo", "fungos",
    "levedura", "microrganismo", "microrganismos", "patogeno", "patógeno",
    # contexto de voo
    "voo", "missao", "missão", "missoes", "missões", "orbita", "órbita",
    "estacao", "estação", "nave", "satelite", "satélite", "biosatelite",
    "experimento", "experimentos", "ensaio", "amostra", "amostras",
    "simulacao", "simulação", "suspensao", "suspensão", "descarregamento",
    "cosmico", "cósmico", "cosmica", "cósmica", "ionizante",
}


def strip_accents(text: str) -> str:
    """Remove acentos, para que 'osteogênese' e 'osteogenese' casem."""
    decomposed = unicodedata.normalize("NFD", text)
    return "".join(c for c in decomposed if unicodedata.category(c) != "Mn")


def _acceptable(term: str) -> bool:
    """
    O termo serve como sinal de domínio?

    Rejeita o que é curto demais para ser específico (duas letras casam em
    qualquer lugar) e o que colide com o vocabulário operacional.
    """
    bare = term.strip()
    if len(bare) < 3:
        return False
    if strip_accents(bare.lower()) in BLOCKLIST:
        return False
    return True


def collect_entity_terms() -> Dict[str, int]:
    """Lê os nomes canônicos das entidades do grafo, por tipo."""
    driver = build_driver()
    terms: Set[str] = set()
    by_label: Dict[str, int] = {}
    try:
        with driver.session(database=settings.neo4j_database) as session:
            for record in session.run(
                "MATCH (e:Entity) RETURN e.canonical AS canonical, e.label AS label"
            ):
                canonical = (record["canonical"] or "").strip()
                if not canonical or not _acceptable(canonical):
                    continue
                terms.add(canonical)

                # Nomes compostos tambem entram decompostos: "Arabidopsis
                # thaliana" esta no grafo inteiro, mas quem pesquisa digita
                # "Arabidopsis". Sem isso, o genero sozinho escapa do guard.
                #
                # Fragmento e mais arriscado que nome inteiro -- "Bed Rest"
                # produziria "rest" -- entao o corte de tamanho e maior aqui.
                if " " in canonical:
                    for part in canonical.split():
                        if len(part) >= 5 and _acceptable(part):
                            terms.add(part)

                label = record["label"] or "Desconhecido"
                by_label[label] = by_label.get(label, 0) + 1
    finally:
        driver.close()

    VOCABULARY_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "entity_terms": sorted(terms),
        "portuguese_terms": sorted(PORTUGUESE_DOMAIN_TERMS),
        "counts": {"entities": len(terms), "portuguese": len(PORTUGUESE_DOMAIN_TERMS)},
    }
    VOCABULARY_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    return by_label


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--show", action="store_true", help="imprime uma amostra")
    args = parser.parse_args()

    by_label = collect_entity_terms()
    payload = json.loads(VOCABULARY_PATH.read_text(encoding="utf-8"))

    print(f"Vocabulário gravado em {VOCABULARY_PATH}")
    print(f"  entidades do grafo : {payload['counts']['entities']}")
    print(f"  termos em português: {payload['counts']['portuguese']}")
    for label, count in sorted(by_label.items(), key=lambda kv: -kv[1]):
        print(f"      {label:24} {count}")

    if args.show:
        print("\n  amostra de entidades:")
        for term in payload["entity_terms"][:20]:
            print(f"      {term}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
