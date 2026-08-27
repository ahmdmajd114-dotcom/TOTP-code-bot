"""Safe parsing helpers for customer-photo intent classification."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass


ALLOWED_PHOTO_INTENTS = {"code_verification", "payment_receipt", "other"}


@dataclass(frozen=True)
class PhotoIntent:
    intent: str = "other"
    confidence: float = 0.0
    description: str = ""


def parse_photo_intent(raw: str) -> PhotoIntent:
    """Parse model JSON while rejecting unknown actions and invalid confidence."""
    match = re.search(r"\{.*\}", raw or "", re.DOTALL)
    if not match:
        return PhotoIntent()
    try:
        value = json.loads(match.group(0))
        intent = value.get("intent")
        confidence = float(value.get("confidence", 0))
        description = value.get("description", "")
    except (TypeError, ValueError, json.JSONDecodeError):
        return PhotoIntent()

    if intent not in ALLOWED_PHOTO_INTENTS or not 0 <= confidence <= 1:
        return PhotoIntent()
    if not isinstance(description, str):
        description = ""
    return PhotoIntent(intent, confidence, description.strip()[:1200])


def is_confident_code_verification(result: PhotoIntent, threshold: float = 0.82) -> bool:
    """Only a strong, explicit verification-screen classification may request a code."""
    return result.intent == "code_verification" and result.confidence >= threshold
