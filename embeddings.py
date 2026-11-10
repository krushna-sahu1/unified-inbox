"""Cloud embeddings for search by meaning (OpenAI, Google Gemini, Cohere, Voyage AI).

The user picks a provider and pastes its key; there is no local model. Every provider listed
here has multilingual models; test search on your own Hindi/Hinglish chats to compare them.
Vectors are requested at 512 dimensions where the model allows a size (smaller and cheaper to
store, ample for chat search). Requests use only the standard library.

Request formats (checked against the providers' docs, Nov 2026, except OpenAI whose docs could
not be fetched; its long-standing /v1/embeddings format is used):
  OpenAI  POST api.openai.com/v1/embeddings          {model, input[], dimensions}
  Gemini  POST .../v1beta/models/{m}:batchEmbedContents {requests:[{model, content, output_dimensionality}]}
          gemini-embedding-2 takes task prefixes in the text instead of task_type.
  Cohere  POST api.cohere.com/v2/embed               {model, texts[], input_type, embedding_types, output_dimension}
  Voyage  POST api.voyageai.com/v1/embeddings        {input[], model, input_type, output_dimension}
"""

from __future__ import annotations

import json
from typing import Any, Callable, Optional
from urllib import error as urlerror
from urllib import parse, request

from .client import InboxError

DIMS = 512
DEFAULT_MODELS = {
    "openai": "text-embedding-3-small",
    "gemini": "gemini-embedding-2",
    "cohere": "embed-v5.0-fast",
    "voyage": "voyage-4-lite",
}
_BATCH = {"openai": 256, "gemini": 100, "cohere": 96, "voyage": 128}
_MAX_INPUT_CHARS = 6000  # chunks are far smaller; this only guards against oversized input

Post = Callable[[str, dict, dict], dict]


def _http_post(url: str, headers: dict, body: dict, timeout: float = 30.0) -> dict:
    req = request.Request(url, data=json.dumps(body).encode("utf-8"), method="POST",
                          headers={**headers, "Content-Type": "application/json", "Accept": "application/json"})
    try:
        with request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read() or b"{}")
    except urlerror.HTTPError as exc:
        try:
            payload = json.loads(exc.read() or b"{}")
        except ValueError:
            payload = {}
        finally:
            exc.close()  # the error response holds the connection open until closed
        err = payload.get("error") if isinstance(payload, dict) else None
        msg = (err.get("message") if isinstance(err, dict) else err) or payload.get("message") or exc.reason
        raise InboxError(f"embedding provider answered {exc.code}: {str(msg)[:200]}", status=exc.code,
                         kind="embedding") from None
    except (urlerror.URLError, TimeoutError, OSError) as exc:
        raise InboxError(f"could not reach the embedding provider: {exc}", kind="embedding") from None


class Embedder:
    def __init__(self, provider: str, api_key: str, model: str = "", *, dims: int = DIMS,
                 post: Optional[Post] = None):
        if provider not in DEFAULT_MODELS:
            raise InboxError(f"Unknown embedding provider {provider!r}. Use openai, gemini, cohere or voyage.",
                             kind="setup")
        self.provider = provider
        self.model = model or DEFAULT_MODELS[provider]
        self.dims = dims
        self._key = api_key
        self._post = post or _http_post

    @property
    def name(self) -> str:
        return f"{self.provider}:{self.model}"

    def embed(self, texts: list[str], *, query: bool = False) -> list[list[float]]:
        out: list[list[float]] = []
        size = _BATCH[self.provider]
        for i in range(0, len(texts), size):
            batch = [(t or " ")[:_MAX_INPUT_CHARS] for t in texts[i:i + size]]
            vecs = getattr(self, f"_{self.provider}")(batch, query)
            if len(vecs) != len(batch) or not all(vecs):
                raise InboxError("the embedding provider returned an unexpected answer", kind="embedding")
            out.extend(vecs)
        return out

    def _openai(self, texts: list[str], query: bool) -> list[list[float]]:
        data = self._post("https://api.openai.com/v1/embeddings", {"Authorization": f"Bearer {self._key}"},
                          {"model": self.model, "input": texts, "dimensions": self.dims})
        items = sorted(data.get("data", []), key=lambda d: d.get("index", 0))
        return [d.get("embedding") or [] for d in items]

    def _gemini(self, texts: list[str], query: bool) -> list[list[float]]:
        model = self.model if self.model.startswith("models/") else f"models/{self.model}"
        legacy = "001" in self.model or "text-embedding" in self.model  # these take task_type
        reqs = []
        for t in texts:
            item: dict[str, Any] = {"model": model, "output_dimensionality": self.dims}
            if not legacy:  # no task_type on gemini-embedding-2: the task goes in the text
                t = f"task: search result | query: {t}" if query else f"title: none | text: {t}"
            else:
                item["taskType"] = "RETRIEVAL_QUERY" if query else "RETRIEVAL_DOCUMENT"
            item["content"] = {"parts": [{"text": t}]}
            reqs.append(item)
        url = f"https://generativelanguage.googleapis.com/v1beta/{parse.quote(model)}:batchEmbedContents"
        data = self._post(url, {"x-goog-api-key": self._key}, {"requests": reqs})
        return [(e or {}).get("values") or [] for e in data.get("embeddings", [])]

    def _cohere(self, texts: list[str], query: bool) -> list[list[float]]:
        body: dict[str, Any] = {"model": self.model, "texts": texts, "embedding_types": ["float"],
                                "input_type": "search_query" if query else "search_document"}
        if "v3" not in self.model:  # v4 and newer take a size; v3 has a fixed one
            body["output_dimension"] = self.dims
        data = self._post("https://api.cohere.com/v2/embed", {"Authorization": f"Bearer {self._key}"}, body)
        return list((data.get("embeddings") or {}).get("float") or [])

    def _voyage(self, texts: list[str], query: bool) -> list[list[float]]:
        body: dict[str, Any] = {"input": texts, "model": self.model, "input_type": "query" if query else "document"}
        if "multilingual-2" not in self.model:  # fixed-size legacy model
            body["output_dimension"] = self.dims
        data = self._post("https://api.voyageai.com/v1/embeddings", {"Authorization": f"Bearer {self._key}"}, body)
        items = sorted(data.get("data", []), key=lambda d: d.get("index", 0))
        return [d.get("embedding") or [] for d in items]
