"""Suite-wide pytest setup. Stdlib only, no plugins.

cw_run launches symdelta.py in a background step, and symdelta starts whatever language server
is installed for the fixture's file type (pyright, gopls, typescript-language-server, ...). That
costs ~16s per run and makes the result depend on the machine, so every test that drives
cw_run.run() paid it. The autouse fixture below swaps that one step for the answer symdelta
gives on an unsupported language. test_symdelta.py and test_cw_e2e.py exercise the real path.
"""

import socketserver
import sys

import pytest

REAL_SYMDELTA = ("test_symdelta.py", "test_cw_e2e.py")


def pytest_configure(config):
    config.addinivalue_line("markers", "e2e: drives the real daemon/subprocess path; run alone with -m e2e")


def pytest_collection_modifyitems(items):
    for item in items:
        if item.path.name == "test_cw_e2e.py":
            item.add_marker(pytest.mark.e2e)


@pytest.fixture(autouse=True)
def _no_language_servers(request, monkeypatch):
    cw_run = sys.modules.get("cw_run")
    if cw_run is None or request.path.name in REAL_SYMDELTA:
        return

    def stub(d, meta, on_event):
        cw_run._emit_step(d, on_event, "symdelta", "running")
        (d / "symdelta.json").write_text('{"language": null, "reason": "language servers are stubbed in tests"}')
        cw_run._emit_step(d, on_event, "symdelta", "ok")
        cw_run._request_render(d)

    monkeypatch.setattr(cw_run, "_run_symdelta", stub)


@pytest.fixture(autouse=True)
def _zero_llm_backoff(monkeypatch):
    cw_llm = sys.modules.get("cw_llm")
    if cw_llm is not None:
        monkeypatch.setattr(cw_llm, "BACKOFF_S", [0, 0])


@pytest.fixture(autouse=True)
def _fast_server_shutdown(monkeypatch):
    """BaseServer.shutdown() blocks until serve_forever's select loop wakes, up to
    poll_interval (0.5s default). Daemon and StubLLM both call serve_forever() bare, so a
    wrapper changing the default speeds every stop()/close() without touching product code."""
    original = socketserver.BaseServer.serve_forever

    def serve_forever(self, poll_interval=0.02):
        return original(self, poll_interval)

    monkeypatch.setattr(socketserver.BaseServer, "serve_forever", serve_forever)
