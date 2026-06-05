# Contributing

Thanks for helping improve OctoPrint PandaBreath.

## Code of Conduct

By participating, you agree to follow the [Code of Conduct](CODE_OF_CONDUCT.md).

## Ways to Contribute

- report bugs with clear reproduction steps
- suggest features with a short problem statement
- submit focused code changes with tests when needed
- improve docs or translations

## Local Setup

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e .[develop]
```

## Useful Commands

```bash
pytest
python -m build --sdist --wheel
./.development/compile_translations.sh
```

## Pull Requests

- keep changes small and focused
- update tests and docs when behavior changes
- avoid unrelated edits
- write a short, clear PR description

## License

Contributions are made under the repository license, MIT.
