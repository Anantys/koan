#!/usr/bin/env python3
"""
Outbox content scanner — defense against agent data exfiltration.

Scans outbox messages before they're sent to Telegram/Slack for patterns
that indicate potential data leakage: secrets, environment variables,
file contents, encoded data, etc.

This is a defense-in-depth layer. The primary defense is Claude's alignment.
This module catches unintentional leaks or prompt-injection-induced exfiltration.

Usage:
    from app.outbox_scanner import scan_outbox_content
    result = scan_outbox_content(content)
    if result.blocked:
        print(f"Blocked: {result.reason}")
    else:
        send_message(content)
"""

import re
from dataclasses import dataclass
from typing import List, Optional


@dataclass
class ScanResult:
    """Result of scanning outbox content."""

    blocked: bool
    reason: Optional[str] = None
    warnings: Optional[List[str]] = None


# Patterns that indicate secrets or credentials
_SECRET_PATTERNS = [
    # API keys and tokens (generic)
    (re.compile(r'(?:api[_-]?key|api[_-]?secret|access[_-]?token|auth[_-]?token'
                r'|bearer|secret[_-]?key|private[_-]?key)\s*[=:]\s*\S{8,}',
                re.IGNORECASE), "API key or token"),
    # Bot tokens (Telegram, Slack, Discord)
    (re.compile(r'\d{8,}:[A-Za-z0-9_-]{30,}'), "Bot token (Telegram-like)"),
    (re.compile(r'xoxb-[0-9a-zA-Z-]{20,}'), "Slack bot token"),
    (re.compile(r'xoxp-[0-9a-zA-Z-]{20,}'), "Slack user token"),
    # AWS keys
    (re.compile(r'AKIA[0-9A-Z]{16}'), "AWS access key"),
    (re.compile(r'(?:aws_secret_access_key|aws_session_token)\s*[=:]\s*\S{20,}',
                re.IGNORECASE), "AWS secret"),
    # GitHub tokens
    (re.compile(r'gh[ps]_[A-Za-z0-9_]{36,}'), "GitHub token"),
    (re.compile(r'github_pat_[A-Za-z0-9_]{20,}'), "GitHub PAT"),
    # Generic high-entropy strings that look like secrets (64+ hex chars)
    (re.compile(r'(?:password|passwd|pwd|secret|token)\s*[=:]\s*\S{12,}',
                re.IGNORECASE), "Password or secret value"),
    # SSH private keys
    (re.compile(r'-----BEGIN\s+(?:RSA|EC|OPENSSH|DSA)\s+PRIVATE\s+KEY-----'),
     "SSH private key"),
    # JWT tokens
    (re.compile(r'eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}'),
     "JWT token"),
]

# Patterns that indicate .env file content being leaked
_ENV_LEAK_PATTERNS = [
    (re.compile(r'^[A-Z][A-Z_]{4,}=\S+', re.MULTILINE), ".env variable assignment"),
    (re.compile(r'KOAN_TELEGRAM_TOKEN\s*[=:]\s*\S+', re.IGNORECASE),
     "Telegram token variable"),
    (re.compile(r'KOAN_SLACK_BOT_TOKEN\s*[=:]\s*\S+', re.IGNORECASE),
     "Slack token variable"),
    (re.compile(r'(?:DATABASE_URL|DB_PASSWORD|POSTGRES_PASSWORD)\s*[=:]\s*\S+',
                re.IGNORECASE), "Database credential"),
]

# Patterns that indicate encoded/obfuscated data exfiltration
_ENCODING_PATTERNS = [
    # Large base64 blocks (>200 chars of base64 — avoids matching long URLs/hashes)
    (re.compile(r'[A-Za-z0-9+/]{200,}={0,2}'), "Large base64-encoded block"),
    # Hex-encoded data (>128 chars of hex — avoids matching SHA-256/SHA-512 hashes)
    (re.compile(r'(?:0x)?[0-9a-fA-F]{128,}'), "Large hex-encoded block"),
]

# Patterns that indicate file path content dumps
_FILE_DUMP_PATTERNS = [
    # Content that looks like a cat/read of a dotfile
    (re.compile(r'Contents?\s+of\s+[~/.].*\.(env|key|pem|crt|json|yaml|yml|conf|cfg)',
                re.IGNORECASE), "File content dump of sensitive file"),
]

# Threshold for env-like lines (multiple KEY=VALUE lines suggest .env dump)
_ENV_LINE_THRESHOLD = 3


def scan_outbox_content(content: str) -> ScanResult:
    """Scan outbox content for potential data leakage.

    Checks for secrets, credentials, encoded data, and file content dumps.
    Returns a ScanResult indicating whether the content should be blocked.

    Args:
        content: The outbox message content to scan

    Returns:
        ScanResult with blocked=True if content should not be sent
    """
    if not content or not content.strip():
        return ScanResult(blocked=False)

    warnings = []

    # Check for secret patterns (BLOCK)
    for pattern, description in _SECRET_PATTERNS:
        if pattern.search(content):
            return ScanResult(
                blocked=True,
                reason=f"Potential secret detected: {description}",
                warnings=[description],
            )

    # Check for .env leaks (BLOCK if multiple env-like lines)
    env_line_count = 0
    for pattern, description in _ENV_LEAK_PATTERNS:
        matches = pattern.findall(content)
        if matches:
            # Specific known-sensitive vars are always blocked
            if "token" in description.lower() or "credential" in description.lower():
                return ScanResult(
                    blocked=True,
                    reason=f"Sensitive variable detected: {description}",
                    warnings=[description],
                )
            env_line_count += len(matches)

    if env_line_count >= _ENV_LINE_THRESHOLD:
        return ScanResult(
            blocked=True,
            reason=f"Possible .env dump detected ({env_line_count} variable assignments)",
            warnings=[f"{env_line_count} env-like variable assignments"],
        )
    elif env_line_count > 0:
        warnings.append(f"{env_line_count} env-like variable assignment(s)")

    # Check for encoded data exfiltration (BLOCK)
    for pattern, description in _ENCODING_PATTERNS:
        if pattern.search(content):
            return ScanResult(
                blocked=True,
                reason=f"Potential encoded exfiltration: {description}",
                warnings=[description],
            )

    # Check for file content dumps (WARN only — could be legitimate)
    for pattern, description in _FILE_DUMP_PATTERNS:
        if pattern.search(content):
            warnings.append(description)

    return ScanResult(
        blocked=False,
        warnings=warnings if warnings else None,
    )


def scan_and_log(content: str) -> ScanResult:
    """Scan content and log results to stderr.

    Convenience wrapper that prints warnings/blocks to stderr for visibility
    in the awake.py console output.

    Args:
        content: The outbox message content to scan

    Returns:
        ScanResult from scan_outbox_content()
    """
    import sys

    result = scan_outbox_content(content)

    if result.blocked:
        print(f"[scanner] BLOCKED outbox message: {result.reason}", file=sys.stderr)
        # Log the first 100 chars for forensics (redact the rest)
        preview = content[:100].replace("\n", " ")
        print(f"[scanner] Preview: {preview}...", file=sys.stderr)
    elif result.warnings:
        for warning in result.warnings:
            print(f"[scanner] WARNING in outbox: {warning}", file=sys.stderr)

    return result
