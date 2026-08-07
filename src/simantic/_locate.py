"""Finding the simulator binaries.

The SDK never bundles a binary: it drives whichever one the caller points at.
Resolution order, most explicit first:

1. an explicit argument
2. the binary's environment variable
3. ~/.simantic/bin, where `simantic install` puts things
4. a `_bin/` directory inside this package
5. PATH

A deliberate `simantic install` outranks a bundled `_bin/` copy, so fetching
a newer binary actually takes effect. The `_bin/` step is what would let a
platform-specific wheel ship a binary and be found with no change here.
"""

import os
import shutil
from pathlib import Path


class BinaryNotFound(RuntimeError):
    """Raised when a simulator binary cannot be located."""


def locate(
    binary: str,
    env_var: str,
    explicit: str | os.PathLike[str] | None = None,
) -> Path:
    """Resolve `binary`, or raise BinaryNotFound explaining how to supply it."""
    if explicit is not None:
        path = Path(explicit)
        if not path.exists():
            raise BinaryNotFound(f"{path} does not exist")
        return path

    env = os.environ.get(env_var)
    if env:
        path = Path(env)
        if not path.exists():
            raise BinaryNotFound(f"${env_var} points at {path}, which does not exist")
        return path

    # Imported here: install imports nothing from this module, but keeping the
    # dependency one-way makes that impossible to get wrong later.
    from .install import bin_dir

    try:
        managed = bin_dir() / binary
    except Exception:  # no HOME and no $SIMANTIC_HOME — just skip this step
        managed = None
    if managed is not None and managed.exists():
        return managed

    bundled = Path(__file__).parent / "_bin" / binary
    if bundled.exists():
        return bundled

    found = shutil.which(binary)
    if found:
        return Path(found)

    raise BinaryNotFound(
        f"no {binary!r} binary found. Run `simantic install {binary}`, put it on "
        f"PATH, or set ${env_var} to its location."
    )


ENV_VAR = "SIMANTIC_ANALOG_CLI"
BINARY = "analog-cli"


def analog_cli(explicit: str | os.PathLike[str] | None = None) -> Path:
    """Resolve the analog-cli binary, or raise BinaryNotFound."""
    return locate(BINARY, ENV_VAR, explicit)
