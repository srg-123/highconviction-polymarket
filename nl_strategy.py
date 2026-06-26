"""
Converts a plain-English strategy description into an executable Python
predicate via Claude, then registers it into the live STRATEGIES registry
(strategies.py) so it can be run through the existing backtester.

A generated predicate must match the same contract as every hand-written
strategy in strategies.py:

    def fn(snapshot: dict, history: list, **params) -> dict

Generated code is untrusted model output, so it's validated against an AST
allowlist (no imports, no file/network access, no exec/eval, no loops that
could hang) before being exec'd in a namespace with a restricted builtins
set. Successfully generated strategies are persisted to
generated_strategies.json so they survive an API restart.
"""

import ast
import json
import re
from pathlib import Path

import anthropic

from strategies import STRATEGIES

MODEL = "claude-sonnet-4-6"
GENERATED_PATH = Path("generated_strategies.json")

_SYSTEM_PROMPT = """You convert a trader's plain-English description of a Polymarket trading strategy into a single Python function.

Contract (must match exactly):
    def STRATEGY_NAME(snapshot: dict, history: list, **params) -> dict:
        ...

- snapshot = {"timestamp": int, "price": float}  — current YES price (0.0-1.0) of the market
- history  = list of prior snapshots, oldest first, same shape as snapshot
- Return {"bet": "yes" | "no" | None, "size_fraction": float, "reason": str}
  - "bet": "yes" to buy the YES contract, "no" to buy the NO contract, None for no signal
  - "size_fraction": fraction of bankroll to stake (0.0-0.10 is reasonable), 0.0 if bet is None
  - "reason": short human-readable explanation of why the signal fired

Rules:
- Pure function: no imports, no I/O, no network calls, no global state, no exec/eval, no while-loops.
- Only use builtins: abs, min, max, round, len, range, sum, float, int, str, bool, enumerate, sorted, zip.
- Bake any specific numeric thresholds mentioned in the description directly into the function as constants.
- snake_case function name derived from the description.

Respond with ONLY a JSON object: {"name": "<function_name>", "code": "<full function source, def to return>", "summary": "<one sentence>"}
No markdown fences, no commentary."""

_ALLOWED_BUILTINS = {
    "abs": abs, "min": min, "max": max, "round": round, "len": len,
    "range": range, "sum": sum, "float": float, "int": int, "str": str,
    "bool": bool, "enumerate": enumerate, "sorted": sorted, "zip": zip,
    "True": True, "False": False, "None": None,
}

_FORBIDDEN_NODE_TYPES = (
    ast.Import, ast.ImportFrom, ast.Global, ast.Nonlocal,
    ast.With, ast.AsyncWith, ast.While,
    ast.FunctionDef, ast.AsyncFunctionDef,  # only the top-level def is allowed
    ast.ClassDef,
)

_FORBIDDEN_NAMES = {
    "eval", "exec", "compile", "open", "__import__", "getattr", "setattr",
    "delattr", "globals", "locals", "vars", "input", "breakpoint", "exit", "quit",
}


def _validate_ast(tree: ast.Module, expected_fn_name: str) -> ast.FunctionDef:
    if len(tree.body) != 1 or not isinstance(tree.body[0], ast.FunctionDef):
        raise ValueError("Generated code must be exactly one top-level function definition")

    fn_def = tree.body[0]
    if fn_def.name != expected_fn_name:
        raise ValueError(f"Function name mismatch: expected {expected_fn_name!r}, got {fn_def.name!r}")

    # Walk the function body only — the top-level FunctionDef itself is fine,
    # but nothing forbidden may appear inside it.
    for node in ast.walk(fn_def):
        if node is fn_def:
            continue
        if isinstance(node, _FORBIDDEN_NODE_TYPES):
            raise ValueError(f"Disallowed construct: {type(node).__name__}")
        if isinstance(node, ast.Attribute) and node.attr.startswith("__"):
            raise ValueError("Disallowed dunder attribute access")
        if isinstance(node, ast.Name) and node.id in _FORBIDDEN_NAMES:
            raise ValueError(f"Disallowed name: {node.id}")

    return fn_def


def _compile_predicate(code: str, fn_name: str):
    tree = ast.parse(code, mode="exec")
    _validate_ast(tree, fn_name)
    namespace = {"__builtins__": _ALLOWED_BUILTINS}
    exec(compile(tree, "<generated_strategy>", "exec"), namespace)
    fn = namespace.get(fn_name)
    if not callable(fn):
        raise ValueError("Generated code did not define the expected function")
    return fn


def _sanitize_name(raw: str) -> str:
    name = re.sub(r"[^a-z0-9_]", "_", raw.strip().lower()).strip("_")
    if not name:
        name = "nl_strategy"
    if name[0].isdigit():
        name = f"nl_{name}"
    return name


def _strip_code_fences(text: str) -> str:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    return text.strip()


def generate_strategy_from_text(description: str) -> dict:
    description = description.strip()
    if not description:
        raise ValueError("Description cannot be empty")

    client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from env
    resp = client.messages.create(
        model=MODEL,
        max_tokens=1024,
        system=_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": description}],
    )
    raw = _strip_code_fences(resp.content[0].text)

    try:
        spec = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Model did not return valid JSON: {exc}") from exc

    for key in ("name", "code", "summary"):
        if key not in spec:
            raise ValueError(f"Model response missing required field: {key}")

    fn = _compile_predicate(spec["code"], spec["name"])

    name = _sanitize_name(spec["name"])
    if name in STRATEGIES and STRATEGIES[name].get("source") != spec["code"]:
        name = f"{name}_{abs(hash(description)) % 10000}"

    STRATEGIES[name] = {
        "fn": fn,
        "description": spec["summary"][:200],
        "params": {},
        "source": spec["code"],
        "generated_from": description,
    }
    _persist()

    return {"name": name, "description": STRATEGIES[name]["description"], "source": spec["code"]}


def _persist() -> None:
    data = {}
    if GENERATED_PATH.exists():
        try:
            data = json.loads(GENERATED_PATH.read_text())
        except json.JSONDecodeError:
            data = {}

    for name, info in STRATEGIES.items():
        if "generated_from" in info:
            data[name] = {
                "source": info["source"],
                "description": info["description"],
                "generated_from": info["generated_from"],
            }

    GENERATED_PATH.write_text(json.dumps(data, indent=2))


def load_generated_strategies() -> None:
    """Re-validate and re-register previously generated strategies on startup."""
    if not GENERATED_PATH.exists():
        return

    try:
        data = json.loads(GENERATED_PATH.read_text())
    except json.JSONDecodeError:
        return

    for name, info in data.items():
        try:
            tree = ast.parse(info["source"], mode="exec")
            fn_name = tree.body[0].name
            fn = _compile_predicate(info["source"], fn_name)
            STRATEGIES[name] = {
                "fn": fn,
                "description": info["description"],
                "params": {},
                "source": info["source"],
                "generated_from": info["generated_from"],
            }
        except Exception as exc:
            print(f"Skipping invalid generated strategy {name!r}: {exc}")
