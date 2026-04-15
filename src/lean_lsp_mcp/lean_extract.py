"""lean_extract.py — insert extract wrappers into Lean source files.

Exported:
    run_lean_extract(client, abs_path, rel_path, jobs) -> LeanExtractBatchResult
"""

from dataclasses import dataclass
import re
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from leanclient import LeanLSPClient

from lean_lsp_mcp.models import (
    DiagnosticMessage,
    LeanExtractApplied,
    LeanExtractBatchApplied,
    LeanExtractBatchResult,
    LeanExtractJob,
)

_SEVERITY: dict[int, str] = {1: "error", 2: "warning", 3: "info", 4: "hint"}
TOP_LEVEL_DECL_RE = re.compile(
    r"^(?P<kind>theorem|lemma|def|example)\s+"
    r"(?P<name>[^\s(:=]+)"
)


@dataclass(slots=True)
class _TopLevelDecl:
    kind: str
    name: str
    start: int
    end: int


# ---------------------------------------------------------------------------
# Block-finding helpers
# ---------------------------------------------------------------------------

def check_overlap(start: int, end: int, used_ranges: List[Tuple[int, int]]) -> bool:
    for r_start, r_end in used_ranges:
        if start < r_end and end > r_start:
            return True
    return False


def find_code_block(
    original_content: str,
    code_block: str,
    used_ranges: List[Tuple[int, int]],
) -> Optional[Tuple[int, int]]:
    """Find code_block in original_content, skipping used_ranges.

    Tries exact match first, then fuzzy indent-insensitive match.
    Returns (start_idx, end_idx) or None.
    """
    # 1. Exact match
    search_start = 0
    while True:
        idx = original_content.find(code_block, search_start)
        if idx == -1:
            break
        end = idx + len(code_block)
        if not check_overlap(idx, end, used_ranges):
            return (idx, end)
        search_start = end

    # 2. Fuzzy match — strip each line, build whitespace-tolerant regex
    lines = [line.strip() for line in code_block.splitlines() if line.strip()]
    if not lines:
        return None

    escaped_lines = [re.escape(line) for line in lines]
    pattern_parts = []
    for i, line in enumerate(escaped_lines):
        if i == 0:
            pattern_parts.append(r"[ \t]*" + line)
        else:
            pattern_parts.append(r"\s+" + line)
    pattern_str = "".join(pattern_parts)

    try:
        fuzzy_regex = re.compile(pattern_str)
        for match in fuzzy_regex.finditer(original_content):
            f_start, f_end = match.span()
            first_line = lines[0]
            first_line_offset = match.group(0).find(first_line)
            if first_line_offset != -1:
                f_start += first_line_offset
            if not check_overlap(f_start, f_end, used_ranges):
                print(f"Info: Used fuzzy match for block: {lines[0][:30]}...")
                return (f_start, f_end)
    except re.error as e:
        print(f"Warning: Failed to compile fuzzy regex: {e}")

    return None


def _decode_block(code_block: str) -> str:
    """Decode the minimal escaped newline forms emitted in extraction JSON."""
    return code_block.replace("\\r\\n", "\n").replace("\\n", "\n").replace("\r\n", "\n")


# ---------------------------------------------------------------------------
# Indentation helpers
# ---------------------------------------------------------------------------

def _get_first_line_indent(content: str, start_idx: int) -> str:
    """Return the leading whitespace of the line that contains start_idx."""
    line_start = content.rfind("\n", 0, start_idx)
    if line_start == -1:
        line_start = 0
    else:
        line_start += 1
    prefix = content[line_start:start_idx]
    if not prefix or prefix.isspace():
        return prefix

    # If matching starts after non-whitespace on the same line
    # (for example in `· intro h`), keep the visual column so the
    # extracted block body is not forced to column 1.
    return " " * len(prefix)


