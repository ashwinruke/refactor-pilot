"""R2: unit tests of the deterministic parts, then end-to-end runs with a scripted model."""

import json
import shutil
from collections import Counter
import subprocess
from pathlib import Path

import pytest

from refactor_pilot.cli import main
from refactor_pilot.evalkit import score as scoremod
from refactor_pilot.llm import Budget, ReplayClient
from refactor_pilot.recipes import r2_ownership as r2
from refactor_pilot.session import Session

ROOT = Path(__file__).parent.parent
FIXTURE = ROOT / "fixtures" / "legacy_inventory"
REPLAYS = ROOT / "examples" / "replays"
TOOLS = ("cmake", "ninja", "ctest", "g++", "git", "clangd")

LOCK = "r2/manual-lock/inventory::Logger"
FILEH = "r2/file-handle/inventory::Logger"
WAREHOUSE = "r2/raw-ownership/inventory::Warehouse"
FACTORY = "r2/raw-ownership/inventory::create_item"


# ---- deterministic helpers ---------------------------------------------------


def test_mask_hides_comments_and_strings_but_keeps_positions():
    source = 'a.lock(); // b.lock()\nputs("c.lock()"); /* d.lock()\n e.lock() */ f.unlock();\n'
    assert r2.mask(source).count("\n") == source.count("\n") and len(r2.mask(source)) == len(source)
    assert [line for line, _ in r2.hits(r2._LOCK, source)] == [1, 3]


def test_delete_expression_is_not_confused_with_deleted_functions():
    source = "A(const A&) = delete;\nvoid f() { delete p; delete[] q; }\nint deleted = 1;\n"
    assert [line for line, _ in r2.hits(r2._DELETE, source)] == [2]


def test_raw_file_declaration_versus_raii_spelling():
    assert r2.hits(r2._FILE_DECL, "FILE* file_;\n") and r2.hits(r2._FILE_DECL, "FILE *f = fopen(p, \"r\");\n")
    assert r2.hits(r2._FILE_DECL, "FILE* file_{nullptr};\n")
    assert not r2.hits(r2._FILE_DECL, "std::unique_ptr<FILE, int (*)(FILE*)> file_;\n")
    assert not r2.hits(r2._FILE_DECL, "struct Close { void operator()(FILE* f) const { fclose(f); } };\n")


def test_test_files_are_recognised():
    assert all(map(r2.is_test_file, ["tests/test_main.cpp", "src/foo_test.cpp", "test/a.cpp", "x/test_util.cc"]))
    assert not any(map(r2.is_test_file, ["src/warehouse.cpp", "include/inventory/item.h", "src/latest.cpp"]))


def test_public_signatures_skip_private_members():
    sigs = r2.public_signatures((FIXTURE / "include/inventory/warehouse.h").read_text())
    assert "Item* add_item(Item* item);" in sigs and "Item* release(ItemId id);" in sigs
    assert not any("Warehouse(const Warehouse&)" in s for s in sigs)  # private, not API
    assert not any("operator=" in s for s in sigs)


def _accept(target, before, after, allow, tmp_path):
    for rel, text in after.items():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(text)
    return r2.make_accept(target, before, allow)(tmp_path)


def test_accept_requires_the_pattern_to_be_gone(tmp_path):
    before = {"l.cpp": "void f() {\n    m.lock();\n    x();\n    m.unlock();\n}\n"}
    target = r2.Target("manual-lock", "L", "class", ["l.cpp"],
                       must_go={"l.cpp": Counter({"m.lock();": 1, "m.unlock();": 1})})
    half = {"l.cpp": "void f() {\n    std::lock_guard<std::mutex> g(m);\n    x();\n    m.unlock();\n}\n"}
    assert "still present" in _accept(target, before, half, False, tmp_path)
    reworded = {"l.cpp": "void f() {\n    m.lock( );\n    x();\n    m.unlock( );\n}\n"}
    assert "still present" in _accept(target, before, reworded, False, tmp_path)
    done = {"l.cpp": "void f() {\n    std::lock_guard<std::mutex> g(m);\n    x();\n}\n"}
    assert _accept(target, before, done, False, tmp_path) is None


