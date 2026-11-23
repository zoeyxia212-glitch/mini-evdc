"""Operational behaviour: liveness / readiness probes and graceful shutdown."""
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

from evdc.api import serve
from evdc.app import App

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def get(url: str) -> tuple[int, dict]:
    try:
        with urllib.request.urlopen(url, timeout=2) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


class ProbeTests(unittest.TestCase):
    def setUp(self):
        self.app = App()
        self.server = serve(self.app, port=0)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.app.close)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def test_probes_are_public(self):
        self.assertEqual(get(self.base + "/healthz"), (200, {"status": "alive"}))
        status, body = get(self.base + "/readyz")
        self.assertEqual((status, body["status"]), (200, "ready"))
        self.assertTrue(all(body["checks"].values()))

    def test_not_ready_while_draining(self):
        self.app.draining = True
        status, body = get(self.base + "/readyz")
        self.assertEqual((status, body["checks"]["accepting_traffic"]), (503, False))
        self.assertEqual(get(self.base + "/healthz")[0], 200)  # still alive, just not taking traffic

    def test_not_ready_when_database_is_gone(self):
        self.app.store.close()
        status, body = get(self.base + "/readyz")
        self.assertEqual((status, body["checks"]["database"]), (503, False))

    def test_not_ready_when_scheduler_died(self):
        sched = self.app.start_scheduler(60)
        sched.stop()
        self.assertEqual(get(self.base + "/readyz")[0], 503)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@unittest.skipIf(sys.platform == "win32", "POSIX signals")
class GracefulShutdownTests(unittest.TestCase):
    def test_sigterm_drains_then_exits_cleanly(self):
        port = free_port()
        db = os.path.join(tempfile.mkdtemp(), "evdc.db")
        env = {**os.environ, "EVDC_ADMIN_KEY": "test-key", "PYTHONDONTWRITEBYTECODE": "1"}
        proc = subprocess.Popen([sys.executable, "-m", "evdc", "serve", "--port", str(port), "--db", db,
                                 "--shutdown-grace", "0.6"], cwd=ROOT, env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(lambda: proc.poll() is None and proc.kill())
        base = f"http://127.0.0.1:{port}"
        for _ in range(50):
            try:
                if get(base + "/readyz")[0] == 200:
                    break
            except OSError:
                time.sleep(0.1)
        else:
            self.fail("server did not become ready")

        proc.send_signal(signal.SIGTERM)
        time.sleep(0.2)
        # during the grace period the process still answers, but readiness is off
        status, body = get(base + "/readyz")
        self.assertEqual((status, body["checks"]["accepting_traffic"]), (503, False))
        self.assertEqual(get(base + "/healthz")[0], 200)

        _, err = proc.communicate(timeout=10)
        self.assertEqual(proc.returncode, 0)
        self.assertIn("shutting down", err)
        self.assertIn("shutdown_complete", err)
        # state written before the signal survives the restart
        again = App(db)
        self.addCleanup(again.close)
        self.assertEqual([u["name"] for u in again.store.list_users()], ["admin"])


if __name__ == "__main__":
    unittest.main()
