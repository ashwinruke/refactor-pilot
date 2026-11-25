"""The chat client, against a real local HTTP server (no internet needed)."""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from refactor_pilot.llm import (
    Budget, BudgetExceeded, ChatClient, LlmError, LlmUnavailable, RecordingClient, ReplayClient, client_from_env,
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


class FakeClock:
    """Time that only moves when the client sleeps, so waiting tests run instantly."""

    def __init__(self):
        self.now, self.sleeps = 1000.0, []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


def client_for(api, **kw):
    clock = FakeClock()
    kw.setdefault("requests_per_minute", 0)
    client = ChatClient(api.url, "test-model", "secret-key", sleep=clock.sleep, clock=clock, **kw)
    return client, clock.sleeps


def error(code, message, status="X", headers=None, wrap=True):
    body = {"error": {"code": code, "message": message, "status": status}}
    return code, ([body] if wrap else body), headers or {}


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
    assert sleeps == [7.0, 10.0]  # the server's Retry-After first, then the doubling backoff
    assert client.budget.requests == 1  # only the answered request counts


def test_gives_up_after_the_waiting_window_with_a_clean_message():
    api = FakeApi([error(503, "This model is currently experiencing high demand.", "UNAVAILABLE")] * 5)
    client, sleeps = client_for(api, max_wait=40)
    with pytest.raises(LlmUnavailable) as caught:
        client.complete([{"role": "user", "content": "x"}])
    message = str(caught.value)
    assert "HTTP 503 overloaded: This model is currently experiencing high demand." in message
    assert "\n" not in message and "[{" not in message  # one readable line, not raw JSON
    assert sleeps == [5.0, 10.0, 20.0] and sum(sleeps) <= 40
    assert LlmUnavailable.fatal


def test_logs_each_wait():
    api = FakeApi([error(503, "busy"), ok("fine")])
    lines = []
    client, _ = client_for(api, log=lines.append)
    client.complete([{"role": "user", "content": "x"}])
    assert lines == ["test-model: HTTP 503 overloaded: busy; waiting 5s before trying again (0s of up to 300s)"]


def test_overloaded_model_falls_back_to_the_next_one_at_once():
    api = FakeApi([error(503, "high demand"), ok("from the fallback"), error(503, "still busy"), ok("again")])
    lines = []
    client, sleeps = client_for(api, fallback_models=["lite-model"], log=lines.append)
    assert client.complete([{"role": "user", "content": "x"}]).text == "from the fallback"
    assert [r["body"]["model"] for r in api.requests] == ["test-model", "lite-model"]
    assert sleeps == [] and client.last_model == "lite-model"
    assert lines == ["answered by fallback model lite-model"]
    # The preferred model is tried first again next time: overload is usually brief.
    client.complete([{"role": "user", "content": "x"}])
    assert [r["body"]["model"] for r in api.requests][2:] == ["test-model", "lite-model"]


def test_daily_quota_sets_a_model_aside_for_the_rest_of_the_run():
    quota = error(429, "Quota exceeded for metric: generate_content_free_tier_requests, "
                       "quotaId: GenerateRequestsPerDayPerProjectPerModel-FreeTier")
    api = FakeApi([quota, ok("one"), ok("two")])
    lines = []
    client, _ = client_for(api, fallback_models=["lite-model"], log=lines.append)
    client.complete([{"role": "user", "content": "x"}])
    client.complete([{"role": "user", "content": "x"}])
    assert [r["body"]["model"] for r in api.requests] == ["test-model", "lite-model", "lite-model"]
    assert client.models == ["lite-model"]
    assert "daily quota of test-model is used up; no longer trying it this run" in lines


def test_unknown_model_is_dropped_when_there_is_another():
    api = FakeApi([error(404, "models/test-model is not found for API version v1main", wrap=False), ok("ok")])
    client, sleeps = client_for(api, fallback_models=["lite-model"])
    assert client.complete([{"role": "user", "content": "x"}]).text == "ok"
    assert client.models == ["lite-model"] and sleeps == []


def test_no_model_left_is_reported():
    api = FakeApi([error(404, "models/test-model is not found")])
    client, _ = client_for(api)
    with pytest.raises(LlmError, match="no usable model left: test-model: not available"):
        client.complete([{"role": "user", "content": "x"}])


def test_bad_key_stops_immediately():
    api = FakeApi([error(403, "API key not valid. Please pass a valid API key.")])
    client, sleeps = client_for(api, fallback_models=["lite-model"])
    with pytest.raises(LlmError, match="HTTP 403 .*API key not valid.*REFACTOR_PILOT_API_KEY"):
        client.complete([{"role": "user", "content": "x"}])
    assert sleeps == [] and len(api.requests) == 1  # no point trying another model with the same key


def test_unreachable_server_is_temporary(tmp_path):
    client = ChatClient("http://127.0.0.1:9/v1/", "m", "k", requests_per_minute=0, max_wait=10,
                        sleep=lambda s: None, timeout=2)
    with pytest.raises(LlmUnavailable, match="could not reach"):
        client.complete([{"role": "user", "content": "x"}])


def test_client_error_is_not_retried():
    api = FakeApi([(400, {"error": {"message": "Invalid JSON payload received."}}, {})])
    client, sleeps = client_for(api, fallback_models=["lite-model"])
    with pytest.raises(LlmError, match="HTTP 400 from test-model: Invalid JSON payload received."):
        client.complete([{"role": "user", "content": "x"}])
    assert sleeps == [] and len(api.requests) == 1


def test_a_400_about_the_model_name_sets_it_aside():
    api = FakeApi([(400, {"error": {"message": "unknown model"}}, {})])
    client, sleeps = client_for(api)
    with pytest.raises(LlmError, match="no usable model left: test-model: not available \\(unknown model\\)"):
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
    assert "generativelanguage.googleapis.com" in client.base_url
    assert client.models == ["gemini-3.8-flash", "gemini-3.5-flash-lite"] and client.max_wait == 300
    listed = client_from_env(environ={"REFACTOR_PILOT_API_KEY": "k", "REFACTOR_PILOT_MODEL": " a , b,,c ",
                                      "REFACTOR_PILOT_MAX_WAIT": "60"})
    assert listed.models == ["a", "b", "c"] and listed.max_wait == 60
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

    assert all(json.loads(line)["model"] == "test-model" for line in (tmp_path / "run.jsonl").read_text().splitlines())
    replay = ReplayClient.from_file(tmp_path / "run.jsonl")
    assert replay.complete(first, tag="target-a").text == "first try"
    assert replay.complete(retry, tag="target-a").text == "second try"
    with pytest.raises(LlmError, match="no recorded response"):
        replay.complete(first, tag="target-b")
