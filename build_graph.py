"""
OBSOLETO — construtor de grafo da geração 1 (Fase 0)

    NÃO EXECUTE ESTE SCRIPT contra o grafo atual.

Foi substituído por `extract_entities.py`. Permanece no repositório apenas
como registro histórico da primeira geração do pipeline.

POR QUE NÃO DEVE RODAR
----------------------
1. LÊ O CORPUS ERRADO. Usa `data/metadata.csv` (594 registros brutos) e o
   texto de `local_path` (sujo). O corpus curado é `data/metadata_clean.csv`
   (493 registros) com o texto de `clean_path`. Rodar este script recriaria no
   grafo as 83 publicações que a SPACEBIO-012.5 descartou de propósito — as 11
   erratas e os 70 de procedência não confiável.

2. EXTRAI AS ENTIDADES ERRADAS. Usa spaCy genérico, que só reconhece
   Organization, Person e Location. O §9.4 do briefing identifica exatamente
   isso como o problema: não representa biologia. A ontologia real
   (`ontology.py`) modela Organism, Gene, Tissue, ExperimentalCondition e mais.

3. CREDENCIAIS HARDCODED. `AUTH = ("neo4j", "password")` viola o §8.2 e nem é
   a senha correta — o script falharia ao conectar de qualquer forma.

4. REBUILD DESTRUTIVO. Com `--rebuild`, apaga TODO o grafo: 45.947 chunks e
   seus embeddings, ~37 minutos de CPU.

O QUE USAR NO LUGAR
-------------------
    python ingest_corpus.py       # publicações, chunks e embeddings
    python extract_entities.py    # entidades da ontologia V1 e menções
    python entity_linking.py      # identificadores NCBI/UniProt

Ver docs/ROADMAP-FASE-3.md para o estado atual da arquitetura.
"""

import spacy
import pandas as pd
import os
import sys
from neo4j import GraphDatabase

# --- CONFIGURAÇÃO NEO4J ---
# Mantidas como estavam na Fase 0, apenas para registro. Não use este padrão:
# o projeto lê credenciais do ambiente desde a SPACEBIO-003 (ver config.py).
URI = "bolt://localhost:7687"
AUTH = ("neo4j", "password")

# --- CONFIGURAÇÃO DO PROJETO ---
DATA_DIR = "data"
METADATA_FILE = os.path.join(DATA_DIR, "metadata.csv")

# O modelo só é carregado se o script for de fato executado — importá-lo para
# inspeção não deve custar os segundos de carga do spaCy.
nlp = None

def build_knowledge_graph(driver):
    """
    Processa os textos, extrai entidades e popula o grafo no Neo4j.
    """
    print("Iniciando a construção do Grafo de Conhecimento...")
    try:
        df = pd.read_csv(METADATA_FILE)
        df.dropna(subset=['local_path'], inplace=True)
    except FileNotFoundError:
        print(f"!!! Erro: Arquivo '{METADATA_FILE}' não encontrado. Rode o script de ingestão primeiro.")
        return
    except pd.errors.EmptyDataError:
        print(f"!!! Erro: Arquivo '{METADATA_FILE}' está vazio. O script de ingestão não conseguiu extrair dados.")
        return

    with driver.session() as session:
        # O rebuild destrutivo deixou de ser o comportamento padrão (§20 do
        # briefing). `MATCH (n) DETACH DELETE n` apagava TODO o grafo — hoje
        # isso inclui os Chunks e embeddings da ingestão do corpus, que levam
        # minutos de CPU para regenerar. Agora exige --rebuild explícito.
        if "--rebuild" in sys.argv:
            print("!!! --rebuild: apagando TODO o grafo (inclusive Chunks e embeddings)...")
            session.run("MATCH (n) DETACH DELETE n")
        else:
            print("Modo incremental (use --rebuild para apagar o grafo antes).")

        for index, row in df.iterrows():
            title = row["title"]
            source_url = row["source_url"]
            text_path = row["local_path"]
            
            if not text_path.endswith('.txt'):
                print(f"Pulando arquivo não processado: {text_path}")
                continue
            
            print(f"\n-> Processando publicação: {title[:30]}...")

            session.run("MERGE (p:Publication {title: $title, source_url: $source_url})", title=title, source_url=source_url)
            
            with open(text_path, "r", encoding="utf-8") as f:
                text = f.read()
            
            nlp.max_length = len(text) + 100
            doc = nlp(text)

            for ent in doc.ents:
                label = ""
                # MUDANÇA AQUI: Adicionamos a etiqueta :Author
                if ent.label_ == "ORG": label = "Organization"
                elif ent.label_ == "PERSON": label = "Person:Author" # Nós de pessoa também são Autores
                elif ent.label_ == "GPE": label = "Location"
                
                if label:
                    # O nome do nó será apenas a primeira parte da etiqueta (ex: "Person")
                    node_label = label.split(':')[0]
                    session.run(f"MERGE (e:{label} {{name: $name}})", name=ent.text)
                    session.run(f"""
                        MATCH (p:Publication {{title: $title}})
                        MATCH (e:{node_label} {{name: $name}})
                        MERGE (p)-[:MENTIONS]->(e)
                    """, title=title, name=ent.text)
            
            print(f"   - Nós e relações criados para a publicação.")

if __name__ == "__main__":
    # Trava de segurança. Este script pertence à geração 1 do pipeline e
    # corromperia o grafo curado (ver docstring no topo). A flag existe para
    # que rodá-lo seja sempre uma escolha consciente, nunca um engano.
    if "--i-know-this-is-obsolete" not in sys.argv:
        print(__doc__)
        print("=" * 74)
        print("EXECUÇÃO BLOQUEADA.")
        print()
        print("Este script é da geração 1 do pipeline e corromperia o grafo atual.")
        print("Use `python ingest_corpus.py` e `python extract_entities.py`.")
        print()
        print("Se precisar mesmo rodá-lo, por algum motivo histórico:")
        print("    python build_graph.py --i-know-this-is-obsolete")
        print("=" * 74)
        sys.exit(1)

    print("!!! Rodando o construtor OBSOLETO da geração 1.")
    nlp = spacy.load("en_core_web_sm")

    try:
        with GraphDatabase.driver(URI, auth=AUTH) as driver:
            driver.verify_connectivity()
            print("Conexão com Neo4j estabelecida com sucesso.")
            build_knowledge_graph(driver)
            print("\nGrafo de Conhecimento construído!")
    except Exception as e:
        print(f"!!! Erro ao conectar ou construir o grafo: {e}")
        print("!!! Verifique se o Neo4j Desktop está rodando e se a senha está correta.")