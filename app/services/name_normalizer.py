from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass
class NameNormalizationResult:
    raw: str
    normalized: str
    short_name: str
    confidence: float
    warnings: list[str] = field(default_factory=list)


_MALE_PATRONYMIC_ENDINGS = ("ович", "евич", "ич")
_FEMALE_PATRONYMIC_ENDINGS = ("овна", "евна", "ична", "инична")


def _clean_name(value: str) -> str:
    text = re.sub(r"\s+", " ", (value or "").strip())
    return text.strip(" ,.;")


def is_probably_not_nominative(full_name: str) -> bool:
    parts = [p for p in _clean_name(full_name).split() if p]
    if len(parts) < 2:
        return False
    suspicious_suffixes = (
        "ого", "его", "ому", "ему", "ой", "ей", "у", "ю",
        "а", "я", "е", "и", "ы",
        "овичу", "евичу", "ичу", "овну", "евну", "ичну",
        "овне", "евне", "ичне",
    )
    suspicious = 0
    for part in parts[:3]:
        lower = part.lower().strip(".,")
        if any(lower.endswith(s) for s in suspicious_suffixes):
            suspicious += 1
    if len(parts) >= 3 and suspicious >= 2:
        return True
    if suspicious >= 1 and any(part.lower().endswith(("ому", "ему", "ой", "евне", "овичу", "ичу")) for part in parts):
        return True
    return False


def _to_nominative_surname(token: str, *, feminine: bool | None = None) -> tuple[str, float]:
    lower = token.lower().strip(".,")
    if lower.endswith(("ского", "цкого", "скому", "цкому")):
        return token[:-3] + "ий", 0.92
    if lower.endswith(("ова", "ева", "ина", "ына")) and feminine is not True:
        return token[:-1], 0.9
    if lower.endswith(("ский", "цкий", "ской", "цкой")):
        if lower.endswith(("ому", "ему")):
            return token[:-3] + "ий", 0.92
        if lower.endswith(("ого", "его")):
            return token[:-3] + "ий", 0.92
        return token, 0.95
    if lower.endswith(("ову", "еву", "ину", "ыну")):
        return token[:-1], 0.9
    if lower.endswith(("овой", "евой", "иной", "ыной")):
        return token[:-2] + "а", 0.9
    if lower.endswith(("ого", "его")):
        stem = token[:-3]
        return stem + ("ая" if feminine else "ов"), 0.88
    if lower.endswith(("ому", "ему")):
        stem = token[:-3]
        if stem.lower().endswith(("ск", "цк", "зьк", "шк", "чк")):
            return stem + "ий", 0.9
        return stem + ("ая" if feminine else "ов"), 0.85
    if lower.endswith("ой") and feminine is not False:
        return token[:-2] + "а", 0.85
    return token, 0.7


def _to_nominative_given(token: str) -> tuple[str, float]:
    lower = token.lower().strip(".,")
    if lower.endswith("не") and len(lower) > 3:
        return token[:-1] + "а", 0.9
    if lower.endswith(("у", "ю")) and len(lower) > 2:
        return token[:-1], 0.9
    if lower.endswith(("а", "я")) and not lower.endswith(("ия", "ья")):
        return token[:-1], 0.85
    if lower.endswith("е") and any(lower.endswith(p) for p in _FEMALE_PATRONYMIC_ENDINGS):
        return token[:-1] + "а", 0.88
    return token, 0.75


def _to_nominative_patronymic(token: str) -> tuple[str, float]:
    lower = token.lower().strip(".,")
    if lower.endswith(("у", "ю")):
        if lower.endswith("ичу"):
            return token[:-1], 0.92
        if lower.endswith(("овну", "евну", "ичну")):
            return token[:-1] + "а", 0.9
        return token[:-1], 0.8
    if lower.endswith(("а", "я")) and any(lower.endswith(p + "а") or lower.endswith(p + "я") for p in ("ович", "евич", "ич")):
        return token[:-1], 0.88
    if lower.endswith(("овне", "евне", "ичне")):
        return token[:-1] + "а", 0.9
    return token, 0.75


def _detect_feminine(parts: list[str]) -> bool | None:
    if len(parts) < 3:
        return None
    patronymic = parts[2].lower().strip(".,")
    if patronymic.endswith(("овна", "евна", "ична", "инична", "овне", "евне", "ичне")):
        return True
    if patronymic.endswith(("ович", "евич", "ич", "овича", "евича", "ича")):
        return False
    if any(patronymic.endswith(p) for p in _FEMALE_PATRONYMIC_ENDINGS):
        return True
    if any(patronymic.endswith(p) for p in _MALE_PATRONYMIC_ENDINGS):
        return False
    if parts[1].lower().endswith(("а", "я")) and not parts[1].lower().endswith(("ия", "ья")):
        return True
    return None


def normalize_person_name_from_ocr(raw_name: str, context: str | None = None) -> NameNormalizationResult:
    raw = _clean_name(raw_name)
    warnings: list[str] = []
    if not raw:
        return NameNormalizationResult(raw=raw, normalized="", short_name="", confidence=0.0, warnings=["empty"])

    parts = [p for p in raw.split() if p]
    if len(parts) < 2:
        return NameNormalizationResult(
            raw=raw,
            normalized=raw,
            short_name=make_short_name(raw),
            confidence=0.4,
            warnings=["too_few_parts"],
        )

    feminine = _detect_feminine(parts)
    confidences: list[float] = []

    surname, c1 = _to_nominative_surname(parts[0], feminine=feminine)
    confidences.append(c1)
    name, c2 = _to_nominative_given(parts[1])
    confidences.append(c2)
    patronymic = ""
    if len(parts) >= 3:
        patronymic, c3 = _to_nominative_patronymic(parts[2])
        confidences.append(c3)

    normalized_parts = [surname, name] + ([patronymic] if patronymic else parts[2:])
    normalized = " ".join(normalized_parts).strip()

    if normalized == raw and is_probably_not_nominative(raw):
        warnings.append("still_suspicious")
        confidence = min(confidences) * 0.7
    elif normalized != raw:
        warnings.append("heuristic_applied")
        confidence = min(confidences)
    else:
        confidence = max(confidences)

    if context and raw.lower() in (context or "").lower():
        confidence = min(1.0, confidence + 0.05)

    return NameNormalizationResult(
        raw=raw,
        normalized=normalized,
        short_name=make_short_name(normalized),
        confidence=round(confidence, 2),
        warnings=warnings,
    )


def make_short_name(full_name: str) -> str:
    parts = [p for p in _clean_name(full_name).split() if p]
    if len(parts) < 2:
        return full_name
    initials = "".join(f"{part[0]}." for part in parts[1:] if part)
    return f"{parts[0]} {initials}".strip()
