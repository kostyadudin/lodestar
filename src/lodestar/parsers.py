"""Tree-sitter symbol extraction.

Falls back gracefully when tree-sitter or a specific language grammar is not installed.
Install the optional extras to activate:  pip install lodestar[parsers]
"""

from __future__ import annotations

from .utils import token_estimate

# ── availability probe ────────────────────────────────────────────────────────
_AVAILABLE = False
try:
    from tree_sitter import Language as _Language, Parser as _TSParser  # type: ignore[import]
    _AVAILABLE = True
except ImportError:
    pass

_parser_cache: dict[str, object] = {}  # language -> _TSParser | None sentinel


def is_available() -> bool:
    return _AVAILABLE


# ── public entry point ────────────────────────────────────────────────────────

def extract_symbols(rel_path: str, language: str, content: str) -> list[dict] | None:
    """Return a symbol list using tree-sitter, or None when unavailable/unsupported."""
    if not _AVAILABLE:
        return None
    parser = _get_parser(language)
    if parser is None:
        return None
    extractor = _EXTRACTORS.get(language)
    if extractor is None:
        return None
    content_bytes = content.encode("utf-8")
    tree = parser.parse(content_bytes)
    lines = content.splitlines()
    symbols = extractor(rel_path, content_bytes, lines, tree.root_node)
    return sorted(symbols, key=lambda s: s["line_start"])


# ── parser loading ────────────────────────────────────────────────────────────

def _get_parser(language: str):
    if language not in _parser_cache:
        _parser_cache[language] = _load_parser(language)
    return _parser_cache[language]


def _load_parser(language: str):
    if not _AVAILABLE:
        return None
    try:
        from tree_sitter import Language, Parser  # type: ignore[import]
        if language == "python":
            import tree_sitter_python as m  # type: ignore[import]
            lang = Language(m.language())
        elif language == "javascript":
            import tree_sitter_javascript as m  # type: ignore[import]
            lang = Language(m.language())
        elif language == "typescript":
            import tree_sitter_typescript as m  # type: ignore[import]
            lang = Language(m.language_typescript())
        elif language == "go":
            import tree_sitter_go as m  # type: ignore[import]
            lang = Language(m.language())
        elif language == "rust":
            import tree_sitter_rust as m  # type: ignore[import]
            lang = Language(m.language())
        elif language == "java":
            import tree_sitter_java as m  # type: ignore[import]
            lang = Language(m.language())
        elif language == "ruby":
            import tree_sitter_ruby as m  # type: ignore[import]
            lang = Language(m.language())
        elif language == "php":
            import tree_sitter_php as m  # type: ignore[import]
            lang = Language(m.language_php())
        else:
            return None
        return Parser(lang)
    except (ImportError, AttributeError, Exception):
        return None


# ── shared helpers ────────────────────────────────────────────────────────────

def _node_text(node, content_bytes: bytes) -> str:
    return content_bytes[node.start_byte:node.end_byte].decode("utf-8", errors="replace")


def _first_line(node, lines: list[str]) -> str:
    row = node.start_point[0]
    return lines[row].strip()[:160] if row < len(lines) else ""


def _make_symbol(
    rel_path: str,
    content_bytes: bytes,
    lines: list[str],
    node,
    name: str,
    kind: str,
    signature: str,
) -> dict:
    line_start = node.start_point[0] + 1  # 1-indexed
    line_end = node.end_point[0] + 1
    text = _node_text(node, content_bytes)
    return {
        "symbol_id": f"symbol:{rel_path}:{name}:{line_start}",
        "path": rel_path,
        "name": name,
        "kind": kind,
        "signature": signature[:160],
        "line_start": line_start,
        "line_end": line_end,
        "summary": f"{kind} {name} in {rel_path} at lines {line_start}-{line_end}",
        "text": text,
        "token_estimate": token_estimate(text),
    }


# ── Python ────────────────────────────────────────────────────────────────────

def _extract_python(rel_path: str, content_bytes: bytes, lines: list[str], root) -> list[dict]:
    symbols: list[dict] = []
    _walk_python(rel_path, content_bytes, lines, root, symbols, parent_class=None)
    return symbols


