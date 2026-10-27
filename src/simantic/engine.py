"""Loading the in-process engine: the `simantic_rust` extension module.

`simantic install engine-rust` (or `$SIMANTIC_RUST_ENGINE_DIR`) gives us the
directory that holds it. Nothing is spawned: the Python objects *are* the
session.
"""

from __future__ import annotations

import os
import sys
from functools import cache
from pathlib import Path

from . import install

RUST_ENV_DIR = "SIMANTIC_RUST_ENGINE_DIR"


class EngineNotFound(RuntimeError):
    """The engine could not be located or loaded."""


def rust_engine_dir(explicit: str | os.PathLike[str] | None = None, *, fetch: bool = True) -> Path:
    """The directory holding the `simantic_rust` extension module.

    Order: an explicit path, $SIMANTIC_RUST_ENGINE_DIR, the managed install
    under ~/.simantic/engine-rust; else fetched from the public release.
    """
    candidates = [Path(p) for p in (explicit, os.environ.get(RUST_ENV_DIR)) if p]
    for d in candidates:
        if install.is_rust_engine(d):
            return d
    managed = install.installed_rust_engine()
    if managed is not None:
        return managed
    if fetch:
        try:
            return install.install_rust_engine()
        except install.InstallError as exc:
            raise EngineNotFound(f"could not fetch the Rust engine: {exc}") from None
    raise EngineNotFound(
        f"simantic_rust not found. Run `simantic install engine-rust`, or set ${RUST_ENV_DIR}."
    )


@cache
def load_rust(explicit: str | os.PathLike[str] | None = None):
    """Import the Rust engine. A `simantic_rust` already importable (a
    development `maturin develop`, or the wheel installed directly) wins."""
    try:
        import simantic_rust  # type: ignore[import-not-found]

        return simantic_rust
    except ImportError:
        pass
    d = rust_engine_dir(explicit)
    if str(d) not in sys.path:
        sys.path.insert(0, str(d))
    try:
        import simantic_rust  # type: ignore[import-not-found]
    except ImportError as exc:
        raise EngineNotFound(f"{d} does not contain a loadable simantic_rust: {exc}") from None
    return simantic_rust
