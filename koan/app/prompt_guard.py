"""Prompt injection guard for incoming missions.

Scans mission text (from Telegram and GitHub @mentions) before queuing to
missions.md. Detects suspicious patterns: instruction overrides, role
confusion, secret extraction, shell injection, and jailbreak markers.

Complements outbox_scanner.py (output-side defense) with input-side defense.

Usage:
    from app.prompt_guard import scan_mission_text
    result = scan_mission_text(text)
    if result.blocked:
        print(f"Blocked: {result.reason}")
"""

import re
from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class GuardResult:
    """Result of scanning mission text for prompt injection."""

    blocked: bool
    reason: Optional[str] = None
    warnings: Optional[List[str]] = None
    matched_categories: List[str] = field(default_factory=list)


# --- Pattern categories ---
# Each entry: (compiled_regex, description, category, severity)
# severity: "high" = always flag, "medium" = flag by default, "low" = only in strict mode

_INSTRUCTION_OVERRIDE_PATTERNS = [
    (re.compile(
        r'\b(?:ignore|disregard|forget)\b.{0,20}\b(?:previous|prior|above|all|earlier)\b'
        r'.{0,20}\b(?:instructions?|prompts?|rules?|guidelines?|context)\b',
        re.IGNORECASE,
    ), "Instruction override attempt", "instruction_override", "high"),
    (re.compile(
        r'\bnew\s+(?:system\s+)?instructions?\b',
        re.IGNORECASE,
    ), "New instructions injection", "instruction_override", "high"),
    (re.compile(
        r'\b(?:override|replace|rewrite)\b.{0,15}\b(?:system\s+)?prompt\b',
        re.IGNORECASE,
    ), "System prompt override", "instruction_override", "high"),
    (re.compile(
        r'\bfrom\s+now\s+on\b.{0,30}\b(?:you\s+(?:will|must|should)|your\s+(?:role|instructions?))\b',
        re.IGNORECASE,
    ), "Behavioral override via 'from now on'", "instruction_override", "high"),
]

_ROLE_CONFUSION_PATTERNS = [
    (re.compile(
        r'\byou\s+are\s+now\b.{0,30}\b(?:a\s+|an\s+|the\s+)',
        re.IGNORECASE,
    ), "Role reassignment: 'you are now'", "role_confusion", "high"),
    (re.compile(
        r'\b(?:pretend|roleplay|act)\b.{0,10}\b(?:to\s+be|as\s+(?:if\s+you\s+(?:are|were)|a\s+|an\s+))\b',
        re.IGNORECASE,
    ), "Role confusion: pretend/act as", "role_confusion", "medium"),
    (re.compile(
        r'\byour\s+new\s+role\b',
        re.IGNORECASE,
    ), "Role reassignment: 'your new role'", "role_confusion", "high"),
    (re.compile(
        r'\bswitch\s+to\b.{0,15}\b(?:mode|persona|character|identity)\b',
        re.IGNORECASE,
    ), "Identity switch attempt", "role_confusion", "medium"),
]

_SECRET_EXTRACTION_PATTERNS = [
    (re.compile(
        r'\b(?:reveal|show|print|dump|output|display|leak|exfiltrate|expose)\b'
        r'.{0,20}\b(?:api\s*key|credentials?|tokens?|passwords?|secrets?)\b',
        re.IGNORECASE,
    ), "Secret extraction attempt", "secret_extraction", "high"),
    (re.compile(
        r'\b(?:cat|read|type|more|less)\b.{0,10}\.env\b',
        re.IGNORECASE,
    ), "Attempt to read .env file", "secret_extraction", "high"),
    (re.compile(
        r'\b(?:echo|print|output)\b.{0,15}\$\{?(?:KOAN_|API_|SECRET_|TOKEN_|PASSWORD)',
        re.IGNORECASE,
    ), "Environment variable extraction", "secret_extraction", "high"),
]

