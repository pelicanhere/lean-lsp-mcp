# Reproduction tests for declaration-lookup bugs

`lean_diagnostic_messages` raises `"Declaration not found"` when `declaration_name`
is supplied, even when the declaration exists in the file. There are two distinct
root causes. See `../DECLARATION_LOOKUP_BUG.md` for the full analysis.

---

## Run the reproduction tests

```bash
cd lean-lsp-mcp

# Both root causes — integration tests (require Lean + Mathlib)
uv run --extra dev python -m pytest tests/test_declaration_lookup_bugs.py -v -s
```

Both tests are `xfail(strict=True)`: they **always fail** until the bug is fixed,
then automatically turn into test-suite errors, forcing removal of the `xfail` marker.

The `-s` flag prints the timing report from Root Cause 2.

---

## Root Cause 1 — private declarations always absent from `documentSymbol`

**File:** `tests/test_declaration_lookup_bugs.py`  
**Test:** `test_private_declaration_found`  
**Type:** MCP integration test (starts full MCP server, needs Lean)

**What happens:**

```
lean_diagnostic_messages(declaration_name="inter_swap_helper")
  → get_declaration_range(client, path, "inter_swap_helper")
  → client.get_document_symbols(path)        # returns only public decls
  → search_symbols(symbols, "inter_swap_helper")  # returns None
  → raise LeanToolError("Declaration not found")
```

Lean's LSP (`textDocument/documentSymbol`) **never** includes `private` lemmas
in its response. Verified empirically: `get_document_symbols` on a file with a
`private lemma` returns only the public `theorem`. The private helper is absent
from the symbol tree regardless of elaboration state.

**Why always fails:** private decl exclusion is unconditional in Lean's LSP —
not a timing issue.

---

## Root Cause 2 — `documentSymbol` stale after file edit

**File:** `tests/test_declaration_lookup_bugs.py`  
**Test:** `test_document_symbol_stale_immediately_after_edit`  
**Type:** leanclient integration test (direct LSP, no MCP, needs Lean)

**What happens:**

```
Edit tool writes new proof to disk
  → lean_diagnostic_messages(declaration_name="edit_race_target")
  → client.open_file()      detects disk change → update_file()
                            → state.complete = False (re-elaboration starts)
  → get_document_symbols()  waits via _wait_for_diagnostics(inactivity_timeout=5.0)
                            waitForDiagnostics RPC completes (Lean returns quickly)
  → documentSymbol called   → returns [] (symbol table not yet updated)
  → search_symbols([], ...) → None
  → raise LeanToolError("Declaration not found")
```

Observed in session log `refactor_repeat_test_20260425_225604/prove/round_1.txt`:
- `15:00:59` Edit writes real proof to disk
- `15:01:02` `lean_diagnostic_messages` fails (3 s later)
- `15:01:05` `lean_goal` on the same file succeeds (LSP IS processing the file)

The test uses real leanclient against the real Lean LSP to show the race:
- `update_file_content` sends `textDocument/didChange` → re-elaboration begins
- `documentSymbol` called at t≈0ms (bypassing `_wait_for_diagnostics`) → new
  declaration absent (Lean has not rebuilt the symbol table for the new version)
- `get_document_symbols` called after full wait → new declaration present

Sample timing report printed during the test (`-s` flag):
```
--- Race Window Timing Report ---
  t≈0ms  (12ms):   symbols = ['initial_lemma']
  t=full (3204ms): symbols = ['initial_lemma', 'race_target']
  'race_target' at t≈0ms:  False     ← race window: absent
  'race_target' at t=full: True      ← correct after elaboration
```

**Why always fails:** `documentSymbol` for an unelaborated version (t≈0ms after
`didChange`) always returns the pre-edit symbol table — `race_target` is never
present at that moment.

---

## The fix (both root causes)

Replace the `get_document_symbols` → `search_symbols` chain in
`src/lean_lsp_mcp/utils.py:get_declaration_range` with a **text scan** of
`client.get_file_content(path)`:

- `get_file_content` returns what the LSP currently holds — updated immediately on
  every `didChange`, available during re-elaboration, and includes private declarations.
- A regex scan for `[private] lemma|theorem|def <name>` on that content is instant,
  requires no LSP round-trip, and works in all states.

After the fix both `xfail` tests become `xpass` and their markers must be removed.
