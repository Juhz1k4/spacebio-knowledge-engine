"""
Testes da SPACEBIO-014 + SPACEBIO-015 — Evidência e grounding

Verifica que a Dra. Aris:
  1. produz o contrato de evidência do §14, completo e válido;
  2. recusa responder quando o corpus não sustenta (§15.7);
  3. detecta citação fabricada em vez de aceitá-la (§15.4);
  4. nunca inventa DOI nem página (§15.4, §15.5);
  5. degrada com evidência quando o modelo falha.

Usa EchoProvider: determinístico, sem rede, sem custo e sem rate limit — o que
permite testar exatamente os modos de falha que uma chamada real não deixa
reproduzir sob demanda.

Uso:
    python test_aris.py           # sem chamar a API do Gemini
    python test_aris.py --live    # inclui uma chamada real ao provedor
"""

from __future__ import annotations

import logging
import sys
from typing import List, Optional

from aris import DraAris
from config import MissingCredentialError
from evidence import (
    INSUFFICIENT_EVIDENCE,
    EvidenceAnswer,
    build_sources,
    extract_citations,
    verify_citations,
)
from graph_manager import build_driver
from llm_provider import EchoProvider, LLMError, LLMProvider, LLMResponse
from prompts import SYSTEM_PROMPT, build_user_prompt, format_passages
from retrieval import RetrievalRepository

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

log = logging.getLogger("test_aris")

IN_DOMAIN = "How does microgravity affect bone density?"
OUT_OF_DOMAIN = "What is the best recipe for Neapolitan pizza?"


class ArisTestFailure(AssertionError):
    """Falha de asserção nos testes da Dra. Aris."""


def check(condition: bool, message: str) -> None:
    if condition:
        print(f"      [OK] {message}")
    else:
        print(f"      [FALHA] {message}")
        raise ArisTestFailure(message)


def section(step: str, title: str) -> None:
    print(f"\n[{step}] {title}")


class BrokenProvider(LLMProvider):
    """Provedor que sempre falha, para testar a degradação."""

    name = "broken"

    def generate(self, system_prompt: str, user_prompt: str) -> LLMResponse:
        raise LLMError("falha simulada do provedor")

    def health(self) -> dict:
        return {"provider": self.name, "configured": False}


# ---------------------------------------------------------------------- #
# Unitários — sem banco, sem rede
# ---------------------------------------------------------------------- #


def test_citation_parsing() -> None:
    """Extração de citações cobre os formatos que o modelo produz."""
    cases = {
        "Afirmação simples [1].": [1],
        "Duas fontes [1][2].": [1, 2],
        "Lista na mesma marca [1, 3].": [1, 3],
        "Com espaço [2,  4] no meio.": [2, 4],
        "Repetida [1] e de novo [1].": [1],
        "Sem nenhuma citação aqui.": [],
        "": [],
    }
    for text, expected in cases.items():
        result = extract_citations(text)
        check(result == expected, f"{text[:34]!r} -> {result}")


def test_citation_verification() -> None:
    """Citação a fonte inexistente é detectada, não aceita."""
    from evidence import EvidenceSource

    sources = [
        EvidenceSource(
            citation_index=index,
            publication_id=f"pub{index}",
            title=f"Publicação {index}",
            passage="trecho",
            relevance=0.5,
            chunk_id=f"c{index}",
        )
        for index in (1, 2)
    ]

    valid = verify_citations("Afirmação sustentada [1] e outra [2].", sources)
    check(valid["cited"] == [1, 2], "citações válidas reconhecidas")
    check(valid["invalid"] == [], "nenhuma citação inválida")
    check(valid["used"] == 2, "chunks_used contado corretamente")
    check(all(s.cited for s in sources), "fontes marcadas como citadas")

    fabricated = verify_citations("Apoiado em [1] e em [7].", sources)
    check(fabricated["cited"] == [1], "citação válida aproveitada")
    check(fabricated["invalid"] == [7], "citação fabricada [7] detectada")
    check(sources[1].cited is False, "fonte não citada é remarcada")

    none = verify_citations("Uma afirmação sem lastro nenhum.", sources)
    check(none["used"] == 0, "resposta sem citação registra 0 usos")


