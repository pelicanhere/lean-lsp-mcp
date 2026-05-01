# Bug: `lean_diagnostic_messages` fails with "Declaration not found"

## Problem

Calling `lean_diagnostic_messages` with `declaration_name` raises:

```
Error executing tool lean_diagnostic_messages: Declaration '<name>' not found in file.
```

even when the declaration **is** in the file.

Two confirmed root causes, both in `get_declaration_range` in `src/lean_lsp_mcp/utils.py:367`.

---

## Root Cause 1 — Private declarations (always fails)

`get_declaration_range` calls `client.get_document_symbols()` and searches by name.
Lean's LSP **excludes `private` declarations from `textDocument/documentSymbol`** by design.

Verified with `leanclient` against `test_work.lean` (which has three `private lemma` helpers):
```
# documentSymbol output — private lemmas are absent:
<section>
  EMetric → uniformEquicontinuousOn_iff_forall_edist_le   ✓ public
  Metric  → uniformEquicontinuousOn_iff_forall_dist_le    ✓ public
  ...
# edist_inter_prod_mem_iff          ← MISSING (private)
# forall_edist_le_inter_prod_imp_iff ← MISSING (private)
# forall_dist_le_inter_prod_imp_iff  ← MISSING (private)
```

`search_symbols` at `utils.py:355` does an exact name match and returns `None` → "not found".

---

## Root Cause 2 — Public declarations immediately after a file edit (race condition)

Observed in `session_logs/refactor_repeat_test_20260425_225604/prove/round_1.txt`:

```
15:00:59  Edit tool  →  mem_inter_swap proof written to disk
15:01:02  lean_diagnostic_messages(declaration_name="mem_inter_swap")  →  FAILS
15:01:05  lean_goal(line=5)  →  SUCCESS (LSP is elaborating the file fine)
```

Sequence:
1. `Edit` writes new file content to disk.
2. `lean_diagnostic_messages` calls `client.open_file()` → detects disk change → calls `update_file()` → `state.complete = False`.
3. `get_document_symbols` calls `_wait_for_diagnostics([uri], inactivity_timeout=5.0)`.
4. `waitForDiagnostics` RPC completes (Lean may return early before the symbol table is fully updated).
5. `textDocument/documentSymbol` is called → returns `[]` or symbols for the pre-edit state → name not found.

`lean_goal` works because it queries the **live elaboration snapshot** directly. `documentSymbol` queries the **finalized symbol table**, which lags behind.

Note: `get_diagnostics` and `get_document_symbols` both call `_wait_for_diagnostics` internally — there is no "live push" equivalent for document symbols. The LSP protocol does not stream declaration ranges incrementally the way `publishDiagnostics` streams errors.

---

## Fix

Replace the `documentSymbol` lookup in `get_declaration_range` with a **text scan** of `client.get_file_content(path)`.

`get_file_content` returns the exact content the LSP currently holds — updated immediately on every `didChange`, available during elaboration, and includes private declarations.

**File:** `src/lean_lsp_mcp/utils.py`  
**Function:** `get_declaration_range` (line 367)

Replace the body with:

```python
def get_declaration_range(
    client, file_path: str, declaration_name: str
) -> tuple[int, int] | None:
    import re
    from lean_lsp_mcp.server import logger

    try:
        client.open_file(file_path)
        content = client.get_file_content(file_path)
    except Exception as e:
        logger.warning("Failed to read file content for '%s': %s", file_path, e)
        return None

    lines = content.splitlines()

    # Match: [private|protected] theorem|lemma|def|... <name>
    decl_re = re.compile(
        r"^(?:private\s+|protected\s+)?"
        r"(?:noncomputable\s+)?"
        r"(?:theorem|lemma|def|abbrev|class|instance|structure|inductive)\s+"
        + re.escape(declaration_name)
        + r"\b"
    )
    start = None
    for i, line in enumerate(lines):
        if decl_re.match(line.lstrip()):
            start = i + 1  # 1-indexed
            break

    if start is None:
        return None

    # End: line before the next top-level declaration keyword, or EOF
    next_decl_re = re.compile(
        r"^(?:private\s+|protected\s+)?"
        r"(?:noncomputable\s+)?"
        r"(?:theorem|lemma|def|abbrev|class|instance|structure|inductive|end\b|section\b|namespace\b)"
    )
    end = len(lines)
    for i in range(start, len(lines)):  # start is 1-indexed; lines[start] is line after decl header
        if next_decl_re.match(lines[i].lstrip()):
            end = i  # exclusive, so last line of prev decl = i (1-indexed)
            break

    return (start, end)
```

Remove the now-unused `search_symbols` and `get_document_symbols` imports/calls from this function.

---

## Verify

1. Call `lean_diagnostic_messages` with `declaration_name` set to a `private lemma` in any file → should return diagnostics (or empty if no errors), not "not found".
2. Write a file with `by sorry`, call `lean_diagnostic_messages` with its name → should work.
3. Edit the file (replace `sorry` with a real proof), immediately call `lean_diagnostic_messages` again → should work without the race condition.

Existing tests in `tests/test_diagnostic_line_range.py::test_diagnostic_messages_declaration_filtering` cover the public declaration case and should still pass.

---

## What NOT to change

- `search_symbols` and `generate_outline` in `outline_utils.py` — those use `documentSymbol` for a different purpose (building the file outline) and are not affected by this bug.
- The `_wait_for_diagnostics` timeout in `get_document_symbols` — not relevant after this fix since `get_declaration_range` no longer calls `get_document_symbols`.
