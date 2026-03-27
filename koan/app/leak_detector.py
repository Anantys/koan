"""Credential leak detector for outbound messages.

Scans outbound content (PR descriptions, issue bodies, journal entries)
for accidental credential leaks and redacts them before they reach
external services.

This complements outbox_scanner.py (which blocks entire outbox messages)
by providing fine-grained redaction for content that should still be sent
but with secrets removed.

Usage:
    from app.leak_detector import redact_secrets
    clean, detected = redact_secrets(content)
    if detected:
        warn_human(detected)
"""

import re
import sys
from typing import List, Tuple

# Each entry: (compiled regex, human-readable label)
_PATTERNS: List[Tuple[re.Pattern, str]] = [
    # Stripe keys
    (re.compile(r'[sr]k_live_[A-Za-z0-9]{20,}'), "Stripe live key"),
    (re.compile(r'[sr]k_test_[A-Za-z0-9]{20,}'), "Stripe test key"),
    (re.compile(r'pk_live_[A-Za-z0-9]{20,}'), "Stripe publishable key"),
    (re.compile(r'pk_test_[A-Za-z0-9]{20,}'), "Stripe test publishable key"),

    # OpenAI / Anthropic API keys
    (re.compile(r'sk-[A-Za-z0-9]{32,}'), "OpenAI/Anthropic API key"),

    # GitHub tokens
    (re.compile(r'ghp_[A-Za-z0-9]{36,}'), "GitHub personal access token"),
    (re.compile(r'gho_[A-Za-z0-9]{36,}'), "GitHub OAuth token"),
    (re.compile(r'ghs_[A-Za-z0-9]{36,}'), "GitHub server token"),
    (re.compile(r'ghr_[A-Za-z0-9]{36,}'), "GitHub refresh token"),
    (re.compile(r'github_pat_[A-Za-z0-9_]{20,}'), "GitHub fine-grained PAT"),

    # AWS access keys
    (re.compile(r'AKIA[0-9A-Z]{16}'), "AWS access key ID"),

    # JWT tokens
    (re.compile(r'eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}'),
     "JWT token"),

    # Database connection strings
    (re.compile(
        r'(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis|amqp)://'
        r'[^\s]{10,}',
        re.IGNORECASE,
    ), "Database connection string"),

    # PEM private keys (match up to closing marker, multiline)
    (re.compile(
        r'-----BEGIN\s+(?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----'
        r'[\s\S]*?'
        r'-----END\s+(?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----',
    ), "PEM private key"),

    # Generic password/token assignments
    (re.compile(
        r'(?:password|passwd|pwd|secret|token|api_key|apikey|access_key)'
        r'\s*[=:]\s*["\']?([^\s"\']{12,})',
        re.IGNORECASE,
    ), "Credential assignment"),

    # Telegram bot tokens (numeric:alphanumeric)
    (re.compile(r'\d{8,}:[A-Za-z0-9_-]{30,}'), "Telegram bot token"),

    # Slack tokens
    (re.compile(r'xox[bprs]-[0-9a-zA-Z-]{20,}'), "Slack token"),
]

_REDACTED = "[REDACTED]"


def redact_secrets(content: str) -> Tuple[str, List[str]]:
    """Scan content and redact any detected credentials.

    Args:
        content: Text to scan.

    Returns:
        Tuple of (redacted_content, list_of_detection_labels).
        If no secrets found, returns (original_content, []).
    """
    if not content:
        return content, []

    detected: List[str] = []
    redacted = content

    for pattern, label in _PATTERNS:
        if pattern.search(redacted):
            redacted = pattern.sub(_REDACTED, redacted)
            if label not in detected:
                detected.append(label)

    return redacted, detected


def scan_and_redact(content: str, context: str = "") -> str:
    """Scan, redact, and log any detected secrets.

    Convenience wrapper that logs warnings to stderr.

    Args:
        content: Text to scan.
        context: Where the content is going (e.g. "PR body", "journal").

    Returns:
        Redacted content.
    """
    redacted, detected = redact_secrets(content)
    if detected:
        prefix = f"[leak-detector] {context}: " if context else "[leak-detector] "
        for label in detected:
            print(f"{prefix}Redacted {label}", file=sys.stderr)
    return redacted