_SHELL_INJECTION_PATTERNS = [
    # Shell metacharacters in natural-language context combined with dangerous commands
    (re.compile(
        r'(?:`[^`]*(?:curl|wget|nc|ncat|bash|sh|python|ruby|perl|rm\s+-rf)[^`]*`'
        r'|\$\([^)]*(?:curl|wget|nc|ncat|bash|sh|python|ruby|perl|rm\s+-rf)[^)]*\))',
        re.IGNORECASE,
    ), "Shell command injection via backticks/subshell", "shell_injection", "high"),
    (re.compile(
        r';\s*(?:curl|wget|nc|ncat)\s',
        re.IGNORECASE,
    ), "Chained network command injection", "shell_injection", "high"),
    (re.compile(
        r'&&\s*(?:curl|wget|nc|ncat)\s',
        re.IGNORECASE,
    ), "Chained network command injection", "shell_injection", "high"),
    (re.compile(
        r'\|\s*(?:bash|sh|zsh|python|ruby|perl)\b',
        re.IGNORECASE,
    ), "Pipe to shell interpreter", "shell_injection", "high"),
]

_JAILBREAK_PATTERNS = [
    (re.compile(
        r'\bDAN\b(?:\s+mode)?',
    ), "DAN jailbreak marker", "jailbreak", "high"),
    (re.compile(
        r'\b(?:developer|god|admin|root)\s+mode\b',
        re.IGNORECASE,
    ), "Privilege escalation mode", "jailbreak", "high"),
    (re.compile(
        r'\bno\s+(?:restrictions?|limitations?|safety|filters?|guardrails?)\b',
        re.IGNORECASE,
    ), "Safety bypass attempt", "jailbreak", "medium"),
    (re.compile(
        r'\bbypass\b.{0,15}\b(?:safety|security|filters?|guardrails?|restrictions?)\b',
        re.IGNORECASE,
    ), "Explicit safety bypass", "jailbreak", "high"),
    (re.compile(
        r'\bjailbreak\b',
        re.IGNORECASE,
    ), "Explicit jailbreak keyword", "jailbreak", "high"),
]

# All pattern lists, in check order
_ALL_PATTERN_GROUPS = [
    _INSTRUCTION_OVERRIDE_PATTERNS,
    _ROLE_CONFUSION_PATTERNS,
    _SECRET_EXTRACTION_PATTERNS,
    _SHELL_INJECTION_PATTERNS,
    _JAILBREAK_PATTERNS,
]


def scan_mission_text(text: str) -> GuardResult:
    """Scan mission text for prompt injection patterns.

    Checks for instruction overrides, role confusion, secret extraction,
    shell injection, and jailbreak attempts.

    Args:
        text: The mission text to scan.

    Returns:
        GuardResult with blocked=True if injection detected.
    """
    if not text or not text.strip():
        return GuardResult(blocked=False)

    warnings: List[str] = []
    matched_categories: List[str] = []
    block_reason: Optional[str] = None

    for pattern_group in _ALL_PATTERN_GROUPS:
        for pattern, description, category, severity in pattern_group:
            if pattern.search(text):
                if severity == "high":
                    # High severity = immediate block
                    return GuardResult(
                        blocked=True,
                        reason=description,
                        warnings=[description],
                        matched_categories=[category],
                    )
                # Medium/low = warning
                warnings.append(description)
                if category not in matched_categories:
                    matched_categories.append(category)

    # If we accumulated medium-severity warnings, block if 2+ categories matched
    if len(matched_categories) >= 2:
        block_reason = f"Multiple suspicious patterns: {', '.join(warnings)}"
        return GuardResult(
            blocked=True,
            reason=block_reason,
            warnings=warnings,
            matched_categories=matched_categories,
        )

    return GuardResult(
        blocked=bool(warnings),
        reason=warnings[0] if len(warnings) == 1 else None,
        warnings=warnings if warnings else None,
        matched_categories=matched_categories,
    )
