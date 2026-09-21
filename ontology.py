"""
SPACEBIO-016 — SpaceBio Ontology V1

A ontologia começa pequena, como o §17 do briefing determina: um tipo só entra
quando (a) aparece no corpus, (b) tem valor de consulta e (c) tem estratégia
clara de extração. Cada tipo abaixo passou por essa peneira com número medido
sobre os 493 documentos do corpus limpo.

O QUE FICOU DE FORA DA V1, E POR QUÊ
------------------------------------
Researcher, Organization      Os nomes de autor estão no cabeçalho do PMC, que
                              o cleaner descarta (R3). Extrair autores é uma
                              issue de ingestão, não de NER sobre o corpo.
Protein                       Indistinguível de Gene sem normalização contra
                              uma base externa (UniProt). Entra com Entity
                              Linking (SPACEBIO-020).
Experiment                    Não tem menção nomeada e estável no texto; é uma
                              abstração que só emerge de missão + condição +
                              organismo. Modelar antes de ter os três seria
                              inventar estrutura.
Phenotype                     Fronteira difusa com BiologicalProcess. Separar
                              exige ontologia externa (HPO/MP).

TIPOS DA V1 (prevalência medida em 493 documentos)
--------------------------------------------------
Organism               human 75%, mouse 52%, rat 28%, E. coli 20%, Arabidopsis 20%
ExperimentalCondition  spaceflight 58%, microgravity 56%, radiation 45%
Mission                ISS 45%, STS-nn 15%, Inspiration4 5%
Spacecraft             Space Shuttle 16%, Dragon/SpaceX 16%, Soyuz 4%
Dataset                GeneLab 18%, GLDS-nn 10%, OSD-nn 4%
Gene                   padrão + dicionário curado
Tissue                 bone 35%, muscle 34%, liver 37%
CellType               osteoblast 12%, osteoclast 9%, stem cell 21%
BiologicalProcess      gene expression 56%, oxidative stress 31%, apoptosis 25%

DECISÃO DE MODELAGEM: A MENÇÃO É DO CHUNK, NÃO DA PUBLICAÇÃO
------------------------------------------------------------
O §17 propõe (:Publication)-[:MENTIONS]->(Entity). A V1 grava
(:Chunk)-[:MENTIONS]->(Entity), por uma razão prática: o retriever híbrido
ranqueia CHUNKS, e o sinal de entity overlap só serve se puder ser calculado
na mesma granularidade. A visão por publicação continua disponível sem
duplicar dado, atravessando HAS_CHUNK:

    (:Publication)-[:HAS_CHUNK]->(:Chunk)-[:MENTIONS]->(:Entity)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Pattern, Set

# Versão da ontologia. Gravada em cada chunk anotado, para que a extração
# incremental (SPACEBIO-023) saiba o que precisa ser reprocessado.
#
# INCREMENTE ao mudar qualquer coisa que altere o resultado da extração:
# entradas do dicionário, formas de superfície, stoplist de genes, padrões.
# Sem isso, um chunk anotado por uma versão antiga permanece com anotação
# obsoleta para sempre — foi o que aconteceria nas três calibrações do
# stoplist que a Fase 2 exigiu.
ONTOLOGY_VERSION = "v1.0.0"

# --------------------------------------------------------------------- #
# Tipos de nó
# --------------------------------------------------------------------- #

ORGANISM = "Organism"
GENE = "Gene"
TISSUE = "Tissue"
CELL_TYPE = "CellType"
BIOLOGICAL_PROCESS = "BiologicalProcess"
EXPERIMENTAL_CONDITION = "ExperimentalCondition"
MISSION = "Mission"
SPACECRAFT = "Spacecraft"
DATASET = "Dataset"

ENTITY_LABELS = [
    ORGANISM,
    GENE,
    TISSUE,
    CELL_TYPE,
    BIOLOGICAL_PROCESS,
    EXPERIMENTAL_CONDITION,
    MISSION,
    SPACECRAFT,
    DATASET,
]

# Todo nó de entidade também recebe :Entity, para consultas que não se
# importam com o tipo — como o entity overlap do retriever.
ENTITY_SUPERLABEL = "Entity"


@dataclass(frozen=True)
class EntityDefinition:
    """
    Uma entidade canônica e as formas como o corpus a escreve.

    `canonical` é a chave do nó no grafo. `surface_forms` são as variações
    aceitas — é aqui que mora a normalização da SPACEBIO-019: "ISS",
    "International Space Station" e "space station" viram o mesmo nó.
    """

    canonical: str
    label: str
    surface_forms: List[str] = field(default_factory=list)
    description: Optional[str] = None
    match_canonical: bool = True
    """
    O nome canônico também serve como forma de busca?

    False quando o canônico é ambíguo fora de contexto. O caso que motivou
    isto: a estação `Mir` casava com `miR-125b`, `miR-21` e toda a família de
    microRNAs — 7 das 8 ocorrências medidas eram microRNA, não a estação. Com
    match_canonical=False, só as formas explícitas ("Mir Space Station")
    contam.
    """

    def all_forms(self) -> List[str]:
        forms = list(self.surface_forms)
        if self.match_canonical:
            forms.append(self.canonical)
        # Mais longas primeiro: casar "International Space Station" antes de
        # "Space Station" evita fragmentar a menção.
        return sorted(set(forms), key=len, reverse=True)


# --------------------------------------------------------------------- #
# SPACEBIO-018 — Space Biology Domain Dictionary
# --------------------------------------------------------------------- #
# Curado a partir da medição do corpus. O que é finito e nomeado vive aqui;
# o que é aberto (genes) depende de padrão e de NER estatístico.

ORGANISMS = [
    EntityDefinition("Homo sapiens", ORGANISM, ["human", "humans", "astronaut", "astronauts", "crew member", "crewmember"]),
    EntityDefinition("Mus musculus", ORGANISM, ["mouse", "mice", "murine", "C57BL/6"]),
    EntityDefinition("Rattus norvegicus", ORGANISM, ["rat", "rats", "rattus"]),
    EntityDefinition("Arabidopsis thaliana", ORGANISM, ["arabidopsis", "thale cress"]),
    EntityDefinition("Drosophila melanogaster", ORGANISM, ["drosophila", "fruit fly", "fruit flies"]),
    EntityDefinition("Caenorhabditis elegans", ORGANISM, ["c. elegans", "c.elegans", "caenorhabditis", "nematode"]),
    EntityDefinition("Escherichia coli", ORGANISM, ["e. coli", "e.coli", "escherichia"]),
    EntityDefinition("Saccharomyces cerevisiae", ORGANISM, ["yeast", "s. cerevisiae", "saccharomyces"]),
    EntityDefinition("Danio rerio", ORGANISM, ["zebrafish", "danio"]),
    EntityDefinition("Bacillus subtilis", ORGANISM, ["b. subtilis", "bacillus subtilis"]),
    EntityDefinition("Staphylococcus aureus", ORGANISM, ["s. aureus", "staphylococcus"]),
    EntityDefinition("Pseudomonas aeruginosa", ORGANISM, ["p. aeruginosa", "pseudomonas"]),
    EntityDefinition("Aspergillus", ORGANISM, ["aspergillus niger", "aspergillus fumigatus"]),
    EntityDefinition("Hypsibius exemplaris", ORGANISM, ["tardigrade", "tardigrades", "water bear"]),
]

CONDITIONS = [
    EntityDefinition("Microgravity", EXPERIMENTAL_CONDITION, ["microgravity", "micro-gravity", "µg", "weightlessness", "zero gravity", "zero-g"]),
    EntityDefinition("Spaceflight", EXPERIMENTAL_CONDITION, ["spaceflight", "space flight", "space travel"]),
    EntityDefinition("Simulated Microgravity", EXPERIMENTAL_CONDITION, ["simulated microgravity", "modeled microgravity", "analog microgravity"]),
    EntityDefinition("Hindlimb Unloading", EXPERIMENTAL_CONDITION, ["hindlimb unloading", "hindlimb suspension", "hind limb unloading", "tail suspension", "HLU"]),
    EntityDefinition("Ionizing Radiation", EXPERIMENTAL_CONDITION, ["ionizing radiation", "cosmic radiation", "galactic cosmic ray", "GCR", "HZE", "space radiation"]),
    EntityDefinition("Hypergravity", EXPERIMENTAL_CONDITION, ["hypergravity", "hyper-gravity", "centrifugation"]),
    EntityDefinition("Clinorotation", EXPERIMENTAL_CONDITION, ["clinorotation", "clinostat", "random positioning machine", "RPM"]),
    EntityDefinition("Partial Gravity", EXPERIMENTAL_CONDITION, ["partial gravity", "lunar gravity", "martian gravity", "fractional gravity"]),
    EntityDefinition("Bed Rest", EXPERIMENTAL_CONDITION, ["bed rest", "bedrest", "head-down tilt"]),
]

MISSIONS = [
    EntityDefinition("International Space Station", MISSION, ["ISS", "international space station", "space station"]),
    EntityDefinition("Bion-M 1", MISSION, ["bion-m 1", "bion-m", "bion m"]),
    EntityDefinition("Inspiration4", MISSION, ["inspiration4", "inspiration-4", "i4 mission"]),
    EntityDefinition("Apollo Program", MISSION, ["apollo program", "apollo mission"]),
    EntityDefinition("Artemis Program", MISSION, ["artemis program", "artemis mission"]),
    # "Mir" sozinho casa com miR-125b, miR-21 e toda a família de microRNAs:
    # medido, 7 de 8 ocorrências eram microRNA. Só as formas explícitas valem.
    EntityDefinition("Mir", MISSION, ["mir station", "mir space station"], match_canonical=False),
    EntityDefinition("Skylab", MISSION, ["skylab"]),
]

SPACECRAFT = [
    EntityDefinition("Space Shuttle", SPACECRAFT, ["space shuttle", "shuttle orbiter"]),
    EntityDefinition("SpaceX Dragon", SPACECRAFT, ["spacex dragon", "dragon capsule", "crew dragon"]),
    EntityDefinition("Soyuz", SPACECRAFT, ["soyuz"]),
    EntityDefinition("Falcon 9", SPACECRAFT, ["falcon 9", "falcon-9"]),
]

TISSUES = [
    EntityDefinition("Bone", TISSUE, ["bone", "bones", "skeletal tissue", "trabecular bone", "cortical bone"]),
    EntityDefinition("Skeletal Muscle", TISSUE, ["skeletal muscle", "soleus", "gastrocnemius"]),
    EntityDefinition("Liver", TISSUE, ["liver", "hepatic tissue"]),
    EntityDefinition("Cardiac Tissue", TISSUE, ["cardiac tissue", "heart tissue", "myocardium"]),
    EntityDefinition("Retina", TISSUE, ["retina", "retinal tissue"]),
    EntityDefinition("Brain", TISSUE, ["brain", "cerebral tissue", "hippocampus"]),
    EntityDefinition("Skin", TISSUE, ["skin", "dermal tissue", "epidermis"]),
    EntityDefinition("Thymus", TISSUE, ["thymus"]),
    EntityDefinition("Spleen", TISSUE, ["spleen"]),
]

CELL_TYPES = [
    EntityDefinition("Osteoblast", CELL_TYPE, ["osteoblast", "osteoblasts", "osteoblastic"]),
    EntityDefinition("Osteoclast", CELL_TYPE, ["osteoclast", "osteoclasts", "osteoclastic"]),
    EntityDefinition("Osteocyte", CELL_TYPE, ["osteocyte", "osteocytes", "osteocytic"]),
    EntityDefinition("Stem Cell", CELL_TYPE, ["stem cell", "stem cells", "mesenchymal stem cell", "MSC"]),
    EntityDefinition("T Cell", CELL_TYPE, ["t cell", "t cells", "t-cell", "t lymphocyte"]),
    EntityDefinition("Endothelial Cell", CELL_TYPE, ["endothelial cell", "endothelial cells"]),
    EntityDefinition("Cardiomyocyte", CELL_TYPE, ["cardiomyocyte", "cardiomyocytes"]),
    EntityDefinition("Fibroblast", CELL_TYPE, ["fibroblast", "fibroblasts"]),
]

PROCESSES = [
    EntityDefinition("Gene Expression", BIOLOGICAL_PROCESS, ["gene expression", "transcriptional response", "differential expression"]),
    EntityDefinition("Oxidative Stress", BIOLOGICAL_PROCESS, ["oxidative stress", "reactive oxygen species", "ROS"]),
    EntityDefinition("Apoptosis", BIOLOGICAL_PROCESS, ["apoptosis", "apoptotic", "programmed cell death"]),
    EntityDefinition("Bone Loss", BIOLOGICAL_PROCESS, ["bone loss", "bone resorption", "osteopenia", "osteoporosis", "bone mineral density loss"]),
    EntityDefinition("Muscle Atrophy", BIOLOGICAL_PROCESS, ["muscle atrophy", "muscle wasting", "sarcopenia"]),
    EntityDefinition("DNA Damage", BIOLOGICAL_PROCESS, ["dna damage", "dna double-strand break", "genomic instability"]),
    EntityDefinition("Inflammation", BIOLOGICAL_PROCESS, ["inflammation", "inflammatory response"]),
    EntityDefinition("Immune Dysregulation", BIOLOGICAL_PROCESS, ["immune dysregulation", "immune suppression", "immunosuppression"]),
    EntityDefinition("Circadian Rhythm", BIOLOGICAL_PROCESS, ["circadian rhythm", "circadian clock"]),
    EntityDefinition("Cell Proliferation", BIOLOGICAL_PROCESS, ["cell proliferation", "proliferative capacity"]),
]

# Genes com nomenclatura irregular, que o padrão genérico não pega ou pega mal.
CURATED_GENES = [
    EntityDefinition("TP53", GENE, ["tp53", "p53"]),
    EntityDefinition("CDKN1A", GENE, ["cdkn1a", "cdkn1a/p21", "p21"]),
    EntityDefinition("NFKB1", GENE, ["nf-κb", "nf-kb", "nfkb", "nf-kappab"]),
    EntityDefinition("IL6", GENE, ["il-6", "il6", "interleukin-6"]),
    EntityDefinition("TNF", GENE, ["tnf-α", "tnf-alpha", "tnfa"]),
    EntityDefinition("SOD1", GENE, ["sod1", "superoxide dismutase 1"]),
    # "CAT" isolado é ambíguo ("CAT scan", o animal), embora no corpus apareça
    # como o gene, ao lado de Sod1 e Prdx6. Casamos só a forma inequívoca.
    EntityDefinition("CAT", GENE, ["catalase"], match_canonical=False),
    EntityDefinition("MYOD1", GENE, ["myod", "myod1"]),
    EntityDefinition("RUNX2", GENE, ["runx2", "cbfa1"]),
]

DICTIONARY: List[EntityDefinition] = [
    *ORGANISMS,
    *CONDITIONS,
    *MISSIONS,
    *SPACECRAFT,
    *TISSUES,
    *CELL_TYPES,
    *PROCESSES,
    *CURATED_GENES,
]


# --------------------------------------------------------------------- #
# Padrões estruturados — alta precisão, sem dicionário
# --------------------------------------------------------------------- #
# Identificadores com forma canônica própria. Não precisam de ML nem de lista:
# a própria notação já é a evidência.

@dataclass(frozen=True)
class PatternRule:
    """Um padrão que reconhece entidades pela forma, não pela lista."""

    label: str
    pattern: Pattern
    canonical_template: str
    description: str


PATTERN_RULES = [
    PatternRule(
        MISSION,
        re.compile(r"\bSTS-(\d{1,3})\b"),
        "STS-{0}",
        "Missões do Space Shuttle: STS-107, STS-135",
    ),
    PatternRule(
        DATASET,
        re.compile(r"\bGLDS-(\d{1,4})\b"),
        "GLDS-{0}",
        "NASA GeneLab Data System",
    ),
    PatternRule(
        DATASET,
        re.compile(r"\bOSD-(\d{1,4})\b"),
        "OSD-{0}",
        "NASA Open Science Data Repository",
    ),
    PatternRule(
        DATASET,
        re.compile(r"\bGSE(\d{3,7})\b"),
        "GSE{0}",
        "NCBI Gene Expression Omnibus",
    ),
    PatternRule(
        DATASET,
        re.compile(r"\bPRJNA(\d{5,8})\b"),
        "PRJNA{0}",
        "NCBI BioProject",
    ),
]

# Nome de gene por convenção de nomenclatura, quando seguido de palavra que
# confirma o sentido. Sem o contexto à direita, siglas como "NASA" e "ISS"
# entrariam como genes — medido: o contexto derruba o falso positivo.
GENE_PATTERN = re.compile(
    r"\b([A-Z][A-Z0-9]{1,7}(?:-\d)?)\b(?=\s+(?:gene|genes|mRNA|protein|expression|transcript|knockout|deficient))"
)

# Siglas frequentes no corpus que a regra acima capturaria por engano.
#
# O segundo bloco foi acrescentado depois de auditar em contexto os genes que a
# extração produziu no corpus inteiro. Todas são siglas que aparecem seguidas
# de "protein" ou "gene" mas nomeiam ESTRUTURAS ou CATEGORIAS, não genes:
# "ECM proteinases" (matriz extracelular), "AMR genes" (classe de resistência),
# "EV proteins" (vesícula extracelular), "VLP core" (partícula tipo vírus).
#
# Ficaram de fora do stoplist, por serem famílias gênicas legítimas conferidas
# no texto: SAHS ("secretory-abundant heat soluble proteins", tardígrados),
# CGR3 ("pectin methyltransferases CGR2 or CGR3", Arabidopsis) e REC
# ("REC gene family mutants").
GENE_STOPLIST: Set[str] = {
    "ISS", "NASA", "ESA", "JAXA", "DNA", "RNA", "PCR", "ATP", "ADP", "GCR", "HZE",
    "ROS", "MRI", "CT", "US", "UV", "LED", "GLDS", "OSD", "GEO", "NCBI", "PMC",
    "STS", "EVA", "ECLSS", "ISSN", "DOI", "RPM", "HLU", "MSC", "SD", "SEM", "ANOVA",
    "WT", "KO", "PBS", "BSA", "FBS", "IACUC", "NIH", "FDA", "GO", "KEGG", "OMIM",
    # estruturas e categorias, não genes — auditadas no corpus
    "ECM", "AMR", "EV", "EVS", "VLP", "VLPS", "MSEV", "LPS", "BMD", "CFU",
    "OD", "PPE", "HEPA", "TEM", "SEM", "AFM", "NMR", "MS", "LC", "GC",
    # bancos de dados e ensaios que apareceram na consulta relacional final
    "ENSEMBL", "BCA", "ELISA", "BLAST", "UNIPROT", "REFSEQ", "SNP", "QTL",
    "CDNA", "MRNA", "SIRNA", "SHRNA", "CRISPR", "QPCR", "RTPCR", "FACS",
}


# --------------------------------------------------------------------- #
# Relações
# --------------------------------------------------------------------- #

MENTIONS = "MENTIONS"

# Relações tipadas do §17 que a V1 consegue derivar por agregação, sem
# extração adicional: se um organismo é mencionado em muitos chunks de uma
# publicação, a publicação estuda aquele organismo.
DERIVED_RELATIONS = {
    ORGANISM: "STUDIES",
    BIOLOGICAL_PROCESS: "INVESTIGATES",
    DATASET: "USES_DATASET",
    EXPERIMENTAL_CONDITION: "EXPOSED_TO",
}

# Menções mínimas numa publicação para que a relação tipada seja criada. Uma
# citação de passagem não significa que o artigo estuda aquilo.
DERIVED_RELATION_MIN_MENTIONS = 3


def dictionary_by_label() -> Dict[str, List[EntityDefinition]]:
    """Agrupa o dicionário por tipo de nó."""
    grouped: Dict[str, List[EntityDefinition]] = {}
    for definition in DICTIONARY:
        grouped.setdefault(definition.label, []).append(definition)
    return grouped


def ontology_summary() -> Dict[str, int]:
    """Quantas entidades canônicas há por tipo."""
    summary = {label: 0 for label in ENTITY_LABELS}
    for definition in DICTIONARY:
        summary[definition.label] += 1
    return summary