def _walk_python(rel_path, content_bytes, lines, node, symbols, parent_class):
    for child in node.children:
        t = child.type
        if t == "decorated_definition":
            # @decorator\ndef foo / class Foo — unwrap to the inner definition
            for sub in child.children:
                if sub.type in ("function_definition", "async_function_definition", "class_definition"):
                    _process_python_def(rel_path, content_bytes, lines, sub, symbols, parent_class)
                    break
        elif t in ("function_definition", "async_function_definition", "class_definition"):
            _process_python_def(rel_path, content_bytes, lines, child, symbols, parent_class)
        else:
            _walk_python(rel_path, content_bytes, lines, child, symbols, parent_class)


def _process_python_def(rel_path, content_bytes, lines, node, symbols, parent_class):
    name_node = node.child_by_field_name("name")
    if not name_node:
        return
    bare = _node_text(name_node, content_bytes)
    sig = _first_line(node, lines)

    if node.type == "class_definition":
        # Qualify with outer class when nested
        name = f"{parent_class}.{bare}" if parent_class else bare
        symbols.append(_make_symbol(rel_path, content_bytes, lines, node, name, "class", sig))
        # Recurse into class body; use bare name so methods are "ClassName.method"
        _walk_python(rel_path, content_bytes, lines, node, symbols, parent_class=bare)
    else:
        name = f"{parent_class}.{bare}" if parent_class else bare
        kind = "method" if parent_class else "function"
        symbols.append(_make_symbol(rel_path, content_bytes, lines, node, name, kind, sig))
        # Recurse into function body for inner functions; reset parent context
        _walk_python(rel_path, content_bytes, lines, node, symbols, parent_class=None)


# ── JavaScript / TypeScript ───────────────────────────────────────────────────

def _extract_js(rel_path: str, content_bytes: bytes, lines: list[str], root) -> list[dict]:
    symbols: list[dict] = []
    _walk_js(rel_path, content_bytes, lines, root, symbols, parent_class=None)
    return symbols


def _walk_js(rel_path, content_bytes, lines, node, symbols, parent_class):
    for child in node.children:
        t = child.type

        if t == "function_declaration":
            name_node = child.child_by_field_name("name")
            if name_node:
                name = _node_text(name_node, content_bytes)
                symbols.append(_make_symbol(rel_path, content_bytes, lines, child, name, "function", _first_line(child, lines)))

        elif t == "class_declaration":
            name_node = child.child_by_field_name("name")
            if name_node:
                class_name = _node_text(name_node, content_bytes)
                symbols.append(_make_symbol(rel_path, content_bytes, lines, child, class_name, "class", _first_line(child, lines)))
                _walk_js(rel_path, content_bytes, lines, child, symbols, parent_class=class_name)

        elif t == "method_definition":
            name_node = child.child_by_field_name("name")
            if name_node:
                bare = _node_text(name_node, content_bytes)
                name = f"{parent_class}.{bare}" if parent_class else bare
                symbols.append(_make_symbol(rel_path, content_bytes, lines, child, name, "method", _first_line(child, lines)))

        elif t in ("lexical_declaration", "variable_declaration"):
            # const foo = () => {} or const foo = function() {}
            for decl in child.children:
                if decl.type == "variable_declarator":
                    var_name = decl.child_by_field_name("name")
                    value = decl.child_by_field_name("value")
                    if var_name and value and value.type in ("arrow_function", "function_expression"):
                        name = _node_text(var_name, content_bytes)
                        symbols.append(_make_symbol(rel_path, content_bytes, lines, child, name, "function", _first_line(child, lines)))

        elif t == "export_statement":
            # export function/class/const — recurse to find the declaration
            _walk_js(rel_path, content_bytes, lines, child, symbols, parent_class)

        elif t in ("interface_declaration", "abstract_class_declaration"):
            name_node = child.child_by_field_name("name")
            if name_node:
                name = _node_text(name_node, content_bytes)
                kind = "interface" if t == "interface_declaration" else "class"
                symbols.append(_make_symbol(rel_path, content_bytes, lines, child, name, kind, _first_line(child, lines)))

        elif t in ("type_alias_declaration", "enum_declaration"):
            name_node = child.child_by_field_name("name")
            if name_node:
                name = _node_text(name_node, content_bytes)
                kind = "type" if t == "type_alias_declaration" else "enum"
                symbols.append(_make_symbol(rel_path, content_bytes, lines, child, name, kind, _first_line(child, lines)))

        else:
            _walk_js(rel_path, content_bytes, lines, child, symbols, parent_class)


