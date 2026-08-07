"""`python -m simantic`, equivalent to the `simantic` command.

The generated launcher lands in the environment's bin/ (Scripts\\ on Windows),
which is not always on PATH. This entry point needs only an interpreter that
can import the package, so it works wherever the install did.
"""

from ._cli import main

raise SystemExit(main())