def _get_last_line_indent(block_text: str) -> str:
    """Return the leading whitespace of the last non-empty line in block_text."""
    for line in reversed(block_text.splitlines()):
        if line.strip():
            m = re.match(r"([ \t]*)", line)
            return m.group(1) if m else ""
    return ""


# ---------------------------------------------------------------------------
# Import helper (no LSP)
# ---------------------------------------------------------------------------

def _ensure_extraction_import(content: str) -> Tuple[str, bool]:
    """Add `import Extraction` after the last import line if absent.

    Returns (new_content, changed).
    """
    if re.search(r"^import Extraction\b", content, re.MULTILINE):
        return content, False

    lines = content.splitlines(keepends=True)
    last_import_idx = -1
    for i, line in enumerate(lines):
        if line.strip().startswith("import "):
            last_import_idx = i

    insertion = "import Extraction\n"
    if last_import_idx >= 0:
        lines.insert(last_import_idx + 1, insertion)
    else:
        lines.insert(0, insertion)

    return "".join(lines), True


# ---------------------------------------------------------------------------
# Diagnostics helper
# ---------------------------------------------------------------------------

def _collect_diagnostics(
    diag_result,
) -> Tuple[List[DiagnosticMessage], bool]:
    """Convert leanclient get_diagnostics result to (list[DiagnosticMessage], success)."""
    if diag_result is None:
        return [], False

    # With inactivity_timeout the result has .diagnostics + .success
    if hasattr(diag_result, "diagnostics"):
        raw = diag_result.diagnostics or []
        success = getattr(diag_result, "success", True)
    else:
        raw = list(diag_result)
        success = True

    items: List[DiagnosticMessage] = []
    for diag in raw:
        r = diag.get("range", {})
        start = r.get("start", {})
        line = start.get("line", 0) + 1
        col = start.get("character", 0) + 1
        sev_int = diag.get("severity", 1)
        msg = diag.get("message", "")
        items.append(
            DiagnosticMessage(
                severity=_SEVERITY.get(sev_int, "error"),
                message=msg,
                line=line,
                column=col,
            )
        )
    return items, success


# ---------------------------------------------------------------------------
# Content builder
# ---------------------------------------------------------------------------

def _line_of(content: str, idx: int) -> int:
    """Return 1-indexed line number for character position idx."""
    return content[:idx].count("\n") + 1


def _build_modified_content(
    original_content: str,
    jobs: List[LeanExtractJob],
) -> Tuple[str, List[dict]]:
    """Insert extract wrappers for all jobs.

    Returns (modified_content, applied_meta_list).
    Each applied_meta dict has: owner_decl, name, start_idx, end_idx,
    block_start_line, block_end_line, occurrence_index.
    """
    used_ranges: List[Tuple[int, int]] = []
    found: List[dict] = []   # {owner_decl, name, start_idx, end_idx}
    name_counts: dict[str, int] = {}  # (owner_decl, name) → count

    for job in jobs:
        owner = job.owner_decl
        for spec in job.extractions:
            decoded_block = _decode_block(spec.block)
            result = find_code_block(original_content, decoded_block, used_ranges)
            if result is None:
                print(
                    f"Warning: block not found for {owner}.{spec.name}: "
                    f"{spec.block[:50]!r}..."
                )
                continue
            start_idx, end_idx = result
            used_ranges.append((start_idx, end_idx))

            key = (owner, spec.name)
            occ = name_counts.get(key, 0) + 1
            name_counts[key] = occ

            found.append(
                {
                    "owner_decl": owner,
                    "name": spec.name,
                    "start_idx": start_idx,
                    "end_idx": end_idx,
                    "occurrence_index": occ,
                }
            )

    # Sort by position so we can walk forward
    found.sort(key=lambda x: x["start_idx"])

    # Annotate with original line numbers (before any modification)
    for item in found:
        item["block_start_line"] = _line_of(original_content, item["start_idx"])
        item["block_end_line"] = _line_of(original_content, item["end_idx"] - 1)

    # Build modified content
    parts: List[str] = []
    cursor = 0
    for item in found:
        start = item["start_idx"]
        end = item["end_idx"]

        # Text before this block
        parts.append(original_content[cursor:start])

        block_text = original_content[start:end]
        first_indent = _get_first_line_indent(original_content, start)
        close_indent = _get_last_line_indent(block_text)
        name = item["name"]

        # Replace block with extract wrapper.
        # The leading indent on the current line is already in parts (from cursor:start),
        # so we just emit the wrapper starting from the block's first character position.
        parts.append(
            f'extract "{name}" {{\n{first_indent}{block_text}\n{close_indent}}}'
        )

        cursor = end

    parts.append(original_content[cursor:])
    modified = "".join(parts)
    return modified, found


