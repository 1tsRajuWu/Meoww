"""The loop: take one question, answer it here, hand it back.

The shape of one question, end to end:

1. **Poll.** Ask the Space whether anything is waiting. The answer to "are you
   switched off" travels on every poll, including the empty ones, so the bot can
   say *switched off* rather than *offline* when that is the truth.
2. **Read.** Fetch the picture, if there is one, in memory. Search, if a provider
   is configured, and read the first few pages.
3. **Draft.** One completion, grounded in those pages, in the lane the question
   belongs to.
4. **Check.** One more completion that is given the draft and the sources and told
   to strike whatever they do not support. This is the pass that makes "verified"
   mean something, so it runs whenever there is anything to verify against.
5. **Record.** ``public`` only: one line in this machine's own history file.
6. **Answer.** Post it back. The Space edits the Discord card.

Everything slow is injectable — the transport, the model, the web, the picture
fetch — for two reasons that are really one: this half runs on a laptop that CI
does not have, so the way to keep it honest is to test it against fakes, and the
way to keep it working on somebody's actual machine is to have exactly one seam
per network call.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

from decat import config, copy, protocol
from decat.mac import net, ollama as ollama_module, prompts
from decat.mac.store import History, summary
from decat.mac.tools import image as image_tool
from decat.mac.tools import web

#: A question shorter than this, with no picture attached, goes to the fast model
#: when one is configured: the big model's advantage is not visible at the length
#: of a sentence, and the small one answers in a fraction of the time.
SHORT_QUESTION = 160


class Transport:
    """How this laptop talks to the Space, and the only place it presents a secret."""

    def __init__(self, *, url: str, token: str, timeout: float = 60.0, post=None, get=None):
        self.url = url.rstrip("/")
        self.token = token
        self.timeout = float(timeout)
        self._post = post or self._default_post
        self._get = get or self._default_get

    def _allowed(self) -> tuple[str, ...]:
        from urllib.parse import urlsplit

        return (urlsplit(self.url).hostname or "",)

    def _default_get(self, url: str, timeout: float):
        return net.get_json(
            url, allowed=self._allowed(), headers={"X-Decat-Token": self.token}, timeout=timeout
        )

    def _default_post(self, url: str, payload: dict, timeout: float):
        return net.post_json(
            url,
            payload,
            allowed=self._allowed(),
            headers={"X-Decat-Token": self.token},
            timeout=timeout,
        )

    def next(self, state: str, *, wait: float | None = None) -> dict | None:
        """The poll. Returns the job payload, or ``None`` when there is nothing."""
        held = config.poll_wait() if wait is None else float(wait)
        payload = self._get(
            f"{self.url}/api/decat/next?state={state}&wait={held:g}", self.timeout
        )
        if not isinstance(payload, dict):
            return None
        job = payload.get("job")
        return job if isinstance(job, dict) else None

    def answer(self, payload: dict) -> bool:
        try:
            reply = self._post(f"{self.url}/api/decat/answer", payload, self.timeout)
        except Exception as exc:  # noqa: BLE001 — the ledger is the Space's, not ours
            return _report_failure(exc)
        return bool(isinstance(reply, dict) and reply.get("ok"))


def _report_failure(exc: Exception) -> bool:
    """The answer could not be delivered — said out loud, without any words."""
    print(f"[DECAT] could not hand the answer back ({type(exc).__name__})", flush=True)
    return False


@dataclass
class Switch:
    """The on/off state, as the agent sees it. Owned by ``control``."""

    on: bool = True

    def to_wire(self) -> str:
        return "on" if self.on else "off"


class Agent:
    """One question at a time, because there is one GPU."""

    def __init__(
        self,
        *,
        transport: Transport,
        llm=None,
        switch: Switch | None = None,
        gather: Callable[..., web.WebResult] | None = None,
        picture: Callable[..., image_tool.Picture] | None = None,
        history: History | None = None,
        log: Callable[[str], None] = print,
        clock: Callable[[], float] = time.time,
    ):
        self.transport = transport
        self.switch = switch or Switch()
        self.llm = llm
        self._gather = gather or web.gather
        self._picture = picture or image_tool.fetch
        self.history = history or History(config.home() / "history")
        self.log = log
        self.clock = clock
        self.handled = 0

    # ── the model ────────────────────────────────────────────────────────────

    def client(self):
        """The Ollama client, built on first use so a test never needs one."""
        if self.llm is None:
            self.llm = ollama_module.Ollama(
                host=config.ollama_host(), model=config.model()
            )
        return self.llm

    def model_for(self, job: protocol.Job) -> str:
        """The fast lane for a short question with no picture, else the big one."""
        fast = config.fast_model()
        if (
            fast
            and fast != config.model()
            and not job.image_url
            and len(job.question) <= SHORT_QUESTION
        ):
            return fast
        return config.model()

    # ── one question ─────────────────────────────────────────────────────────

    def answer_job(self, job: protocol.Job) -> protocol.Answer:
        """Do the work. Never raises: an answer that says what failed is an answer."""
        lane = copy.lane_of(job.question)
        started = self.clock()

        if job.deadline and started > job.deadline:
            return protocol.Answer(
                id=job.id, text="", lane=lane,
                error="this one arrived too late to answer",
                ms=int((self.clock() - started) * 1000),
            )

        picture = image_tool.Picture()
        if job.image_url:
            picture = self._picture(job.image_url)

        result = web.WebResult(provider="none")
        if config.search_provider() != "none":
            try:
                result = self._gather(
                    job.question,
                    provider=config.search_provider(),
                    api_key=config.search_key(),
                    searxng_url=config.searxng_url(),
                )
            except Exception as exc:  # noqa: BLE001 — searching is best-effort, by design
                result.failures.append(f"web: {type(exc).__name__}")

        sources = result.sources()
        block = web.sources_block(result)
        model = self.model_for(job)
        spent_ms = 0
        try:
            client = self.client()
            draft = client.chat(
                prompts.draft_messages(
                    job.question, block, lane=lane, image_b64=picture.data or None
                ),
                model=model,
            )
            spent_ms += draft.total_ms
            text, checked = draft.text, False
            if result.grounded:
                reply = client.chat(
                    prompts.verify_messages(job.question, draft.text, block), model=model
                )
                spent_ms += reply.total_ms
                text, checked = prompts.parse_verify(reply.text, draft.text)
            model = draft.model or model
        except ollama_module.OllamaError as exc:
            return protocol.Answer(
                id=job.id, text="", lane=lane, error=str(exc),
                ms=int((self.clock() - started) * 1000),
            )

        note = ""
        if picture.error:
            note = f"\n\n_(I couldn't read the picture you attached: {picture.error}.)_"
        answer = protocol.Answer(
            id=job.id,
            text=text + note,
            sources=sources,
            model=model,
            ms=spent_ms or int((self.clock() - started) * 1000),
            lane=lane,
            verified=checked,
            ungrounded=not result.grounded,
        )
        if job.saves:
            # The one write this half makes, and it is inside the `public` branch
            # so a Private question cannot reach a disk even by a mistake here.
            try:
                self.history.append(job, answer, when=started)
            except OSError as exc:
                self.log(f"[DECAT] history not written ({type(exc).__name__})")
        return answer

    # ── the loop ─────────────────────────────────────────────────────────────

    def run_once(self) -> bool:
        """One poll. Returns whether a question was answered."""
        try:
            payload = self.transport.next(self.switch.to_wire())
        except Exception as exc:  # noqa: BLE001 — a broken network is not a crash
            self.log(f"[DECAT] poll failed ({type(exc).__name__})")
            return False
        if not payload:
            return False
        try:
            job = protocol.Job.from_wire(payload)
        except (KeyError, TypeError, ValueError):
            # A payload this version cannot read is the Space's problem to report;
            # nothing here can answer it, and it carries words this half will not log.
            self.log("[DECAT] a question arrived in a shape this version cannot read")
            return False

        answer = self.answer_job(job)
        delivered = self.transport.answer(answer.to_wire())
        self.handled += 1
        facts = dict(summary(job, answer))
        facts["delivered"] = delivered
        # Counts and timings only — never a question, an answer or a user id. See
        # the module docstring of `decat/relay.py` for why this is not paranoia.
        self.log("[DECAT] " + " ".join(f"{key}={value}" for key, value in facts.items()))
        return True

    def loop(self, stop: threading.Event | None = None) -> None:
        """Poll until told to stop. The switch is checked here, not inside."""
        while stop is None or not stop.is_set():
            try:
                answered = self.run_once()
            except Exception as exc:  # noqa: BLE001 — this loop outlives every bug in it
                self.log(f"[DECAT] unexpected {type(exc).__name__} while answering")
                answered = False
            if not answered and stop is not None:
                stop.wait(0.2)


def from_config(**overrides: Any) -> Agent:
    """The agent a real laptop runs, wired from the environment."""
    token = config.token()
    if not token:
        raise RuntimeError(
            "No credential: set DECAT_TOKEN (or ADMIN_PASSKEY) to the same value "
            "the host has."
        )
    transport = overrides.pop("transport", None) or Transport(
        url=config.space_url(), token=token
    )
    return Agent(transport=transport, **overrides)