# ── Go ────────────────────────────────────────────────────────────────────────

def _extract_go(rel_path: str, content_bytes: bytes, lines: list[str], root) -> list[dict]:
    symbols: list[dict] = []
    for child in root.children:
        t = child.type
        if t == "function_declaration":
            name_node = child.child_by_field_name("name")
            if name_node:
                name = _node_text(name_node, content_bytes)
                symbols.append(_make_symbol(rel_path, content_bytes, lines, child, name, "function", _first_line(child, lines)))

        elif t == "method_declaration":
            name_node = child.child_by_field_name("name")
            receiver_node = child.child_by_field_name("receiver")
            if name_node:
                bare = _node_text(name_node, content_bytes)
                receiver_type = ""
                if receiver_node:
                    # receiver text is like "(r *MyType)" — extract the type name
                    rv = _node_text(receiver_node, content_bytes).strip("() \t\n")
                    parts = rv.split()
                    receiver_type = parts[-1].lstrip("*") if parts else ""
                name = f"{receiver_type}.{bare}" if receiver_type else bare
                symbols.append(_make_symbol(rel_path, content_bytes, lines, child, name, "method", _first_line(child, lines)))

        elif t == "type_declaration":
            for spec in child.children:
                if spec.type == "type_spec":
                    name_node = spec.child_by_field_name("name")
                    type_node = spec.child_by_field_name("type")
                    if name_node:
                        name = _node_text(name_node, content_bytes)
                        kind = "struct" if type_node and type_node.type == "struct_type" else "type"
                        symbols.append(_make_symbol(rel_path, content_bytes, lines, child, name, kind, _first_line(child, lines)))
    return symbols


# ── Rust ──────────────────────────────────────────────────────────────────────

def _extract_rust(rel_path: str, content_bytes: bytes, lines: list[str], root) -> list[dict]:
    symbols: list[dict] = []
    _walk_rust(rel_path, content_bytes, lines, root, symbols, impl_type=None)
    return symbols


def _walk_rust(rel_path, content_bytes, lines, node, symbols, impl_type):
    for child in node.children:
        t = child.type
        if t == "function_item":
            name_node = child.child_by_field_name("name")
            if name_node:
                bare = _node_text(name_node, content_bytes)
                name = f"{impl_type}.{bare}" if impl_type else bare
                kind = "method" if impl_type else "function"
                symbols.append(_make_symbol(rel_path, content_bytes, lines, child, name, kind, _first_line(child, lines)))

        elif t in ("struct_item", "enum_item", "trait_item"):
            name_node = child.child_by_field_name("name")
            if name_node:
                name = _node_text(name_node, content_bytes)
                kind = t.replace("_item", "")
                symbols.append(_make_symbol(rel_path, content_bytes, lines, child, name, kind, _first_line(child, lines)))

        elif t == "impl_item":
            type_node = child.child_by_field_name("type")
            new_impl = _node_text(type_node, content_bytes) if type_node else None
            _walk_rust(rel_path, content_bytes, lines, child, symbols, impl_type=new_impl)

        else:
            _walk_rust(rel_path, content_bytes, lines, child, symbols, impl_type)


# ── Java ──────────────────────────────────────────────────────────────────────

def _extract_java(rel_path: str, content_bytes: bytes, lines: list[str], root) -> list[dict]:
    symbols: list[dict] = []
    _walk_java(rel_path, content_bytes, lines, root, symbols, parent_class=None)
    return symbols


def _walk_java(rel_path, content_bytes, lines, node, symbols, parent_class):
    for child in node.children:
        t = child.type
        if t in ("class_declaration", "interface_declaration", "enum_declaration", "record_declaration"):
            name_node = child.child_by_field_name("name")
            if name_node:
                name = _node_text(name_node, content_bytes)
                kind = t.replace("_declaration", "")
                symbols.append(_make_symbol(rel_path, content_bytes, lines, child, name, kind, _first_line(child, lines)))
                _walk_java(rel_path, content_bytes, lines, child, symbols, parent_class=name)

        elif t == "method_declaration":
            name_node = child.child_by_field_name("name")
            if name_node:
                bare = _node_text(name_node, content_bytes)
                name = f"{parent_class}.{bare}" if parent_class else bare
                symbols.append(_make_symbol(rel_path, content_bytes, lines, child, name, "method", _first_line(child, lines)))

        elif t == "constructor_declaration":
            name_node = child.child_by_field_name("name")
            if name_node:
                bare = _node_text(name_node, content_bytes)
                name = f"{parent_class}.{bare}" if parent_class else bare
                symbols.append(_make_symbol(rel_path, content_bytes, lines, child, name, "constructor", _first_line(child, lines)))

        else:
            _walk_java(rel_path, content_bytes, lines, child, symbols, parent_class)


