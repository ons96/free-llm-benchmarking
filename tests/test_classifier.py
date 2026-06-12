"""Mocked tests for fallback_chain.classify_probe_result.

Covers all branches defined in the spec:
  - 401/403 invalid key       -> EXCLUDE (24h)
  - 404 model not found       -> QUARANTINE (7d)
  - 429 / 5xx / timeout       -> COOLDOWN (1h, transient)
  - malformed JSON            -> SKIP (0s, no record)
  - tool-call unsupported     -> DOWNGRADE (6h)
  - success (2xx + body)      -> OK (0s)
  - network exceptions        -> COOLDOWN
  - unknown status            -> COOLDOWN (defensive)
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts.fallback_chain import classify_probe_result, ClassAction, COOLDOWN_S


@pytest.mark.parametrize(
    "status_code,body,exc,has_tool_call,expected_action,expected_reason_prefix",
    [
        # 2xx + tool_call -> OK
        (200, '{"ok":1,"tool_calls":[]}', None, True, ClassAction.OK, "ok"),
        # 2xx without tool_call -> DOWNGRADE
        (200, '{"ok":1}', None, False, ClassAction.DOWNGRADE, "no_tool_call"),
        # 401 -> EXCLUDE
        (401, "invalid api key", None, False, ClassAction.EXCLUDE, "auth_401"),
        # 403 -> EXCLUDE
        (403, "forbidden", None, False, ClassAction.EXCLUDE, "auth_403"),
        # 404 -> QUARANTINE
        (404, "model not found", None, False, ClassAction.QUARANTINE, "model_not_found"),
        # 429 -> COOLDOWN
        (429, "rate limited", None, False, ClassAction.COOLDOWN, "http_429"),
        # 5xx -> COOLDOWN
        (500, "internal", None, False, ClassAction.COOLDOWN, "http_500"),
        (502, "bad gateway", None, False, ClassAction.COOLDOWN, "http_502"),
        (503, "unavailable", None, False, ClassAction.COOLDOWN, "http_503"),
        # 400 -> SKIP
        (400, "bad request body", None, False, ClassAction.SKIP, "http_400"),
        # None status -> SKIP
        (None, "", None, False, ClassAction.SKIP, "no_status"),
        # Unknown 4xx -> COOLDOWN (defensive)
        (418, "teapot", None, False, ClassAction.COOLDOWN, "other_418"),
        # Network exception -> COOLDOWN with network: prefix
        (None, "", TimeoutError("read timed out"), False, ClassAction.COOLDOWN, "network:TimeoutError"),
        (None, "", ConnectionError("refused"), False, ClassAction.COOLDOWN, "network:ConnectionError"),
    ],
)
def test_classify_branches(
    status_code, body, exc, has_tool_call, expected_action, expected_reason_prefix
):
    cls = classify_probe_result(
        status_code=status_code, body=body, exception=exc, has_tool_call=has_tool_call
    )
    assert cls.action == expected_action, f"got {cls.action} for {status_code}/{exc}"
    assert cls.reason.startswith(expected_reason_prefix), f"reason={cls.reason!r}"
    # Cooldown durations must match spec
    if cls.action == ClassAction.OK or cls.action == ClassAction.SKIP:
        assert cls.cooldown_s == 0
    else:
        assert cls.cooldown_s == COOLDOWN_S[cls.action]


def test_classify_2xx_with_tool_call_is_ok():
    cls = classify_probe_result(200, '{"choices":[],"tool_calls":[1]}', None, True)
    assert cls.action == ClassAction.OK
    assert cls.cooldown_s == 0


def test_classify_2xx_streaming_assumed_tool_capable():
    # If a probe returns 2xx with no tool call, we still record DOWNGRADE
    # so tool-heavy vms demote non-tool models. Title-fast vms ignore this.
    cls = classify_probe_result(201, "", None, False)
    assert cls.action == ClassAction.DOWNGRADE


def test_classify_400_keeps_reason_short():
    # Body is truncated to 50 chars in reason to keep table lean
    cls = classify_probe_result(400, "x" * 200, None, False)
    assert cls.action == ClassAction.SKIP
    assert len(cls.reason) <= len("http_400:") + 50
