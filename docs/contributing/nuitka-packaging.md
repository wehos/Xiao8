# Nuitka Packaging

The tracked packaging contract is the desktop workflow plus its preparation and verification scripts. There is no tracked `build_nuitka.bat` in the current repository, so do not cite one as an authoritative second entrypoint.

## Python package names

Directories containing importable `.py` files use underscore names and contain `__init__.py`. Hyphenated import packages fail normal Python naming and interact badly with data inclusion. `tests/unit/test_no_hyphen_python_packages.py` enforces this source rule.

Do not use `--include-data-dir` to ship importable Python. Nuitka filters code-like suffixes from data directories. Compile packages normally; use raw-data inclusion only for an intentionally interpreted/sandboxed source payload with an explicit runtime contract.

## Built-in plugin staging

Current desktop workflows run:

1. `scripts/prepare_nuitka_plugins.py prepare`
2. compile generated `build_nuitka_launcher.py`
3. `scripts/prepare_nuitka_plugins.py install` into the built distribution
4. `scripts/check_nuitka_dist.py <dist> --plugin-stage build/nuitka-plugins`

The staging script applies each plugin's `[tool.neko.build]` rules and generates selective exclusions. Do not restore blanket `--include-data-dir=plugin/plugins=plugin/plugins` or blanket `--nofollow-import-to=plugin.plugins`; both bypass the staged contract in different ways.

## Assets and dynamic imports

A new runtime asset or dynamic import may require coordinated updates to:

- Nuitka include options in both cross-platform and Linux-only workflows;
- `scripts/prepare_nuitka_plugins.py` for plugin payloads;
- `scripts/check_nuitka_dist.py` required-asset verification;
- targeted import/package tests.

Embedding and tiktoken assets are prepared and verified separately by the workflow.

## Diagnose safely

The packaged launcher starts multiple services. Killing only the parent can leave processes holding `dist/Xiao8`. Prefer static checks before launching:

```bash
uv run python scripts/check_nuitka_dist.py dist/Xiao8 --plugin-stage build/nuitka-plugins
uv run pytest tests/unit/test_no_hyphen_python_packages.py -q
```

If a packaged run is necessary, record the exact artifact/revision and shut down all child services before rebuilding. Never fix a stale locked distribution with an unreviewed recursive delete.
