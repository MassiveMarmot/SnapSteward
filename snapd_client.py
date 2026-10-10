# SPDX-License-Identifier: GPL-3.0-or-later
import http.client
import json
import os
import re
import socket
import time
import urllib.parse


DEFAULT_SOCKET = "/run/snapd.socket"
POLL_INTERVAL_START = 0.5
POLL_INTERVAL_MAX = 2.0
POLL_TIMEOUT = 60.0
MUTATION_TIMEOUT = 120

ALLOWED_CALLS = (
    ("GET", "/v2/snaps"),
    ("GET", "/v2/connections"),
    ("POST", "/v2/interfaces"),
)
CHANGE_ID_RE = re.compile(r"[A-Za-z0-9-]+")


def check_allowed(method, path):
    base, _, query = path.partition("?")
    if (method, base) in ALLOWED_CALLS:
        if query and (method, base) != ("GET", "/v2/connections"):
            raise SnapdError("query not allowed: %s" % path,
                             kind="not-allowed")
        return
    if method == "GET" and base.startswith("/v2/changes/") \
            and not query \
            and re.fullmatch(CHANGE_ID_RE, base[len("/v2/changes/"):]):
        return
    raise SnapdError("call not allowed: %s %s" % (method, path),
                     kind="not-allowed")


class SnapdError(Exception):
    def __init__(self, message, kind=None, status_code=None):
        super().__init__(message)
        self.message = message
        self.kind = kind
        self.status_code = status_code


class UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, socket_path, timeout=10):
        super().__init__("localhost", timeout=timeout)
        self.socket_path = socket_path

    def connect(self):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.settimeout(self.timeout)
            sock.connect(self.socket_path)
        except OSError:
            sock.close()
            raise
        self.sock = sock


class Client:
    def __init__(self, socket_path=None):
        self.socket_path = socket_path or os.environ.get("SNAPD_SOCKET",
                                                         DEFAULT_SOCKET)

    def _request(self, method, path, body=None, allow_interaction=False,
                 timeout=10):
        check_allowed(method, path)
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json"}
        if allow_interaction:
            headers["X-Allow-Interaction"] = "true"
        conn = UnixHTTPConnection(self.socket_path, timeout=timeout)
        try:
            conn.request(method, path, body=data, headers=headers)
            resp = conn.getresponse()
            raw = resp.read()
        except socket.timeout as e:
            raise SnapdError(str(e), kind="request-timeout")
        except OSError as e:
            raise SnapdError(str(e), kind="connection-failed")
        finally:
            conn.close()
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            raise SnapdError("snapd returned malformed JSON",
                             status_code=resp.status)
        if not isinstance(payload, dict):
            raise SnapdError("unexpected snapd response",
                             status_code=resp.status)
        if payload.get("type") == "error":
            result = payload.get("result")
            if not isinstance(result, dict):
                result = {}
            raise SnapdError(result.get("message", "snapd error"),
                             kind=result.get("kind"), status_code=resp.status)
        return payload

    def list_snaps(self):
        result = self._request("GET", "/v2/snaps")
        if not isinstance(result.get("result"), list):
            raise SnapdError("unexpected snaps response")
        return result["result"]

    def list_connections(self, snap=None, select="all"):
        query = {"select": select}
        if snap is not None:
            query["snap"] = snap
        path = "/v2/connections?" + urllib.parse.urlencode(query)
        result = self._request("GET", path).get("result")
        if not isinstance(result, dict):
            raise SnapdError("unexpected connections response")
        return result

    def change_interface(self, action, plug_snap, plug, slot_snap, slot,
                         timeout=MUTATION_TIMEOUT):
        if action not in ("connect", "disconnect"):
            raise SnapdError("invalid action: %s" % action)
        body = {"action": action,
                "plugs": [{"snap": plug_snap, "plug": plug}],
                "slots": [{"snap": slot_snap, "slot": slot}]}
        payload = self._request("POST", "/v2/interfaces", body,
                                allow_interaction=True, timeout=timeout)
        change = payload.get("change")
        if not change:
            raise SnapdError("snapd did not return a change id")
        return str(change)

    def get_change(self, change_id):
        path = "/v2/changes/" + urllib.parse.quote(str(change_id), safe="")
        result = self._request("GET", path).get("result")
        if not isinstance(result, dict):
            raise SnapdError("unexpected change response")
        return result

    def wait_for_change(self, change_id, timeout=POLL_TIMEOUT,
                        sleep=time.sleep, monotonic=time.monotonic):
        deadline = monotonic() + timeout
        interval = POLL_INTERVAL_START
        while True:
            change = self.get_change(change_id)
            err = change.get("err")
            if err:
                raise SnapdError(str(err), kind="change-error")
            if change.get("status") == "Done" and change.get("ready"):
                return change
            if change.get("ready") or change.get("status") == "Error":
                raise SnapdError(str(change.get("summary") or "change failed"),
                                 kind="change-error")
            if monotonic() >= deadline:
                raise SnapdError("Timed out waiting for change",
                                 kind="change-timeout")
            sleep(interval)
            interval = min(interval * 2, POLL_INTERVAL_MAX)
