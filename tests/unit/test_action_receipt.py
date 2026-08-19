"""ActionIntent receipt signing and tamper detection."""

from common.action_receipt import sign_action_receipt, verify_action_receipt


def test_action_receipt_is_signed_and_bound_to_claims(monkeypatch):
    monkeypatch.setenv(
        "LAS_ACTION_RECEIPT_SECRET", "test-secret-0123456789abcdef")
    receipt = sign_action_receipt({
        "actionIntentId": "AI-1", "status": "approved",
        "decidedBy": "user", "taskId": "T-1",
        "interactionId": "dsh:rpc-1", "nativeRequestId": "rpc-1",
    })
    assert verify_action_receipt(receipt) is True
    assert verify_action_receipt({**receipt, "taskId": "T-other"}) is False
