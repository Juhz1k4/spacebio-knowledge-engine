# -*- coding: utf-8 -*-
"""
SPACEBIO E3-06 — metadados bibliográficos via Crossref

PARA QUE ISTO EXISTE
--------------------
O grafo sabe o título e a revista de cada publicação, porque isso veio do
metadata do PMC. Não sabe AUTORES nem ANO — e sem esses dois não se monta uma
citação. Uma referência ABNT sem autor e sem ano não é uma referência; é um
link com texto ao redor.

Este módulo busca o registro bibliográfico completo a partir do DOI, que 488
das 493 publicações já têm gravado.

POR QUE INGESTÃO PRÉVIA, E NÃO CONSULTA SOB DEMANDA
---------------------------------------------------
A alternativa seria um endpoint que o frontend chama por cartão de fonte. Foi
descartada por três motivos, nesta ordem de peso:

  1. LATÊNCIA. Seis cartões por resposta são seis idas à rede, cada uma
     sujeita à fila do Crossref. A resposta da Dra. Aris já compete com a
     variação do LLM; somar outra dependência externa ao caminho de leitura
     é trocar um problema conhecido por dois.

  2. A DEMONSTRAÇÃO. O corpus é fixo e conhecido: 493 publicações que não
     mudam. Buscar em tempo real um dado imutável significa que a apresentação
     passa a depender de o Crossref estar no ar naquele minuto.

  3. O DADO NÃO MUDA. Autor e ano de um artigo publicado são imutáveis. Cachear
     o que muda é otimização; cachear o que nunca muda é apenas não desperdiçar.

O custo é um job de ~2 minutos que roda uma vez. Ver `enrich_metadata.py`.

SOBRE O "POLITE POOL"
---------------------
O Crossref serve requisições anônimas por uma fila mais lenta e sem garantia.
Quem se identifica com um e-mail no User-Agent entra no "polite pool", que é
mais rápido e avisa antes de bloquear.

O e-mail NÃO está no código de propósito: vai por `CROSSREF_MAILTO` no .env.
Endereço de contato é dado pessoal, não pertence ao versionamento, e um
repositório público transformaria o campo num alvo de coleta.

Sem a variável o módulo funciona igual, apenas pela fila anônima.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

from config import settings

log = logging.getLogger(__name__)

CROSSREF_API = "https://api.crossref.org/works"

# 0,12s entre requisições = ~8/s. O Crossref tolera bem mais no polite pool,
# mas 488 DOIs a 8/s levam ~60s -- não há o que ganhar apertando, e um job
# educado é um job que não é bloqueado no meio.
REQUEST_DELAY = 0.12
REQUEST_TIMEOUT = 20

# Duas tentativas além da primeira. Falha de rede pontual não deveria custar
# uma publicação sem metadados, mas insistir mais que isso num DOI que o
# Crossref não tem só arrasta o job.
MAX_RETRIES = 2
RETRY_BACKOFF = 1.5

CACHE_FILE = "crossref_metadata.json"

# Estados possíveis de uma busca. Gravados no grafo para que a próxima
# execução saiba o que já foi tentado e não repita o que não tem solução.
STATUS_ENRICHED = "enriched"      # o Crossref devolveu o registro
STATUS_NOT_FOUND = "not_found"    # DOI válido na forma, inexistente lá
STATUS_NO_DOI = "no_doi"          # a publicação não tem DOI para consultar
STATUS_ERROR = "error"            # falha de rede ou resposta inesperada


def _user_agent() -> str:
    """
    Identificação enviada ao Crossref.

    Com `CROSSREF_MAILTO` definido, entra no polite pool. Sem ela, a fila é
    a anônima -- funciona, só é mais lenta e sem aviso prévio de bloqueio.
    """
    base = "SpaceBio-KnowledgeEngine/1.0 (NASA Space Apps 2025)"
    mailto = os.getenv("CROSSREF_MAILTO", "").strip()
    return f"{base} mailto:{mailto}" if mailto else base


@dataclass
class PublicationMetadata:
    """
    Registro bibliográfico de uma publicação, no formato que a citação usa.

    Os campos são os exigidos por ABNT e BibTeX. Todos são opcionais porque
    o Crossref devolve registros incompletos com frequência -- um preprint
    sem volume, um artigo sem número de página -- e a formatação precisa
    lidar com ausência sem quebrar.
    """

    doi: str
    status: str
    # "Sobrenome, Nome" -- forma canônica única, para que o frontend não
    # precise saber como o Crossref estrutura nomes. Ver _format_authors.
    authors: List[str] = field(default_factory=list)
    title: Optional[str] = None
    container_title: Optional[str] = None  # revista/periódico
    year: Optional[int] = None
    volume: Optional[str] = None
    issue: Optional[str] = None
    pages: Optional[str] = None
    publisher: Optional[str] = None
    url: Optional[str] = None

    @property
    def is_usable(self) -> bool:
        """Tem o mínimo para virar uma citação: ao menos autor OU ano."""
        return self.status == STATUS_ENRICHED and bool(self.authors or self.year)


def _format_authors(raw: Any) -> List[str]:
    """
    Converte a lista de autores do Crossref para "Sobrenome, Nome".

    O Crossref entrega `[{"given": "Jane", "family": "Doe"}, ...]`, mas nem
    sempre: consórcios vêm como `{"name": "The ISS Consortium"}` e alguns
    registros trazem só o sobrenome. As três formas aparecem no nosso corpus.

    A normalização para uma string única é deliberada: o frontend formata
    ABNT e BibTeX a partir daqui, e fazê-lo entender o esquema do Crossref
    espalharia esse conhecimento por dois repositórios.
    """
    if not isinstance(raw, list):
        return []

    authors: List[str] = []
    for person in raw:
        if not isinstance(person, dict):
            continue
        family = (person.get("family") or "").strip()
        given = (person.get("given") or "").strip()
        name = (person.get("name") or "").strip()

        if family and given:
            authors.append(f"{family}, {given}")
        elif family:
            authors.append(family)
        elif name:
            # Consórcio ou instituição: entra inteiro, sem vírgula, e a
            # formatação reconhece isso pela ausência dela.
            authors.append(name)
    return authors


def _extract_year(message: Dict[str, Any]) -> Optional[int]:
    """
    Ano de publicação, tentando os campos na ordem de confiabilidade.

    `issued` é a data oficial de publicação. `published-print` e
    `published-online` existem quando as duas versões saíram em anos
    diferentes -- caso comum em periódicos científicos, e a razão de não
    bastar ler um campo só.
    """
    for campo in ("issued", "published-print", "published-online", "created"):
        partes = (message.get(campo) or {}).get("date-parts") or []
        if partes and partes[0] and partes[0][0]:
            try:
                return int(partes[0][0])
            except (TypeError, ValueError):
                continue
    return None


def _first(value: Any) -> Optional[str]:
    """
    Primeiro item, quando o Crossref devolve lista onde caberia uma string.

    `title` e `container-title` são SEMPRE listas na API, mesmo com um único
    elemento -- ler como string devolveria o primeiro caractere.
    """
    if isinstance(value, list):
        return str(value[0]).strip() if value else None
    if isinstance(value, str):
        return value.strip() or None
    return None


class CrossrefCache:
    """
    Cache em disco das respostas do Crossref.

    Existe para que reexecutar o job não refaça 488 requisições. Também torna
    o desenvolvimento offline possível: uma vez baixado, o conteúdo serve sem
    rede.
    """

    def __init__(self, path: Optional[Path] = None):
        self.path = path or (settings.data_dir / CACHE_FILE)
        self.entries: Dict[str, Dict[str, Any]] = {}
        self.hits = 0
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            self.entries = json.loads(self.path.read_text(encoding="utf-8"))
            log.info("Cache Crossref: %d registro(s)", len(self.entries))
        except (OSError, ValueError) as error:
            # Cache ilegível não é motivo para abortar: o job refaz as
            # requisições e sobrescreve.
            log.warning("Cache Crossref ilegível (%s); será refeito.", error)
            self.entries = {}

    def get(self, doi: str) -> Optional[Dict[str, Any]]:
        entry = self.entries.get(doi.lower())
        if entry is not None:
            self.hits += 1
        return entry

    def put(self, doi: str, payload: Dict[str, Any]) -> None:
        self.entries[doi.lower()] = payload

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(self.entries, ensure_ascii=False, indent=1), encoding="utf-8"
        )


class CrossrefClient:
    """Cliente do Crossref, com cache, ritmo controlado e retry limitado."""

    def __init__(self, cache: Optional[CrossrefCache] = None):
        self.cache = cache if cache is not None else CrossrefCache()
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": _user_agent()})
        self.requests_made = 0
        self._last_request = 0.0

    def _wait(self) -> None:
        """Respeita REQUEST_DELAY contando do fim da requisição anterior."""
        elapsed = time.monotonic() - self._last_request
        if elapsed < REQUEST_DELAY:
            time.sleep(REQUEST_DELAY - elapsed)

    def fetch(self, doi: str) -> PublicationMetadata:
        """
        Busca o registro de um DOI.

        NUNCA levanta: devolve PublicationMetadata com `status` descrevendo o
        que houve. Um DOI ausente no Crossref é um resultado legítimo do job,
        não um erro que deva interrompê-lo no meio de 488 publicações.
        """
        doi = (doi or "").strip()
        if not doi:
            return PublicationMetadata(doi="", status=STATUS_NO_DOI)

        cached = self.cache.get(doi)
        if cached is not None:
            return self._parse(doi, cached)

        for tentativa in range(MAX_RETRIES + 1):
            try:
                self._wait()
                response = self.session.get(
                    f"{CROSSREF_API}/{requests.utils.quote(doi, safe='')}",
                    timeout=REQUEST_TIMEOUT,
                )
                self.requests_made += 1
                self._last_request = time.monotonic()

                if response.status_code == 404:
                    # O DOI existe na forma, mas o Crossref não o tem. Cacheia
                    # o negativo: sem isso, toda reexecução refaz a requisição
                    # que já se sabe que falha.
                    self.cache.put(doi, {})
                    return PublicationMetadata(doi=doi, status=STATUS_NOT_FOUND)

                response.raise_for_status()
                message = response.json().get("message") or {}
                self.cache.put(doi, message)
                return self._parse(doi, message)

            except requests.RequestException as error:
                if tentativa >= MAX_RETRIES:
                    log.warning("Crossref falhou para %s: %s", doi, error)
                    return PublicationMetadata(doi=doi, status=STATUS_ERROR)
                time.sleep(RETRY_BACKOFF ** tentativa)

        return PublicationMetadata(doi=doi, status=STATUS_ERROR)

    def _parse(self, doi: str, message: Dict[str, Any]) -> PublicationMetadata:
        """Converte a resposta do Crossref no nosso registro."""
        if not message:
            return PublicationMetadata(doi=doi, status=STATUS_NOT_FOUND)

        return PublicationMetadata(
            doi=doi,
            status=STATUS_ENRICHED,
            authors=_format_authors(message.get("author")),
            title=_first(message.get("title")),
            container_title=_first(message.get("container-title")),
            year=_extract_year(message),
            volume=_first(message.get("volume")),
            issue=_first(message.get("issue")),
            pages=_first(message.get("page")),
            publisher=_first(message.get("publisher")),
            url=message.get("URL") or f"https://doi.org/{doi}",
        )
