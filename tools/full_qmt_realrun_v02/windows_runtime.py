"""Owned child + private stdin + bounded OS stdout/stderr pipes.

Uses subprocess's retained process handle on Windows. No PID/name/tree control.
Only fake workers can be created in this release.
"""

import os
from pathlib import Path
import subprocess
import threading
import time

try:
    from .profile import CALLBACK_FAILURE_MARKER, encode
except ImportError:
    from profile import CALLBACK_FAILURE_MARKER, encode


class OwnedWorker:
    def __init__(self, python, worker_file, request, max_output, cwd):
        # No account/path/profile in argv or environment; request uses stdin only.
        self.argv = [python, "-I", "-S", "-B", str(worker_file)]
        self.environment = {k: os.environ[k] for k in ("SystemRoot", "WINDIR") if k in os.environ}
        self.created_ns = time.perf_counter_ns()
        self._process = subprocess.Popen(self.argv, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=self.environment,
            cwd=str(cwd), close_fds=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.pid = self._process.pid
        self._overflow = threading.Event()
        self._callback_failure = threading.Event()
        self.stdin_complete = False
        self.channels = {name: {"bytes": 0, "eof": False, "error": None, "overflow": False}
                         for name in ("stdout", "stderr")}
        self._threads = []
        for name in self.channels:
            thread = threading.Thread(target=self._drain,
                args=(getattr(self._process, name), self.channels[name], max_output, name), daemon=True)
            thread.start()
            self._threads.append(thread)
        data = encode(request)
        if len(data) > 65536:
            self.terminate_and_wait(1)
            raise ValueError("private_request_size")
        self._writer = threading.Thread(target=self._send, args=(data,), daemon=True)
        self._writer.start()

    def _send(self, data):
        try:
            self._process.stdin.write(data)
            self._process.stdin.flush()
            self.stdin_complete = True
        except (OSError, ValueError):
            # Broken private pipe is a failure fact; never format its exception.
            self.stdin_complete = False
        finally:
            try:
                self._process.stdin.close()
            except (OSError, ValueError):
                self.stdin_complete = False

    def _drain(self, pipe, facts, maximum, name):
        tail = b""
        try:
            while True:
                chunk = pipe.read(4096)
                if chunk == b"":
                    facts["eof"] = True
                    break
                facts["bytes"] += len(chunk)
                if name == "stderr":
                    combined = tail + chunk
                    if CALLBACK_FAILURE_MARKER in combined:
                        self._callback_failure.set()
                    tail = combined[-(len(CALLBACK_FAILURE_MARKER) - 1):]
                # Discard raw bytes immediately. Even secrets in native output
                # never reach terminal, files, tracebacks, logs or evidence.
                if facts["bytes"] > maximum:
                    facts["overflow"] = True
                    self._overflow.set()
        except (OSError, ValueError):
            facts["error"] = "PIPE_ERROR"
        finally:
            pipe.close()

    def terminate_and_wait(self, seconds):
        """Terminate only the retained Popen child; kill is NOT a stronger layer."""
        if self._process.poll() is None:
            try:
                self._process.terminate()
            except OSError:
                pass
        try:
            self._process.wait(timeout=max(0.01, seconds))
        except subprocess.TimeoutExpired:
            return False
        return self._process.poll() is not None

    def finish(self, deadline):
        timed_out, confirmed, cleanup = False, False, []
        try:
            while self._process.poll() is None:
                if self._overflow.is_set():
                    break
                deadline.check()
                time.sleep(min(0.01, deadline.remaining()))
            deadline.check()  # Covers finite return between watchdog polls.
            confirmed = self._process.poll() is not None
        except TimeoutError:
            timed_out = True
        if not confirmed:
            # Cleanup is bounded even after run expiry; it cannot revive PASS.
            confirmed = self.terminate_and_wait(1.0)
        if not confirmed:
            cleanup.append("WORKER_EXIT_UNCONFIRMED")
        for thread in [self._writer, *self._threads]:
            thread.join(timeout=0.5)
        return {"exit_confirmed": confirmed, "returncode": self._process.poll(),
                "timed_out": timed_out, "cleanup_errors": cleanup,
                "callback_failure": self._callback_failure.is_set(),
                "controller_error": None, "stdin_complete": self.stdin_complete,
                "stdout": dict(self.channels["stdout"]), "stderr": dict(self.channels["stderr"]),
                "elapsed_ns": time.perf_counter_ns() - self.created_ns}


def start_fake_worker(profile, request, directory):
    if profile["execution_mode"] != "FAKE_ONLY" or profile["real_authorization"] is not None:
        raise ValueError("real_execution_disabled")
    worker = Path(__file__).with_name("worker.py").absolute()
    return OwnedWorker(profile["selection"]["python"], worker, request,
                       profile["max_output_bytes"], directory)
