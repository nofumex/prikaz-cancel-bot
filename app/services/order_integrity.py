from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from app.services.legal_data import clean_case_number, money_to_decimal, normalize_order_data
from app.utils import parse_russian_date


# Fields that can change the legal meaning of the generated statement.  They are
# independently read from the source image and compared before generation.
CRITICAL_ORDER_FIELDS = (
    "court_name",
    "judge",
    "debtor_full_name",
    "debtor_address",
    "creditor_name",
    "creditor_address",
    "case_number",
    "uid",
    "order_date",
    "debt_contract",
    "debt_period",
    "debt_amount",
    "state_duty",
    "total_amount",
)


def evidence_field_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "value": {"type": "string"},
            "source_fragment": {"type": "string"},
        },
        "required": ["value", "source_fragment"],
    }


def build_order_evidence_schema(fields: tuple[str, ...] | list[str]) -> dict[str, Any]:
    selected = tuple(field for field in fields if field in CRITICAL_ORDER_FIELDS)
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "fields": {
                "type": "object",
                "additionalProperties": False,
                "properties": {field: evidence_field_schema() for field in selected},
                "required": list(selected),
            },
            "document_comment": {"type": "string"},
        },
        "required": ["fields", "document_comment"],
    }


ORDER_EVIDENCE_SCHEMA = build_order_evidence_schema(CRITICAL_ORDER_FIELDS)


@dataclass
class FieldEvidence:
    value: str = ""
    source_fragment: str = ""


@dataclass
class IntegrityDecision:
    data: dict[str, Any]
    conflicts: list[str] = field(default_factory=list)
    unresolved_fields: list[str] = field(default_factory=list)
    applied_fields: dict[str, str] = field(default_factory=dict)
    evidence: dict[str, dict[str, Any]] = field(default_factory=dict)


def evidence_payload_fields(payload: dict[str, Any] | None) -> dict[str, FieldEvidence]:
    raw_fields = (payload or {}).get("fields")
    if not isinstance(raw_fields, dict):
        return {}
    result: dict[str, FieldEvidence] = {}
    for name in CRITICAL_ORDER_FIELDS:
        raw = raw_fields.get(name)
        if not isinstance(raw, dict):
            continue
        result[name] = FieldEvidence(
            value=str(raw.get("value") or "").strip(),
            source_fragment=str(raw.get("source_fragment") or "").strip(),
        )
    return result


def _canonical_text(value: Any) -> str:
    text = str(value or "").lower().replace("ё", "е")
    text = text.replace("«", '"').replace("»", '"')
    text = re.sub(r"\s+", " ", text)
    return text.strip(" \t\r\n,.;\"")


def _canonical_court(value: Any) -> str:
    text = _canonical_text(value)
    text = re.sub(r"^(?:мировому судье|мировым судьей|мировой судья|мировой суд)\s+", "", text)
    text = re.sub(r"^судебный участок", "судебного участка", text)
    return text


def canonical_field_value(field: str, value: Any) -> str:
    if field in {"debt_amount", "state_duty", "total_amount"}:
        parsed = money_to_decimal(value)
        return f"{parsed:.2f}" if parsed is not None else _canonical_text(value)
    if field == "order_date":
        parsed = parse_russian_date(value)
        return parsed.isoformat() if parsed else _canonical_text(value)
    if field == "case_number":
        text = clean_case_number(value)
        text = re.sub(r"\s*/\s*", "/", text)
        text = re.sub(r"\s*-\s*", "-", text)
        return _canonical_text(text)
    if field == "court_name":
        return _canonical_court(value)
    if field == "uid":
        return re.sub(r"\s+", "", _canonical_text(value))
    return _canonical_text(value)


def conflicting_fields(primary: dict[str, Any], verifier: dict[str, FieldEvidence]) -> list[str]:
    conflicts: list[str] = []
    for field in CRITICAL_ORDER_FIELDS:
        primary_value = primary.get(field)
        verified = verifier.get(field)
        if not verified or not verified.value:
            if canonical_field_value(field, primary_value):
                conflicts.append(field)
            continue
        left = canonical_field_value(field, primary_value)
        right = canonical_field_value(field, verified.value)
        if left and right and left != right:
            conflicts.append(field)
        elif not left and right:
            conflicts.append(field)
    return conflicts


def merge_verified_order_data(
    primary: dict[str, Any],
    verifier_payload: dict[str, Any] | None,
    adjudicator_payload: dict[str, Any] | None = None,
) -> IntegrityDecision:
    normalized_primary = normalize_order_data(primary)
    verifier = evidence_payload_fields(verifier_payload)
    conflicts = conflicting_fields(normalized_primary, verifier)
    adjudicator = evidence_payload_fields(adjudicator_payload)
    merged = dict(normalized_primary)
    applied: dict[str, str] = {}
    unresolved: list[str] = []
    audit: dict[str, dict[str, Any]] = {}

    for field in CRITICAL_ORDER_FIELDS:
        verified = verifier.get(field)
        judged = adjudicator.get(field)
        audit[field] = {
            "primary": str(normalized_primary.get(field) or ""),
            "verifier": verified.value if verified else "",
            "verifier_fragment": verified.source_fragment if verified else "",
            "adjudicator": judged.value if judged else "",
            "adjudicator_fragment": judged.source_fragment if judged else "",
        }
        candidate = ""
        # A field is trusted because two independent readings agree, or because
        # an image-grounded adjudicator explicitly resolves their conflict.
        if field in conflicts and judged and judged.value:
            candidate = judged.value
        elif field in conflicts:
            unresolved.append(field)
        if candidate and canonical_field_value(field, merged.get(field)) != canonical_field_value(field, candidate):
            merged[field] = candidate
            applied[field] = candidate

    return IntegrityDecision(
        data=normalize_order_data(merged),
        conflicts=conflicts,
        unresolved_fields=unresolved,
        applied_fields=applied,
        evidence=audit,
    )
