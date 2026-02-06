from __future__ import annotations

import argparse
import ast
import csv
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path


@dataclass(frozen=True)
class Occurrence:
    path: str
    line: int
    detail: str | None = None

    def format(self) -> str:
        if self.detail:
            return f"{self.path}:{self.line} ({self.detail})"
        return f"{self.path}:{self.line}"


@dataclass
class FieldUsage:
    name: str
    template_occurrences: list[Occurrence] = field(default_factory=list)
    route_read_occurrences: list[Occurrence] = field(default_factory=list)
    model_write_occurrences: list[Occurrence] = field(default_factory=list)
    labels: set[str] = field(default_factory=set)
    wip_marked: bool = False
    wip_evidence: set[str] = field(default_factory=set)

    @property
    def in_templates(self) -> bool:
        return bool(self.template_occurrences)

    @property
    def read_in_routes(self) -> bool:
        return bool(self.route_read_occurrences)

    @property
    def written_to_models(self) -> bool:
        return bool(self.model_write_occurrences)

    @property
    def status(self) -> str:
        if self.in_templates and not self.read_in_routes:
            return "rendered_not_read"
        if self.in_templates and self.read_in_routes and not self.written_to_models:
            return "read_not_written"
        return "used"


TAG_NAME_RE = re.compile(
    r"<(?P<tag>input|select|textarea)\b[^>]*\bname\s*=\s*(?P<q>[\"'])(?P<name>.*?)(?P=q)",
    re.IGNORECASE | re.DOTALL,
)

LABEL_RE = re.compile(
    r"<label\b[^>]*>(?P<label>.*?)</label>", re.IGNORECASE | re.DOTALL
)


def _normalize_token(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (value or "").lower())


def _is_dynamic_template_value(value: str) -> bool:
    return any(token in value for token in ("{{", "}}", "{%", "%}"))


def _strip_html(value: str) -> str:
    cleaned = re.sub(r"<[^>]+>", " ", value)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


def _nearby_label(text: str, pos: int) -> str | None:
    window_start = max(0, pos - 4000)
    window = text[window_start:pos]
    matches = list(LABEL_RE.finditer(window))
    if not matches:
        return None
    label_raw = matches[-1].group("label")
    label_text = _strip_html(label_raw)
    if not label_text or _is_dynamic_template_value(label_text):
        return None
    return label_text


def scan_templates(root: Path, templates_dir: Path) -> dict[str, FieldUsage]:
    field_map: dict[str, FieldUsage] = {}
    for path in sorted(templates_dir.rglob("*.html")):
        rel_path = str(path.relative_to(root).as_posix())
        text = path.read_text(encoding="utf-8", errors="replace")
        for match in TAG_NAME_RE.finditer(text):
            field_name = match.group("name").strip()
            if not field_name:
                continue
            if _is_dynamic_template_value(field_name):
                continue
            if any(ch.isspace() for ch in field_name):
                continue
            line = text.count("\n", 0, match.start()) + 1
            label = _nearby_label(text, match.start())
            usage = field_map.setdefault(field_name, FieldUsage(name=field_name))
            usage.template_occurrences.append(Occurrence(path=rel_path, line=line))
            if label:
                usage.labels.add(label)
    return field_map


def scan_model_classes(root: Path, models_dir: Path) -> dict[str, str]:
    model_class_to_file: dict[str, str] = {}
    for path in sorted(models_dir.rglob("*.py")):
        rel_path = str(path.relative_to(root).as_posix())
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"), filename=rel_path)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                # Many model files only define models; mapping by class name is good enough.
                model_class_to_file.setdefault(node.name, rel_path)
    return model_class_to_file


def _is_request_form_await(value: ast.AST) -> bool:
    # Matches: form = await request.form()
    if not isinstance(value, ast.Await):
        return False
    call = value.value
    if not isinstance(call, ast.Call):
        return False
    func = call.func
    return (
        isinstance(func, ast.Attribute)
        and func.attr == "form"
        and isinstance(func.value, ast.Name)
        and func.value.id == "request"
    )


def _is_db_get_call(value: ast.AST) -> tuple[str, ast.AST] | None:
    # Matches: obj = db.get(ModelClass, ...)
    if not isinstance(value, ast.Call):
        return None
    func = value.func
    if (
        isinstance(func, ast.Attribute)
        and func.attr == "get"
        and isinstance(func.value, ast.Name)
        and func.value.id == "db"
        and value.args
    ):
        first = value.args[0]
        if isinstance(first, ast.Name):
            return first.id, value
    return None