def test_accept_counts_only_the_targets_own_lines(tmp_path):
    """Two classes in one file both `delete p_;`. Fixing one must not be blocked by the other."""
    before = {"two.cpp": "A::~A() {\n    delete p_;\n}\nB::~B() {\n    delete p_;\n}\n"}
    target = r2.Target("raw-ownership", "A", "class", ["two.cpp"], must_go={"two.cpp": Counter({"delete p_;": 1})})
    one_fixed = {"two.cpp": "A::~A() {\n}\nB::~B() {\n    delete p_;\n}\n"}
    assert _accept(target, before, one_fixed, False, tmp_path) is None
    assert "still present" in _accept(target, before, before, False, tmp_path)


def test_accept_rejects_a_public_signature_change_unless_allowed(tmp_path):
    target = r2.Target("raw-ownership", "W", "class", ["w.h"])
    before = {"w.h": "class W {\npublic:\n    T* take(T* t);\nprivate:\n    T* t_;\n};\n"}
    after = {"w.h": "class W {\npublic:\n    T* take(std::unique_ptr<T> t);\nprivate:\n    std::unique_ptr<T> t_;\n};\n"}
    assert "changed a public signature" in _accept(target, before, after, False, tmp_path)
    assert _accept(target, before, after, True, tmp_path) is None
    private_only = {"w.h": before["w.h"].replace("    T* t_;", "    std::unique_ptr<T> t_;")}
    assert _accept(target, before, private_only, False, tmp_path) is None


def test_accept_refuses_weakened_tests(tmp_path):
    target = r2.Target("raw-ownership", "W", "class", ["w.cpp"], api_symbols=["take"],
                       callers={"tests/test_w.cpp": ["take"]})
    before = {"w.cpp": "int x;\n", "tests/test_w.cpp": "T* t = w.take(p);\nCHECK(total == 3);\ndelete t;\n"}
    adapted = {"w.cpp": "int y;\n", "tests/test_w.cpp": "std::unique_ptr<T> t = w.take(p);\nCHECK(total == 3);\n"}
    assert _accept(target, before, adapted, True, tmp_path) is None
    weakened = {"w.cpp": "int y;\n", "tests/test_w.cpp": "std::unique_ptr<T> t = w.take(p);\n"}
    assert "never weakened" in _accept(target, before, weakened, True, tmp_path)


# ---- end to end with a scripted model -----------------------------------------

needs_toolchain = pytest.mark.skipif(any(shutil.which(t) is None for t in TOOLS), reason="clangd or C++ toolchain not installed")


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture()
def repo(tmp_path):
    dest = tmp_path / "user" / "legacy_inventory"
    shutil.copytree(FIXTURE, dest)
    git(dest.parent, "init", "-q")
    git(dest.parent, "-c", "user.name=u", "-c", "user.email=u@u", "add", "-A")
    git(dest.parent, "-c", "user.name=u", "-c", "user.email=u@u", "commit", "-qm", "init")
    return dest


def recorded(name):
    return ReplayClient.from_file(REPLAYS / name).responses


def block(path, search, replace):
    return f"<<<<<<< SEARCH {path}\n{search}=======\n{replace}>>>>>>> REPLACE\n"


def run_r2(repo, tmp_path, responses, sanitizers=False, **kw):
    client = ReplayClient(responses, budget=kw.pop("budget", Budget(max_requests=100)))
    with Session(repo, work_dir=tmp_path / "work", compiler="g++", branch="rp/r2", sanitizers=sanitizers) as session:
        result = r2.run(session, client, **kw)
    return result, client, session


def show(repo, file):
    return git(repo.parent, "show", f"rp/r2:legacy_inventory/{file}")


@needs_toolchain
@pytest.mark.toolchain
def test_internal_run_with_two_rejected_first_attempts(repo, tmp_path, capsys):
    """The shipped demo: the goal check and the sanitizers each reject one first
    attempt, the rejection reason reaches the model, and the retry lands."""
    head = git(repo.parent, "rev-parse", "HEAD")
    code = main(["run", str(repo), "--recipe", "r2", "--replay", str(REPLAYS / "r2-fixture-internal.jsonl"),
                 "--manifest", str(repo / "manifest.json"), "--compiler", "g++",
                 "--branch", "rp/r2", "--work-dir", str(tmp_path / "work")])
    out = capsys.readouterr().out
    assert code == 0
    assert "3 committed, 0 rolled back, 0 reverted, 0 unchanged, 1 skipped" in out
    assert "attempt 1/3 of r2/manual-lock/inventory::Logger rejected: goal not met" in out
    assert "attempt 1/3 of r2/raw-ownership/inventory::Warehouse rejected: sanitizers" in out
    assert "Rerun with --allow-api-changes" in out
    assert "R2: 0/8 -> 4/8" in out

    assert git(repo.parent, "rev-parse", "HEAD") == head and git(repo.parent, "status", "--porcelain") == ""
    assert "lock_guard" in show(repo, "src/logger.cpp") and ".unlock()" not in show(repo, "src/logger.cpp")
    assert "std::unique_ptr<FILE" in show(repo, "include/inventory/logger.h")
    warehouse_h = show(repo, "include/inventory/warehouse.h")
    assert "std::vector<std::unique_ptr<Item> > items_;" in warehouse_h
    assert "Item* add_item(Item* item);" in warehouse_h  # public API untouched
    assert "delete" not in r2.mask(show(repo, "src/warehouse.cpp"))
    assert show(repo, "tests/test_main.cpp") == (FIXTURE / "tests/test_main.cpp").read_text().strip()  # tests untouched


