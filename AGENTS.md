Run these commands in order after modifying Python code (skip for documentation-only `.md` edits):

```bash
uv run ruff format .
uv run ruff check .
uv run pytest -v
```

- Keep all library code in `sans_mcp.py` using only the Python standard library. Do not add external runtime dependencies.
- Keep the public API surface minimal via `__all__` and prefix all internal functions and classes with `_`. `pytest` verifies public exports and signatures against `etc/api_surface.txt` (run `uv run pytest -v --update-api` for intentional API changes).
- Do not preserve legacy APIs or shims solely for backward compatibility.
- Keep the API small and opinionated. Provide only one way to achieve a given task rather than offering multiple alternatives.
- Write clean, modern Python using standard library features rather than verbose workarounds.
- Do not use ASCII banner dividers (`===`, `---`), box frames, or redundant section labels. Use two blank lines between top-level definitions.
- In documentation, state technical facts directly using active voice and simple sentences. Avoid em-dash clauses (`—`) and promotional or marketing language.