def _string_constant(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _extract_subscript_key(node: ast.AST, expected_container_names: set[str]) -> str | None:
    # Matches: payload["field"]
    if not isinstance(node, ast.Subscript):
        return None
    if not isinstance(node.value, ast.Name) or node.value.id not in expected_container_names:
        return None
    key = _string_constant(node.slice)
    return key


class _FunctionScanner(ast.NodeVisitor):
    def __init__(
        self,
        *,
        file_path: str,
        form_vars: set[str],
        form_helper_names: set[str],
        model_class_to_file: dict[str, str],
    ) -> None:
        self.file_path = file_path
        self.form_vars = form_vars
        self.form_helper_names = form_helper_names
        self.model_class_to_file = model_class_to_file
        self.route_reads: list[tuple[str, int, str | None]] = []
        self.model_writes: list[tuple[str, int, str | None, str | None]] = []
        self.var_model_types: dict[str, str] = {}

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: N802
        # Skip nested function bodies to avoid double counting.
        return

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:  # noqa: N802
        return

    def visit_Assign(self, node: ast.Assign) -> None:  # noqa: N802
        # Track simple model instance bindings: product = Product(...), customer = db.get(Customer,...)
        model_from_db_get = _is_db_get_call(node.value)
        if model_from_db_get:
            model_class, _ = model_from_db_get
            for target in node.targets:
                if isinstance(target, ast.Name):
                    self.var_model_types[target.id] = model_class

        if isinstance(node.value, ast.Call) and isinstance(node.value.func, ast.Name):
            model_class = node.value.func.id
            if model_class in self.model_class_to_file:
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        self.var_model_types[target.id] = model_class

        # Detect: model.FIELD = payload["FIELD"]
        payload_key = _extract_subscript_key(node.value, {"payload"})
        if payload_key:
            for target in node.targets:
                if isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name):
                    var = target.value.id
                    inferred_model = self._infer_model_class(var)
                    detail = None
                    if inferred_model:
                        model_file = self.model_class_to_file.get(inferred_model)
                        if model_file:
                            detail = f"{inferred_model} ({model_file})"
                    self.model_writes.append((payload_key, node.lineno, detail, inferred_model))

        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:  # noqa: N802
        # Form reads: form.get("FIELD")
        if isinstance(node.func, ast.Attribute) and node.func.attr == "get":
            owner = node.func.value
            if isinstance(owner, ast.Name) and owner.id in self.form_vars and node.args:
                field_name = _string_constant(node.args[0])
                if field_name:
                    self.route_reads.append((field_name, node.lineno, "form.get"))

        # Form reads: _form_value(form, "FIELD") or helper("FIELD")
        if isinstance(node.func, ast.Name):
            func_name = node.func.id
            if func_name in self.form_helper_names:
                if len(node.args) >= 2 and isinstance(node.args[0], ast.Name) and node.args[0].id in self.form_vars:
                    field_name = _string_constant(node.args[1])
                    if field_name:
                        self.route_reads.append((field_name, node.lineno, func_name))
                elif node.args:
                    field_name = _string_constant(node.args[0])
                    if field_name:
                        self.route_reads.append((field_name, node.lineno, func_name))

        # Query param reads: request.query_params.get("FIELD")
        if isinstance(node.func, ast.Attribute) and node.func.attr == "get":
            owner = node.func.value
            if (
                isinstance(owner, ast.Attribute)
                and owner.attr == "query_params"
                and isinstance(owner.value, ast.Name)
                and owner.value.id == "request"
                and node.args
            ):
                field_name = _string_constant(node.args[0])
                if field_name:
                    self.route_reads.append((field_name, node.lineno, "query_params.get"))

        # Model constructor writes: ModelClass(field=payload["field"])
        if isinstance(node.func, ast.Name) and node.func.id in self.model_class_to_file:
            model_class = node.func.id
            for keyword in node.keywords:
                if not keyword.arg:
                    continue
                payload_key = _extract_subscript_key(keyword.value, {"payload"})
                if not payload_key:
                    continue
                model_file = self.model_class_to_file.get(model_class)
                detail = f"{model_class} ({model_file})" if model_file else model_class
                self.model_writes.append((payload_key, node.lineno, detail, model_class))

        self.generic_visit(node)

    def visit_Subscript(self, node: ast.Subscript) -> None:  # noqa: N802
        # Form reads: form["FIELD"]
        if isinstance(node.value, ast.Name) and node.value.id in self.form_vars:
            field_name = _string_constant(node.slice)
            if field_name:
                self.route_reads.append((field_name, node.lineno, "form[...]"))

        # Query param reads: request.query_params["FIELD"]
        if (
            isinstance(node.value, ast.Attribute)
            and node.value.attr == "query_params"
            and isinstance(node.value.value, ast.Name)
            and node.value.value.id == "request"
        ):
            field_name = _string_constant(node.slice)
            if field_name:
                self.route_reads.append((field_name, node.lineno, "query_params[...]"))

        self.generic_visit(node)

    def _infer_model_class(self, var_name: str) -> str | None:
        if var_name in self.var_model_types:
            return self.var_model_types[var_name]
        # Simple name-based inference: "customer" -> "Customer"
        for model_class in self.model_class_to_file.keys():
            if model_class.lower() == var_name.lower():
                return model_class
        return None


