"""Keep secrets out of memory.

Message text is redacted before it is sent to the language model or the embedding provider,
and every fact is checked again before it is saved. Covers one-time codes, passwords, card
numbers (Luhn-checked), CVVs, bank account and IBAN numbers, and common ID numbers (Aadhaar,
PAN, US SSN). The inbox itself still shows messages as they are: this only governs what the
context engine sends out and remembers.
"""

from __future__ import annotations

import re

_OTP = re.compile(
    r"(?i)(\b(?:otp|o\.t\.p|one[- ]time|verification|verify|security|login|auth(?:entication)?|passcode|pin|code)"
    r"\b[^\d\n]{0,30})(\d(?:[\s-]?\d){3,7})\b")
_OTP_AFTER = re.compile(r"(?i)\b(\d(?:[\s-]?\d){3,7})(\s*(?:is|हैं?|hai)\s+(?:your|the|aapka|apka)?\s*"
                        r"(?:otp|code|verification|pin))")
_PASSWORD = re.compile(r"(?i)\b(password|passwd|pwd|pass ?code|पासवर्ड)(\s*(?:is|hai|है|:|=|-)?\s*)(\S+)")
_CVV = re.compile(r"(?i)\b(cvv2?|cvc|card verification)(\D{0,6})(\d{3,4})\b")
_CARD = re.compile(r"\b(?:\d[ -]?){12,18}\d\b")
_IBAN = re.compile(r"\b[A-Z]{2}\d{2}(?: ?[A-Z0-9]){11,30}\b")
_ACCOUNT = re.compile(r"(?i)\b(a/?c|acct|account|khata)(\s*(?:no\.?|number|num|#)?\s*[:\-]?\s*)(\d{9,18})\b")
# 12 digits grouped 4-4-4 and not part of a longer number (a 16-digit card is handled above).
_AADHAAR = re.compile(r"(?<!\d[ -])(?<!\d)\d{4}[ -]\d{4}[ -]\d{4}(?![ -]?\d)"
                      r"|(?i:\b(?:aadhaa?r|uidai?)\D{0,12}(\d{12})\b)")
_PAN = re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b")
_SSN = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")


def _luhn(digits: str) -> bool:
    total, double = 0, False
    for ch in reversed(digits):
        d = int(ch)
        if double:
            d = d * 2 - 9 if d > 4 else d * 2
        total += d
        double = not double
    return total % 10 == 0


def _card(m: re.Match) -> str:
    digits = re.sub(r"\D", "", m.group(0))
    return "[card number]" if 13 <= len(digits) <= 19 and _luhn(digits) else m.group(0)


def redact(text: str) -> str:
    """``text`` with secrets replaced by a short label."""
    if not text:
        return text
    text = _OTP.sub(lambda m: m.group(1) + "[code]", text)
    text = _OTP_AFTER.sub(lambda m: "[code]" + m.group(2), text)
    text = _PASSWORD.sub(lambda m: m.group(1) + m.group(2) + "[password]", text)
    text = _CVV.sub(lambda m: m.group(1) + m.group(2) + "[cvv]", text)
    text = _ACCOUNT.sub(lambda m: m.group(1) + m.group(2) + "[account number]", text)
    text = _IBAN.sub("[account number]", text)
    text = _CARD.sub(_card, text)
    text = _AADHAAR.sub("[ID number]", text)
    text = _PAN.sub("[ID number]", text)
    text = _SSN.sub("[ID number]", text)
    return text


def is_sensitive(text: str) -> bool:
    """True if ``text`` holds anything ``redact`` would hide, or refers to a hidden value."""
    return redact(text) != text or bool(re.search(r"\[(code|password|cvv|card number|account number|ID number)\]",
                                                  text or ""))
