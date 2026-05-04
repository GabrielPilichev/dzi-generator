"""
Минимален Ollama API клиент.

Wrapper около Ollama HTTP API:
  - chat() — за многоходови разговори (правилно използва chat template)
  - generate() — еднократно generation (raw, без template)
  - embed() — embeddings (за RAG)

По default ползва BgGPT-v1.0:9b-q8 за chat и nomic-embed-text за embeddings.

Употреба:
    from agents.ollama_client import OllamaClient
    client = OllamaClient()
    response = client.chat([{"role": "user", "content": "Здрасти"}])
    print(response)
"""

from __future__ import annotations

import json
import time
from typing import Optional
from urllib import request as urlrequest
from urllib.error import URLError, HTTPError


# Default models
DEFAULT_CHAT_MODEL = "s_emanuilov/BgGPT-v1.0:9b-q8"
DEFAULT_FAST_MODEL = "s_emanuilov/BgGPT-v1.0:2.6b"
DEFAULT_EMBED_MODEL = "nomic-embed-text"
DEFAULT_HOST = "http://localhost:11434"

# Recommended params per BgGPT model card
BGGPT_PARAMS = {
    "temperature": 0.1,
    "top_k": 25,
    "top_p": 1.0,
    "repeat_penalty": 1.1,
}


class OllamaError(Exception):
    pass


class OllamaClient:
    def __init__(self, host: str = DEFAULT_HOST, timeout: int = 120):
        self.host = host.rstrip("/")
        self.timeout = timeout
    
    # ------------------------------------------------------------
    # Low-level HTTP
    # ------------------------------------------------------------
    
    def _post_json(self, endpoint: str, payload: dict) -> dict:
        url = f"{self.host}{endpoint}"
        data = json.dumps(payload).encode("utf-8")
        req = urlrequest.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlrequest.urlopen(req, timeout=self.timeout) as resp:
                body = resp.read().decode("utf-8")
                return json.loads(body)
        except HTTPError as e:
            msg = e.read().decode("utf-8", errors="replace")
            raise OllamaError(f"HTTP {e.code} from {url}: {msg}")
        except URLError as e:
            raise OllamaError(
                f"Не мога да достигна Ollama на {self.host}. "
                f"Стартирай: ollama serve\n   {e}"
            )
        except json.JSONDecodeError as e:
            raise OllamaError(f"Невалиден JSON отговор: {e}")
    
    # ------------------------------------------------------------
    # High-level methods
    # ------------------------------------------------------------
    
    def chat(self,
             messages: list,
             model: str = DEFAULT_CHAT_MODEL,
             options: Optional[dict] = None,
             system: Optional[str] = None) -> dict:
        """
        Chat с дадения модел.
        
        messages: [{"role": "user|assistant|system", "content": "..."}]
        system: ако е дадено, добавя се като първо message с role="system"
        options: override на default params
        
        Връща dict с keys:
          - content: текстовия отговор (string)
          - model: името на модела
          - elapsed_seconds: реално време
          - eval_count: брой генерирани токени
          - prompt_eval_count: брой prompt токени
        """
        opts = dict(BGGPT_PARAMS)
        if options:
            opts.update(options)
        
        msgs = []
        if system:
            msgs.append({"role": "system", "content": system})
        msgs.extend(messages)
        
        payload = {
            "model": model,
            "messages": msgs,
            "stream": False,
            "options": opts,
        }
        
        t0 = time.monotonic()
        result = self._post_json("/api/chat", payload)
        elapsed = time.monotonic() - t0
        
        msg = result.get("message", {})
        return {
            "content": msg.get("content", "").strip(),
            "model": result.get("model", model),
            "elapsed_seconds": round(elapsed, 2),
            "eval_count": result.get("eval_count", 0),
            "prompt_eval_count": result.get("prompt_eval_count", 0),
            "raw": result,
        }
    
    def generate(self,
                 prompt: str,
                 model: str = DEFAULT_CHAT_MODEL,
                 options: Optional[dict] = None,
                 system: Optional[str] = None) -> dict:
        """Single-prompt generation. Same response shape as chat()."""
        opts = dict(BGGPT_PARAMS)
        if options:
            opts.update(options)
        
        payload = {
            "model": model,
            "prompt": prompt,
            "stream": False,
            "options": opts,
        }
        if system:
            payload["system"] = system
        
        t0 = time.monotonic()
        result = self._post_json("/api/generate", payload)
        elapsed = time.monotonic() - t0
        
        return {
            "content": result.get("response", "").strip(),
            "model": result.get("model", model),
            "elapsed_seconds": round(elapsed, 2),
            "eval_count": result.get("eval_count", 0),
            "prompt_eval_count": result.get("prompt_eval_count", 0),
            "raw": result,
        }
    
    def embed(self,
              text: str,
              model: str = DEFAULT_EMBED_MODEL) -> list:
        """Връща embedding вектор (list of floats)."""
        payload = {
            "model": model,
            "prompt": text,
        }
        result = self._post_json("/api/embeddings", payload)
        return result.get("embedding", [])
    
    def list_models(self) -> list:
        """Връща списък инсталирани модели."""
        url = f"{self.host}/api/tags"
        req = urlrequest.Request(url, method="GET")
        try:
            with urlrequest.urlopen(req, timeout=self.timeout) as resp:
                body = resp.read().decode("utf-8")
                data = json.loads(body)
                return data.get("models", [])
        except (URLError, HTTPError) as e:
            raise OllamaError(f"Не мога да получа списък с модели: {e}")
    
    def is_alive(self) -> bool:
        """Quick check дали Ollama е достъпен."""
        try:
            self.list_models()
            return True
        except OllamaError:
            return False
