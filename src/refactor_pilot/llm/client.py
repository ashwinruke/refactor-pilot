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
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Protocol

GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"
# 3.8 Flash first; 3.5 Flash-Lite as a fallback with a much larger free daily quota.
DEFAULT_MODELS = "gemini-3.8-flash,gemini-3.5-flash-lite"


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


RETRYABLE = {429, 500, 502, 503, 504}


class LlmUnavailable(LlmError):
    """Every model kept failing with a temporary error (overloaded, rate limited, unreachable)
    for the whole waiting window. Stops the recipe; resume later with --continue."""


@dataclass
class ChatClient:
    """An OpenAI-compatible chat client built for free tiers.

    Free tiers fail in ordinary ways: a model is briefly overloaded (HTTP 503), a
    per-minute or per-day quota is spent (HTTP 429). So:

    * Several models can be given. When one fails with a temporary error, the
      next one is tried at once. A model whose *daily* quota is spent, or which
      does not exist, is set aside for the rest of the run.
    * When every model is failing, the client waits (honouring Retry-After,
      otherwise 5, 10, 20, 40, 60, 60 ... seconds) and tries again, for up to
      `max_wait` seconds per request, telling the log what it is doing.
    * Only answered requests count against the budget.
    """

    base_url: str
    model: str  # the preferred model, tried first on every request
    api_key: str
    budget: Budget = field(default_factory=Budget)
    requests_per_minute: float = 10.0  # stay under free-tier per-minute limits
    fallback_models: list[str] = field(default_factory=list)
    max_wait: float = 300.0  # seconds to keep retrying temporary failures, per request
    timeout: float = 180.0
    temperature: float = 0.0
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.monotonic
    log: Callable[[str], None] = lambda message: None
    last_model: str = ""  # the model that produced the last answer
    _last_request: float = field(default=0.0, repr=False)
    _set_aside: dict = field(default_factory=dict, repr=False)  # model -> why it is no longer tried

    def __repr__(self) -> str:  # never print the key
        return f"ChatClient(base_url={self.base_url!r}, models={self.models!r})"

    @property
    def models(self) -> list[str]:
        """Models still in use, in order of preference."""
        ordered = list(dict.fromkeys([self.model, *self.fallback_models]))
        return [m for m in ordered if m not in self._set_aside]

    def complete(self, messages: list[dict], tag: str = "") -> Completion:
        self.budget.check()
        started, delay = self.clock(), 5.0
        while True:
            problems: list[str] = []
            retry_after = 0.0
            if not self.models:
                raise LlmError("no usable model left: " + "; ".join(f"{m}: {why}" for m, why in self._set_aside.items()))
            for model in self.models:
                self._throttle()
                outcome = self._post(model, messages)
                if isinstance(outcome, Completion):
                    if model != self.model and model != self.last_model:
                        self.log(f"answered by fallback model {model}")
                    self.last_model = model
                    self.budget.spend(outcome)
                    return outcome
                code, message, raw, after = outcome
                problem = f"{model}: {_label(code)}: {message}" if code else f"{model}: {message}"
                if code in (401, 403):
                    raise LlmError(f"HTTP {code} from {model}: {message} (check REFACTOR_PILOT_API_KEY)")
                if code == 404 or (code == 400 and re.search(r"model", message, re.I)
                                   and re.search(r"not (?:found|supported|available)|invalid|unknown", message, re.I)):
                    self._set_aside[model] = f"not available ({message})"
                    self.log(f"model {model} is not available to this key; no longer trying it")
                    continue
                if code == 429 and re.search(r"per ?day|daily", raw, re.I):
                    self._set_aside[model] = "daily quota used up"
                    self.log(f"daily quota of {model} is used up; no longer trying it this run")
                    continue
                if code is not None and code not in RETRYABLE:
                    raise LlmError(f"HTTP {code} from {model}: {message}")
                problems.append(problem)
                retry_after = max(retry_after, after or 0.0)
            if not problems:
                continue  # every failure set its model aside; try the remaining ones (or report none left)
            wait = max(delay, retry_after)
            waited = self.clock() - started
            if waited + wait > self.max_wait:
                raise LlmUnavailable(
                    f"model unavailable for {waited:.0f}s ({problems[-1]}). This is usually temporary"
                )
            self.log(f"{problems[-1]}; waiting {wait:.0f}s before trying again "
                     f"({waited:.0f}s of up to {self.max_wait:.0f}s)")
            self.sleep(wait)
            delay = min(delay * 2, 60.0)

    def _post(self, model: str, messages: list[dict]):
        """Completion on success, else (HTTP status or None, message, raw body, Retry-After)."""
        url = self.base_url.rstrip("/") + "/chat/completions"
        body = json.dumps({"model": model, "messages": messages, "temperature": self.temperature}).encode()
        headers = {"Content-Type": "application/json", "Authorization": f"Bearer {self.api_key}"}
        try:
            request = urllib.request.Request(url, data=body, headers=headers, method="POST")
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", "replace")
            return exc.code, _error_message(raw), raw, _retry_after(exc.headers.get("Retry-After"))
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            return None, f"could not reach {self.base_url} ({getattr(exc, 'reason', exc)})", "", None
        except json.JSONDecodeError:
            return None, "the server sent a reply that is not JSON", "", None
        return _parse(payload)

    def _throttle(self) -> None:
        if self.requests_per_minute <= 0:
            return
        gap = 60.0 / self.requests_per_minute
        elapsed = self.clock() - self._last_request
        if self._last_request and elapsed < gap:
            self.sleep(gap - elapsed)
        self._last_request = self.clock()


def _label(code: int) -> str:
    names = {429: "HTTP 429 rate limit or quota", 500: "HTTP 500 server error", 502: "HTTP 502 bad gateway",
             503: "HTTP 503 overloaded", 504: "HTTP 504 timeout"}
    return names.get(code, f"HTTP {code}")


def _error_message(raw: str) -> str:
    """The human part of an error body, on one line. Handles Gemini's list-wrapped form."""
    try:
        data = json.loads(raw)
        if isinstance(data, list) and data:
            data = data[0]
        error = data.get("error", data) if isinstance(data, dict) else data
        message = error.get("message") if isinstance(error, dict) else error
        text = str(message or raw)
    except (ValueError, AttributeError):
        text = raw
    return " ".join(text.split())[:200] or "(empty reply)"


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
            entry = {"tag": tag, "attempt": attempt, "response": completion.text}
            if getattr(self.inner, "last_model", ""):
                entry["model"] = self.inner.last_model
            handle.write(json.dumps(entry) + "\n")
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
    models = [m.strip() for m in (env.get("REFACTOR_PILOT_MODEL") or DEFAULT_MODELS).split(",") if m.strip()]
    return ChatClient(
        base_url=env.get("REFACTOR_PILOT_BASE_URL") or GEMINI_BASE_URL,
        model=models[0],
        fallback_models=models[1:],
        api_key=key,
        budget=budget or Budget(),
        requests_per_minute=float(env.get("REFACTOR_PILOT_RPM") or 10),
        max_wait=float(env.get("REFACTOR_PILOT_MAX_WAIT") or 300),
    )