@needs_toolchain
@pytest.mark.toolchain
def test_api_run_resolves_every_r2_construct(repo, tmp_path, capsys):
    code = main(["run", str(repo), "--recipe", "r2", "--allow-api-changes",
                 "--replay", str(REPLAYS / "r2-fixture-api.jsonl"), "--manifest", str(repo / "manifest.json"),
                 "--compiler", "g++", "--branch", "rp/r2", "--work-dir", str(tmp_path / "work")])
    out = capsys.readouterr().out
    assert code == 0 and "4 committed, 0 rolled back" in out and "R2: 0/8 -> 8/8" in out
    assert "std::unique_ptr<Item> create_item(" in show(repo, "include/inventory/item.h")
    assert "std::unique_ptr<Item> release(ItemId id);" in show(repo, "include/inventory/warehouse.h")
    tests = show(repo, "tests/test_main.cpp")
    assert "delete" not in r2.mask(tests) and tests.count("CHECK(") == (FIXTURE / "tests/test_main.cpp").read_text().count("CHECK(")


@needs_toolchain
@pytest.mark.toolchain
def test_retry_message_carries_the_real_failure(repo, tmp_path):
    responses = recorded("r2-fixture-internal.jsonl")
    result, client, _ = run_r2(repo, tmp_path, responses, sanitizers=True, kinds=("manual-lock", "raw-ownership"))
    # The Warehouse answers were written on top of the FILE* change; without it they still apply.
    by_tag = {}
    for tag, messages in client.calls:
        by_tag.setdefault(tag, []).append(messages)
    lock_retry = by_tag[LOCK][1][-1]["content"]
    assert "rejected and rolled back" in lock_retry and "mutex_.unlock();" in lock_retry
    leak_retry = by_tag[WAREHOUSE][1][-1]["content"]
    assert "sanitizers" in leak_retry and "LeakSanitizer" in leak_retry  # the sanitizer's own report
    assert by_tag[WAREHOUSE][1][-2]["role"] == "assistant"  # the model sees its own rejected answer
    assert [o.status for o in result.outcomes] == ["committed", "committed", "skipped"]
    assert [o.attempts for o in result.outcomes[:2]] == [2, 2]


@needs_toolchain
@pytest.mark.toolchain
def test_prompt_contains_what_the_model_needs(repo, tmp_path):
    _, client, _ = run_r2(repo, tmp_path, recorded("r2-fixture-api.jsonl"), allow_api_changes=True)
    prompts = {tag: messages[1]["content"] for tag, messages in client.calls}
    warehouse = prompts[WAREHOUSE]
    assert "C++11" in warehouse and "make_unique" in warehouse
    assert "src/warehouse.cpp:9: `delete *it;`" in warehouse  # where the pattern is
    assert "tests/test_main.cpp:" in warehouse and "add_item(" in warehouse  # the callers
    assert "### include/inventory/warehouse.h" in warehouse and "### tests/test_main.cpp" in warehouse
    assert "You MAY change public signatures" in warehouse
    # The second target sees the first target's committed result, not the original file.
    assert "std::lock_guard<std::mutex> lock(mutex_);" in prompts[FILEH]


@needs_toolchain
@pytest.mark.toolchain
def test_frozen_api_is_enforced_not_just_requested(repo, tmp_path):
    good = recorded("r2-fixture-api.jsonl")[LOCK][0]
    sneaky = good + block("include/inventory/logger.h",
                          "    void log(const char* level, const std::string& message);\n",
                          "    void log(const std::string& level, const std::string& message);\n")
    result, client, _ = run_r2(repo, tmp_path, {LOCK: [sneaky, good]}, kinds=("manual-lock",))
    outcome = result.outcomes[0]
    assert outcome.status == "committed" and outcome.attempts == 2
    assert "changed a public signature" in client.calls[1][1][-1]["content"]