def test_prompt_construction() -> None:
    """O prompt carrega as regras e as passagens numeradas."""
    from evidence import EvidenceSource

    sources = [
        EvidenceSource(
            citation_index=1,
            publication_id="Artigo X",
            title="Artigo X",
            passage="Microgravidade reduz a densidade óssea.",
            relevance=0.9,
            chunk_id="c1",
        )
    ]

    check("No evidence" in SYSTEM_PROMPT or "SEM EVIDÊNCIA" in SYSTEM_PROMPT,
          "prompt carrega o princípio 'sem evidência, sem afirmação'")
    check("DOI" in SYSTEM_PROMPT, "prompt proíbe fabricar DOI (§15.4)")
    check("página" in SYSTEM_PROMPT.lower(), "prompt proíbe fabricar página (§15.5)")

    formatted = format_passages(sources)
    check("[1]" in formatted, "passagem vem numerada")
    check("Artigo X" in formatted, "título identifica a origem")
    check("evidência:" in formatted, "o texto é rotulado como a evidência")

    user = build_user_prompt(IN_DOMAIN, sources)
    check(IN_DOMAIN in user, "a pergunta entra no prompt")
    check("[1]" in user, "as passagens entram no prompt")


# ---------------------------------------------------------------------- #
# Integração — com corpus, sem API externa
# ---------------------------------------------------------------------- #


def test_grounded_answer(repo, embeddings) -> EvidenceAnswer:
    """Resposta fundamentada produz o contrato completo."""
    provider = EchoProvider(cite=[1, 2])
    aris = DraAris(repo, embeddings, provider=provider)
    result = aris.answer(IN_DOMAIN, top_k=4)

    check(isinstance(result, EvidenceAnswer), "devolve EvidenceAnswer")
    check(result.grounded is True, "resposta marcada como fundamentada")
    check(len(result.sources) == 4, f"{len(result.sources)} fontes consideradas")
    check(result.retrieval.chunks_used == 2, "chunks_used reflete as citações reais")
    check(result.retrieval.chunks_considered == 4, "chunks_considered reflete o recuperado")
    check(result.warnings == [], "sem avisos numa resposta limpa")

    check(len(provider.calls) == 1, "o provedor foi chamado uma vez")
    prompt = provider.calls[0]["user"]
    check(IN_DOMAIN in prompt, "a pergunta chegou ao modelo")
    check("[1]" in prompt and "[4]" in prompt, "as 4 passagens foram numeradas no prompt")

    cited = result.cited_sources
    check(len(cited) == 2, f"{len(cited)} fontes marcadas como citadas")
    check([s.citation_index for s in cited] == [1, 2], "as fontes certas foram marcadas")

    for source in result.sources:
        check_doi = source.doi is None or source.doi.startswith("10.")
        check(check_doi, f"DOI real ou None em [{source.citation_index}]: {source.doi}")
        check(source.page is None, f"page é None em [{source.citation_index}] (§15.5)")
        check(bool(source.passage.strip()), f"[{source.citation_index}] traz o trecho")
        check(bool(source.channels), f"[{source.citation_index}] registra o canal de origem")

    return result


def test_refusal(repo, embeddings) -> None:
    """Fora do domínio, recusa sem sequer chamar o modelo."""
    provider = EchoProvider(cite=[1])
    aris = DraAris(repo, embeddings, provider=provider)
    result = aris.answer(OUT_OF_DOMAIN)

    check(result.grounded is False, "resposta marcada como não fundamentada")
    check(result.answer == INSUFFICIENT_EVIDENCE, "usa a mensagem padrão de insuficiência")
    check(result.sources == [], "nenhuma fonte é apresentada")
    check(result.retrieval.chunks_used == 0, "chunks_used = 0")
    check(bool(result.warnings), f"o motivo é registrado: {result.warnings[0][:50]}")
    check(
        len(provider.calls) == 0,
        "o modelo NÃO foi chamado — a trava age antes, sem gastar a API",
    )
    check(
        result.retrieval.top_score is not None
        and result.retrieval.top_score < result.retrieval.evidence_threshold,
        f"top_score {result.retrieval.top_score} < limiar {result.retrieval.evidence_threshold}",
    )


def test_fabricated_citation(repo, embeddings) -> None:
    """Citação a fonte inexistente derruba o grounding e é reportada."""
    provider = EchoProvider(text="Afirmação apoiada em [1] e em [99].")
    aris = DraAris(repo, embeddings, provider=provider)
    result = aris.answer(IN_DOMAIN, top_k=3)

    check(result.grounded is False, "resposta com citação fabricada não é fundamentada")
    check(
        any("inexistentes" in warning for warning in result.warnings),
        "a fabricação é reportada em warnings",
    )
    check("99" in " ".join(result.warnings), "o índice fabricado aparece no aviso")
    check(result.retrieval.chunks_used == 1, "só a citação válida entra em chunks_used")


def test_uncited_answer(repo, embeddings) -> None:
    """Resposta sem nenhuma citação não passa por fundamentada."""
    provider = EchoProvider(text="Uma afirmação confiante e sem nenhuma citação.")
    aris = DraAris(repo, embeddings, provider=provider)
    result = aris.answer(IN_DOMAIN, top_k=3)

    check(result.grounded is False, "sem citação, não é fundamentada")
    check(result.retrieval.chunks_used == 0, "chunks_used = 0")
    check(
        any("não citou" in warning for warning in result.warnings),
        "a ausência de citação é reportada",
    )


