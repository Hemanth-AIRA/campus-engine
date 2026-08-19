#!/usr/bin/env python3
"""Functional test for email sending (MSEmailClient) — no real mail is sent.

Mocks the Microsoft Graph token + HTTP POST and asserts the message is CONSTRUCTED
correctly (subject, HTML body, recipients) and that guard-rails hold (empty recipients
are refused). Also covers _resolve_email_recipients (recipient resolution logic).
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from unittest.mock import MagicMock

import integrations.MSEmailClient as mc

ok = True
def check(cond, msg):
    global ok
    ok = ok and cond
    print(f"  {'✅' if cond else '❌'} {msg}")


# ── install mocks: no token service, no network ──────────────────────────────
_fake_app = MagicMock()
_fake_app.acquire_token_for_client.return_value = {"access_token": "TEST_TOKEN"}
mc.msal = MagicMock()
mc.msal.ConfidentialClientApplication.return_value = _fake_app

_captured = {}
class _Resp:
    status_code = 202
    text = ""
    def json(self):
        return {}
def _fake_post(url, headers=None, json=None, timeout=None):
    _captured.clear()
    _captured.update(url=url, headers=headers, json=json)
    return _Resp()
mc.requests.post = _fake_post


if __name__ == "__main__":
    # 1. Recipient resolution: list / dict / recruiter fallback.
    r_list = mc._resolve_email_recipients({"email_config": ["a@x.com", "b@x.com"]})
    check("a@x.com" in r_list and "b@x.com" in r_list, "resolves a list email_config")
    r_dict = mc._resolve_email_recipients({"email_config": {"to": ["c@x.com"]}})
    check("c@x.com" in r_dict, "resolves a dict email_config ('to')")
    r_rec = mc._resolve_email_recipients({"recruiter_email": "rec@x.com"})
    check("rec@x.com" in r_rec, "falls back to recruiter_email when no email_config")

    # 2. send_email constructs the correct Graph payload (mocked send).
    res = mc.send_email("Interview Result", "<b>Congrats</b>", ["to@x.com"], "sender@corp.com")
    msg = (_captured.get("json") or {}).get("message", {})
    check(msg.get("subject") == "Interview Result", "subject set on the outgoing message")
    check(msg.get("body", {}).get("contentType") == "HTML"
          and "Congrats" in msg.get("body", {}).get("content", ""),
          "HTML body carried through")
    check({"emailAddress": {"address": "to@x.com"}} in msg.get("toRecipients", []),
          "recipient placed in toRecipients")
    check("graph.microsoft.com" in (_captured.get("url") or ""), "posts to the Graph sendMail endpoint")
    check("Bearer TEST_TOKEN" in (_captured.get("headers", {}) or {}).get("Authorization", ""),
          "auth token attached to the request")
    check(res is not False, "send_email reports success on a 202")

    # 3. Guard-rail: empty recipients are refused (no send).
    _captured.clear()
    refused = mc.send_email("Subj", "body", [], "sender@corp.com")
    check(refused is False and not _captured, "empty recipients → refused, nothing posted")

    print("\n" + ("🎉 EMAIL SENDING WORKS (payload built + guarded; no real mail sent)"
                  if ok else "⚠️ EMAIL ISSUE — see above"))
    sys.exit(0 if ok else 1)
