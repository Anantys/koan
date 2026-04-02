# Silent Failure Analysis

You are performing a focused security and reliability audit on a pull request diff.
Your mission: hunt for **silent failures** — patterns where errors are swallowed,
ignored, or converted into silent no-ops that make bugs invisible in production.

## Pull Request Diff

```diff
{DIFF}
```

---

## What to Look For

Scan the diff for these semantic patterns (language-agnostic):

**Exception/error swallowing**
- Empty catch/except blocks with no logging or re-raise
- Catch-all handlers (`except Exception`, `catch (e) {}`) that only log but don't propagate
- Error returns discarded without checking (ignoring return values that signal failure)

**Silent null/empty returns on error paths**
- Functions that return `None`, `null`, `""`, `[]`, `{}` instead of raising when something goes wrong
- Optional chaining used to mask missing data rather than handle it explicitly

**Fallback values that hide failures**
- `or default_value` / `?? fallback` applied to results that should be validated first
- Default constructors silently replacing failed deserialization

**Fire-and-forget async operations**
- Unhandled promise rejections (missing `.catch()` or `await` without try/catch)
- Background tasks whose failures are never surfaced

**Resource management failures**
- Files, connections, or locks opened but never closed on error paths
- Context managers / `with` blocks missing on code that acquires resources

**Condition inversions and dead error branches**
- `if err != nil { return nil }` (Go pattern: returning nil instead of the error)
- Error checks present but returning the wrong value

---

## Output Format

Respond with a JSON array of findings. Each finding must have:
- `severity`: `"CRITICAL"`, `"HIGH"`, or `"MEDIUM"`
- `file`: the file path from the diff
- `line_hint`: approximate line number or range (as a string, e.g. `"42"` or `"38-45"`)
- `pattern`: short label for the anti-pattern (e.g. `"swallowed exception"`, `"silent null return"`)
- `snippet`: the relevant code snippet (3–6 lines max)
- `explanation`: one concise sentence explaining why this is risky
- `suggestion`: one concise sentence describing the fix

If there are **no findings**, respond with an empty JSON array: `[]`

Do **not** include findings for:
- Intentional no-ops that are clearly documented with a comment
- Test code that deliberately swallows errors for assertion purposes
- Logging-only catch blocks when the logged error is enough (e.g. background cleanup tasks)

Example output:
```json
[
  {
    "severity": "HIGH",
    "file": "src/api/handler.py",
    "line_hint": "47",
    "pattern": "swallowed exception",
    "snippet": "except Exception:\n    pass",
    "explanation": "Any exception from the database call is silently discarded, masking connection failures.",
    "suggestion": "At minimum log the exception and re-raise, or return an explicit error to the caller."
  }
]
```
