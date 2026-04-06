import ast
import argparse
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Set, Tuple


ROOT = Path(__file__).resolve().parents[1]
OUTPUT_PATH = ROOT / "code_review_graph.md"
IMPACT_OUTPUT_PATH = ROOT / "code_review_impact.md"
SKIP_DIRS = {".git", ".idea", ".venv", "__pycache__", "mnt"}

RISK_WEIGHTS = {
    "network": 3,
    "io": 2,
    "xml": 2,
    "threading": 2,
    "broad_except": 2,
    "global_state": 2,
    "regex": 1,
}


@dataclass
class FunctionInfo:
    name: str
    lineno: int
    calls: Set[str] = field(default_factory=set)
    tags: Set[str] = field(default_factory=set)


@dataclass
class ModuleInfo:
    path: Path
    module_name: str
    imports: Set[str] = field(default_factory=set)
    imported_functions: Dict[str, str] = field(default_factory=dict)
    functions: Dict[str, FunctionInfo] = field(default_factory=dict)
    tags: Set[str] = field(default_factory=set)
    has_entrypoint: bool = False
    line_count: int = 0


def iter_python_files(root: Path) -> List[Path]:
    result = []
    for path in root.rglob("*.py"):
        if any(part in SKIP_DIRS for part in path.relative_to(root).parts):
            continue
        result.append(path)
    return sorted(result)


def node_to_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = node_to_name(node.value)
        return f"{base}.{node.attr}" if base else node.attr
    if isinstance(node, ast.Call):
        return node_to_name(node.func)
    if isinstance(node, ast.Subscript):
        return node_to_name(node.value)
    return ""


class FunctionAnalyzer(ast.NodeVisitor):
    def __init__(self) -> None:
        self.calls: Set[str] = set()
        self.tags: Set[str] = set()

    def visit_Call(self, node: ast.Call) -> None:
        call_name = node_to_name(node.func)
        if call_name:
            self.calls.add(call_name)
            lowered = call_name.lower()
            if "translator" in lowered or "translate" in lowered:
                self.tags.add("network")
            if lowered.startswith("et.") or "xml" in lowered:
                self.tags.add("xml")
            if any(part in lowered for part in ("open", "write", "read", "mkdir", "makedirs")):
                self.tags.add("io")
            if any(part in lowered for part in ("thread", "executor", "lock")):
                self.tags.add("threading")
            if "re." in lowered:
                self.tags.add("regex")
        self.generic_visit(node)

    def visit_Try(self, node: ast.Try) -> None:
        for handler in node.handlers:
            if handler.type is None or (isinstance(handler.type, ast.Name) and handler.type.id == "Exception"):
                self.tags.add("broad_except")
        self.generic_visit(node)

    def visit_Global(self, node: ast.Global) -> None:
        self.tags.add("global_state")
        self.generic_visit(node)


class ModuleAnalyzer(ast.NodeVisitor):
    def __init__(self, module: ModuleInfo) -> None:
        self.module = module

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self.module.imports.add(alias.name)
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if not node.module:
            return
        self.module.imports.add(node.module)
        for alias in node.names:
            self.module.imported_functions[alias.asname or alias.name] = f"{node.module}.{alias.name}"
        self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        analyzer = FunctionAnalyzer()
        analyzer.visit(node)
        self.module.functions[node.name] = FunctionInfo(
            name=node.name,
            lineno=node.lineno,
            calls=analyzer.calls,
            tags=analyzer.tags,
        )

    def visit_If(self, node: ast.If) -> None:
        if (
            isinstance(node.test, ast.Compare)
            and isinstance(node.test.left, ast.Name)
            and node.test.left.id == "__name__"
            and any(isinstance(comp, ast.Constant) and comp.value == "__main__" for comp in node.test.comparators)
        ):
            self.module.has_entrypoint = True
        self.generic_visit(node)


