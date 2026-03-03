from __future__ import annotations

import re
from pathlib import Path


_REVISION_RE = re.compile(r"^revision\s*=\s*['\"]([^'\"]+)['\"]", re.MULTILINE)
_DOWN_RE = re.compile(r"^down_revision\s*=\s*(.+)$", re.MULTILINE)


def _parse_down_revision(raw_value: str) -> list[str]:
    raw = str(raw_value or "").strip()
    if raw == "None":
        return []
    if raw.startswith("(") or raw.startswith("["):
        return re.findall(r"['\"]([^'\"]+)['\"]", raw)
    match = re.search(r"['\"]([^'\"]+)['\"]", raw)
    return [match.group(1)] if match else []


def _migration_graph() -> tuple[dict[str, Path], dict[str, list[str]]]:
    revision_files = sorted(Path("alembic/versions").glob("*.py"))
    revisions: dict[str, Path] = {}
    parents: dict[str, list[str]] = {}

    for file_path in revision_files:
        source = file_path.read_text(encoding="utf-8")
        revision_match = _REVISION_RE.search(source)
        down_match = _DOWN_RE.search(source)
        assert revision_match is not None, f"Missing revision in {file_path}"
        assert down_match is not None, f"Missing down_revision in {file_path}"

        revision = revision_match.group(1)
        assert revision not in revisions, f"Duplicate revision id: {revision}"
        revisions[revision] = file_path
        parents[revision] = _parse_down_revision(down_match.group(1))

    return revisions, parents


def test_migration_graph_has_single_head_and_valid_down_revisions() -> None:
    revisions, parents = _migration_graph()
    assert revisions, "No migration files found in alembic/versions"

    children: dict[str, set[str]] = {revision: set() for revision in revisions}
    for revision, down_revisions in parents.items():
        for parent in down_revisions:
            assert parent in revisions, (
                f"{revision} references missing down_revision {parent}"
            )
            children[parent].add(revision)

    heads = [revision for revision, next_nodes in children.items() if not next_nodes]
    assert len(heads) == 1, f"Expected exactly 1 migration head, found {heads}"
