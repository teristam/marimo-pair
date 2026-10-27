"""HTTP-level regression tests for the execute-code.sh input transport."""

import json
import os
import shutil
import signal
import subprocess
import tempfile
import threading
import time
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
        if mode == "hang":
            self.wfile.flush()
            time.sleep(5)
            return
        if mode != "missing":
            success = "false" if mode == "failure" else "true"
            self.wfile.write((f'event: done{chr(10)}data: {{"success":{success}}}{chr(10)}{chr(10)}').encode())


CURL_SHIM = """#!/usr/bin/env bash
if [[ "${1:-}" == "--version" ]]; then
  if [[ -n "${FAKE_CURL_VERSION:-}" ]]; then
    echo "curl ${FAKE_CURL_VERSION} (fake)"
    exit 0
  fi
  exec "$REAL_CURL" --version
fi
for arg in "$@"; do printf '%s\\n' "$arg" >> "$CURL_ARGV_LOG"; done
exec "$REAL_CURL" "$@"
"""

UNAME_SHIM = """#!/usr/bin/env bash
if [[ -n "${FAKE_UNAME:-}" && "${1:-}" == "-s" ]]; then
  echo "$FAKE_UNAME"
  exit 0
fi
exec "$REAL_UNAME" "$@"
"""

# Stands in for MSYS cygpath: logs the call and returns a recognisable
# alternate spelling (a symlink) of the path it was given.
CYGPATH_SHIM = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$CYGPATH_LOG"
link="$CYGPATH_LINK_DIR/converted-$(basename "$2")"
ln -s "$2" "$link"
printf '%s\\n' "$link"
"""


@unittest.skipUnless(shutil.which("bash") and shutil.which("jq") and shutil.which("curl"), "requires Bash, jq and curl")
class ExecuteCodeTransportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _FixtureHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"
        cls.real_curl = shutil.which("curl")
        cls.shim_dir = Path(tempfile.mkdtemp())
        shim = cls.shim_dir / "curl"
        shim.write_text(CURL_SHIM, encoding="utf-8")
        shim.chmod(0o755)
        # cygpath and uname shims live apart so they only apply when requested.
        cls.msys_dir = Path(tempfile.mkdtemp())
        for name, text in (("uname", UNAME_SHIM), ("cygpath", CYGPATH_SHIM)):
            path = cls.msys_dir / name
            path.write_text(text, encoding="utf-8")
            path.chmod(0o755)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)
        shutil.rmtree(cls.shim_dir, ignore_errors=True)
        shutil.rmtree(cls.msys_dir, ignore_errors=True)

    def setUp(self):
        _FixtureHandler.bodies = []
        _FixtureHandler.headers = []
        _FixtureHandler.session_lookups = 0
        _FixtureHandler.done_mode = "success"
        handle, name = tempfile.mkstemp(prefix="curl-argv-")
        os.close(handle)
        self.argv_log = Path(name)
        self.addCleanup(self.argv_log.unlink, missing_ok=True)

    def _env(self, *, token=True, env=None):
        child_env = os.environ.copy()
        child_env.pop("MARIMO_TOKEN_FILE", None)
        if token:
            child_env["MARIMO_TOKEN"] = "fixture-secret"
        else:
            child_env.pop("MARIMO_TOKEN", None)
        child_env.update({
            "PATH": f"{self.shim_dir}{os.pathsep}{child_env.get('PATH', '')}",
            "CURL_ARGV_LOG": str(self.argv_log),
            "REAL_CURL": self.real_curl,
        })
        child_env.update(env or {})
        return child_env

    def _command(self, args):
        return [str(branch.resolve_bash()), str(SCRIPT), "--url", self.base, *args]

    def _run(self, args, payload=b"", *, token=True, env=None):
        return subprocess.run(
            self._command(args), input=payload, capture_output=True, timeout=30,
            env=self._env(token=token, env=env),
        )

    def _curl_args(self):
        return self.argv_log.read_text(encoding="utf-8").splitlines()

    def _header_files(self):
        return [arg[1:] for arg in self._curl_args() if arg.startswith("@") and arg != "@-"]

    def test_large_utf8_byte_stdin_is_streamed_and_session_skips_lookup(self):
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

    def test_missing_script_file_fails_before_any_server_request(self):
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "missing.py"
            completed = self._run([str(missing)])
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn(b"readable", completed.stderr.lower())
        self.assertEqual(_FixtureHandler.session_lookups, 0)
        self.assertEqual(_FixtureHandler.bodies, [])

    def test_failed_or_missing_done_events_return_nonzero(self):
        for mode in ("failure", "missing"):
            _FixtureHandler.done_mode = mode
            completed = self._run(["--session", "s", "-c", "pass"])
            self.assertNotEqual(completed.returncode, 0, mode)

    def test_bearer_token_reaches_server_but_never_curl_argv(self):
        completed = self._run(["-c", "pass"])
        self.assertEqual(completed.returncode, 0, completed.stderr[-1000:])
        self.assertEqual(_FixtureHandler.session_lookups, 1)
        self.assertEqual(_FixtureHandler.headers[-1]["Authorization"], "Bearer fixture-secret")
        args = self._curl_args()
        self.assertFalse(any("fixture-secret" in arg for arg in args), args)
        header_files = self._header_files()
        self.assertEqual(len(header_files), 2, args)
        for path in header_files:
            self.assertFalse(Path(path).exists(), path)

    def test_header_file_removed_after_failed_execution(self):
        _FixtureHandler.done_mode = "failure"
        completed = self._run(["--session", "s", "-c", "pass"])
        self.assertNotEqual(completed.returncode, 0)
        header_files = self._header_files()
        self.assertEqual(len(header_files), 1)
        self.assertFalse(Path(header_files[0]).exists())

    @unittest.skipIf(os.name == "nt", "POSIX signal delivery")
    def test_header_file_removed_when_interrupted(self):
        _FixtureHandler.done_mode = "hang"
        process = subprocess.Popen(
            self._command(["--session", "s", "-c", "pass"]), stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=self._env(), start_new_session=True,
        )
        try:
            deadline = time.monotonic() + 10
            while not _FixtureHandler.bodies and time.monotonic() < deadline:
                time.sleep(0.05)
            header_files = self._header_files()
            self.assertEqual(len(header_files), 1)
            os.killpg(process.pid, signal.SIGINT)
            process.communicate(timeout=10)
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.communicate()
        self.assertNotEqual(process.returncode, 0)
        self.assertFalse(Path(header_files[0]).exists())

    def test_token_sources_follow_flag_then_file_then_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            flag_file = Path(directory) / "flag-token"
            flag_file.write_text("flag-file-secret\n", encoding="utf-8")
            env_file = Path(directory) / "env-token"
            env_file.write_bytes(b"env-file-secret\r\n")
            cases = [
                ([], {}, "fixture-secret"),
                ([], {"MARIMO_TOKEN_FILE": str(env_file)}, "env-file-secret"),
                (["--token-file", str(flag_file)], {"MARIMO_TOKEN_FILE": str(env_file)}, "flag-file-secret"),
                (["--token", "inline-secret", "--token-file", str(flag_file)], {}, "inline-secret"),
            ]
            for extra, env, expected in cases:
                with self.subTest(expected=expected):
                    completed = self._run([*extra, "--session", "s", "-c", "pass"], env=env)
                    self.assertEqual(completed.returncode, 0, completed.stderr[-1000:])
                    self.assertEqual(_FixtureHandler.headers[-1]["Authorization"], f"Bearer {expected}")
                    if "--token" in extra:
                        self.assertIn(b"exposes", completed.stderr)
                    else:
                        self.assertNotIn(b"exposes", completed.stderr)
        self.assertFalse(any("secret" in arg for arg in self._curl_args()))

    def test_invalid_tokens_fail_before_any_request(self):
        with tempfile.TemporaryDirectory() as directory:
            empty = Path(directory) / "empty"
            empty.write_text("\n", encoding="utf-8")
            multi = Path(directory) / "multi"
            multi.write_text("first\nsecond\n", encoding="utf-8")
            missing = Path(directory) / "missing"
            cases = [
                (["--token-file", str(empty)], {}, b"empty"),
                (["--token-file", str(multi)], {}, b"control"),
                (["--token-file", str(missing)], {}, b"readable"),
                ([], {"MARIMO_TOKEN": "bad\x01secret"}, b"control"),
            ]
            for extra, env, message in cases:
                with self.subTest(message=message, extra=extra):
                    completed = self._run([*extra, "--session", "s", "-c", "pass"], env=env)
                    self.assertNotEqual(completed.returncode, 0)
                    self.assertIn(message, completed.stderr.lower())
        self.assertEqual(_FixtureHandler.bodies, [])
        self.assertEqual(self._curl_args(), [])

    def test_without_token_no_header_file_or_authorization(self):
        completed = self._run(["--session", "s", "-c", "pass"], token=False)
        self.assertEqual(completed.returncode, 0, completed.stderr[-1000:])
        self.assertNotIn("Authorization", _FixtureHandler.headers[-1])
        self.assertEqual(self._header_files(), [])

    def test_msys_header_path_goes_through_cygpath_only_there(self):
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "cygpath.log"
            msys = {
                "PATH": f"{self.msys_dir}{os.pathsep}{self.shim_dir}{os.pathsep}{os.environ['PATH']}",
                "FAKE_UNAME": "MINGW64_NT-10.0-19045", "REAL_UNAME": shutil.which("uname"),
                "CYGPATH_LOG": str(log), "CYGPATH_LINK_DIR": directory,
            }
            # Without a MINGW/MSYS/CYGWIN uname the cygpath on PATH is never consulted.
            plain = dict(msys, FAKE_UNAME="")
            completed = self._run(["--session", "s", "-c", "pass"], env=plain)
            self.assertEqual(completed.returncode, 0, completed.stderr[-1000:])
            self.assertFalse(log.exists())
            self.assertTrue(all(not Path(p).name.startswith("converted-") for p in self._header_files()))

            self.argv_log.write_text("", encoding="utf-8")
            completed = self._run(["--session", "s", "-c", "pass"], env=msys)
            self.assertEqual(completed.returncode, 0, completed.stderr[-1000:])
            self.assertEqual(_FixtureHandler.headers[-1]["Authorization"], "Bearer fixture-secret")
            header_files = self._header_files()
            self.assertEqual(len(header_files), 1)
            self.assertEqual(Path(header_files[0]).parent, Path(directory))
            self.assertTrue(Path(header_files[0]).name.startswith("converted-marimo-pair-auth."))
            self.assertIn("-m ", log.read_text(encoding="utf-8"))

    def test_token_file_trailing_cr_lf_runs_are_stripped_like_python(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "token"
            path.write_bytes(b"crlf-secret\r\n\r\n")
            self.assertEqual(branch.parent_token(str(path)), "crlf-secret")
            completed = self._run(["--token-file", str(path), "--session", "s", "-c", "pass"])
            self.assertEqual(completed.returncode, 0, completed.stderr[-1000:])
            self.assertEqual(_FixtureHandler.headers[-1]["Authorization"], "Bearer crlf-secret")

    def test_old_curl_is_refused_only_when_a_token_is_needed(self):
        old = {"FAKE_CURL_VERSION": "7.54.1"}
        completed = self._run(["--session", "s", "-c", "pass"], env=old)
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn(b"7.55", completed.stderr)
        self.assertEqual(_FixtureHandler.bodies, [])
        completed = self._run(["--session", "s", "-c", "pass"], token=False, env=old)
        self.assertEqual(completed.returncode, 0, completed.stderr[-1000:])


if __name__ == "__main__":
    unittest.main()
