"""Chat clients.

Every client has one method:

    complete(messages, tag="") -> Completion

`messages` is the usual list of {"role": ..., "content": ...}. `tag` names the
change-set the call belongs to; it is what recordings and replays are keyed on.

ChatClient      talks to any OpenAI-compatible /chat/completions endpoint
                (Gemini, Groq, OpenRouter, OpenAI, a local server, ...).
RecordingClient wraps another client and appends every exchange to a file.
ReplayClient    answers from such a file without any network or API key.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Protocol

GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"
DEFAULT_MODEL = "gemini-3.8-flash"


class LlmError(RuntimeError):
    """The model could not be reached or answered unusably. Stops the recipe:
    retrying an edit cannot fix a bad key, a dead network or a spent quota."""

    fatal = True


class BudgetExceeded(LlmError):
    """The run's request or token budget is spent. Stops the recipe; never retried."""

    fatal = True


@dataclass
class Completion:
    text: str
    prompt_tokens: int = 0
    completion_tokens: int = 0


@dataclass
class Budget:
    """A hard ceiling for one run, so a loop can never burn through a quota."""

    max_requests: int = 40
    max_tokens: int = 2_000_000
    requests: int = 0
    tokens: int = 0

    def check(self) -> None:
        if self.requests >= self.max_requests:
            raise BudgetExceeded(f"request budget spent ({self.requests}/{self.max_requests})")
        if self.tokens >= self.max_tokens:
            raise BudgetExceeded(f"token budget spent ({self.tokens}/{self.max_tokens})")

    def spend(self, completion: Completion) -> None:
        self.requests += 1
        self.tokens += completion.prompt_tokens + completion.completion_tokens


class Client(Protocol):
    budget: Budget

    def complete(self, messages: list[dict], tag: str = "") -> Completion: ...


@dataclass
class ChatClient:
    base_url: str
    model: str
    api_key: str
    budget: Budget = field(default_factory=Budget)
    requests_per_minute: float = 10.0  # stay under free-tier per-minute limits
    max_retries: int = 4
    timeout: float = 180.0
    temperature: float = 0.0
    sleep: Callable[[float], None] = time.sleep
    _last_request: float = field(default=0.0, repr=False)

    def __repr__(self) -> str:  # never print the key
        return f"ChatClient(base_url={self.base_url!r}, model={self.model!r})"

    def complete(self, messages: list[dict], tag: str = "") -> Completion:
        self.budget.check()
        url = self.base_url.rstrip("/") + "/chat/completions"
        body = json.dumps({"model": self.model, "messages": messages, "temperature": self.temperature}).encode()
        headers = {"Content-Type": "application/json", "Authorization": f"Bearer {self.api_key}"}
        delay = 2.0
        for attempt in range(self.max_retries + 1):
            self._throttle()
            try:
                request = urllib.request.Request(url, data=body, headers=headers, method="POST")
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                completion = _parse(payload)
                self.budget.spend(completion)
                return completion
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", "replace")[:300]
                retryable = exc.code == 429 or 500 <= exc.code < 600
                if not retryable or attempt == self.max_retries:
                    hint = " (rate limit or daily quota reached)" if exc.code == 429 else ""
                    raise LlmError(f"HTTP {exc.code} from {self.model}{hint}: {detail}") from None
                wait = _retry_after(exc.headers.get("Retry-After")) or delay
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
                if attempt == self.max_retries:
                    raise LlmError(f"could not reach {self.base_url}: {exc}") from None
                wait = delay
            self.sleep(min(wait, 60.0))
            delay *= 2
        raise LlmError("unreachable")  # pragma: no cover

    def _throttle(self) -> None:
        if self.requests_per_minute <= 0:
            return
        gap = 60.0 / self.requests_per_minute
        elapsed = time.monotonic() - self._last_request
        if self._last_request and elapsed < gap:
            self.sleep(gap - elapsed)
        self._last_request = time.monotonic()


def _retry_after(value: str | None) -> float | None:
    try:
        return max(0.0, float(value)) if value else None
    except ValueError:
        return None


def _parse(payload: dict) -> Completion:
    try:
        content = payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        raise LlmError(f"unexpected response shape: {json.dumps(payload)[:300]}") from None
    if isinstance(content, list):  # some servers return a list of content parts
        content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
    usage = payload.get("usage") or {}
    return Completion(content or "", int(usage.get("prompt_tokens") or 0), int(usage.get("completion_tokens") or 0))


@dataclass
class RecordingClient:
    """Pass-through that appends each exchange to a JSON-lines file."""

    inner: Client
    path: Path

    @property
    def budget(self) -> Budget:
        return self.inner.budget

    def complete(self, messages: list[dict], tag: str = "") -> Completion:
        completion = self.inner.complete(messages, tag)
        attempt = sum(m["role"] == "assistant" for m in messages) + 1
        with open(self.path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps({"tag": tag, "attempt": attempt, "response": completion.text}) + "\n")
        return completion


@dataclass
class ReplayClient:
    """Answers from recorded responses: {tag: [first attempt, second attempt, ...]}.

    Used by the tests and by `--replay`, so the whole pipeline can run with no
    network, no key and no cost.
    """

    responses: dict[str, list[str]]
    budget: Budget = field(default_factory=lambda: Budget(max_requests=10_000))
    calls: list[tuple[str, list[dict]]] = field(default_factory=list)

    @classmethod
    def from_file(cls, path: Path) -> "ReplayClient":
        responses: dict[str, list[str]] = {}
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            if line.strip():
                entry = json.loads(line)
                responses.setdefault(entry["tag"], []).append(entry["response"])
        return cls(responses)

    def complete(self, messages: list[dict], tag: str = "") -> Completion:
        self.budget.check()
        attempt = sum(m["role"] == "assistant" for m in messages)
        self.calls.append((tag, [dict(m) for m in messages]))  # a snapshot, not the live list
        queue = self.responses.get(tag, [])
        if attempt >= len(queue):
            raise LlmError(f"no recorded response for '{tag}' attempt {attempt + 1}")
        completion = Completion(queue[attempt])
        self.budget.spend(completion)
        return completion


def client_from_env(budget: Budget | None = None, environ: dict | None = None) -> ChatClient:
    """Build a ChatClient from REFACTOR_PILOT_* environment variables."""
    env = os.environ if environ is None else environ
    key = env.get("REFACTOR_PILOT_API_KEY") or env.get("GEMINI_API_KEY")
    if not key:
        raise LlmError(
            "no API key. Set REFACTOR_PILOT_API_KEY (see docs/PHASE4_GUIDE.md), "
            "or use --replay to run from a recording."
        )
    return ChatClient(
        base_url=env.get("REFACTOR_PILOT_BASE_URL") or GEMINI_BASE_URL,
        model=env.get("REFACTOR_PILOT_MODEL") or DEFAULT_MODEL,
        api_key=key,
        budget=budget or Budget(),
        requests_per_minute=float(env.get("REFACTOR_PILOT_RPM") or 10),
    )