def _join_lines(lines: Sequence[str], trailing_newline: bool) -> str:
    content = "\n".join(lines)
    if trailing_newline:
        return content + "\n"
    return content


def _scan_top_level_declarations(lines: Sequence[str]) -> List[_TopLevelDecl]:
    starts: List[tuple[int, str, str]] = []
    for idx, line in enumerate(lines):
        if line.startswith(" ") or line.startswith("\t"):
            continue
        match = TOP_LEVEL_DECL_RE.match(line)
        if not match:
            continue
        starts.append((idx, match.group("kind"), match.group("name")))

    decls: List[_TopLevelDecl] = []
    for i, (start, kind, name) in enumerate(starts):
        end = (starts[i + 1][0] - 1) if i + 1 < len(starts) else len(lines) - 1
        decls.append(_TopLevelDecl(kind=kind, name=name, start=start, end=end))
    return decls


def _find_owner_decl(lines: Sequence[str], owner_decl: str) -> _TopLevelDecl | None:
    for decl in _scan_top_level_declarations(lines):
        if decl.name == owner_decl:
            return decl
    return None


def _find_owner_group_start(lines: Sequence[str], owner_start: int) -> int:
    group_start = owner_start

    while group_start > 0 and lines[group_start - 1].strip().startswith("@["):
        group_start -= 1

    doc_end = group_start - 1
    if doc_end >= 0 and lines[doc_end].strip().endswith("-/"):
        probe = doc_end
        while probe >= 0:
            stripped = lines[probe].lstrip()
            if stripped.startswith("/--"):
                group_start = probe
                break
            probe -= 1

    return group_start


def _extract_signature_map(
    diagnostics: Sequence[dict],
    owner_decl: str,
    owner_start: int,
    owner_end: int,
) -> Dict[str, str]:
    signatures: Dict[str, str] = {}
    for diag in diagnostics:
        severity = diag.get("severity", 1)
        message = diag.get("message", "")
        if severity != 3 or not isinstance(message, str):
            continue

        range_info = diag.get("fullRange", diag.get("range"))
        if range_info is not None:
            diag_line = range_info["start"]["line"]
            if not (owner_start <= diag_line <= owner_end):
                continue

        match = re.match(
            r"^(?P<kind>theorem|lemma)\s+"
            r"(?P<name>[^\s(:=]+)"
            r"(?P<rest>[\s\S]*)$",
            message.strip(),
        )
        if not match:
            continue

        short_name = match.group("name")
        full = f"{match.group('kind')} {owner_decl}.{short_name}{match.group('rest')}"
        full = re.sub(r"\s*:=\s*sorry\s*$", " := by sorry", full)
        signatures[short_name] = full
    return signatures


