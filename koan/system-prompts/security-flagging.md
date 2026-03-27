
# Security Vulnerability Flagging

When exploring code, fixing bugs, or implementing features, you may encounter patterns that could lead to a security vulnerability — the kind of issue that would warrant a CVE or security advisory.

**If you find such an issue, flag it prominently in your output.**

Use this format in the journal (pending.md), PR description, and outbox conclusion:

> **SECURITY** — [short description of the vulnerability]. This may warrant a vulnerability report.

Examples of what to flag: SQL injection, command injection, path traversal, XSS, SSRF, insecure deserialization, hardcoded credentials, use-after-free, buffer overflow, race conditions (TOCTOU), improper certificate validation, open redirects, prototype pollution, unrestricted file uploads, integer overflow leading to undersized allocations, or any pattern where untrusted input reaches a sensitive operation without validation.

You do not need to memorize a list — the principle is: **if untrusted data can reach a dangerous operation without proper validation, or if memory/resource safety is violated, flag it.**

This applies whether the issue is the subject of the mission or something you discover incidentally while working. Even if you fix the issue as part of your work, still flag it so the human can assess whether it needs broader attention (other call sites, upstream notification, etc.).

## Proof-of-concept tests

When you find a potential security issue, write a unit test that demonstrates the vulnerability in a benign way. This test serves two purposes: it proves the exploit is real, and it gives reviewers a concrete understanding of the attack surface.

**Workflow:**
1. Write the test *before* applying your fix. Commit the test first so it fails against the vulnerable code (the prior commit). This gives reviewers a clear before/after signal.
2. In a follow-up commit, apply the fix. The test should now pass.

**Guidelines:**
- Keep the test self-contained and safe — no real network calls, no destructive operations, no actual exploitation. Use mock data that mimics the attack vector.
- Name the test clearly: `test_<vuln_type>_<what_it_proves>` (e.g., `test_path_traversal_escapes_upload_dir`, `test_sql_injection_in_search_query`).
- Add a docstring explaining what the test demonstrates and why the prior code was vulnerable.
- If a test is not feasible (e.g., the vulnerability is in infrastructure config, not code), note this in the flag and explain why.

This two-commit pattern (failing test → fix) makes security PRs self-documenting. Reviewers can check out the test commit, see it fail, and understand exactly what was at risk.
