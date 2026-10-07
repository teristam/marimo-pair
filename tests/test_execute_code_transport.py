"""HTTP-level regression tests for the execute-code.sh input transport."""

import json
import os
import shutil
import subprocess
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import sys

SCRIPTS_DIR = Path(__file__).parents[1] / "skills" / "marimo-pair" / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))
import branch

SCRIPT = SCRIPTS_DIR / "execute-code.sh"


class _FixtureHandler(BaseHTTPRequestHandler):
    bodies = []
    headers = []
    session_lookups = 0
    done_mode = "success"

    def log_message(self, *_args):
        pass

    def do_GET(self):
        type(self).session_lookups += 1
        body = json.dumps({"session-1": {"path": "C:/tmp/fixture.py"}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        size = int(self.headers.get("Content-Length", "0"))
        payload = self.rfile.read(size)
        type(self).bodies.append(json.loads(payload))
        type(self).headers.append(dict(self.headers.items()))
        mode = type(self).done_mode
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        nl = bytes([10])
        self.wfile.write(b"event: stdout" + nl + b'data: {"data":"ok"}' + nl + nl)
        if mode != "missing":
            success = "false" if mode == "failure" else "true"
            self.wfile.write((f'event: done{chr(10)}data: {{"success":{success}}}{chr(10)}{chr(10)}').encode())


@unittest.skipUnless(shutil.which("bash") and shutil.which("jq") and shutil.which("curl"), "requires Bash, jq and curl")
class ExecuteCodeTransportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _FixtureHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def setUp(self):
        _FixtureHandler.bodies = []
        _FixtureHandler.headers = []
        _FixtureHandler.session_lookups = 0
        _FixtureHandler.done_mode = "success"

    def _run(self, args, payload=b"", *, token=True):
        env = os.environ.copy()
        if token:
            env["MARIMO_TOKEN"] = "fixture-secret"
        else:
            env.pop("MARIMO_TOKEN", None)
        completed = subprocess.run(
            [str(branch.resolve_bash()), str(SCRIPT), "--url", self.base, *args],
            input=payload, capture_output=True, timeout=30, env=env,
        )
        return completed

    def test_large_utf8_byte_stdin_is_streamed_and_session_skips_lookup(self):
        # The bearer header is still passed in curl's argv; only source and JSON bodies stay off argv.
        text = ("naïve 🚀 東京" + chr(10)) * 180_000
        payload = text.encode("utf-8")
        completed = self._run(["--session", "session-explicit", "-"], payload)
        self.assertEqual(completed.returncode, 0, completed.stderr[-1000:])
        self.assertEqual(_FixtureHandler.bodies[-1]["code"], text)
        self.assertEqual(_FixtureHandler.session_lookups, 0)
        self.assertEqual(_FixtureHandler.headers[-1]["Marimo-Session-Id"], "session-explicit")
        self.assertEqual(_FixtureHandler.headers[-1]["Authorization"], "Bearer fixture-secret")

    def test_c_dash_file_and_inherited_stdin_use_the_same_json_code_transport(self):
        with tempfile.TemporaryDirectory() as directory:
            source = "print('file: café')" + chr(10)
            file_path = Path(directory) / "code.py"
            file_path.write_text(source, encoding="utf-8")
            cases = [
                (["--session", "s", "-c", "print('inline: ☃')"], b"", "print('inline: ☃')"),
                (["--session", "s", str(file_path)], b"", source),
                (["--session", "s"], ("stdin: π" + chr(10)).encode(), "stdin: π" + chr(10)),
                (["--session", "s", "-"], ("dash: 🧪" + chr(10)).encode(), "dash: 🧪" + chr(10)),
            ]
            for args, payload, expected in cases:
                with self.subTest(args=args):
                    completed = self._run(args, payload)
                    self.assertEqual(completed.returncode, 0, completed.stderr[-1000:])
                    self.assertEqual(_FixtureHandler.bodies[-1]["code"], expected)
        self.assertEqual(_FixtureHandler.session_lookups, 0)

    def test_file_and_automatic_resolution_lookup_session(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "x.py"
            path.write_text("pass", encoding="utf-8")
            for args in (["--file", "C:/tmp/fixture.py", "-c", "pass"], ["-c", "pass"]):
                completed = self._run(args)
                self.assertEqual(completed.returncode, 0, completed.stderr[-1000:])
                self.assertEqual(_FixtureHandler.headers[-1]["Marimo-Session-Id"], "session-1")
        self.assertEqual(_FixtureHandler.session_lookups, 2)

    def test_failed_or_missing_done_events_return_nonzero(self):
        for mode in ("failure", "missing"):
            _FixtureHandler.done_mode = mode
            completed = self._run(["--session", "s", "-c", "pass"])
            self.assertNotEqual(completed.returncode, 0, mode)


if __name__ == "__main__":
    unittest.main()