def parse_module(path: Path) -> ModuleInfo:
    module_name = ".".join(path.relative_to(ROOT).with_suffix("").parts)
    source = path.read_text(encoding="utf-8")
    module = ModuleInfo(path=path, module_name=module_name, line_count=len(source.splitlines()))
    tree = ast.parse(source)
    analyzer = ModuleAnalyzer(module)
    analyzer.visit(tree)

    for fn in module.functions.values():
        module.tags.update(fn.tags)
    return module


def build_project_map(modules: List[ModuleInfo]) -> Dict[str, ModuleInfo]:
    return {module.module_name: module for module in modules}


def resolve_project_imports(module: ModuleInfo, project_modules: Dict[str, ModuleInfo]) -> Set[str]:
    resolved = set()
    for imported in module.imports:
        if imported in project_modules:
            resolved.add(imported)
    return resolved


def resolve_function_edges(module: ModuleInfo, project_modules: Dict[str, ModuleInfo]) -> List[Tuple[str, str]]:
    edges = []
    local_functions = set(module.functions)
    for fn in module.functions.values():
        source = f"{module.module_name}.{fn.name}"
        for call in sorted(fn.calls):
            if call in local_functions:
                edges.append((source, f"{module.module_name}.{call}"))
                continue
            root_name = call.split(".", 1)[0]
            if root_name in module.imported_functions:
                target = module.imported_functions[root_name]
                target_module, _, target_fn = target.rpartition(".")
                if target_module in project_modules:
                    edges.append((source, f"{target_module}.{target_fn}"))
    return edges


def module_name_from_target(target: str) -> str:
    target_path = Path(target)
    if not target_path.is_absolute():
        target_path = (ROOT / target_path).resolve()
    try:
        rel = target_path.relative_to(ROOT)
    except ValueError:
        return target.replace("\\", ".").replace("/", ".").removesuffix(".py")
    return ".".join(rel.with_suffix("").parts)


def extract_module_from_issue(issue: str, project_modules: Dict[str, ModuleInfo]) -> str:
    match = re.search(r'File\s+"([^"]+\.py)"', issue)
    if match:
        return module_name_from_target(match.group(1))

    fallback = re.search(r'([A-Za-z]:[\\/][^:\n]*?\.py|[\w./\\-]+\.py)', issue)
    if fallback:
        return module_name_from_target(fallback.group(1))

    lowered_issue = issue.lower()
    for module_name, module in sorted(project_modules.items()):
        filename = module.path.name.lower()
        if filename in lowered_issue or module_name.lower() in lowered_issue:
            return module_name

    return ""


def find_import_chain(target_module: str, source_module: str, project_modules: Dict[str, ModuleInfo]) -> List[str]:
    reverse_edges: Dict[str, Set[str]] = {name: set() for name in project_modules}
    for module in project_modules.values():
        for imported in resolve_project_imports(module, project_modules):
            reverse_edges[imported].add(module.module_name)

    queue: List[Tuple[str, List[str]]] = [(source_module, [source_module])]
    visited = {source_module}
    while queue:
        current, path = queue.pop(0)
        if current == target_module:
            return path
        for nxt in sorted(reverse_edges.get(current, set())):
            if nxt in visited:
                continue
            visited.add(nxt)
            queue.append((nxt, path + [nxt]))
    return []


