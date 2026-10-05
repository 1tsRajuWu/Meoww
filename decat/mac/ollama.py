"""The model, on this machine, over the loopback interface and nothing else.

Ollama serves an HTTP API on ``127.0.0.1:11434``. That is the whole reason this
feature can promise what it promises: **the model runs here**, so a question is
never posted to a model vendor, is never part of anybody's training set, and does
not need a per-token bill or a privacy policy to be read first. The only thing
that leaves this laptop while answering is the search query, and that is a
setting the operator owns (``DECAT_SEARCH``).

Two smaller decisions worth stating because they are about *speed*, which is the
other half of the request:

* ``keep_alive`` asks Ollama to hold the weights in memory between questions.
  Without it the first question after an idle spell pays several gigabytes of
  load before it pays any thinking.
* ``think`` is turned off for the drafting pass where the model supports it. A
  reasoning model that thinks out loud before answering a "what is the capital
  of Peru" is slower for no gain — and the verification pass is where the care
  belongs, because that is the pass that can *check* something.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from decat import config
from decat.mac import net

#: How long the weights stay warm, and the value a caller may override per client
#: or per machine with ``DECAT_KEEP_ALIVE``.
#:
#: Long enough that a conversation in Discord never pays a reload, short enough
#: that the memory comes back — and the second half of that sentence is why this
#: is ten minutes rather than the half hour it started as: on a 16 GB laptop,
#: holding 3.2 GB of weights after the answer is *the* thing that makes the rest
#: of the machine slow.
KEEP_ALIVE = config.DEFAULT_KEEP_ALIVE

#: Context and answer-length budgets, in tokens, pinned rather than left to
#: whatever the model's own default is.
#:
#: Both are memory. The context window is the KV cache the model keeps live for
#: the whole answer, and the answer length is how long it keeps generating without
#: ever giving the laptop its GPU back — so on a fanless Air the honest thing is to
#: say the numbers out loud instead of hoping the defaults are kind. The measurements
#: behind them: a page is excerpted to 1200 characters (~300 tokens), so a `fast`
#: prompt is two pages plus instructions (4096 is roomy) while a `deep` draft is
#: three pages, and the verify pass sees the sources a third time (8192 covers it).
DRAFT_CTX = 8192
DRAFT_TOKENS = 900
VERIFY_CTX = 8192
VERIFY_TOKENS = 400
FAST_CTX = 4096
FAST_TOKENS = 700


class OllamaError(OSError):
    """The model could not be asked."""


@dataclass
class Reply:
    """One completion, with the numbers worth showing an operator."""

    text: str
    model: str = ""
    total_ms: int = 0
    eval_count: int = 0
    raw: dict = field(default_factory=dict)


class Ollama:
    """A thin client. ``post`` is injectable so the agent is testable offline."""

    def __init__(
        self,
        *,
        host: str,
        model: str,
        timeout: float = 180.0,
        keep_alive: str | None = None,
        post=None,
        get=None,
        clock=time.time,
    ):
        self.host = host.rstrip("/")
        self.model = model
        self.timeout = float(timeout)
        self.keep_alive = keep_alive or config.keep_alive()
        self._post = post or self._default_post
        self._get = get or self._default_get
        self._clock = clock

    # ── transport ────────────────────────────────────────────────────────────

    def _allowed(self) -> tuple[str, ...]:
        from urllib.parse import urlsplit

        return (urlsplit(self.host).hostname or "127.0.0.1",)

    def _default_post(self, url: str, payload: dict, timeout: float):
        return net.post_json(url, payload, allowed=self._allowed(), timeout=timeout)

    def _default_get(self, url: str, timeout: float):
        return net.get_json(url, allowed=self._allowed(), timeout=timeout)

    # ── the two calls that matter ────────────────────────────────────────────

    def chat(
        self,
        messages: list[dict],
        *,
        model: str | None = None,
        timeout: float | None = None,
        think: bool = False,
        temperature: float = 0.2,
        num_ctx: int = DRAFT_CTX,
        num_predict: int = DRAFT_TOKENS,
    ) -> Reply:
        """One completion. Raises :class:`OllamaError` when it cannot be had.

        ``num_ctx``/``num_predict`` are always sent, because both are memory and a
        default that changes under us is a memory budget that changes under us.
        """
        name = model or self.model
        payload = {
            "model": name,
            "messages": messages,
            "stream": False,
            "keep_alive": self.keep_alive,
            "options": {
                "temperature": float(temperature),
                "num_ctx": int(num_ctx),
                "num_predict": int(num_predict),
            },
        }
        if not think:
            # Ignored by models that do not reason, and it is an option rather
            # than a message for exactly that reason.
            payload["think"] = False
        started = self._clock()
        try:
            answer = self._post(
                f"{self.host}/api/chat", payload, timeout or self.timeout
            )
        except Exception as exc:  # noqa: BLE001 — one error type for every reason
            raise OllamaError(f"ollama could not answer: {type(exc).__name__}: {exc}") from exc
        if not isinstance(answer, dict):
            raise OllamaError("ollama returned something that is not an answer")
        if answer.get("error"):
            raise OllamaError(str(answer["error"]))
        message = answer.get("message") or {}
        text = str(message.get("content") or "").strip()
        if not text:
            raise OllamaError("ollama returned an empty answer")
        elapsed_ms = int((self._clock() - started) * 1000)
        reported = answer.get("total_duration")
        total_ms = int(reported / 1_000_000) if isinstance(reported, int) else elapsed_ms
        return Reply(
            text=text,
            model=str(answer.get("model") or name),
            total_ms=total_ms,
            eval_count=int(answer.get("eval_count") or 0),
            raw=answer,
        )

    def models(self) -> list[str]:
        """Every model this Ollama has pulled, by name and by tag."""
        try:
            payload = self._get(f"{self.host}/api/tags", 10.0)
        except Exception as exc:  # noqa: BLE001
            raise OllamaError(f"ollama is not answering on {self.host}: {type(exc).__name__}") from exc
        names: list[str] = []
        for entry in (payload or {}).get("models", []) if isinstance(payload, dict) else []:
            if isinstance(entry, dict) and entry.get("name"):
                names.append(str(entry["name"]))
        return names

    def has(self, model: str | None = None) -> bool:
        """Whether a model is pulled — the difference between slow and broken."""
        wanted = (model or self.model).strip()
        for name in self.models():
            if name == wanted or name.split(":")[0] == wanted.split(":")[0]:
                return True
        return False