def _is_route_decorator(dec: ast.AST) -> bool:
    if not isinstance(dec, ast.Call):
        return False
    func = dec.func
    return (
        isinstance(func, ast.Attribute)
        and isinstance(func.value, ast.Name)
        and func.attr in {"get", "post", "put", "patch", "delete"}
        and func.value.id in {"router", "app"}
    )


def _extract_query_params_from_route_signature(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
) -> list[tuple[str, int, str]]:
    reads: list[tuple[str, int, str]] = []
    args = list(node.args.posonlyargs) + list(node.args.args) + list(node.args.kwonlyargs)
    defaults = list(node.args.defaults)
    kw_defaults = list(node.args.kw_defaults)
    # Align defaults with args.args (positional-with-defaults only)
    default_offset = len(args) - len(defaults) - len(kw_defaults)
    for i, arg in enumerate(args):
        name = arg.arg
        if name in {"request", "db"}:
            continue
        default: ast.AST | None = None
        if i >= default_offset and i - default_offset < len(defaults):
            default = defaults[i - default_offset]
        elif i >= len(args) - len(kw_defaults):
            default = kw_defaults[i - (len(args) - len(kw_defaults))]
        # Exclude Depends(...) (dependency injection)
        if isinstance(default, ast.Call) and isinstance(default.func, ast.Name) and default.func.id == "Depends":
            continue
        reads.append((name, node.lineno, "route_param"))
    return reads


def scan_backend(
    root: Path, python_dirs: list[Path], model_class_to_file: dict[str, str]
) -> tuple[dict[str, list[Occurrence]], dict[str, list[Occurrence]]]:
    route_reads: dict[str, list[Occurrence]] = defaultdict(list)
    model_writes: dict[str, list[Occurrence]] = defaultdict(list)

    for base_dir in python_dirs:
        for path in sorted(base_dir.rglob("*.py")):
            rel_path = str(path.relative_to(root).as_posix())
            try:
                tree = ast.parse(
                    path.read_text(encoding="utf-8", errors="replace"), filename=rel_path
                )
            except SyntaxError:
                continue

            # Route signature query param reads.
            for node in tree.body:
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                if any(_is_route_decorator(dec) for dec in node.decorator_list):
                    for name, line, detail in _extract_query_params_from_route_signature(node):
                        route_reads[name].append(Occurrence(path=rel_path, line=line, detail=detail))

            # Walk all functions (including helpers) for form/query reads and model writes.
            for node in ast.walk(tree):
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue

                # Identify form variables within this function.
                form_vars: set[str] = set()
                for arg in node.args.args:
                    if arg.arg == "form":
                        form_vars.add("form")

                for stmt in node.body:
                    if isinstance(stmt, ast.Assign) and _is_request_form_await(stmt.value):
                        for target in stmt.targets:
                            if isinstance(target, ast.Name):
                                form_vars.add(target.id)

                # Identify nested helper functions that read from form via `.get`.
                helper_names: set[str] = {"_form_value"}
                for stmt in node.body:
                    if not isinstance(stmt, ast.FunctionDef):
                        continue
                    helper_name = stmt.name
                    if not helper_name:
                        continue
                    reads_form = False
                    for inner in ast.walk(stmt):
                        if isinstance(inner, ast.Call) and isinstance(inner.func, ast.Attribute) and inner.func.attr == "get":
                            if isinstance(inner.func.value, ast.Name) and inner.func.value.id in form_vars:
                                reads_form = True
                                break
                    if reads_form:
                        helper_names.add(helper_name)

                scanner = _FunctionScanner(
                    file_path=rel_path,
                    form_vars=form_vars,
                    form_helper_names=helper_names,
                    model_class_to_file=model_class_to_file,
                )

                for stmt in node.body:
                    scanner.visit(stmt)

                for field_name, line, detail in scanner.route_reads:
                    route_reads[field_name].append(
                        Occurrence(path=rel_path, line=line, detail=detail)
                    )

                for field_name, line, detail, _model_class in scanner.model_writes:
                    model_writes[field_name].append(
                        Occurrence(path=rel_path, line=line, detail=detail)
                    )

    return route_reads, model_writes