def test_provider_failure(repo, embeddings) -> None:
    """Modelo fora do ar: entrega a evidência em vez de erro."""
    aris = DraAris(repo, embeddings, provider=BrokenProvider())
    result = aris.answer(IN_DOMAIN, top_k=3)

    check(isinstance(result, EvidenceAnswer), "ainda devolve o contrato")
    check(result.grounded is False, "sem síntese, não é fundamentada")
    check(len(result.sources) == 3, "as passagens recuperadas são preservadas")
    # E3-04: o estado tem campo próprio. Antes isto checava um prefixo de
    # mensagem que o código controla — frágil, e quebrou quando o texto mudou.
    # O que importa verificar é o contrato: o estado e a causa do provedor.
    check(
        result.status == "synthesis_unavailable",
        "estado marcado como synthesis_unavailable",
    )
    check(
        all(source.cited for source in result.sources),
        "sem síntese, as fontes não ficam escondidas como não citadas",
    )
    check(
        result.retrieval.chunks_used == 3,
        "o rastro reflete as passagens entregues",
    )
    check(
        any("falha simulada do provedor" in warning for warning in result.warnings),
        "a causa vinda do provedor é reportada",
    )


def test_input_validation(repo, embeddings) -> None:
    """Entradas inválidas falham cedo e claramente."""
    aris = DraAris(repo, embeddings, provider=EchoProvider())
    for invalid in ("", "   "):
        try:
            aris.answer(invalid)
            check(False, f"pergunta {invalid!r} deveria falhar")
        except ValueError:
            check(True, f"pergunta {invalid!r} levanta ValueError")


def test_live_provider(repo, embeddings) -> None:
    """Uma chamada real ao provedor configurado."""
    aris = DraAris(repo, embeddings)
    result = aris.answer(IN_DOMAIN, top_k=4)

    check(bool(result.answer.strip()), "o provedor real devolveu texto")
    print(f"\n      {result.answer[:220].strip()}...\n")
    if result.grounded:
        check(result.retrieval.chunks_used > 0, f"{result.retrieval.chunks_used} fontes citadas")
        for source in result.cited_sources[:2]:
            print(f"      [{source.citation_index}] {source.title[:58]}")
            print(f"          DOI: {source.doi or 'não disponível'}")
    else:
        print(f"      (não fundamentada: {result.warnings})")


# ---------------------------------------------------------------------- #
# Orquestração
# ---------------------------------------------------------------------- #


def run(live: bool = False) -> int:
    print("=" * 78)
    print("SPACEBIO-014 + 015 — Contrato de evidência e grounding")
    print("=" * 78)

    section("1/8", "Extração de citações (unitário)")
    test_citation_parsing()

    section("2/8", "Verificação de citações (unitário)")
    test_citation_verification()

    section("3/8", "Construção do prompt (unitário)")
    test_prompt_construction()

    driver = build_driver()
    try:
        repo = RetrievalRepository(driver)
        from embedder import EmbeddingService

        embeddings = EmbeddingService()

        stats = repo.corpus_stats()
        print(f"\n      Corpus: {stats['publications']} publicações, {stats['chunks']:,} chunks")

        section("4/8", "Resposta fundamentada")
        test_grounded_answer(repo, embeddings)

        section("5/8", "Recusa por evidência insuficiente")
        test_refusal(repo, embeddings)

        section("6/8", "Citação fabricada e resposta sem citação")
        test_fabricated_citation(repo, embeddings)
        test_uncited_answer(repo, embeddings)

        section("7/8", "Degradação e validação de entrada")
        test_provider_failure(repo, embeddings)
        test_input_validation(repo, embeddings)

        section("8/8", "Provedor real")
        if live:
            test_live_provider(repo, embeddings)
        else:
            print("      (pulado — use --live para chamar a API do Gemini)")

    finally:
        driver.close()

    print("\n" + "=" * 78)
    print("SPACEBIO-014 + 015 — todas as asserções passaram.")
    print("=" * 78)
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Testes da SPACEBIO-014/015")
    parser.add_argument("--live", action="store_true", help="chamar o provedor real")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )

    try:
        return run(live=args.live)
    except ArisTestFailure as failure:
        print(f"\nTESTE REPROVADO: {failure}")
        return 1
    except MissingCredentialError as error:
        print(f"\nCONFIGURAÇÃO AUSENTE:\n{error}")
        return 2
    except Exception as error:  # noqa: BLE001
        print(f"\nERRO INESPERADO: {type(error).__name__}: {error}")
        import traceback

        traceback.print_exc()
        return 3


if __name__ == "__main__":
    sys.exit(main())