def build_impact_report(target_module_name: str, issue: str, modules: List[ModuleInfo]) -> str:
    project_modules = build_project_map(modules)
    target_module = project_modules.get(target_module_name)
    source_module_name = extract_module_from_issue(issue, project_modules)
    source_module = project_modules.get(source_module_name) if source_module_name else None
    if target_module is None:
        available = ", ".join(sorted(project_modules))
        return "\n".join(
            [
                "# Code Review Impact",
                "",
                f"Không tìm thấy module `{target_module_name}`.",
                "",
                f"Modules hiện có: {available}",
                "",
            ]
        )

    target_imports = resolve_project_imports(target_module, project_modules)
    imports_failing_module = source_module_name in target_imports if source_module_name else False

    lines = [
        "# Code Review Impact",
        "",
        f"Target: `{target_module.path.relative_to(ROOT).as_posix()}`",
        f"Source lỗi: `{source_module.path.relative_to(ROOT).as_posix()}`" if source_module is not None else "Source lỗi: `không suy ra được từ issue`",
        f"Issue: `{issue}`",
        "",
        "## Impact Summary",
        "",
    ]

    if "deep_translator" in issue or "GoogleTranslator" in issue or "ModuleNotFoundError" in issue:
        lines.extend(
            [
                "- Mức ảnh hưởng: cao với runtime.",
                "- Lỗi xảy ra ở import-time của `translate.py`, nên mọi file import module này sẽ fail trước khi vào test logic.",
                "- Đây là lỗi dependency môi trường, không phải bug nghiệp vụ trong `test_special_cases.py`.",
                "",
            ]
        )

    lines.extend(
        [
            "## Direct Import Impact",
            "",
            "| Module bị ảnh hưởng | Chuỗi ảnh hưởng | Trạng thái |",
            "| --- | --- |",
        ]
    )

    if source_module is not None:
        chain = find_import_chain(target_module_name, source_module_name, project_modules)
        pretty_chain = " -> ".join(chain) if chain else f"{source_module_name} -> {target_module_name}"
        status = "bị chặn ở import-time" if imports_failing_module else "không import trực tiếp source lỗi"
        lines.append(f"| `{target_module.path.relative_to(ROOT).as_posix()}` | `{pretty_chain}` | {status} |")
    else:
        lines.append("| - | - | - |")

    lines.extend(
        [
            "",
            "## Impacted Functions",
            "",
            "| Function | Why impacted |",
            "| --- | --- |",
        ]
    )

    if imports_failing_module:
        for fn in sorted(target_module.functions.values(), key=lambda item: item.lineno):
            lines.append(
                f"| `{target_module.path.name}:{fn.name}()` @ L{fn.lineno} | Import `translate.py` fail trước khi function này được gọi |"
            )
    else:
        lines.append("| - | - |")

    lines.extend(
        [
            "",
            "## Review Notes",
            "",
            "- `test_special_cases.py` import trực tiếp nhiều helper từ `translate.py`, nên test bị chặn hoàn toàn.",
            "- Muốn test độc lập hơn, có thể tách lớp import `GoogleTranslator` ra lazy import hoặc inject dependency.",
            "- Nếu chỉ cần chạy test unit helper, nên tránh hard dependency vào package ngoài ngay tại top-level import.",
            "",
            "## Suggested Fix",
            "",
            "1. Cài `deep_translator` trong môi trường test, hoặc",
            "2. Chuyển `from deep_translator import GoogleTranslator` vào trong `throttled_translate()`, hoặc",
            "3. Bao import bằng fallback rõ ràng để test helper không phụ thuộc package ngoài.",
            "",
        ]
    )

    return "\n".join(lines)


def score_module(module: ModuleInfo) -> int:
    score = module.line_count // 80
    for tag in module.tags:
        score += RISK_WEIGHTS.get(tag, 0)
    if module.has_entrypoint:
        score += 1
    return score


def score_function(fn: FunctionInfo) -> int:
    score = len(fn.calls) // 4
    for tag in fn.tags:
        score += RISK_WEIGHTS.get(tag, 0)
    return score


def sanitize_id(value: str) -> str:
    return value.replace(".", "_").replace("-", "_")


def build_mermaid_file_graph(modules: List[ModuleInfo], project_modules: Dict[str, ModuleInfo]) -> str:
    lines = ["flowchart LR"]
    for module in modules:
        node_id = sanitize_id(module.module_name)
        label = module.path.relative_to(ROOT).as_posix()
        lines.append(f'    {node_id}["{label}"]')
    for module in modules:
        for imported in sorted(resolve_project_imports(module, project_modules)):
            lines.append(f"    {sanitize_id(module.module_name)} --> {sanitize_id(imported)}")
    return "\n".join(lines)