def parse_dev_notes(dev_notes_path: Path) -> tuple[set[str], set[str]]:
    if not dev_notes_path.exists():
        return set(), set()
    raw = dev_notes_path.read_text(encoding="utf-8", errors="replace")

    normalized_tokens: set[str] = set()
    evidence: set[str] = set()

    # Code spans: `field_name`
    for match in re.finditer(r"`([^`]+)`", raw):
        token = match.group(1).strip()
        if token:
            normalized_tokens.add(_normalize_token(token))
            evidence.add(token)

    # Parenthesized tokens: (yard_id)
    for match in re.finditer(r"\(([^)]+)\)", raw):
        token = match.group(1).strip()
        if token and re.fullmatch(r"[A-Za-z0-9_ -]{2,}", token):
            normalized_tokens.add(_normalize_token(token))
            evidence.add(token)

    # Bullet list items under "WIP fields"
    in_wip_list = False
    for line in raw.splitlines():
        stripped = line.strip()
        if re.match(r"^-?\s*wip fields\b", stripped, flags=re.IGNORECASE):
            in_wip_list = True
            continue
        if stripped.startswith("## "):
            in_wip_list = False
        if not in_wip_list:
            continue
        if not stripped.startswith("-"):
            continue
        item = stripped.lstrip("-").strip()
        if not item:
            continue
        normalized_tokens.add(_normalize_token(item))
        evidence.add(item)

    return normalized_tokens, evidence


def write_csv_report(path: Path, rows: list[FieldUsage]) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "field_name",
                "in_templates",
                "read_in_routes",
                "written_to_models",
                "status",
                "wip_marked",
                "labels",
                "template_locations",
                "route_read_locations",
                "model_write_locations",
            ],
        )
        writer.writeheader()
        for usage in rows:
            writer.writerow(
                {
                    "field_name": usage.name,
                    "in_templates": "yes" if usage.in_templates else "no",
                    "read_in_routes": "yes" if usage.read_in_routes else "no",
                    "written_to_models": "yes" if usage.written_to_models else "no",
                    "status": usage.status,
                    "wip_marked": "yes" if usage.wip_marked else "no",
                    "labels": "; ".join(sorted(usage.labels)),
                    "template_locations": "; ".join(
                        occ.format() for occ in usage.template_occurrences
                    ),
                    "route_read_locations": "; ".join(
                        occ.format() for occ in usage.route_read_occurrences
                    ),
                    "model_write_locations": "; ".join(
                        occ.format() for occ in usage.model_write_occurrences
                    ),
                }
            )


def _md_escape(value: str) -> str:
    return value.replace("|", "\\|")


def write_md_report(path: Path, rows: list[FieldUsage], generated_at: str) -> None:
    total = len(rows)
    dead = sum(1 for r in rows if r.status == "rendered_not_read")
    read_not_written = sum(1 for r in rows if r.status == "read_not_written")

    dead_unmarked = [
        r for r in rows if r.status == "rendered_not_read" and not r.wip_marked
    ]

    lines: list[str] = []
    lines.append("# Field Usage Audit Report")
    lines.append("")
    lines.append(f"Generated at: `{generated_at}`")
    lines.append("")
    lines.append("## Summary")
    lines.append("")
    lines.append(f"- Template field names found: **{total}**")
    lines.append(f"- Rendered but never read: **{dead}**")
    lines.append(f"- Rendered + read but never written: **{read_not_written}**")
    lines.append("")

    if dead_unmarked:
        lines.append("## Rendered-but-not-read (not marked WIP)")
        lines.append("")
        for usage in dead_unmarked:
            locs = ", ".join(occ.format() for occ in usage.template_occurrences[:5])
            label = ", ".join(sorted(usage.labels)) if usage.labels else ""
            lines.append(
                f"- `{usage.name}`{f' - {label}' if label else ''} (templates: {locs})"
            )
        lines.append("")

    lines.append("## Full Table")
    lines.append("")
    lines.append(
        "| Field | Status | In templates | Read in routes | Written to models | WIP note | Labels | Templates | Routes | Models |"
    )
    lines.append("|---|---|---|---|---|---|---|---|---|---|")
    for usage in rows:
        tmpl = "<br>".join(_md_escape(o.format()) for o in usage.template_occurrences)
        routes = "<br>".join(_md_escape(o.format()) for o in usage.route_read_occurrences)
        models = "<br>".join(_md_escape(o.format()) for o in usage.model_write_occurrences)
        labels = "<br>".join(_md_escape(l) for l in sorted(usage.labels))
        lines.append(
            "| "
            + " | ".join(
                [
                    f"`{usage.name}`",
                    usage.status,
                    "yes" if usage.in_templates else "no",
                    "yes" if usage.read_in_routes else "no",
                    "yes" if usage.written_to_models else "no",
                    "yes" if usage.wip_marked else "no",
                    labels or "",
                    tmpl or "",
                    routes or "",
                    models or "",
                ]
            )
            + " |"
        )

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _sort_key(usage: FieldUsage) -> tuple[int, str]:
    order = {"rendered_not_read": 0, "read_not_written": 1, "used": 2}
    return (order.get(usage.status, 99), usage.name)


