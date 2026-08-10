# `__main__.py`

Lets wharf run as `python -m wharf` in addition to the `wharf` console
script installed by `pyproject.toml`'s `[project.scripts]`. Just calls
[`cli.main()`](cli.md) and exits with its return code.