@needs_toolchain
@pytest.mark.toolchain
def test_model_may_not_weaken_tests_or_touch_other_files(repo, tmp_path):
    answers = recorded("r2-fixture-api.jsonl")
    weakened = answers[WAREHOUSE][0] + block("tests/test_main.cpp", "    CHECK(warehouse.find(99) == NULL);\n", "")
    elsewhere = answers[WAREHOUSE][0] + block("src/pricing.cpp", "    return 0.10;\n", "    return 0.15;\n")
    result, client, _ = run_r2(repo, tmp_path, {WAREHOUSE: [weakened, elsewhere, answers[WAREHOUSE][0]]},
                               allow_api_changes=True, kinds=("raw-ownership",), max_attempts=3)
    warehouse = result.outcomes[0]
    assert warehouse.status == "committed" and warehouse.attempts == 3
    feedback = [messages[-1]["content"] for tag, messages in client.calls if tag == WAREHOUSE][1:]
    assert "never weakened" in feedback[0] and "CHECK(warehouse.find(99) == NULL);" in feedback[0]
    assert "not one of the files you may change" in feedback[1]


@needs_toolchain
@pytest.mark.toolchain
def test_unusable_answers_exhaust_attempts_and_leave_no_trace(repo, tmp_path):
    result, _, session = run_r2(repo, tmp_path, {LOCK: ["I think a lock_guard would be better here.", "Sure!"]},
                                kinds=("manual-lock",), max_attempts=2)
    outcome = result.outcomes[0]
    assert outcome.status == "rolled_back" and outcome.attempts == 2 and "No edit blocks found" in outcome.reason
    assert session.report.head_sha == session.report.base_sha
    assert git(repo.parent, "branch", "--list", "rp/r2") == ""


@needs_toolchain
@pytest.mark.toolchain
def test_budget_stops_the_run_and_keeps_what_was_committed(repo, tmp_path):
    result, client, _ = run_r2(repo, tmp_path, recorded("r2-fixture-api.jsonl"), budget=Budget(max_requests=1))
    assert [o.status for o in result.outcomes] == ["committed", "skipped"]
    assert result.outcomes[1].reason.startswith("not finished: request budget spent")
    assert "request budget spent" in result.stopped and client.budget.requests == 1
    assert "lock_guard" in show(repo, "src/logger.cpp")  # the first target survived
    assert "FILE* file_;" in show(repo, "include/inventory/logger.h")  # the second never ran


def test_shipped_replays_are_well_formed():
    for name in ("r2-fixture-internal.jsonl", "r2-fixture-api.jsonl"):
        for line in (REPLAYS / name).read_text().splitlines():
            entry = json.loads(line)
            assert set(entry) == {"tag", "attempt", "response"} and entry["tag"].startswith("r2/")


@needs_toolchain
@pytest.mark.toolchain
def test_continue_finishes_a_run_that_stopped_and_never_deletes_the_branch(repo, tmp_path, capsys):
    """The model goes away after one target. --continue adds the rest to the same branch."""
    answers = recorded("r2-fixture-api.jsonl")
    first = tmp_path / "first.jsonl"
    first.write_text(json.dumps({"tag": LOCK, "attempt": 1, "response": answers[LOCK][0]}) + "\n")
    common = ["--recipe", "r2", "--allow-api-changes", "--compiler", "g++", "--no-sanitizers",
              "--work-dir", str(tmp_path / "work"), "--manifest", str(repo / "manifest.json")]
    head = git(repo.parent, "rev-parse", "HEAD")

    assert main(["run", str(repo), *common, "--branch", "rp/r2", "--replay", str(first)]) == 0
    out = capsys.readouterr().out
    assert "1 committed, 0 rolled back, 0 reverted, 0 unchanged, 1 skipped" in out
    assert "reason: not finished: no recorded response" in out  # an outage, not a verdict on any edit
    assert "to finish later, run the same command with --continue rp/r2" in out
    after_first = git(repo.parent, "rev-parse", "rp/r2")

    full = tmp_path / "full.jsonl"
    full.write_text((REPLAYS / "r2-fixture-api.jsonl").read_text())
    assert main(["run", str(repo), *common, "--continue", "rp/r2", "--replay", str(full)]) == 0
    out = capsys.readouterr().out
    assert "continuing branch rp/r2" in out
    assert "3 committed, 0 rolled back" in out and "R2: 1/8 -> 8/8" in out  # the lock target is not redone
    subjects = git(repo.parent, "log", "--reverse", "--format=%s", f"{head}..rp/r2").splitlines()
    assert [s.split(": ")[0] for s in subjects] == [LOCK, FILEH, WAREHOUSE, FACTORY]
    assert git(repo.parent, "rev-parse", "HEAD") == head and git(repo.parent, "status", "--porcelain") == ""

    # Continuing again finds nothing left to do. The branch must survive that.
    tip = git(repo.parent, "rev-parse", "rp/r2")
    assert main(["run", str(repo), *common, "--continue", "rp/r2", "--replay", str(full)]) == 0
    assert "nothing new was committed; branch 'rp/r2' is unchanged." in capsys.readouterr().out
    assert git(repo.parent, "rev-parse", "rp/r2") == tip != after_first


