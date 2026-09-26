# -*- coding: utf-8 -*-
"""
A1 — endpoint de contagens públicas (`GET /api/v1/stats`)

O que esta suíte protege é uma regra, não um número: **nenhum valor exibido
ao usuário é escrito à mão**. A página inicial já mostrou contagens inventadas
de pesquisadores e de artigos publicados; a correção não foi trocá-las pelos
valores certos digitados no código, e sim contá-las no grafo.

O repositório é substituído por um dublê. Isso NÃO é para evitar o Neo4j por
conveniência: é para que o teste verifique o que o endpoint faz com os números
— cache, formato, degradação — sem depender de quantas publicações existem
hoje. Um teste que afirme "493" quebra no dia em que o acervo crescer, que é
justamente o dia em que ele deveria continuar passando.
"""

import sys
import time

from fastapi.testclient import TestClient

import main
from main import ChatRequest  # noqa: F401  (garante que o módulo carrega)

passed = failed = 0


def check(label, condition, detail=""):
    global passed, failed
    if condition:
        passed += 1
        print(f"  [OK]    {label}")
    else:
        failed += 1
        print(f"  [FALHA] {label}   {detail}")


CONTAGENS = {
    "publications": 493,
    "chunks": 45947,
    "entities": 979,
    "organisms": 14,
    "datasets": 285,
}


class RepositorioDuble:
    """Devolve contagens fixas e conta quantas vezes foi consultado."""

    def __init__(self):
        self.consultas = 0

    def public_stats(self):
        self.consultas += 1
        return dict(CONTAGENS)


def instalar(repositorio):
    """Substitui a dependência e zera o cache entre os cenários."""
    main.app.dependency_overrides[main.get_retrieval] = lambda: repositorio
    main._stats_cache = None
    return TestClient(main.app)


print("=" * 74)
print("A1 — GET /api/v1/stats")
print("=" * 74)

# ---------------------------------------------------------------------------
print("\n1. FORMATO DA RESPOSTA")
# ---------------------------------------------------------------------------

repo = RepositorioDuble()
client = instalar(repo)
resposta = client.get("/api/v1/stats")

check("responde 200", resposta.status_code == 200, f"-> {resposta.status_code}")
corpo = resposta.json()

for campo, valor in CONTAGENS.items():
    check(f"{campo} = {valor}", corpo.get(campo) == valor, f"-> {corpo.get(campo)}")

check("traz generated_at", bool(corpo.get("generated_at")))
check("generated_at em UTC", "+00:00" in corpo.get("generated_at", ""))
check("traz o campo cached", "cached" in corpo)
check("a primeira chamada NÃO vem do cache", corpo["cached"] is False)

# O contrato existe para ser espelhado no TypeScript. Um campo a mais ou a
# menos aqui quebra silenciosamente o frontend, então a checagem é exata.
check(
    "conjunto EXATO de campos do contrato",
    set(corpo) == {*CONTAGENS, "generated_at", "cached"},
    f"-> {sorted(corpo)}",
)

# ---------------------------------------------------------------------------
print("\n2. CACHE")
# ---------------------------------------------------------------------------

repo = RepositorioDuble()
client = instalar(repo)

primeira = client.get("/api/v1/stats").json()
seguintes = [client.get("/api/v1/stats").json() for _ in range(5)]

check("o grafo foi consultado UMA vez em 6 requisições",
      repo.consultas == 1, f"-> {repo.consultas} consultas")
check("da segunda em diante, cached=True",
      all(r["cached"] is True for r in seguintes))
check("generated_at é preservado (não recalcula)",
      all(r["generated_at"] == primeira["generated_at"] for r in seguintes))
check("os números não mudam entre cache e origem",
      all(r["publications"] == primeira["publications"] for r in seguintes))

# A expiração é verificada mexendo no relógio guardado, e não dormindo 10
# minutos. O que importa é que a idade seja comparada, não que o teste espere.
main._stats_cache = (time.monotonic() - main.STATS_CACHE_SECONDS - 1, dict(primeira))
depois = client.get("/api/v1/stats").json()
check("cache expirado força nova consulta ao grafo",
      repo.consultas == 2, f"-> {repo.consultas}")
check("após expirar, cached volta a False", depois["cached"] is False)

check(f"janela padrão de {main.STATS_CACHE_SECONDS}s (10 min)",
      main.STATS_CACHE_SECONDS == 600, f"-> {main.STATS_CACHE_SECONDS}")

# ---------------------------------------------------------------------------
print("\n3. GRAFO VAZIO E INDISPONÍVEL")
# ---------------------------------------------------------------------------


class RepositorioVazio:
    def public_stats(self):
        return {"publications": 0, "chunks": 0, "entities": 0,
                "organisms": 0, "datasets": 0}


client = instalar(RepositorioVazio())
vazio = client.get("/api/v1/stats")
check("grafo vazio responde 200, não erro", vazio.status_code == 200)
check("grafo vazio devolve zeros", vazio.json()["publications"] == 0)

# Sem Neo4j, `get_retrieval` levanta 503. A pagina inicial trata isso e mostra
# "contagem indisponivel" em vez de inventar valor -- ver Hero.tsx.
main.app.dependency_overrides.clear()
main._stats_cache = None
sem_grafo = TestClient(main.app).get("/api/v1/stats")
check("sem camada de recuperação responde 503, não 500",
      sem_grafo.status_code == 503, f"-> {sem_grafo.status_code}")
check("o 503 explica o que falta",
      "Neo4j" in sem_grafo.json().get("detail", ""),
      f"-> {sem_grafo.json()}")

# ---------------------------------------------------------------------------
print("\n4. CABEÇALHOS DE SEGURANÇA TAMBÉM AQUI")
# ---------------------------------------------------------------------------

client = instalar(RepositorioDuble())
r = client.get("/api/v1/stats")
check("X-Content-Type-Options", r.headers.get("X-Content-Type-Options") == "nosniff")
check("X-Frame-Options", r.headers.get("X-Frame-Options") == "DENY")
check("rota entra no balde global do limitador",
      "X-RateLimit-Limit" in r.headers)

main.app.dependency_overrides.clear()

print("\n" + "=" * 74)
print(f"  {passed} verificações OK, {failed} falha(s)")
print("=" * 74)
sys.exit(1 if failed else 0)
