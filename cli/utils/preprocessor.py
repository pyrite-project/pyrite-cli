import ast
from typing import Set

import libcst as cst


def _is_macro(node) -> bool:
    return (
        isinstance(node, cst.Call)
        and isinstance(node.func, cst.Name)
        and node.func.value in ("feature", "target")
        and len(node.args) == 1
    )


def _tag(call: cst.Call) -> str:
    arg = call.args[0].value
    return ast.literal_eval(arg.value) if isinstance(arg, cst.SimpleString) else ""


class _Transformer(cst.CSTTransformer):
    def __init__(self, active_tags: Set[str]):
        self.active_tags = active_tags

    def leave_With(self, original_node, updated_node):
        if len(updated_node.items) != 1:
            return updated_node
        item = updated_node.items[0].item
        if not _is_macro(item):
            return updated_node
        test = cst.Name("True") if self.active_tags is None or _tag(item) in self.active_tags else cst.Name("False")
        return cst.If(
            test=test,
            body=updated_node.body,
            orelse=None,
            leading_lines=updated_node.leading_lines,
        )

    def leave_FunctionDef(self, original_node, updated_node):
        macro_decs = [d for d in updated_node.decorators if _is_macro(d.decorator)]
        if not macro_decs:
            return updated_node
        matched = self.active_tags is None or all(_tag(d.decorator) in self.active_tags for d in macro_decs)
        clean = updated_node.with_changes(
            decorators=[d for d in updated_node.decorators if not _is_macro(d.decorator)]
        )
        if matched:
            return clean
        return cst.If(
            test=cst.Name("False"),
            body=cst.IndentedBlock(body=[clean]),
            orelse=None,
            leading_lines=macro_decs[0].leading_lines,
        )


class _Analyzer(cst.CSTVisitor):
    def __init__(self, active_tags: Set[str]):
        self.active_tags = active_tags
        self._fn_tags: dict = {}
        self._disabled: Set[str] = set()
        self._guarded_calls: Set[str] = set()
        self._bare_calls: list = []
        self._in_guard = False

    def visit_FunctionDef(self, node: cst.FunctionDef):
        macro_decs = [d for d in node.decorators if _is_macro(d.decorator)]
        if macro_decs:
            name = node.name.value
            self._fn_tags.setdefault(name, []).extend(_tag(d.decorator) for d in macro_decs)

    def _compute_disabled(self):
        for name, tags in self._fn_tags.items():
            if not any(t in self.active_tags for t in tags):
                self._disabled.add(name)

    def visit_With(self, node: cst.With):
        if len(node.items) == 1 and _is_macro(node.items[0].item):
            self._in_guard = True

    def leave_With(self, node: cst.With):
        if len(node.items) == 1 and _is_macro(node.items[0].item):
            self._in_guard = False

    def visit_Call(self, node: cst.Call):
        if isinstance(node.func, cst.Name):
            name = node.func.value
            if self._in_guard:
                self._guarded_calls.add(name)
            else:
                self._bare_calls.append(name)


def _warn(msg: str):
    import typer
    typer.secho(msg, fg=typer.colors.YELLOW, err=True)


def _analyze_warnings(tree: cst.Module, active_tags: Set[str], filename: str):
    analyzer = _Analyzer(active_tags)
    tree.visit(analyzer)
    analyzer._compute_disabled()
    for name in analyzer._disabled:
        _warn(f"  [警告] {filename}: 函数 '{name}' 所有实现均不匹配当前 tags")
    for name in analyzer._bare_calls:
        if name in analyzer._disabled and name not in analyzer._guarded_calls:
            _warn(f"  [警告] {filename}: 裸调用 '{name}()' 未被 guard 保护，运行时将 NameError")


def preprocess(source: str, active_tags: Set[str], filename: str = "") -> str:
    tree = cst.parse_module(source)
    _analyze_warnings(tree, active_tags, filename)
    return tree.visit(_Transformer(active_tags)).code
