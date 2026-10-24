"""One line-oriented JSON-RPC client over an MCP server subprocess.

stderr is drained by a thread (an unread PIPE blocks the server past ~4 KB) and
shown when the server dies; a watchdog kills a server that never answers so a
test fails fast instead of hanging.
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading


class McpServer:
    def __init__(self, args, cwd=None, env=None):
        self.proc = subprocess.Popen(
            [sys.executable, *map(str, args)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            cwd=None if cwd is None else str(cwd),
            env=env,
        )
        self._stderr: list[str] = []
        self._drain = threading.Thread(target=self._drain_stderr, daemon=True)
        self._drain.start()

    def _drain_stderr(self):
        self._stderr.extend(self.proc.stderr)

    def send(self, obj):
        self.proc.stdin.write(json.dumps(obj) + "\n")
        self.proc.stdin.flush()

    def read(self, timeout=20):
        result = {}

        def _reader():
            result["line"] = self.proc.stdout.readline()

        t = threading.Thread(target=_reader, daemon=True)
        t.start()
        t.join(timeout)
        if t.is_alive():
            self.proc.kill()
            raise AssertionError("timed out waiting for MCP server response")
        line = result.get("line", "")
        if not line:
            self._drain.join(2)
            raise AssertionError(f"server closed stdout; stderr:\n{''.join(self._stderr)}")
        return json.loads(line)

    def rpc(self, obj, timeout=20):
        self.send(obj)
        return self.read(timeout)

    def request(self, rid, method, params=None):
        msg = {"jsonrpc": "2.0", "id": rid, "method": method}
        if params is not None:
            msg["params"] = params
        return self.rpc(msg)

    def notify(self, method, params=None):
        msg = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        self.send(msg)

    def call(self, rid, name, arguments=None):
        params = {"name": name}
        if arguments is not None:
            params["arguments"] = arguments
        return self.request(rid, "tools/call", params)

    def close(self):
        try:
            self.proc.stdin.close()
        except Exception:
            pass
        try:
            self.proc.wait(timeout=5)
        except Exception:
            self.proc.kill()
