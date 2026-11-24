# Code style

- Python is formatted with Ruff, 80 columns (configured in `pyproject.toml`).
- After editing Python, run `uv run ruff format` on the files you changed.
- Ruff doesn't wrap comments, docstrings or long strings: keep those within
  80 columns by hand, except unbreakable things like URLs.
- Pull long expressions into named variables rather than splitting them
  awkwardly.
- Don't reformat or restyle code unrelated to the change.
