"""The MCP server: protocol behaviour in-process, then a real stdio session."""

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from refactor_pilot import mcp_server

ROOT = Path(__file__).parent.parent
FIXTURE = ROOT / "fixtures" / "legacy_inventory"
PATCHES = ROOT / "examples" / "patches"
TOOLS = ("cmake", "ninja", "ctest", "g++", "git", "clangd")


def request(method, params=None, request_id=1):
    message = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        message["params"] = params
    return message


@pytest.fixture()
def server(tmp_path):
    return mcp_server.Server(tmp_path, compiler="g++", work_dir=tmp_path / "w", log=open("/dev/null", "w"))


# ---- protocol, no toolchain needed -------------------------------------------------


def test_initialize_echoes_a_supported_version_and_falls_back_otherwise(server):
    result = server.handle(request("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}}))["result"]
    assert result["protocolVersion"] == "2025-06-18"
    assert result["capabilities"] == {"tools": {"listChanged": False}}
    assert result["serverInfo"]["name"] == "refactor-pilot"
    newer = server.handle(request("initialize", {"protocolVersion": "2099-01-01"}))["result"]
    assert newer["protocolVersion"] == mcp_server.SUPPORTED_VERSIONS[0]


def test_notifications_get_no_answer(server):
    assert server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    assert server.handle({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 3}}) is None


def test_tools_list_is_stable_and_every_tool_has_a_schema(server):
    first = server.handle(request("tools/list"))["result"]["tools"]
    assert [t["name"] for t in first] == ["find_symbol", "references", "outline", "diagnostics", "baseline",
                                          "check_patch", "run_recipes"]
    assert first == server.handle(request("tools/list"))["result"]["tools"]
    for tool in first:
        assert tool["description"] and tool["inputSchema"]["type"] == "object"
        assert set(tool["inputSchema"]["required"]) <= set(tool["inputSchema"]["properties"])


def test_unknown_method_is_a_protocol_error_so_newer_clients_can_fall_back(server):
    assert server.handle(request("server/discover"))["error"]["code"] == -32601
    assert server.handle({"id": 1, "method": "ping"})["error"]["code"] == -32600  # not JSON-RPC 2.0
    assert server.handle(request("ping"))["result"] == {}


def test_bad_tool_calls_are_tool_errors_not_protocol_errors(server):
    def call(name, arguments):
        return server.handle(request("tools/call", {"name": name, "arguments": arguments}))["result"]

    unknown = call("delete_everything", {})
    assert unknown["isError"] and "Unknown tool" in unknown["content"][0]["text"]
    missing = call("references", {})
    assert missing["isError"] and "missing: symbol" in missing["content"][0]["text"]
    extra = call("outline", {"file": "a.cpp", "depth": 3})
    assert extra["isError"] and "unknown: depth" in extra["content"][0]["text"]
    model = call("run_recipes", {"recipes": "r1,r2"})
    assert model["isError"] and "run from the command line" in model["content"][0]["text"]
    typo = call("run_recipes", {"recipes": "r1,r9"})
    assert typo["isError"] and "unknown recipe 'r9'" in typo["content"][0]["text"]


# ---- a real session over stdio ------------------------------------------------------

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


def session(repo, tmp_path, messages):
    """Start the server as a subprocess, send every message, return the decoded replies by id."""
    proc = subprocess.run(
        [sys.executable, "-m", "refactor_pilot.cli", "mcp", str(repo), "--compiler", "g++", "--work-dir", str(tmp_path / "w")],
        input="".join(json.dumps(m) + "\n" for m in messages), capture_output=True, text=True, timeout=900,
    )
    assert proc.returncode == 0, proc.stderr[-800:]
    lines = [line for line in proc.stdout.splitlines() if line.strip()]
    replies = [json.loads(line) for line in lines]  # stdout carries nothing but protocol messages
    return {reply["id"]: reply for reply in replies}, proc.stderr


def call(name, arguments, request_id):
    return request("tools/call", {"name": name, "arguments": arguments}, request_id)


def text_of(reply):
    return reply["result"]["content"][0]["text"]


@needs_toolchain
@pytest.mark.toolchain
def test_navigation_tools_over_stdio(repo, tmp_path):
    replies, stderr = session(repo, tmp_path, [
        request("initialize", {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}),
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        call("find_symbol", {"name": "Warehouse::add_item"}, 2),
        call("references", {"symbol": "create_item"}, 3),
        call("references", {"symbol": "total_value"}, 4),
        call("outline", {"file": "include/inventory/logger.h"}, 5),
        call("diagnostics", {"file": "src/warehouse.cpp"}, 6),
        call("outline", {"file": "../../etc/passwd"}, 7),
    ])
    assert len(replies) == 7  # the notification got no reply
    assert "inventory::Warehouse::add_item  (method)" in text_of(replies[2]) and "Item *" in text_of(replies[2])
    assert "4 references in 3 files" in text_of(replies[3]) and "tests/test_main.cpp" in text_of(replies[3])
    assert replies[4]["result"]["isError"] and "inventory::Warehouse::total_value" in text_of(replies[4])
    assert "inventory::Logger::file_" in text_of(replies[5])
    assert "sign" in text_of(replies[6])
    assert replies[7]["result"]["isError"] and "No such file in the repository" in text_of(replies[7])
    assert "serving" in stderr  # logs go to stderr


@needs_toolchain
@pytest.mark.toolchain
def test_check_patch_accepts_a_good_change_and_explains_a_bad_one(repo, tmp_path):
    head = git(repo.parent, "rev-parse", "HEAD")
    replies, _ = session(repo, tmp_path, [
        call("check_patch", {"patch": (PATCHES / "01-nullptr-logger.patch").read_text(), "description": "use nullptr"}, 1),
        call("check_patch", {"patch": (PATCHES / "02-wrong-discount.patch").read_text()}, 2),
        call("check_patch", {"patch": (PATCHES / "04-leak-in-remove.patch").read_text()}, 3),
        call("check_patch", {"patch": "this is not a diff"}, 4),
    ])
    good = text_of(replies[1])
    assert good.startswith("ACCEPTED") and "git merge refactor-pilot/" in good and "src/logger.cpp" in good
    wrong = text_of(replies[2])
    assert wrong.startswith("REJECTED (tests: test now fails: pricing)") and "CHECK failed" in wrong
    leak = text_of(replies[3])
    assert leak.startswith("REJECTED (sanitizers") and "LeakSanitizer" in leak  # only the sanitizers see this one
    assert text_of(replies[4]).startswith("REJECTED (could not apply")
    # The agent's own checkout is untouched; exactly one branch was created, for the accepted change.
    assert git(repo.parent, "rev-parse", "HEAD") == head and git(repo.parent, "status", "--porcelain") == ""
    branches = git(repo.parent, "branch", "--list", "refactor-pilot/*").split()
    assert len(branches) == 1
    assert git(repo.parent, "log", "-1", "--format=%s", branches[0]) == "mcp/patch: use nullptr"


@needs_toolchain
@pytest.mark.toolchain
def test_run_recipes_over_stdio(repo, tmp_path):
    replies, _ = session(repo, tmp_path, [
        call("run_recipes", {"recipes": "r6", "flags": ["INVENTORY_ENABLE_LEGACY_PRICING=off"]}, 1),
    ])
    text = text_of(replies[1])
    assert text.startswith("5 committed, 0 rolled back, 0 reverted, 1 skipped.")
    assert "The working tree was not modified." in text and "r6/flag/INVENTORY_ENABLE_LEGACY_PRICING/src/pricing.cpp" in text