def print_summary(rows: list[FieldUsage], max_rows: int | None = None) -> None:
    header = ["field", "tmpl", "read", "write", "status", "wip"]
    col_widths = [max(len(h), 12) for h in header]

    def fmt_row(values: list[str]) -> str:
        padded = []
        for value, width in zip(values, col_widths):
            padded.append(value.ljust(width))
        return "  ".join(padded).rstrip()

    print(fmt_row(header))
    print(fmt_row(["-" * len(h) for h in header]))

    for i, usage in enumerate(rows):
        if max_rows is not None and i >= max_rows:
            break
        print(
            fmt_row(
                [
                    usage.name,
                    "yes" if usage.in_templates else "no",
                    "yes" if usage.read_in_routes else "no",
                    "yes" if usage.written_to_models else "no",
                    usage.status,
                    "yes" if usage.wip_marked else "no",
                ]
            )
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit template form fields vs backend usage.")
    parser.add_argument("--root", default=".", help="Project root (default: .)")
    parser.add_argument(
        "--out-md",
        default="audit_field_usage_report.md",
        help="Markdown report path (default: audit_field_usage_report.md)",
    )
    parser.add_argument(
        "--out-csv",
        default="audit_field_usage_report.csv",
        help="CSV report path (default: audit_field_usage_report.csv)",
    )
    parser.add_argument(
        "--max-print",
        type=int,
        default=200,
        help="Max rows to print to stdout (default: 200; 0 to disable)",
    )
    args = parser.parse_args()

    root = Path(args.root).resolve()
    templates_dir = root / "app" / "templates"
    models_dir = root / "app" / "models"
    python_dirs = [root / "app" / "routes", root / "app" / "routers"]
    dev_notes_path = root / "DEV_NOTES.md"

    if not templates_dir.exists():
        raise SystemExit(f"Templates dir not found: {templates_dir}")
    if not models_dir.exists():
        raise SystemExit(f"Models dir not found: {models_dir}")

    generated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    template_fields = scan_templates(root, templates_dir)
    model_class_to_file = scan_model_classes(root, models_dir)
    route_reads, model_writes = scan_backend(root, python_dirs, model_class_to_file)

    wip_tokens, wip_evidence = parse_dev_notes(dev_notes_path)

    for field_name, usage in template_fields.items():
        usage.route_read_occurrences.extend(route_reads.get(field_name, []))
        usage.model_write_occurrences.extend(model_writes.get(field_name, []))
        norm = _normalize_token(field_name)
        if norm in wip_tokens:
            usage.wip_marked = True
            usage.wip_evidence.add(field_name)
        else:
            # Also check labels against DEV_NOTES tokens (crude matching).
            for label in usage.labels:
                if _normalize_token(label) in wip_tokens:
                    usage.wip_marked = True
                    usage.wip_evidence.add(label)

    rows = sorted(template_fields.values(), key=_sort_key)

    out_md = (root / args.out_md).resolve()
    out_csv = (root / args.out_csv).resolve()
    write_md_report(out_md, rows, generated_at)
    write_csv_report(out_csv, rows)

    if args.max_print != 0:
        print_summary(rows, max_rows=args.max_print if args.max_print > 0 else None)
        print("")
        print(f"Wrote: {out_md}")
        print(f"Wrote: {out_csv}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