def test_continue_refuses_what_it_cannot_do_safely(repo, tmp_path, capsys):
    from refactor_pilot.checkpoint import CheckpointError, Workspace

    with pytest.raises(CheckpointError, match="no branch named 'nope'"):
        Workspace.create(repo, tmp_path / "w", continue_branch="nope")
    current = git(repo.parent, "rev-parse", "--abbrev-ref", "HEAD")
    with pytest.raises(CheckpointError, match="checked out somewhere else"):
        Workspace.create(repo, tmp_path / "w", continue_branch=current)
    assert main(["run", str(repo), "--recipe", "r1", "--branch", "a", "--continue", "b"]) == 2
    assert "either --branch" in capsys.readouterr().err


@needs_toolchain
@pytest.mark.toolchain
def test_overloaded_primary_model_reproduces_the_first_live_run_and_now_finishes(repo, tmp_path, capsys, monkeypatch):
    """A local stand-in for the Gemini endpoint. The preferred model answers once, then
    returns the 503 'high demand' error seen in the first live run, for good. The
    fallback model answers everything after that."""
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    answers = recorded("r2-fixture-api.jsonl")
    by_goal = {"manual `lock()`": answers[LOCK][0], "`FILE*` through RAII": answers[FILEH][0],
               "explicit in `inventory::Warehouse`": answers[WAREHOUSE][0],
               "explicit in `inventory::create_item`": answers[FACTORY][0]}
    overloaded = [{"error": {"code": 503, "status": "UNAVAILABLE",
                             "message": "This model is currently experiencing high demand. Spikes in demand are "
                                        "usually temporary. Please try again later."}}]
    served = []

    class Gemini(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            model, prompt = body["model"], body["messages"][1]["content"]
            served.append(model)
            if model == "gemini-3.8-flash" and served.count("gemini-3.8-flash") > 1:
                status, payload = 503, overloaded
            else:
                text = next(answer for goal, answer in by_goal.items() if goal in prompt)
                status, payload = 200, {"choices": [{"message": {"role": "assistant", "content": text}}],
                                        "usage": {"prompt_tokens": 1000, "completion_tokens": 300}}
            data = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Gemini)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv("REFACTOR_PILOT_BASE_URL", f"http://127.0.0.1:{server.server_port}/v1beta/openai/")
    monkeypatch.setenv("REFACTOR_PILOT_API_KEY", "test-key")
    monkeypatch.setenv("REFACTOR_PILOT_RPM", "0")
    monkeypatch.delenv("REFACTOR_PILOT_MODEL", raising=False)  # the shipped default: 3.8 Flash, then 3.5 Flash-Lite
    record = tmp_path / "live.jsonl"

    code = main(["run", str(repo), "--recipe", "r2", "--allow-api-changes", "--compiler", "g++", "--no-sanitizers",
                 "--branch", "rp/r2", "--work-dir", str(tmp_path / "work"), "--record", str(record),
                 "--manifest", str(repo / "manifest.json")])
    out = capsys.readouterr().out
    server.shutdown()
    assert code == 0
    assert "model: gemini-3.8-flash (then gemini-3.5-flash-lite)" in out
    assert "answered by fallback model gemini-3.5-flash-lite" in out
    assert "4 committed, 0 rolled back" in out and "R2: 0/8 -> 8/8" in out
    assert "model usage: 4 requests" in out  # the refused requests cost nothing
    models = [json.loads(line)["model"] for line in record.read_text().splitlines()]
    assert models == ["gemini-3.8-flash"] + ["gemini-3.5-flash-lite"] * 3
