# Publishing `simantic` to PyPI

First-time setup, then the per-release loop. Steps marked **[you]** need a
human with the accounts; everything else is automated in
`.github/workflows/python.yml`.

## One-time setup

### 1. Accounts **[you]**

Create accounts on both indexes, with 2FA (PyPI requires it for publishing):

- <https://test.pypi.org/account/register/> — the rehearsal index
- <https://pypi.org/account/register/> — the real one

TestPyPI is a genuinely separate site with separate credentials. Use it for
the first upload so a mistake is not permanent.

### 2. Claim the name **[you]**

`simantic` was unregistered as of this writing. Names are first-come and a
published version number can never be reused or overwritten — only yanked —
so publish `0.1.0` to TestPyPI first, confirm it looks right, and only then
push the real tag. Claiming early is cheap insurance against someone else
taking it.

### 3. Configure Trusted Publishing **[you]**

This replaces API tokens with short-lived OIDC credentials, so there is no
long-lived secret in the repo. On PyPI, go to your account's **Publishing**
page and add a *pending* publisher (pending = the project does not exist
yet, which is the case before the first upload):

| Field | Value |
|---|---|
| PyPI project name | `simantic` |
| Owner | `simantic-dev` |
| Repository name | `pippy` (the GitHub repo name, not the package name) |
| Workflow name | `ci.yml` |
| Environment name | `pypi` |

Then in GitHub: **Settings → Environments → New environment → `pypi`**. Add
required reviewers there if you want a human approval gate before any upload.

Repeat the whole step on TestPyPI if you want the rehearsal automated;
otherwise do the rehearsal upload by hand (below).

## Rehearsal upload **[you]**

```bash
uv build
uvx twine check dist/*                       # metadata PyPI would reject
uvx twine upload --repository testpypi dist/*
```

Then confirm a clean machine can install and import it:

```bash
uv run --with simantic --index https://test.pypi.org/simple/ \
       --index-strategy unsafe-best-match \
       python -c "import simantic; print(simantic.__version__)"
```

## Releasing

1. Bump `version` in `pyproject.toml` **and** `__version__` in
   `src/simantic/__init__.py`. They are asserted equal by the test suite.
2. Merge to `main`.
3. Tag and push:

```bash
git tag v0.1.0
git push origin v0.1.0
```

The workflow tests on Linux/macOS/Windows across 3.11 and 3.13, builds the
sdist and wheel, runs `twine check`, and uploads via trusted publishing.

## Versioning

Semantic versioning on the SDK's own surface, which is independent of the
`analog-cli` version it drives. The coupling that matters is the report
schema: `simantic` speaks `analog-cli.test-report/1` and refuses anything
else, so a schema revision in the CLI is a major bump here.

Stay on `0.x` until the API has survived real use. Pre-1.0 signals that
breaking changes can still happen, which is honest for a first release.

## Notes for later

- **Binary wheels.** The SDK deliberately does not bundle a simulator; it
  resolves each binary from its `$SIMANTIC_*` variable, then a `_bin/`
  directory inside the package, then PATH. A separate platform-specific wheel
  can therefore drop a binary into `_bin/` and be found with no SDK change,
  which is the seam to use if engines are ever distributed through PyPI.
- **Closed-source binaries are fine on PyPI.** Wheels need not contain
  source, and the index has no open-source requirement. Keeping this SDK MIT
  while the engines stay proprietary is a normal arrangement.
