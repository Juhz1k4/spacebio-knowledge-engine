"""
Camada de provedores de LLM (Master Briefing §31)

O SpaceBio não deve depender de um único fornecedor. Esta é a fronteira: tudo
acima dela (aris.py, main.py) fala com `LLMProvider`, nunca com um SDK
específico.

Providers:
    GeminiProvider  Google Gemini, o provider atual do projeto
    EchoProvider    determinístico, para testes sem rede nem custo

Nota sobre embeddings: o §31 recomenda separar LLMProvider de
EmbeddingProvider, e é o que fazemos — os embeddings vivem em embedder.py e
são locais (sentence-transformers), independentes de qualquer API.
"""

from __future__ import annotations

import logging
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import List, Optional


log = logging.getLogger(__name__)

DEFAULT_GEMINI_MODEL = os.getenv("LLM_MODEL", "gemini-flash-latest")

# 4096, não 1024. Os modelos Gemini atuais gastam o orçamento de saída com
# raciocínio interno antes de escrever: medido neste projeto, 968-984 tokens de
# "thinking" por resposta. Com max_output_tokens=1024 sobravam 36 tokens e a
# resposta era cortada no meio de uma citação — o pior modo de falha possível
# aqui, porque uma citação truncada some na verificação e a resposta parece
# apenas "não fundamentada", sem revelar a causa.
DEFAULT_MAX_OUTPUT_TOKENS = int(os.getenv("LLM_MAX_OUTPUT_TOKENS", "4096"))

# Códigos de finish_reason do Gemini que interessam ao contrato de evidência.
FINISH_REASONS = {
    1: "STOP",
    2: "MAX_TOKENS",
    3: "SAFETY",
    4: "RECITATION",
    5: "OTHER",
}


class LLMError(RuntimeError):
    """Falha ao gerar texto com o provedor."""


class LLMUnavailable(LLMError):
    """O provedor não está configurado (credencial ausente, SDK faltando)."""


@dataclass
class LLMResponse:
    """Resposta de um provedor, com o que precisamos para observabilidade (§29)."""

    text: str
    model: str
    provider: str
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    finish_reason: Optional[str] = None


class LLMProvider(ABC):
    """Interface mínima que a Dra. Aris precisa de um modelo de linguagem."""

    name: str = "abstract"

    @abstractmethod
    def generate(self, system_prompt: str, user_prompt: str) -> LLMResponse:
        """Gera uma resposta. Deve levantar LLMError em caso de falha."""

    @abstractmethod
    def health(self) -> dict:
        """Estado do provedor, para o endpoint de health."""


class GeminiProvider(LLMProvider):
    """
    Provedor Google Gemini.

    Usa o pacote `google-generativeai`, que está descontinuado pelo fornecedor
    mas segue funcional. A migração para `google-genai` fica isolada aqui
    dentro — é exatamente o que esta camada existe para permitir.
    """

    name = "gemini"

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: str = DEFAULT_GEMINI_MODEL,
        temperature: float = 0.2,
        max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
    ):
        """
        Args:
            temperature: 0.2 de propósito. Uma assistente que precisa se ater
                às passagens não deve ser criativa; temperatura alta é o que
                faz um modelo "completar" lacunas com conhecimento próprio.
        """
        self.api_key = api_key or os.getenv("GOOGLE_API_KEY")
        self.model_name = model
        self.temperature = temperature
        self.max_output_tokens = max_output_tokens
        self._model = None

    def _ensure_model(self):
        if self._model is not None:
            return self._model

        if not self.api_key:
            raise LLMUnavailable(
                "GOOGLE_API_KEY não está definida. Preencha o .env para a Dra. Aris responder."
            )
        try:
            import google.generativeai as genai
        except ImportError as error:  # pragma: no cover
            raise LLMUnavailable(f"google-generativeai não instalado: {error}") from error

        genai.configure(api_key=self.api_key)
        self._model = genai.GenerativeModel(
            self.model_name,
            generation_config={
                "temperature": self.temperature,
                "max_output_tokens": self.max_output_tokens,
            },
        )
        log.info("Gemini configurado (modelo=%s, temp=%.2f)", self.model_name, self.temperature)
        return self._model

    def generate(self, system_prompt: str, user_prompt: str) -> LLMResponse:
        model = self._ensure_model()
        # O SDK atual não separa system de user; concatenar preserva a ordem
        # que o prompt assume (regras antes das passagens).
        prompt = f"{system_prompt}\n\n---\n\n{user_prompt}"

        try:
            response = model.generate_content(prompt)
        except Exception as error:  # noqa: BLE001
            raise LLMError(f"Falha na chamada ao Gemini: {error}") from error

        text = getattr(response, "text", None)
        if not text or not text.strip():
            raise LLMError("Gemini devolveu resposta vazia (possível bloqueio de segurança).")

        usage = getattr(response, "usage_metadata", None)

        # finish_reason vira dado do contrato: MAX_TOKENS significa texto
        # cortado, e uma resposta cortada pode ter perdido citações.
        finish = None
        candidates = getattr(response, "candidates", None)
        if candidates:
            raw = getattr(candidates[0], "finish_reason", None)
            if raw is not None:
                finish = FINISH_REASONS.get(int(raw), str(raw))

        return LLMResponse(
            text=text.strip(),
            model=self.model_name,
            provider=self.name,
            input_tokens=getattr(usage, "prompt_token_count", None),
            output_tokens=getattr(usage, "candidates_token_count", None),
            finish_reason=finish,
        )

    def health(self) -> dict:
        return {
            "provider": self.name,
            "model": self.model_name,
            "configured": bool(self.api_key),
        }


class EchoProvider(LLMProvider):
    """
    Provedor determinístico para testes.

    Não chama rede. Produz uma resposta que cita as passagens indicadas em
    `cite`, o que permite testar a verificação de citações — inclusive o caso
    de citação fabricada, passando um índice fora do intervalo.
    """

    name = "echo"

    def __init__(self, cite: Optional[List[int]] = None, text: Optional[str] = None):
        self.cite = cite if cite is not None else [1]
        self.text = text
        self.calls: List[dict] = []

    def generate(self, system_prompt: str, user_prompt: str) -> LLMResponse:
        self.calls.append({"system": system_prompt, "user": user_prompt})
        if self.text is not None:
            body = self.text
        else:
            citations = "".join(f"[{index}]" for index in self.cite)
            body = f"Resposta de teste fundamentada nas passagens {citations}."
        return LLMResponse(text=body, model="echo-1", provider=self.name)

    def health(self) -> dict:
        return {"provider": self.name, "model": "echo-1", "configured": True}


def default_provider() -> LLMProvider:
    """
    Escolhe o provedor a partir do ambiente.

    Não levanta erro quando a chave falta: quem decide o que fazer sem LLM é a
    camada acima, que pode continuar servindo recuperação de evidência mesmo
    sem geração de texto.
    """
    return GeminiProvider()
