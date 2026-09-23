"""
Calibração do limiar de evidência (EVIDENCE_THRESHOLD)

Mede a separação entre perguntas que o corpus responde e perguntas que ele
não responde, e sugere o piso que a Dra. Aris deve usar para recusar.

POR QUE ISTO PRECISA SER UMA FERRAMENTA
---------------------------------------
O limiar não é uma constante universal: depende do modelo de embeddings, do
corpus e do idioma. Trocar qualquer um dos três invalida a calibração
anterior. Aconteceu duas vezes neste projeto:

  - o limiar 0,72 foi calibrado só com perguntas em inglês, e recusava 3 de 4
    perguntas em português sobre temas que ESTÃO no corpus;
  - a migração para multilingual-e5-small mudou a escala inteira dos scores.

Sem medir, o limiar vira chute — e um limiar errado ou cala a assistente
sobre o que ela sabe, ou a faz responder sobre o que não sabe.

COMO LER O RESULTADO
--------------------
O bom limiar fica no VÃO entre o pior caso do domínio e o melhor caso de fora.
Quanto maior o vão, mais robusta a decisão. Vão estreito ou invertido
significa que o modelo não separa os dois grupos, e nenhum limiar resolve.

Uso:
    python calibrate_threshold.py
    python calibrate_threshold.py --top-k 3      # média dos 3 melhores
"""

from __future__ import annotations

import argparse
import logging
import sys
from typing import Dict, List, Optional, Tuple

from config import MissingCredentialError

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

log = logging.getLogger("calibrate_threshold")

# Perguntas que o corpus DEVE responder — temas centrais, verificados como
# presentes. Pares PT/EN do mesmo conteúdo, para expor viés de idioma.
IN_DOMAIN: List[Tuple[str, str]] = [
    ("Como a microgravidade afeta a densidade óssea?",
     "How does microgravity affect bone density?"),
    ("Quais genes respondem à radiação espacial?",
     "Which genes respond to space radiation?"),
    ("O que acontece com o sistema imune em voo espacial?",
     "What happens to the immune system during spaceflight?"),
    ("Como as plantas crescem no espaço?",
     "How do plants grow in space?"),
    ("Quais são os efeitos da microgravidade nos músculos?",
     "What are the effects of microgravity on muscle?"),
    ("Como o voo espacial altera a expressão gênica em camundongos?",
     "How does spaceflight alter gene expression in mice?"),
    ("O que se sabe sobre estresse oxidativo em astronautas?",
     "What is known about oxidative stress in astronauts?"),
    ("Quais experimentos foram feitos com Arabidopsis na ISS?",
     "Which experiments were done with Arabidopsis on the ISS?"),
]

# Perguntas que o corpus NÃO responde. Precisam ser plausíveis como perguntas
# — não texto aleatório —, senão a medição fica otimista demais.
OUT_OF_DOMAIN: List[Tuple[str, str]] = [
    ("Qual a melhor receita de pizza napolitana?",
     "What is the best recipe for Neapolitan pizza?"),
    ("Quem ganhou a Copa do Mundo de 2018?",
     "Who won the 2018 FIFA World Cup?"),
    ("Como conserto uma torneira que está vazando?",
     "How do I fix a leaking kitchen faucet?"),
    ("Qual é a capital de Portugal?",
     "What is the capital of Portugal?"),
    ("Como declarar imposto de renda?",
     "How do I file my income tax return?"),
    ("Qual o melhor framework de JavaScript para front-end?",
     "What is the best JavaScript framework for front-end?"),
]


def score_question(repo, embed, question: str, top_k: int) -> float:
    """Melhor similaridade (ou média dos top_k) que a pergunta alcança."""
    hits = repo.semantic_search(embed(question), top_k=top_k)
    if not hits:
        return 0.0
    if top_k == 1:
        return hits[0].score
    return sum(h.score for h in hits) / len(hits)


def suggest_threshold(in_scores: List[float], out_scores: List[float]) -> Dict:
    """
    Sugere o limiar a partir da separação medida.

    O ponto médio do vão maximiza a margem dos dois lados. Quando os grupos se
    sobrepõem não existe limiar sem erro, e isso é reportado em vez de
    escondido atrás de um número.
    """
    worst_in = min(in_scores)
    best_out = max(out_scores)
    gap = worst_in - best_out

    if gap <= 0:
        return {
            "threshold": None,
            "gap": gap,
            "worst_in": worst_in,
            "best_out": best_out,
            "separable": False,
        }

    return {
        # Arredondado para duas casas: precisão maior sugeriria uma exatidão
        # que 14 perguntas não sustentam.
        "threshold": round((worst_in + best_out) / 2, 2),
        "gap": gap,
        "worst_in": worst_in,
        "best_out": best_out,
        "separable": True,
    }


