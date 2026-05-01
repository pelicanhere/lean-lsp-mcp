"""
Integration-level regressions for declaration-lookup bugs.

Root Cause 1: private declarations absent from textDocument/documentSymbol
  → lean_diagnostic_messages must still find them via current file text.

Root Cause 2: documentSymbol returns stale (old/empty) symbols in the race window
  immediately after update_file_content, before Lean finishes re-elaborating;
  get_declaration_range must use current file text before falling back to symbols.

See DECLARATION_LOOKUP_BUG.md for full analysis.
"""

from __future__ import annotations

import textwrap
import time
from collections.abc import Callable
from pathlib import Path
from typing import AsyncContextManager

import pytest

from lean_lsp_mcp.utils import get_declaration_range
from tests.helpers.mcp_client import MCPClient


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _collect_names(symbols: list) -> list[str]:
    """Recursively extract all declaration names from a documentSymbol tree."""
    names = []
    for s in symbols:
        names.append(s["name"])
        if "children" in s:
            names.extend(_collect_names(s["children"]))
    return names


# ---------------------------------------------------------------------------
# Root Cause 1 — private declarations always absent from documentSymbol
# ---------------------------------------------------------------------------

_PRIVATE_DECL_LEAN = textwrap.dedent(
    """
    import Mathlib

    -- private: Lean LSP excludes this from textDocument/documentSymbol
    private lemma inter_swap_helper {α : Type*} {s t : Set α} {x : α}
        (h : x ∈ s ∩ t) : x ∈ t ∩ s :=
      ⟨h.2, h.1⟩

    -- public: present in textDocument/documentSymbol
    theorem inter_comm_repro {α : Type*} (s t : Set α) : s ∩ t = t ∩ s := by
      ext x; exact ⟨inter_swap_helper, inter_swap_helper⟩
    """
).strip()


@pytest.fixture(scope="module")
def private_decl_file(test_project_path: Path) -> Path:
    path = test_project_path / "PrivateDeclTest.lean"
    content = _PRIVATE_DECL_LEAN + "\n"
    if not path.exists() or path.read_text(encoding="utf-8") != content:
        path.write_text(content, encoding="utf-8")
    return path


@pytest.mark.asyncio
async def test_private_declaration_found(
    mcp_client_factory: Callable[[], AsyncContextManager[MCPClient]],
    private_decl_file: Path,
) -> None:
    """
    lean_diagnostic_messages with declaration_name targeting a private lemma
    must return diagnostics (not raise MCPToolError).

    Regression coverage for private declarations, which are absent from
    textDocument/documentSymbol but present in the LSP's current file content.
    """
    async with mcp_client_factory() as client:
        # Sanity: public theorem is reachable
        await client.call_tool(
            "lean_diagnostic_messages",
            {
                "file_path": str(private_decl_file),
                "declaration_name": "inter_comm_repro",
            },
        )

        await client.call_tool(
            "lean_diagnostic_messages",
            {
                "file_path": str(private_decl_file),
                "declaration_name": "inter_swap_helper",
            },
        )


# ---------------------------------------------------------------------------
# Root Cause 2 — documentSymbol stale after file edit (race condition)
# ---------------------------------------------------------------------------

_EDIT_RACE_INITIAL = (
    "import Mathlib.Tactic\n\n"
    "lemma initial_lemma : 1 = 1 := by rfl\n"
)


@pytest.fixture(scope="module")
def edit_race_file(test_project_path: Path) -> Path:
    path = test_project_path / "EditRaceTest.lean"
    if not path.exists() or path.read_text(encoding="utf-8") != _EDIT_RACE_INITIAL:
        path.write_text(_EDIT_RACE_INITIAL, encoding="utf-8")
    return path


def test_document_symbol_stale_immediately_after_edit(
    test_project_path: Path,
    edit_race_file: Path,
) -> None:
    """
    Demonstrates the real race condition using leanclient directly (no mocks).

    Scenario (mirrors session log refactor_repeat_test_20260425_225604):
      15:00:59  Edit writes new proof to disk
      15:01:02  lean_diagnostic_messages(declaration_name="mem_inter_swap") failed
      15:01:05  lean_goal on same file → SUCCESS (LSP is processing the file fine)

    Race window reproduced here:
      1. Elaborate EditRaceTest.lean (initial_lemma present in symbol table)
      2. update_file_content appends race_target → triggers re-elaboration (didChange)
      3. documentSymbol called at t≈0ms, bypassing _wait_for_diagnostics
      4. Lean is re-elaborating version N+1 → race_target ABSENT from symbol table
      5. Same call after full elaboration → race_target PRESENT

    get_declaration_range must still find the new declaration during this window
    by scanning get_file_content before falling back to documentSymbol.
    """
    from leanclient.client import LeanLSPClient

    rel_path = edit_race_file.relative_to(test_project_path).as_posix()

    client = LeanLSPClient(str(test_project_path), prevent_cache_get=True)
    try:
        # Step 1: Open and fully elaborate the file
        client.open_file(rel_path)
        client.get_document_symbols(rel_path)  # wait for complete elaboration

        # Step 2: Insert a new lemma on disk and in the LSP view.
        # update_file_content sends didChange and sets state.complete = False.
        new_content = _EDIT_RACE_INITIAL + (
            "\nlemma race_target {α : Type*} {s t : Set α} {x : α}\n"
            "    (h : x ∈ s ∩ t) : x ∈ t ∩ s :=\n"
            "  ⟨h.2, h.1⟩\n"
        )
        edit_race_file.write_text(new_content, encoding="utf-8")
        t0 = time.monotonic()
        client.update_file_content(rel_path, new_content)

        # Step 3: Call documentSymbol at t≈0ms, bypassing _wait_for_diagnostics
        # Lean has received didChange but has not finished re-elaborating version N+1
        with client._opened_files_lock:
            state = client.opened_files[rel_path]
            uri = state.uri
            version = state.version

        params = {"textDocument": {"uri": uri, "version": version}}
        symbols_0ms = client._send_request_sync("textDocument/documentSymbol", params)
        t_0ms = (time.monotonic() - t0) * 1000
        names_0ms = _collect_names(symbols_0ms)

        decl_range = get_declaration_range(client, rel_path, "race_target")

        # Step 4: Full wait — Lean eventually rebuilds the document symbols
        symbols_full = client.get_document_symbols(rel_path)
        t_full = (time.monotonic() - t0) * 1000
        names_full = _collect_names(symbols_full)

        print(
            f"\n--- Race Window Timing Report ---\n"
            f"  t≈0ms  ({t_0ms:.0f}ms):   symbols = {names_0ms}\n"
            f"  t=full ({t_full:.0f}ms):  symbols = {names_full}\n"
            f"  'race_target' at t≈0ms:  {'race_target' in names_0ms}\n"
            f"  'race_target' at t=full: {'race_target' in names_full}\n"
        )

        assert "race_target" not in names_0ms, (
            f"Expected stale documentSymbol during the race window, got {names_0ms}"
        )
        assert decl_range == (5, 7)
        assert "race_target" in names_full
    finally:
        edit_race_file.write_text(_EDIT_RACE_INITIAL, encoding="utf-8")
        client.close()
