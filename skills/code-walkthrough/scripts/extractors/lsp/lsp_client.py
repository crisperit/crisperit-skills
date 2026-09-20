"""Minimal raw JSON-RPC / LSP client for typescript-language-server. No deps.

Vendored from an earlier experiment (/tmp/claude-1000/lsptest/lspclient.py), which proved
Content-Length-framed stdio JSON-RPC works reliably against typescript-language-server 5.3.0.
`stderr_log` defaults to /dev/null rather than a fixed path, so this is usable from any repo.
"""
import json
import os
import subprocess
import threading
import time


class LSPClient:
    def __init__(self, cmd, cwd, stderr_log=None):
        self.proc = subprocess.Popen(
            cmd, cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=open(stderr_log, "ab") if stderr_log else subprocess.DEVNULL,
            bufsize=0,
        )
        self._id = 0
        self._lock = threading.Lock()
        self._pending = {}
        self._notifications = []
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    def _read_loop(self):
        f = self.proc.stdout
        while True:
            line = f.readline()
            if not line:
                break
            if line.strip() == b"":
                continue
            headers = {}
            while line.strip() != b"":
                k, _, v = line.decode().partition(":")
                headers[k.strip().lower()] = v.strip()
                line = f.readline()
            length = int(headers["content-length"])
            body = b""
            while len(body) < length:
                chunk = f.read(length - len(body))
                if not chunk:
                    break
                body += chunk
            try:
                msg = json.loads(body.decode("utf-8"))
            except Exception:
                continue
            if "id" in msg and ("result" in msg or "error" in msg):
                with self._lock:
                    self._pending[msg["id"]] = msg
            else:
                with self._lock:
                    self._notifications.append(msg)

    def _send(self, obj):
        body = json.dumps(obj).encode("utf-8")
        header = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
        self.proc.stdin.write(header + body)
        self.proc.stdin.flush()

    def request(self, method, params, timeout=30):
        with self._lock:
            self._id += 1
            mid = self._id
        self._send({"jsonrpc": "2.0", "id": mid, "method": method, "params": params})
        start = time.time()
        while time.time() - start < timeout:
            with self._lock:
                if mid in self._pending:
                    return self._pending.pop(mid)
            time.sleep(0.01)
        raise TimeoutError(f"{method} timed out after {timeout}s")

    def notify(self, method, params):
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def shutdown(self):
        try:
            self.request("shutdown", None, timeout=5)
            self.notify("exit", None)
        except Exception:
            pass
        self.proc.terminate()
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass  # survived SIGKILL; unreapable, and shutdown() is always called from a
                # `finally`, so this must never shadow the exception that's already propagating


def uri(path):
    return "file://" + os.path.abspath(path)
