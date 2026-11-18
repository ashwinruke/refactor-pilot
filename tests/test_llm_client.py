"""The chat client, against a real local HTTP server (no internet needed)."""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from refactor_pilot.llm import (
    Budget, BudgetExceeded, ChatClient, LlmError, RecordingClient, ReplayClient, client_from_env,
)


class FakeApi:
    """Serves /chat/completions from a queue of (status, body, headers)."""

    def __init__(self, script):
        self.script, self.requests = list(script), []
        api = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                api.requests.append({"path": self.path, "auth": self.headers.get("Authorization"), "body": body})
                status, payload, headers = api.script.pop(0)
                data = json.dumps(payload).encode()
                self.send_response(status)
                for key, value in headers.items():
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}/v1/"


def ok(text, prompt=10, completion=5):
    return 200, {"choices": [{"message": {"role": "assistant", "content": text}}],
                 "usage": {"prompt_tokens": prompt, "completion_tokens": completion}}, {}


def client_for(api, **kw):
    sleeps = []
    kw.setdefault("requests_per_minute", 0)
    return ChatClient(api.url, "test-model", "secret-key", sleep=sleeps.append, **kw), sleeps


def test_successful_request_shape_and_accounting():
    api = FakeApi([ok("hello")])
    client, _ = client_for(api)
    completion = client.complete([{"role": "user", "content": "hi"}])
    assert completion.text == "hello"
    sent = api.requests[0]
    assert sent["path"] == "/v1/chat/completions" and sent["auth"] == "Bearer secret-key"
    assert sent["body"]["model"] == "test-model" and sent["body"]["messages"][0]["content"] == "hi"
    assert (client.budget.requests, client.budget.tokens) == (1, 15)


def test_rate_limit_is_retried_after_the_servers_delay():
    api = FakeApi([(429, {"error": "slow down"}, {"Retry-After": "7"}), (503, {}, {}), ok("done")])
    client, sleeps = client_for(api)
    assert client.complete([{"role": "user", "content": "x"}]).text == "done"
    assert sleeps[0] == 7.0 and len(sleeps) == 2
    assert client.budget.requests == 1  # only the answered request counts


def test_gives_up_after_max_retries_with_a_quota_hint():
    api = FakeApi([(429, {"error": "quota"}, {})] * 3)
    client, _ = client_for(api, max_retries=2)
    with pytest.raises(LlmError, match="429.*quota"):
        client.complete([{"role": "user", "content": "x"}])


def test_client_error_is_not_retried():
    api = FakeApi([(400, {"error": {"message": "unknown model"}}, {})])
    client, sleeps = client_for(api)
    with pytest.raises(LlmError, match="400"):
        client.complete([{"role": "user", "content": "x"}])
    assert sleeps == [] and len(api.requests) == 1


def test_budget_stops_before_sending():
    api = FakeApi([ok("one")])
    client, _ = client_for(api, budget=Budget(max_requests=1))
    client.complete([{"role": "user", "content": "x"}])
    with pytest.raises(BudgetExceeded):
        client.complete([{"role": "user", "content": "x"}])
    assert len(api.requests) == 1
    assert BudgetExceeded.fatal


def test_throttle_spaces_requests():
    api = FakeApi([ok("a"), ok("b")])
    client, sleeps = client_for(api, requests_per_minute=6)  # one every 10 s
    client.complete([{"role": "user", "content": "x"}])
    client.complete([{"role": "user", "content": "x"}])
    assert len(sleeps) == 1 and 9.0 < sleeps[0] <= 10.0


def test_unexpected_response_shape_is_an_error():
    api = FakeApi([(200, {"nope": 1}, {})])
    client, _ = client_for(api)
    with pytest.raises(LlmError, match="unexpected response"):
        client.complete([{"role": "user", "content": "x"}])


def test_key_never_appears_in_repr():
    client, _ = client_for(FakeApi([]))
    assert "secret-key" not in repr(client)


def test_client_from_env():
    with pytest.raises(LlmError, match="no API key"):
        client_from_env(environ={})
    client = client_from_env(environ={"REFACTOR_PILOT_API_KEY": "k"})
    assert "generativelanguage.googleapis.com" in client.base_url and client.model.startswith("gemini")
    custom = client_from_env(environ={"REFACTOR_PILOT_API_KEY": "k", "REFACTOR_PILOT_BASE_URL": "https://api.groq.com/openai/v1",
                                      "REFACTOR_PILOT_MODEL": "m", "REFACTOR_PILOT_RPM": "30"})
    assert (custom.base_url, custom.model, custom.requests_per_minute) == ("https://api.groq.com/openai/v1", "m", 30.0)


def test_record_then_replay_roundtrip(tmp_path):
    api = FakeApi([ok("first try"), ok("second try")])
    client, _ = client_for(api)
    recorder = RecordingClient(client, tmp_path / "run.jsonl")
    first = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]
    recorder.complete(first, tag="target-a")
    retry = first + [{"role": "assistant", "content": "first try"}, {"role": "user", "content": "rejected"}]
    recorder.complete(retry, tag="target-a")

    replay = ReplayClient.from_file(tmp_path / "run.jsonl")
    assert replay.complete(first, tag="target-a").text == "first try"
    assert replay.complete(retry, tag="target-a").text == "second try"
    with pytest.raises(LlmError, match="no recorded response"):
        replay.complete(first, tag="target-b")
