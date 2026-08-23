"""Platform text for the Rust backend.

The Renode backend hands `.replx` templates to Simantic.Core, which renders
them. The Rust engine parses plain `.repl`, so the same rendering happens
here: every `{{a:b:default}}` placeholder becomes its default, and a default
that is an arithmetic expression (`84000000 / 1000000 * 1.25`) is evaluated.
Model names resolve the way `sim --mcu` does — `~/.sim_cache`, else the
backend with stored credentials, then cached — or from a local model library
when $SIMANTIC_MCU_LIB is set.
"""

from __future__ import annotations

import ast
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from . import auth
from .fixtures import MCU_LIB_ENV, platform_path
from .mcu import SimError

MCU_DETAILS_URL = "https://drjdhqfvrttolueolzif.supabase.co/functions/v1/get-mcu-details"

_PLACEHOLDER = re.compile(r"\{\{([^}]*)\}\}")
_ARITHMETIC = re.compile(r"[0-9. */+()-]+")


def _evaluate(expr: str) -> str:
    """Fold a numeric expression; anything else passes through untouched."""
    expr = expr.strip()
    if not _ARITHMETIC.fullmatch(expr) or re.fullmatch(r"[0-9.]+", expr):
        return expr
    tree = ast.parse(expr, mode="eval")

    def fold(node):
        if isinstance(node, ast.Expression):
            return fold(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return node.value
        if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Sub, ast.Mult, ast.Div)):
            a, b = fold(node.left), fold(node.right)
            return {ast.Add: a + b, ast.Sub: a - b, ast.Mult: a * b, ast.Div: a / b}[type(node.op)]
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
            return -fold(node.operand)
        raise ValueError(f"unsupported expression in platform template: {expr!r}")

    value = fold(tree)
    return str(int(value)) if float(value).is_integer() else str(value)


def render(text: str) -> str:
    """`.replx` → `.repl`: placeholders take their defaults."""
    return _PLACEHOLDER.sub(lambda m: _evaluate(m.group(1).split(":")[-1]), text)


def cache_dir() -> Path:
    return Path(os.environ.get("HOME", "")) / ".sim_cache"


def model_replx(mcu: str, *, use_cache: bool = True) -> str:
    """The `.replx` text for a model name, like `sim --mcu`."""
    if os.environ.get(MCU_LIB_ENV):
        return platform_path(mcu, None, Path.cwd()).read_text()
    cached = cache_dir() / f"{mcu.lower()}.json"
    if use_cache and cached.exists():
        replx = json.loads(cached.read_text()).get("replx")
        if replx:
            return replx
    try:
        credentials = auth.load()
    except auth.NotAuthenticated as exc:
        raise SimError(f"mcu={mcu!r} needs credentials to fetch the model: {exc}") from None
    request = urllib.request.Request(
        f"{MCU_DETAILS_URL}?model={urllib.parse.quote(mcu)}",
        headers={"Authorization": f"Bearer {credentials.api_key}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            details = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        raise SimError(f"mcu={mcu!r} is not a supported model (HTTP {exc.code})") from None
    except urllib.error.URLError as exc:
        raise SimError(f"cannot reach the model backend: {exc.reason}") from None
    replx = details.get("replx")
    if not replx:
        raise SimError(f"the backend returned no platform for mcu={mcu!r}")
    cached.parent.mkdir(parents=True, exist_ok=True)
    cached.write_text(json.dumps({"model": mcu, "replx": replx, "deprecated": bool(details.get("deprecated"))}))
    return replx


def platform_text(*, repl: Path | None, mcu: str | None, overlay: Path | None) -> str:
    """Rendered `.repl` text for one machine."""
    if repl is not None:
        text = repl.read_text()
    else:
        assert mcu is not None
        text = model_replx(mcu)
    if overlay is not None:
        # The platform grammar has no comment syntax; strip note lines first.
        body = "\n".join(l for l in overlay.read_text().splitlines() if not l.lstrip().startswith(("#", "//")))
        text = text.rstrip() + "\n\n" + body.strip() + "\n"
    return render(text)
