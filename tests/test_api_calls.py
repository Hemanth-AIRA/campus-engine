#!/usr/bin/env python3
"""Functional test for the Hub API layer (aira_sql._api_call) — no live Hub hit.

Mocks the HTTP transport and asserts the engine builds requests correctly (method, URL,
JSON body, params), parses responses, and handles the allow_404 / error paths. This is the
'checking api calls' coverage.
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import integrations.aira_sql as aira_sql

ok = True
def check(cond, msg):
    global ok
    ok = ok and cond
    print(f"  {'✅' if cond else '❌'} {msg}")


_captured = {}
class _Resp:
    def __init__(self, payload=None, status=200, raiser=None):
        self._payload = payload if payload is not None else {"ok": 1}
        self.status_code = status
        self.content = b"x"
        self.text = ""
        self._raiser = raiser
    def raise_for_status(self):
        if self._raiser:
            raise self._raiser
    def json(self):
        return self._payload


def _install(resp):
    def fake_request(method, url, json=None, params=None, timeout=None):
        _captured.clear()
        _captured.update(method=method, url=url, json=json, params=params)
        return resp
    aira_sql._http_requests.request = fake_request


if __name__ == "__main__":
    base = aira_sql._ENGINE_API_BASE

    # 1. GET builds URL from base + path, parses JSON response.
    _install(_Resp({"id": 780}))
    out = aira_sql._api_call("GET", "/interviews/780")
    check(out == {"id": 780}, "GET returns parsed JSON body")
    check(_captured["method"] == "GET" and _captured["url"] == f"{base}/interviews/780",
          "GET builds URL from ENGINE_API_BASE + path")

    # 2. POST forwards the JSON body + params.
    _install(_Resp({"claimed": True}))
    payload = {"engineId": "aira_3", "status": "Queued"}
    aira_sql._api_call("POST", "/interviews/claim", json=payload, params={"x": 1})
    check(_captured["method"] == "POST" and _captured["json"] == payload and _captured["params"] == {"x": 1},
          "POST forwards method + JSON body + params unchanged")

    # 3. allow_404 → returns None instead of raising on a 404.
    import requests as _rq
    err = _rq.exceptions.HTTPError()
    err.response = _Resp(status=404)
    _install(_Resp(status=404, raiser=err))
    got = aira_sql._api_call("GET", "/interviews/999", allow_404=True)
    check(got is None, "allow_404=True → returns None on 404 (no raise)")

    # 4. Non-404 HTTP errors propagate (fail loud).
    err2 = _rq.exceptions.HTTPError()
    err2.response = _Resp(status=500)
    _install(_Resp(status=500, raiser=err2))
    raised = False
    try:
        aira_sql._api_call("GET", "/interviews/500")
    except _rq.exceptions.HTTPError:
        raised = True
    check(raised, "HTTP 500 propagates (loud failure, not silent)")

    print("\n" + ("🎉 API CALLS BUILT + HANDLED CORRECTLY (mocked transport)"
                  if ok else "⚠️ API CALL ISSUE — see above"))
    sys.exit(0 if ok else 1)
