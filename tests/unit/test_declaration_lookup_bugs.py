"""
Root Cause 2 reproduction has been promoted to a real leanclient integration test.

See: tests/test_declaration_lookup_bugs.py::test_document_symbol_stale_immediately_after_edit

The previous mock-based test in this file froze the race state artificially.
The integration test demonstrates the real race using leanclient directly:
  - Elaborates a real Lean file
  - Calls textDocument/documentSymbol at t≈0ms after update_file_content
  - Shows the new declaration is absent from the symbol table in the race window
  - Records timing at t≈0ms and after full elaboration

Run it with:
    uv run --extra dev python -m pytest tests/test_declaration_lookup_bugs.py -v -s
"""