def run(top_k: int) -> int:
    from embedder import EmbeddingService
    from graph_manager import build_driver
    from retrieval import RetrievalRepository

    print("=" * 78)
    print("Calibração do limiar de evidência")
    print("=" * 78)

    service = EmbeddingService()
    driver = build_driver()

    try:
        repo = RetrievalRepository(driver)
        stats = repo.corpus_stats()
        print(f"\nModelo : {service.model_name}")
        print(f"Corpus : {stats['publications']} publicações · {stats['chunks']:,} chunks")
        print(f"Métrica: {'melhor score' if top_k == 1 else f'média dos {top_k} melhores'}\n")

        results: Dict[str, Dict[str, List[float]]] = {
            "in": {"pt": [], "en": []},
            "out": {"pt": [], "en": []},
        }

        for group, pairs, label in (
            ("in", IN_DOMAIN, "NO DOMÍNIO"),
            ("out", OUT_OF_DOMAIN, "FORA DO DOMÍNIO"),
        ):
            print(f"--- {label} ---")
            print(f"  {'pergunta':<50}{'PT':>8}{'EN':>8}{'Δ':>8}")
            for pt, en in pairs:
                score_pt = score_question(repo, service.embed_query, pt, top_k)
                score_en = score_question(repo, service.embed_query, en, top_k)
                results[group]["pt"].append(score_pt)
                results[group]["en"].append(score_en)
                print(
                    f"  {pt[:48]:<50}{score_pt:>8.3f}{score_en:>8.3f}"
                    f"{score_pt - score_en:>8.3f}"
                )
            print()

    finally:
        driver.close()

    # ---------------------------------------------------------------- #
    print("=" * 78)
    print("ANÁLISE")
    print("=" * 78)

    all_in = results["in"]["pt"] + results["in"]["en"]
    all_out = results["out"]["pt"] + results["out"]["en"]

    print(f"\n{'grupo':<22}{'mín':>9}{'média':>9}{'máx':>9}")
    for label, scores in (
        ("no domínio  PT", results["in"]["pt"]),
        ("no domínio  EN", results["in"]["en"]),
        ("fora        PT", results["out"]["pt"]),
        ("fora        EN", results["out"]["en"]),
    ):
        print(
            f"{label:<22}{min(scores):>9.3f}{sum(scores)/len(scores):>9.3f}{max(scores):>9.3f}"
        )

    # Viés de idioma: se PT pontua sistematicamente abaixo de EN no domínio,
    # um limiar único penaliza quem pergunta em português.
    bias = [p - e for p, e in zip(results["in"]["pt"], results["in"]["en"])]
    mean_bias = sum(bias) / len(bias)
    print(f"\nViés PT−EN no domínio: {mean_bias:+.3f} (média)")
    if mean_bias < -0.05:
        print("  ATENÇÃO: perguntas em português pontuam sistematicamente menos.")
        print("  Um limiar calibrado só em inglês recusaria perguntas válidas em PT.")
    else:
        print("  Os dois idiomas pontuam de forma comparável.")

    verdict = suggest_threshold(all_in, all_out)
    print()
    if verdict["separable"]:
        print(f"Pior caso no domínio : {verdict['worst_in']:.3f}")
        print(f"Melhor caso fora     : {verdict['best_out']:.3f}")
        print(f"Vão                  : {verdict['gap']:.3f}")
        print()
        print(f"  LIMIAR SUGERIDO: {verdict['threshold']:.2f}")
        print(f"  EVIDENCE_THRESHOLD={verdict['threshold']:.2f}")
        if verdict["gap"] < 0.05:
            print("\n  Vão estreito: a decisão é frágil. Acrescente perguntas ao")
            print("  conjunto antes de confiar neste número.")
    else:
        print(f"Pior caso no domínio : {verdict['worst_in']:.3f}")
        print(f"Melhor caso fora     : {verdict['best_out']:.3f}")
        print()
        print("  OS GRUPOS SE SOBREPÕEM. Nenhum limiar separa os dois sem erro.")
        print("  Ou o corpus não cobre parte das perguntas ditas do domínio,")
        print("  ou o modelo não discrimina bem — investigar antes de escolher.")

    print("=" * 78)
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Calibra o limiar de evidência")
    parser.add_argument(
        "--top-k",
        type=int,
        default=1,
        help="1 usa o melhor score; >1 usa a média dos N melhores",
    )
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )

    try:
        return run(top_k=args.top_k)
    except MissingCredentialError as error:
        print(f"\nCONFIGURAÇÃO AUSENTE:\n{error}")
        return 2
    except Exception as error:  # noqa: BLE001
        print(f"\nERRO: {type(error).__name__}: {error}")
        import traceback

        traceback.print_exc()
        return 3


if __name__ == "__main__":
    sys.exit(main())
