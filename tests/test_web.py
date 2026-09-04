"""The browser gateway: lines replayed from a session's history are flagged
so the page re-renders them without flashing for long-gone bombs."""

import json
import queue
import socket
import threading
import time
import unittest
from http.server import ThreadingHTTPServer

from tichu import web


class FakeStdout:
    """Blocks like a subprocess pipe; feed it lines, close it with None."""

    def __init__(self):
        self.q = queue.Queue()

    def __iter__(self):
        while (item := self.q.get()) is not None:
            yield item


class FakeProc:
    def __init__(self):
        self.stdout = FakeStdout()
        self.stdin = None

    def wait(self, timeout=None):
        return 0


def read_event(f):
    """The JSON payload of the next SSE event on a file object."""
    data = None
    while True:
        line = f.readline()
        if not line:
            return None
        line = line.rstrip(b"\r\n")
        if line.startswith(b"data:"):
            data = line[5:].strip()
        elif not line and data is not None:
            return json.loads(data)


class TestGatewayReplayFlag(unittest.TestCase):
    def test_replayed_lines_are_marked_as_history(self):
        proc = FakeProc()
        sess = web.Session("s1", "Tester", proc, cwd=None)
        for raw in (b"old line\n", b"an old bomb\n"):
            proc.stdout.q.put(raw)
        deadline = time.time() + 5
        with sess.cond:
            while len(sess.lines) < 2 and time.time() < deadline:
                sess.cond.wait(0.1)
        self.assertEqual(sess.lines, ["old line", "an old bomb"])

        gateway = web.Gateway(game_port=0)
        gateway.sessions["s1"] = sess
        web.Handler.gateway = gateway
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
        httpd.daemon_threads = True
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            sock = socket.create_connection(("127.0.0.1", httpd.server_address[1]), timeout=5)
            sock.sendall(b"GET /events?s=s1&i=0 HTTP/1.1\r\nHost: x\r\n\r\n")
            f = sock.makefile("rb")
            while f.readline() not in (b"\r\n", b""):
                pass  # response headers
            # what was already there is history...
            self.assertEqual(read_event(f), {"d": "old line", "h": 1})
            self.assertEqual(read_event(f), {"d": "an old bomb", "h": 1})
            # ...what arrives afterwards is live
            proc.stdout.q.put(b"a live bomb\n")
            self.assertEqual(read_event(f), {"d": "a live bomb"})
            proc.stdout.q.put(None)  # the client process exits
            self.assertEqual(read_event(f), {"end": True})
            sock.close()
        finally:
            httpd.shutdown()
            httpd.server_close()


if __name__ == "__main__":
    unittest.main()