# ── Ruby ──────────────────────────────────────────────────────────────────────

def _extract_ruby(rel_path: str, content_bytes: bytes, lines: list[str], root) -> list[dict]:
    symbols: list[dict] = []
    _walk_ruby(rel_path, content_bytes, lines, root, symbols, parent_class=None)
    return symbols


def _walk_ruby(rel_path, content_bytes, lines, node, symbols, parent_class):
    for child in node.children:
        t = child.type
        if t == "method":
            name_node = child.child_by_field_name("name")
            if name_node:
                bare = _node_text(name_node, content_bytes)
                name = f"{parent_class}.{bare}" if parent_class else bare
                kind = "method" if parent_class else "function"
                symbols.append(_make_symbol(rel_path, content_bytes, lines, child, name, kind, _first_line(child, lines)))
                _walk_ruby(rel_path, content_bytes, lines, child, symbols, parent_class=None)

        elif t in ("class", "module"):
            name_node = child.child_by_field_name("name")
            if name_node:
                name = _node_text(name_node, content_bytes)
                kind = t  # "class" or "module"
                symbols.append(_make_symbol(rel_path, content_bytes, lines, child, name, kind, _first_line(child, lines)))
                _walk_ruby(rel_path, content_bytes, lines, child, symbols, parent_class=name)

        elif t == "singleton_method":
            # def self.foo
            obj_node = child.child_by_field_name("object")
            name_node = child.child_by_field_name("name")
            if name_node:
                bare = _node_text(name_node, content_bytes)
                obj = _node_text(obj_node, content_bytes) if obj_node else "self"
                name = f"{obj}.{bare}"
                symbols.append(_make_symbol(rel_path, content_bytes, lines, child, name, "method", _first_line(child, lines)))

        else:
            _walk_ruby(rel_path, content_bytes, lines, child, symbols, parent_class)


# ── PHP ───────────────────────────────────────────────────────────────────────

def _extract_php(rel_path: str, content_bytes: bytes, lines: list[str], root) -> list[dict]:
    symbols: list[dict] = []
    # PHP tree has a `program` root; walk its direct children
    _walk_php(rel_path, content_bytes, lines, root, symbols, parent_class=None)
    return symbols


def _walk_php(rel_path, content_bytes, lines, node, symbols, parent_class):
    for child in node.children:
        t = child.type

        if t == "function_definition":
            name_node = child.child_by_field_name("name")
            if name_node:
                name = _node_text(name_node, content_bytes)
                symbols.append(_make_symbol(rel_path, content_bytes, lines, child, name, "function", _first_line(child, lines)))

        elif t in ("class_declaration", "interface_declaration", "trait_declaration"):
            name_node = child.child_by_field_name("name")
            if name_node:
                class_name = _node_text(name_node, content_bytes)
                kind = t.replace("_declaration", "")
                symbols.append(_make_symbol(rel_path, content_bytes, lines, child, class_name, kind, _first_line(child, lines)))
                _walk_php(rel_path, content_bytes, lines, child, symbols, parent_class=class_name)

        elif t == "method_declaration":
            name_node = child.child_by_field_name("name")
            if name_node:
                bare = _node_text(name_node, content_bytes)
                name = f"{parent_class}.{bare}" if parent_class else bare
                symbols.append(_make_symbol(rel_path, content_bytes, lines, child, name, "method", _first_line(child, lines)))

        else:
            _walk_php(rel_path, content_bytes, lines, child, symbols, parent_class)


# ── dispatch table ────────────────────────────────────────────────────────────

_EXTRACTORS = {
    "python": _extract_python,
    "javascript": _extract_js,
    "typescript": _extract_js,   # TS grammar is a JS superset; same walker handles TS nodes
    "go": _extract_go,
    "rust": _extract_rust,
    "java": _extract_java,
    "ruby": _extract_ruby,
    "php": _extract_php,
}
