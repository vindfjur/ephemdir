# Contributing to ephemdir

Thanks for taking the time to contribute! ephemdir is a small, security-sensitive
library — it deletes directories — so the bar for changes is deliberately high.
This guide explains how to get set up and what is expected of a pull request.

## Code of Conduct

This project is governed by the [Contributor Covenant](CODE_OF_CONDUCT.md). By
participating you are expected to uphold it. Please report unacceptable
behaviour to the maintainer (see the Code of Conduct for contact details).

## Reporting bugs and requesting features

- Search the [existing issues](https://github.com/vindfjur/ephemdir/issues)
  first.
- Use the issue templates. For anything involving unexpected deletion, include
  the operating system, ephemdir version, the exact command/API call, and the
  directory layout, permissions and mount/symlink setup.
- **Do not** open a public issue for a security vulnerability. Follow the
  [Security Policy](SECURITY.md) instead.

## Development setup

ephemdir uses a `src/` layout and targets Python 3.10+ on Linux and macOS.

```bash
git clone https://github.com/vindfjur/ephemdir
cd ephemdir
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

## Checks before opening a pull request

All of these must pass; CI runs the same commands:

```bash
pytest                       # the full test suite
ruff check src tests         # lint (and `ruff format` if you reformat)
mypy                         # static types (strict, configured in pyproject)
```

When you change behaviour, add or update tests in `tests/`. New safety
properties in particular should come with a regression test that fails without
the fix — see `tests/test_hardening.py`, `tests/test_platform_safety.py` and
`tests/test_recovery_safety.py` for the style (real, non-mocked scenarios where
practical, synthetic ownership where a second uid would be needed).

## Pull request guidelines

- Keep each PR focused on one change; smaller is easier to review.
- Match the surrounding code style: explicit error handling, no broad
  `except Exception`, and comments that explain *why* a safety check exists.
- Update `CHANGELOG.md` under an `## [Unreleased]` heading (or the maintainer
  will fold your entry into the next release).
- Update `README.md` when you add or change user-facing behaviour.
- Never weaken a deletion-safety guarantee without a clear discussion in the PR.

## Releases

Releases are cut by the maintainer. Publishing to PyPI is automated via GitHub
Actions and [PyPI Trusted Publishing](https://docs.pypi.org/trusted-publishers/):
publishing a GitHub Release triggers `.github/workflows/publish.yml`, which
builds the distributions from a hash-pinned toolchain and uploads them. No API
tokens are stored in the repository.

## License

By contributing, you agree that your contributions will be licensed under the
[MIT License](LICENSE) that covers the project.