def _upsert_scaffold_declarations(
    lines: List[str], owner_decl: str, signatures: Dict[str, str]
) -> None:
    decls = _scan_top_level_declarations(lines)
    owner = next((decl for decl in decls if decl.name == owner_decl), None)
    if owner is None:
        raise ValueError(f"Owner declaration `{owner_decl}` not found during insert.")

    replacements: List[tuple[int, int, List[str]]] = []
    signature_names = {f"{owner_decl}.{name}": sig for name, sig in signatures.items()}

    for decl in decls:
        if decl.name in signature_names:
            replacement_lines = signature_names.pop(decl.name).splitlines()
            replacements.append((decl.start, decl.end, replacement_lines))

    for start, end, replacement_lines in sorted(replacements, reverse=True):
        lines[start : end + 1] = replacement_lines + [""]

    if not signature_names:
        return

    decls_after_replace = _scan_top_level_declarations(lines)
    owner_after = next((decl for decl in decls_after_replace if decl.name == owner_decl), None)
    if owner_after is None:
        raise ValueError(
            f"Owner declaration `{owner_decl}` not found after scaffold replacement."
        )
    insert_at = _find_owner_group_start(lines, owner_after.start)

    payload: List[str] = []
    for _full_name, signature in signature_names.items():
        payload.extend(signature.splitlines())
        payload.append("")

    lines[insert_at:insert_at] = payload


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def run_lean_extract(
    client: LeanLSPClient,
    abs_path: str,
    rel_path: str,
    jobs: List[LeanExtractJob],
) -> LeanExtractBatchResult:
    """Insert extract wrappers for all jobs in one pass.

    Workflow:
      1. Read file; add `import Extraction` if missing (direct I/O, no LSP).
      2. Build modified content with extract wrappers.
      3. Push to LSP via DidChange → first diagnostic.
      4. Write to disk → open_file → second diagnostic.
      5. Return result (keep file on error — no revert, no snapshot).
    """
    abs_path_obj = Path(abs_path)

    # Step 1: read + ensure import
    try:
        content = abs_path_obj.read_text(encoding="utf-8")
    except OSError as exc:
        return LeanExtractBatchResult(
            success=False,
            applied_by_decl=[],
            diagnostics=[],
            error=f"Cannot read file: {exc}",
        )

    content, import_changed = _ensure_extraction_import(content)
    if import_changed:
        abs_path_obj.write_text(content, encoding="utf-8")

    try:
        client.open_file(rel_path)
    except Exception as exc:
        print(f"Warning: open_file after import check failed: {exc}")

    # Step 2: build modified content
    modified_content, applied_meta = _build_modified_content(content, jobs)

    if not applied_meta:
        return LeanExtractBatchResult(
            success=False,
            applied_by_decl=[],
            diagnostics=[],
            error="No extraction blocks found in file.",
        )

    # Step 3: push wrapped content to LSP and capture scaffold signatures
    try:
        client.update_file_content(rel_path, modified_content)
        diag_result_1 = client.get_diagnostics(rel_path, inactivity_timeout=30.0)
    except Exception as exc:
        return LeanExtractBatchResult(
            success=False,
            applied_by_decl=[],
            diagnostics=[
                DiagnosticMessage(
                    severity="error",
                    message=f"Diagnostic call failed: {exc}",
                    line=1,
                    column=1,
                )
            ],
            error=f"Diagnostic call failed: {exc}",
        )

    wrapper_diags, _ = _collect_diagnostics(diag_result_1)
    has_wrapper_errors = any(d.severity == "error" for d in wrapper_diags)
    if has_wrapper_errors:
        abs_path_obj.write_text(modified_content, encoding="utf-8")
        try:
            client.open_file(rel_path)
        except Exception as exc:
            print(f"Warning: open_file after wrapper failure failed: {exc}")
        return LeanExtractBatchResult(
            success=False,
            applied_by_decl=[],
            diagnostics=wrapper_diags,
            error="Lean errors after wrapper insertion — file kept as-is.",
        )

    raw_diagnostics_1 = (
        diag_result_1.diagnostics if hasattr(diag_result_1, "diagnostics") else list(diag_result_1)
    )
    wrapped_lines = modified_content.splitlines()
    trailing_newline = modified_content.endswith("\n")
    wrapped_decls = _scan_top_level_declarations(wrapped_lines)
    wrapped_owner_ranges: Dict[str, tuple[int, int]] = {}
    for job in jobs:
        owner = next((d for d in wrapped_decls if d.name == job.owner_decl), None)
        if owner is not None:
            wrapped_owner_ranges[job.owner_decl] = (owner.start, owner.end)

    meta_by_owner: Dict[str, List[dict]] = {}
    for item in applied_meta:
        meta_by_owner.setdefault(item["owner_decl"], []).append(item)

    all_signature_maps: Dict[str, Dict[str, str]] = {}
    for job in jobs:
        owner_meta = meta_by_owner.get(job.owner_decl, [])
        if not owner_meta:
            continue
        start, end = wrapped_owner_ranges.get(job.owner_decl, (0, len(wrapped_lines) - 1))
        sig_map = _extract_signature_map(raw_diagnostics_1, job.owner_decl, start, end)
        missing = [item["name"] for item in owner_meta if item["name"] not in sig_map]
        if missing:
            abs_path_obj.write_text(modified_content, encoding="utf-8")
            try:
                client.open_file(rel_path)
            except Exception as exc:
                print(f"Warning: open_file after signature failure failed: {exc}")
            return LeanExtractBatchResult(
                success=False,
                applied_by_decl=[],
                diagnostics=wrapper_diags,
                error="Failed to capture scaffold signature(s) for: "
                + ", ".join(sorted(missing)),
            )
        all_signature_maps[job.owner_decl] = sig_map

    final_lines = wrapped_lines[:]
    owner_order = sorted(
        [owner for owner in meta_by_owner if owner in wrapped_owner_ranges],
        key=lambda owner: wrapped_owner_ranges[owner][0],
        reverse=True,
    )
    for owner_decl in owner_order:
        _upsert_scaffold_declarations(
            final_lines,
            owner_decl,
            all_signature_maps[owner_decl],
        )

    final_content = _join_lines(final_lines, trailing_newline)

    # Step 4: write final content → open_file → final diagnostic
    abs_path_obj.write_text(final_content, encoding="utf-8")
    try:
        client.open_file(rel_path)
    except Exception as exc:
        print(f"Warning: open_file failed: {exc}")

    try:
        diag_result_2 = client.get_diagnostics(rel_path, inactivity_timeout=30.0)
        all_diags, _ = _collect_diagnostics(diag_result_2)
    except Exception as exc:
        all_diags = [
            DiagnosticMessage(
                severity="error",
                message=f"Diagnostic call failed: {exc}",
                line=1,
                column=1,
            )
        ]

    has_errors = any(d.severity == "error" for d in all_diags)

    # Step 5: build structured result using inserted declaration positions
    final_decls = _scan_top_level_declarations(final_lines)
    by_owner: dict[str, List[LeanExtractApplied]] = {}
    for item in applied_meta:
        owner = item["owner_decl"]
        if owner not in by_owner:
            by_owner[owner] = []
        full_decl_name = f"{owner}.{item['name']}"
        decl_entry = next((d for d in final_decls if d.name == full_decl_name), None)
        if decl_entry is None:
            return LeanExtractBatchResult(
                success=False,
                applied_by_decl=[],
                diagnostics=all_diags,
                error=f"Inserted scaffold declaration `{full_decl_name}` not found after write.",
            )
        by_owner[owner].append(
            LeanExtractApplied(
                name=item["name"],
                full_decl_name=full_decl_name,
                occurrence_index=item["occurrence_index"],
                block_start_line=item["block_start_line"],
                block_end_line=item["block_end_line"],
                insert_decl_start_line=decl_entry.start + 1,
                insert_decl_end_line=decl_entry.end + 1,
                signature=all_signature_maps.get(owner, {}).get(item["name"], ""),
            )
        )

    applied_by_decl = [
        LeanExtractBatchApplied(owner_decl=owner, applied=applied)
        for owner, applied in by_owner.items()
    ]

    return LeanExtractBatchResult(
        success=not has_errors,
        applied_by_decl=applied_by_decl,
        diagnostics=all_diags,
        error="Lean errors after extraction — file kept as-is." if has_errors else None,
    )
