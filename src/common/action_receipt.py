"""Signed ActionIntent receipts delivered to native agent adapters."""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

from common import config as cfg


def _payload(claims: dict[str, Any]) -> bytes:
    return json.dumps(
        claims, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def sign_action_receipt(claims: dict[str, Any]) -> dict[str, Any]:
    secret = cfg.action_receipt_secret()
    if len(secret) < 16:
        raise RuntimeError(
            "LAS_ACTION_RECEIPT_SECRET must contain at least 16 characters")
    signed = dict(claims)
    signed["signature"] = hmac.new(
        secret.encode("utf-8"), _payload(claims), hashlib.sha256
    ).hexdigest()
    return signed


def verify_action_receipt(receipt: dict[str, Any]) -> bool:
    signature = receipt.get("signature")
    if not isinstance(signature, str):
        return False
    claims = {key: value for key, value in receipt.items()
              if key != "signature"}
    try:
        expected = sign_action_receipt(claims)["signature"]
    except RuntimeError:
        return False
    return hmac.compare_digest(signature, expected)