def build_mermaid_function_graph(modules: List[ModuleInfo], project_modules: Dict[str, ModuleInfo]) -> str:
    lines = ["flowchart TD"]
    included_nodes: Set[str] = set()
    top_function_names: Dict[str, Set[str]] = {}
    edge_count = 0
    for module in modules:
        top_functions = sorted(
            module.functions.values(),
            key=lambda fn: (-score_function(fn), fn.lineno)
        )[:4]
        top_function_names[module.module_name] = {fn.name for fn in top_functions}
        for fn in top_functions:
            node_id = sanitize_id(f"{module.module_name}.{fn.name}")
            label = f"{module.path.name}:{fn.name}"
            lines.append(f'    {node_id}["{label}"]')
            included_nodes.add(f"{module.module_name}.{fn.name}")
    for module in modules:
        allowed = top_function_names.get(module.module_name, set())
        for source, target in resolve_function_edges(module, project_modules):
            source_fn = source.rsplit(".", 1)[-1]
            target_fn = target.rsplit(".", 1)[-1]
            target_module = target.rsplit(".", 1)[0]
            target_module_info = project_modules.get(target_module)
            if source_fn not in allowed:
                continue
            if target_module_info is not None:
                target_allowed = top_function_names.get(target_module, set())
                if target_fn not in target_allowed:
                    continue
            if source not in included_nodes or target not in included_nodes:
                continue
            source_id = sanitize_id(source)
            target_id = sanitize_id(target)
            lines.append(f"    {source_id} --> {target_id}")
            edge_count += 1
            if edge_count >= 18:
                break
        if edge_count >= 18:
            break
    return "\n".join(lines)


def build_mermaid_module_detail_graph(module: ModuleInfo, project_modules: Dict[str, ModuleInfo]) -> str:
    lines = ["flowchart LR"]
    internal_edges = []
    external_edges = []

    for fn in sorted(module.functions.values(), key=lambda item: item.lineno):
        node_id = sanitize_id(f"{module.module_name}.{fn.name}")
        tag_suffix = f" [{', '.join(sorted(fn.tags))}]" if fn.tags else ""
        lines.append(f'    {node_id}["{fn.name}()\\nL{fn.lineno}{tag_suffix}"]')

    local_functions = set(module.functions)
    for fn in sorted(module.functions.values(), key=lambda item: item.lineno):
        source = f"{module.module_name}.{fn.name}"
        for call in sorted(fn.calls):
            if call in local_functions:
                internal_edges.append((source, f"{module.module_name}.{call}"))
                continue
            root_name = call.split(".", 1)[0]
            if root_name in module.imported_functions:
                target = module.imported_functions[root_name]
                target_module, _, target_fn = target.rpartition(".")
                if target_module in project_modules:
                    external_edges.append((source, f"{target_module}.{target_fn}"))

    if internal_edges:
        lines.append("    %% Internal calls")
        for source, target in internal_edges:
            lines.append(f"    {sanitize_id(source)} --> {sanitize_id(target)}")

    if external_edges:
        lines.append("    %% Cross-module calls")
        added_external = set()
        for _, target in external_edges:
            if target in added_external:
                continue
            added_external.add(target)
            target_module, _, target_fn = target.rpartition(".")
            target_id = sanitize_id(target)
            lines.append(f'    {target_id}["{target_module}.{target_fn}()"]')
            lines.append(f"    style {target_id} fill:#f4f4f4,stroke:#999,stroke-dasharray: 4 4")
        for source, target in external_edges:
            lines.append(f"    {sanitize_id(source)} -.-> {sanitize_id(target)}")

    return "\n".join(lines)


