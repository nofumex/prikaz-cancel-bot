from app.services.documents import _review_blocks_delivery, _safe_review_fixes


FIXED_ADDRESS = "\u0433. \u0415\u0441\u0441\u0435\u043d\u0442\u0443\u043a\u0438, \u0443\u043b. \u0412\u043e\u043b\u043e\u0434\u0430\u0440\u0441\u043a\u043e\u0433\u043e, \u0434. 14"


def test_ai_review_safe_fix_requires_confidence_and_safe_field():
    data = {"debtor_address": "\u0430\u0434\u0440\u0435\u0441: \u0433. \u0410\u0447\u0438\u043d\u0441\u043a, \u0437\u0430\u0440\u0435\u0433\u0438\u0441\u0442\u0440\u0438\u0440\u043e\u0432\u0430\u043d\u043d\u043e\u043c\u0443 \u0432 \u0433\u043e\u0440\u043e\u0434\u0435 \u0415\u0441\u0441\u0435\u043d\u0442\u0443\u043a\u0438"}
    review = {
        "issues": [
            {
                "field": "debtor_address",
                "severity": "blocker",
                "confidence": 0.95,
                "suggested_fix": FIXED_ADDRESS,
            },
            {"field": "case_number", "severity": "blocker", "confidence": 0.99, "suggested_fix": "2-123/2026"},
        ],
        "clean_fields": {
            "debtor_address": FIXED_ADDRESS,
            "case_number": "2-123/2026",
        },
    }

    assert _safe_review_fixes(data, review) == {"debtor_address": FIXED_ADDRESS}


def test_ai_review_blocks_delivery_when_blocker_has_no_safe_fix():
    review = {
        "ok": False,
        "severity": "blocker",
        "needs_regeneration": False,
        "issues": [
            {"field": "debtor_address", "severity": "blocker", "confidence": 0.5, "suggested_fix": ""},
        ],
    }

    assert _review_blocks_delivery(review, {}) is True


def test_ai_review_allows_delivery_after_auto_fixed_blocker():
    review = {
        "ok": False,
        "severity": "blocker",
        "needs_regeneration": True,
        "issues": [
            {"field": "debtor_address", "severity": "blocker", "confidence": 0.95, "suggested_fix": FIXED_ADDRESS},
        ],
    }

    assert _review_blocks_delivery(review, {"debtor_address": FIXED_ADDRESS}) is False
