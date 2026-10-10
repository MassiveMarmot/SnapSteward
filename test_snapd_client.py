# SPDX-License-Identifier: GPL-3.0-or-later
import json
import os
import socket
import tempfile
import threading
import time
import unittest
import urllib.parse

import snapd_client
from snapd_client import Client, SnapdError, MUTATION_TIMEOUT


class MockSnapd:
    DELAY = 1.0

    def __init__(self, socket_path):
        self.socket_path = socket_path
        self.snaps = []
        self.requests = []  # (method, path) in order
        self.connections = {}  # snap name -> connections result dict
        self.default_connections = {"established": [], "undesired": [],
                                   "plugs": [], "slots": []}
        self.posts = []  # (path, parsed_body, interaction_allowed) in order
        self.responses = []  # (status, body) overrides, consumed in order
        self.interface_responses = []  # (status, body) for POST /v2/interfaces
        self.change_script = {}  # change id -> [result dicts], consumed per poll
        self.next_change_id = 1
        self.delay = 0  # seconds to sleep before answering POST /v2/interfaces
        self.running = True
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.listener.bind(socket_path)
        self.listener.listen(4)
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def stop(self):
        self.running = False
        self.listener.close()
        try:
            os.unlink(self.socket_path)
        except FileNotFoundError:
            pass

    def _serve(self):
        while self.running:
            try:
                conn, _ = self.listener.accept()
            except OSError:
                return
            with conn:
                data = b""
                while b"\r\n\r\n" not in data:
                    chunk = conn.recv(4096)
                    if not chunk:
                        return
                    data += chunk
                head, body = data.split(b"\r\n\r\n", 1)
                lines = head.split(b"\r\n")
                method, path, _ = lines[0].decode().split(" ", 2)
                headers = {}
                length = 0
                for line in lines[1:]:
                    name, _, value = line.decode().partition(":")
                    headers[name.strip().lower()] = value.strip()
                    if name.strip().lower() == "content-length":
                        length = int(value)
                while len(body) < length:
                    chunk = conn.recv(4096)
                    if not chunk:
                        break
                    body += chunk
                try:
                    self._handle(conn, method, path, body, headers)
                except BrokenPipeError:
                    pass

    def apply_interface_change(self, parsed):
        """Apply a connect or disconnect to default_connections so
        a sequence of steps converges like real snapd."""
        action = parsed.get("action")
        conns = self.default_connections
        plug = (parsed.get("plugs") or [{}])[0]
        slot = (parsed.get("slots") or [{}])[0]
        key = {"plug": {"snap": plug.get("snap"), "plug": plug.get("plug")},
               "slot": {"snap": slot.get("snap"), "slot": slot.get("slot")}}
        established = [e for e in conns.get("established", [])
                       if (e.get("plug") or {}).get("plug")
                       != plug.get("plug")]
        if action == "connect":
            interface = next(
                (p.get("interface") for p in conns.get("plugs", [])
                 if p.get("plug") == plug.get("plug")), plug.get("plug"))
            established.append({**key, "interface": interface,
                                "manual": True})
        conns["established"] = established

    def _handle(self, conn, method, path, body, headers):
        self.requests.append((method, path))
        if self.delay and method == "POST" and path == "/v2/interfaces":
            time.sleep(self.delay)
        if self.responses:
            status, payload = self.responses.pop(0)
        elif method == "GET" and path.startswith("/v2/connections"):
            query = urllib.parse.parse_qs(urllib.parse.urlparse(path).query)
            snap = (query.get("snap") or [None])[0]
            if snap is not None and snap in self.connections:
                result = self.connections[snap]
            else:
                result = self.default_connections
            status, payload = 200, {"type": "sync", "status-code": 200,
                                    "result": result}
        elif method == "POST" and path == "/v2/interfaces":
            parsed = json.loads(body)
            self.posts.append((path, parsed,
                               headers.get("x-allow-interaction") == "true"))
            if not self.interface_responses \
                    and headers.get("x-allow-interaction") == "true":
                self.apply_interface_change(parsed)
            if self.interface_responses:
                status, payload = self.interface_responses.pop(0)
            elif headers.get("x-allow-interaction") != "true":
                status, payload = 401, {
                    "type": "error", "status-code": 401,
                    "result": {"message": "access denied",
                               "kind": "login-required"}}
            else:
                change_id = str(self.next_change_id)
                self.next_change_id += 1
                self.change_script.setdefault(
                    change_id,
                    [{"status": "Doing", "ready": False, "err": None},
                     {"status": "Done", "ready": True, "err": None,
                      "summary": "done"}])
                status, payload = 202, {
                    "type": "async", "status": "Accepted",
                    "status-code": 202, "result": None,
                    "change": change_id}
        elif method == "GET" and path.startswith("/v2/changes/"):
            change_id = urllib.parse.unquote(
                path[len("/v2/changes/"):].split("?")[0])
            script = self.change_script.get(change_id)
            if script:
                result = script.pop(0) if len(script) > 1 else script[0]
                status, payload = 200, {"type": "sync",
                                        "status-code": 200,
                                        "result": result}
            else:
                status, payload = 404, {
                    "type": "error", "status-code": 404,
                    "result": {"message": "change not found",
                               "kind": "not-found"}}
        elif method == "GET" and path.startswith("/v2/change/"):
            status, payload = 404, {
                "type": "error", "status-code": 404,
                "result": {"message": "not found", "kind": "not-found"}}
        elif method == "GET" and path == "/v2/snaps":
            status, payload = 200, {"type": "sync", "status-code": 200,
                                    "result": self.snaps}
        else:
            status, payload = 404, {"type": "error", "status-code": 404,
                                    "result": {"message": "not found",
                                               "kind": "not-found"}}
        wire = payload if isinstance(payload, bytes) else \
            json.dumps(payload).encode()
        conn.sendall(("HTTP/1.1 %d X\r\nContent-Length: %d\r\n\r\n"
                      % (status, len(wire))).encode())
        conn.sendall(wire)


class SnapdClientTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.socket_path = os.path.join(self.tmpdir.name, "snapd.socket")
        self.server = MockSnapd(self.socket_path)
        self.client = Client(socket_path=self.socket_path)

    def tearDown(self):
        self.server.stop()
        self.tmpdir.cleanup()

    def error_response(self, kind, message="error", status=400):
        self.server.responses.append((status, {
            "type": "error", "status-code": status,
            "result": {"message": message, "kind": kind},
        }))

    def test_list_snaps(self):
        self.server.snaps = [{"name": "firefox", "type": "app",
                              "version": "1.0", "summary": "browser"},
                             {"name": "core24", "type": "base"}]
        snaps = self.client.list_snaps()
        self.assertEqual(len(snaps), 2)
        self.assertEqual(snaps[0]["name"], "firefox")

    def test_error_with_kind(self):
        self.error_response("some-kind", "conflict")
        with self.assertRaises(SnapdError) as ctx:
            self.client.list_snaps()
        self.assertEqual(ctx.exception.kind, "some-kind")
        self.assertEqual(ctx.exception.message, "conflict")

    def test_connection_failure(self):
        bad = Client(socket_path=os.path.join(self.tmpdir.name,
                                              "missing.socket"))
        with self.assertRaises(SnapdError) as ctx:
            bad.list_snaps()
        self.assertEqual(ctx.exception.kind, "connection-failed")

    def test_malformed_json(self):
        self.server.responses.append((200, b"not-json"))
        with self.assertRaises(SnapdError) as ctx:
            self.client.list_snaps()
        self.assertIn("malformed", ctx.exception.message)

    def test_non_dict_json(self):
        self.server.responses.append((200, [1, 2]))
        with self.assertRaises(SnapdError):
            self.client.list_snaps()

    def test_non_dict_error_result(self):
        self.server.responses.append((400, {
            "type": "error", "status-code": 400, "result": "oops",
        }))
        with self.assertRaises(SnapdError) as ctx:
            self.client.list_snaps()
        self.assertEqual(ctx.exception.message, "snapd error")


class ConnectionsTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.socket_path = os.path.join(self.tmpdir.name, "snapd.socket")
        self.server = MockSnapd(self.socket_path)
        self.client = Client(socket_path=self.socket_path)
        self.result = {"established": [
            {"slot": {"snap": "snapd", "slot": "camera"},
             "plug": {"snap": "firefox", "plug": "camera"},
             "interface": "camera", "manual": True}],
            "undesired": [
                {"slot": {"snap": "snapd", "slot": "removable-media"},
                 "plug": {"snap": "firefox", "plug": "removable-media"},
                 "interface": "removable-media", "manual": True}],
            "plugs": [{"snap": "firefox", "plug": "camera",
                       "interface": "camera", "apps": [],
                       "connections": [{"snap": "snapd", "slot": "camera"}]}],
            "slots": [{"snap": "snapd", "slot": "camera",
                       "interface": "camera",
                       "connections": [{"snap": "firefox",
                                        "plug": "camera"}]}]}

    def tearDown(self):
        self.server.stop()
        self.tmpdir.cleanup()

    def test_list_connections_with_snap(self):
        self.server.connections["firefox"] = self.result
        result = self.client.list_connections("firefox")
        self.assertEqual(result["established"][0]["plug"]["snap"], "firefox")
        self.assertEqual(result["undesired"][0]["plug"]["plug"],
                         "removable-media")
        self.assertTrue(result["established"][0]["manual"])

    def test_list_connections_without_snap(self):
        self.server.default_connections = self.result
        result = self.client.list_connections()
        self.assertEqual(len(result["established"]), 1)

    def test_snap_name_quoted_in_query(self):
        self.server.connections["a b&c"] = self.result
        result = self.client.list_connections("a b&c")
        self.assertEqual(result["established"][0]["plug"]["snap"], "firefox")

    def test_connections_no_header_sent_on_read(self):
        self.server.connections["firefox"] = self.result
        self.client.list_connections("firefox")
        self.assertEqual(self.server.posts, [])


class MutationTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.socket_path = os.path.join(self.tmpdir.name, "snapd.socket")
        self.server = MockSnapd(self.socket_path)
        self.client = Client(socket_path=self.socket_path)

    def tearDown(self):
        self.server.stop()
        self.tmpdir.cleanup()

    def test_change_interface_sends_header_and_body(self):
        change_id = self.client.change_interface(
            "connect", "firefox", "camera", "snapd", "camera")
        self.assertEqual(change_id, "1")
        path, body, allowed = self.server.posts[0]
        self.assertEqual(path, "/v2/interfaces")
        self.assertTrue(allowed)
        self.assertEqual(body, {
            "action": "connect",
            "plugs": [{"snap": "firefox", "plug": "camera"}],
            "slots": [{"snap": "snapd", "slot": "camera"}]})

    def test_change_interface_403_auth_cancelled(self):
        self.server.interface_responses.append((403, {
            "type": "error", "status-code": 403,
            "result": {"message": "auth cancelled",
                       "kind": "auth-cancelled"}}))
        with self.assertRaises(SnapdError) as ctx:
            self.client.change_interface("disconnect", "firefox", "camera",
                                         "snapd", "camera")
        self.assertEqual(ctx.exception.kind, "auth-cancelled")
        self.assertEqual(ctx.exception.status_code, 403)

    def test_change_interface_snapd_error(self):
        self.server.interface_responses.append((500, {
            "type": "error", "status-code": 500,
            "result": {"message": "snapd exploded",
                       "kind": "some-kind"}}))
        with self.assertRaises(SnapdError) as ctx:
            self.client.change_interface("connect", "firefox", "camera",
                                         "snapd", "camera")
        self.assertEqual(ctx.exception.status_code, 500)
        self.assertEqual(ctx.exception.message, "snapd exploded")

    def test_change_interface_rejects_unknown_action(self):
        with self.assertRaises(SnapdError) as ctx:
            self.client.change_interface("install", "firefox", "camera",
                                         "snapd", "camera")
        self.assertEqual(self.server.posts, [])

    def test_change_interface_waits_for_slow_polkit(self):
        self.server.delay = MockSnapd.DELAY
        start = time.monotonic()
        change_id = self.client.change_interface(
            "connect", "firefox", "camera", "snapd", "camera",
            timeout=MockSnapd.DELAY + 10)
        elapsed = time.monotonic() - start
        self.assertEqual(change_id, "1")
        self.assertGreaterEqual(elapsed, MockSnapd.DELAY)

    def test_change_interface_times_out_with_request_timeout(self):
        self.server.delay = 5
        with self.assertRaises(SnapdError) as ctx:
            self.client.change_interface("connect", "firefox", "camera",
                                         "snapd", "camera", timeout=1)
        self.assertEqual(ctx.exception.kind, "request-timeout")
        self.server.delay = 0


class ChangeTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.socket_path = os.path.join(self.tmpdir.name, "snapd.socket")
        self.server = MockSnapd(self.socket_path)
        self.client = Client(socket_path=self.socket_path)

    def tearDown(self):
        self.server.stop()
        self.tmpdir.cleanup()

    def start_change(self):
        return self.client.change_interface(
            "connect", "firefox", "camera", "snapd", "camera")

    def test_get_change_done(self):
        change_id = self.start_change()
        self.server.change_script[change_id] = [
            {"status": "Done", "ready": True, "err": None,
             "summary": "done"}]
        change = self.client.get_change(change_id)
        self.assertEqual(change["status"], "Done")
        self.assertTrue(change["ready"])

    def test_get_change_error_with_err(self):
        change_id = self.start_change()
        self.server.change_script[change_id] = [
            {"status": "Error", "ready": True, "err": "cannot connect",
             "summary": "failed"}]
        change = self.client.get_change(change_id)
        self.assertEqual(change["err"], "cannot connect")

    def test_get_change_not_found(self):
        with self.assertRaises(SnapdError) as ctx:
            self.client.get_change("999")
        self.assertEqual(ctx.exception.status_code, 404)

    def test_change_endpoint_without_s_404(self):
        self.server.responses.append((404, {
            "type": "error", "status-code": 404,
            "result": {"message": "not found", "kind": "not-found"}}))
        with self.assertRaises(SnapdError) as ctx:
            self.client.get_change("1")
        self.assertEqual(ctx.exception.status_code, 404)

    def test_wait_for_change_polls_to_done(self):
        change_id = self.start_change()
        sleeps = []
        change = self.client.wait_for_change(
            change_id, sleep=sleeps.append,
            monotonic=FakeClock().monotonic)
        self.assertEqual(change["status"], "Done")
        self.assertEqual(sleeps, [0.5])

    def test_wait_for_change_error(self):
        change_id = self.start_change()
        self.server.change_script[change_id] = [
            {"status": "Error", "ready": True, "err": "cannot connect",
             "summary": "failed"}]
        with self.assertRaises(SnapdError) as ctx:
            self.client.wait_for_change(change_id, sleep=lambda s: None,
                                        monotonic=FakeClock().monotonic)
        self.assertEqual(ctx.exception.kind, "change-error")
        self.assertIn("cannot connect", ctx.exception.message)

    def test_wait_for_change_timeout(self):
        change_id = self.start_change()
        self.server.change_script[change_id] = [
            {"status": "Doing", "ready": False, "err": None}]
        sleeps = []
        with self.assertRaises(SnapdError) as ctx:
            self.client.wait_for_change(change_id, timeout=10,
                                        sleep=sleeps.append,
                                        monotonic=FakeClock(step=5).monotonic)
        self.assertEqual(ctx.exception.kind, "change-timeout")
        self.assertEqual(sleeps, [0.5])


class FakeClock:
    def __init__(self, step=0.5):
        self.now = 1000.0
        self.step = step

    def monotonic(self):
        self.now += self.step
        return self.now


class AllowlistTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.socket_path = os.path.join(self.tmpdir.name, "snapd.socket")
        self.server = MockSnapd(self.socket_path)
        self.client = Client(socket_path=self.socket_path)

    def tearDown(self):
        self.server.stop()
        self.tmpdir.cleanup()

    def assert_denied(self, method, path):
        with self.assertRaises(SnapdError) as cm:
            self.client._request(method, path)
        self.assertEqual(cm.exception.kind, "not-allowed")

    def test_allowed_calls_pass(self):
        cases = [
            ("GET", "/v2/snaps"),
            ("GET", "/v2/connections"),
            ("GET", "/v2/connections?snap=a&select=all"),
            ("POST", "/v2/interfaces"),
            ("GET", "/v2/changes/25"),
            ("GET", "/v2/changes/abc-DEF-12"),
        ]
        for method, path in cases:
            with self.subTest(method=method, path=path):
                self.assertIsNone(snapd_client.check_allowed(method, path))

    def test_denied_calls_raise_without_connecting(self):
        cases = [
            ("DELETE", "/v2/snaps"),
            ("PUT", "/v2/connections"),
            ("POST", "/v2/snaps"),
            ("GET", "/v2/snaps/foo"),
            ("GET", "/v2/changes/"),
            ("GET", "/v2/changes/a/b"),
            ("GET", "/v2/changes/../../etc"),
            ("GET", "/v2/change/25"),
            ("GET", "/v2/interfaces"),
            ("GET", "/v2/system-info"),
            ("GET", "/v2/apps"),
            ("POST", "/v2/interfaces/requests"),
            ("GET", "v2/snaps"),
            ("GET", "/v2/connections%3Fselect%3Dall"),
            ("GET", "/run/snapd.socket"),
            ("GET", "/v2/changes/ok;rm-rf"),
            ("GET", "/v2/changes/ok id"),
            ("POST", "/v2/interfaces?x=1"),
            ("GET", "/v2/changes/25?x=1"),
            ("GET", "/v2/snaps?x=1"),
        ]
        for method, path in cases:
            with self.subTest(method=method, path=path):
                self.assert_denied(method, path)
                self.assertEqual(self.server.requests, [])

    def test_allowed_methods_only_on_exact_paths(self):
        self.assert_denied("GET", "/v2/connectionsx")
        self.assert_denied("GET", "/V2/SNAPS")


if __name__ == "__main__":
    unittest.main()