def build_markdown(modules: List[ModuleInfo]) -> str:
    project_modules = build_project_map(modules)
    file_graph = build_mermaid_file_graph(modules, project_modules)
    function_graph = build_mermaid_function_graph(modules, project_modules)
    translate_module = project_modules.get("translate")
    translate_detail_graph = (
        build_mermaid_module_detail_graph(translate_module, project_modules)
        if translate_module is not None
        else ""
    )

    hot_modules = sorted(modules, key=lambda module: (-score_module(module), module.path.as_posix()))
    hot_functions: List[Tuple[ModuleInfo, FunctionInfo]] = []
    for module in modules:
        for fn in module.functions.values():
            hot_functions.append((module, fn))
    hot_functions.sort(key=lambda item: (-score_function(item[1]), item[0].path.as_posix(), item[1].lineno))

    lines = [
        "# Code Review Graph",
        "",
        "Artifact này được sinh tự động từ mã Python trong repo để hỗ trợ review nhanh theo dependency, call flow và hotspot rủi ro.",
        "",
        "## File Dependency Graph",
        "",
        "```mermaid",
        file_graph,
        "```",
        "",
        "## Function Call Graph",
        "",
        "```mermaid",
        function_graph,
        "```",
        "",
        "## Detailed Function Graph: translate.py",
        "",
        "```mermaid",
        translate_detail_graph,
        "```",
        "",
        "## Review Hotspots",
        "",
        "| Module | Score | Tags | Notes |",
        "| --- | ---: | --- | --- |",
    ]

    for module in hot_modules[:10]:
        tags = ", ".join(sorted(module.tags)) or "-"
        notes = []
        if module.has_entrypoint:
            notes.append("entrypoint")
        if module.line_count >= 200:
            notes.append(f"{module.line_count} lines")
        if "threading" in module.tags:
            notes.append("concurrency")
        if "network" in module.tags:
            notes.append("external API")
        lines.append(
            f"| `{module.path.relative_to(ROOT).as_posix()}` | {score_module(module)} | {tags} | {', '.join(notes) or '-'} |"
        )

    lines.extend(
        [
            "",
            "## Function Hotspots",
            "",
            "| Function | Score | Tags |",
            "| --- | ---: | --- |",
        ]
    )
    for module, fn in hot_functions[:12]:
        tags = ", ".join(sorted(fn.tags)) or "-"
        lines.append(
            f"| `{module.path.name}:{fn.name}()` @ L{fn.lineno} | {score_function(fn)} | {tags} |"
        )

    lines.extend(
        [
            "",
            "## Review Order",
            "",
            "1. `translate.py:translate_language()` vì đây là luồng chính, có I/O, XML transform, cache và concurrency.",
            "2. `translate.py:throttled_translate()` vì đụng API ngoài, retry và global rate limit.",
            "3. `translate.py:translate_string()` vì là lớp bảo toàn placeholder/HTML trước khi gọi dịch.",
            "4. `translate.py:postprocess_cdata()` vì sửa nội dung XML sau khi serialize, dễ gây hỏng output.",
            "5. `test_special_cases.py` để kiểm tra coverage hiện tại và khoảng trống test.",
            "",
            "## Coverage Gaps Suggested For Review",
            "",
            "- Chưa thấy test race condition quanh `_last_call_time`, `_memory_lock` và `thread_status`.",
            "- Chưa thấy test end-to-end cho `main()` với file đích đã tồn tại và dữ liệu bị lệch schema.",
            "- Chưa thấy validation cho trường hợp parse XML lỗi ở file nguồn hoặc file đích.",
            "",
        ]
    )

    return "\n".join(lines)


def generate_graph() -> None:
    modules = [parse_module(path) for path in iter_python_files(ROOT)]
    markdown = build_markdown(modules)
    OUTPUT_PATH.write_text(markdown, encoding="utf-8")
    print(f"Wrote {OUTPUT_PATH}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Build code review graph for this repo.")
    parser.add_argument("command", nargs="?", default="init", choices=["init", "impact"], help="CLI command")
    parser.add_argument("target", nargs="?", default=".", help="Target path or module path.")
    parser.add_argument("issue", nargs="?", default="", help="Issue description for impact analysis.")
    args = parser.parse_args()

    if args.command == "init":
        target = Path(args.target).resolve()
        if target != ROOT:
            print(f"Only repo root is supported currently: {ROOT}")
            raise SystemExit(1)
        generate_graph()
        return

    if args.command == "impact":
        modules = [parse_module(path) for path in iter_python_files(ROOT)]
        target_module_name = module_name_from_target(args.target)
        report = build_impact_report(target_module_name, args.issue, modules)
        IMPACT_OUTPUT_PATH.write_text(report, encoding="utf-8")
        print(f"Wrote {IMPACT_OUTPUT_PATH}")
        return


if __name__ == "__main__":
    main()
