# N.E.K.O Plugin Package Format

## Overview

`neko_plugin_cli` defines two package types:

- `.neko-plugin`: single plugin package
- `.neko-bundle`: multi-plugin bundle package

The two package types share the same internal directory layout. Their semantic difference is determined by:

- the file extension
- `package_type` in `manifest.toml`

Both package types are standard ZIP archives and must remain compatible with normal archive tools.

## Design Goals

- Human-readable package metadata
- Simple implementation in Python
- Compatible with normal unzip tools
- Unified reader/writer for plugin package and bundle package
- Optional trust/signature support without blocking basic use

## CLI Entry

`neko_plugin_cli` now uses a single CLI entry:

```bash
uv run neko-plugin <command> ...
```

Current commands:

- `init`
- `setup-repo`
- `check` (use `check -r` / `check --release` for the pre-release readiness check, `check --release --market-release` for the Market-publication variant)
- `sync` (deps): reinstall all declared dependencies into `vendor/` from `pyproject.toml`
- `build`
- `install` (compatibility command; runtime installation is handled by Plugin Center)
- `analyze`
- `publish`

Examples:

```bash
uv run neko-plugin check qq_auto_reply
uv run neko-plugin check -r qq_auto_reply
uv run neko-plugin check --release --market-release qq_auto_reply
uv run neko-plugin sync qq_auto_reply --clean
uv run neko-plugin build qq_auto_reply
uv run neko-plugin analyze qq_auto_reply mijia
```

To install a built package, open the N.E.K.O Plugin Center and use **Import**.
The compatibility `neko-plugin install` command intentionally refuses to write
plugin runtime directories so command-line tools cannot bypass the same
confirmation, rollback, locking, and source-tracking workflow used by Core.

To add or safely upgrade the standard Market GitHub Actions files in an
existing plugin repository, run from the N.E.K.O checkout:

```bash
uv run neko-plugin setup-repo qq_auto_reply \
  --upgrade-github-actions
```

Add `--dry-run` to preview the plan. The migration only manages `ruff.toml`,
`.github/workflows/verify.yml`, and `.github/workflows/release.yml`. If any of
those files contains unrecognized custom content, no files are changed.

## Backend API Response Surface

The plugin manager backend exposes the same package workflow with explicit response models:

- `POST /plugin-cli/build` returns `built`, `built_count`, `failed`, `failed_count`, and `ok`.
- Each build result reports `package_path`, `package_type`, `plugin_ids`, `package_size_bytes`, `payload_hash`, and counts.
- `staged_files` and `profile_files` are filesystem paths from the temporary staging directory. They are only populated when `keep_staging = true`; otherwise their counts are `0`.
- `POST /plugin-cli/install-plan` classifies the request as `install`, `upgrade`, `reinstall`, `downgrade`, `override_builtin`, or `blocked`, and returns identity, version, reason, and confirmation-token fields.
- `POST /plugin-cli/install` returns `installed_plugins` and `installed_plugin_count`.
- `upgrade`, `reinstall`, and `downgrade` require the current plan token plus explicit confirmation; the server rebuilds the plan before changing files.
- `POST /plugin-cli/upload-and-install` returns `{ upload, install }`, where `install` uses the same shape as `/plugin-cli/install`.

## Archive Layout

```text
package.neko-plugin / package.neko-bundle
├── manifest.toml
├── metadata.toml
├── sign.toml              # optional
└── payload/
    ├── dependencies.toml  # auto-generated, do not edit
    ├── plugins/
    │   ├── <plugin_id>/
    │   │   ├── plugin.toml
    │   │   ├── pyproject.toml
    │   │   ├── vendor/
    │   │   └── ...
    │   └── ...
    └── profiles/
        ├── default.toml
        └── ...
```

## Top-Level Files

### `manifest.toml`

Primary package descriptor. This file is required.

Responsibilities:

- identify package type
- describe package identity and version
- provide lightweight package-level display metadata
- declare included plugins for bundle packages when needed

This file should stay small and stable.

### `metadata.toml`

Supplementary package metadata. This file is recommended.

Responsibilities:

- payload hash
- source information

This file must not carry core loading rules that are required for basic extraction.

### `sign.toml`

Optional trust/signature metadata.

Responsibilities:

- declare whether the package is signed
- identify signer and algorithm
- carry signature material for manifest or payload hashes

Packages without `sign.toml` are treated as unsigned, not invalid.

## Payload Layout

### `payload/plugins/`

Contains packaged plugin directories.

Each plugin directory should be restorable into a runtime plugin directory. In other words, it should contain the plugin runtime files, not only configuration fragments.

Each plugin directory must contain:

- `plugin.toml`

Recommended:

- `pyproject.toml`
- `vendor/` when `pyproject.toml [project].dependencies` declares runtime Python packages
- plugin source files
- static assets
- runtime resources that belong to the distributed plugin

Python dependency rules:

- `[plugin].dependencies` in `plugin.toml` declares dependencies on other N.E.K.O plugins only.
- Python runtime dependencies must be declared in `pyproject.toml [project].dependencies`.
- `requirements.txt` is not supported for plugin packages.
- Python runtime dependencies must not be installed into the shared N.E.K.O interpreter. Packages that need third-party libraries must vendor them under their own `vendor/` directory or use a future managed isolated dependency store.

Must not include:

- `__pycache__/`
- `.pytest_cache/`
- `.mypy_cache/`
- `.venv/`
- `dist/`
- `build/`
- other temporary or development-only artifacts

### `payload/profiles/`

Contains extracted package profiles.

Profiles are the portable configuration layer for distribution. This replaces a separate `configs/` directory in the package format.

Profiles may define:

- enabled plugins
- per-plugin runtime flags
- per-plugin config values

Current lightweight rule for single-plugin builds:

- `[plugin_runtime]` contributes runtime flags such as `auto_start`
- the top-level table whose name equals the plugin id is migrated into the default profile as plugin config
- other metadata tables stay in `plugin.toml`

### `payload/dependencies.toml` (internal, auto-generated)

This file is automatically generated by the build tool and included in the payload hash. Plugin developers never create or edit this file — it is produced entirely from each plugin's `plugin.toml` and `pyproject.toml` during `neko-plugin build`.

It is consumed only during package install validation to verify that vendored dependencies satisfy declared requirements without needing to re-parse each plugin's source files from the archive.

Contents (for reference only):

- Python runtime requirements (from `pyproject.toml [project].dependencies`)
- host-provided Python requirements (e.g. `N.E.K.O`)
- simple plugin dependencies (from `[plugin].dependencies`)
- advanced plugin dependency tables (from `[[plugin.dependency]]`)
- whether `vendor/` was present at build time

## Package Type Rules

### `.neko-plugin`

Rules:

- file extension should be `.neko-plugin`
- `manifest.toml` must declare `package_type = "plugin"`
- `payload/plugins/` must contain exactly one plugin directory

### `.neko-bundle`

Rules:

- file extension should be `.neko-bundle`
- `manifest.toml` must declare `package_type = "bundle"`
- `payload/plugins/` must contain one or more plugin directories
- bundle plugin list should be declared in `manifest.toml`

## `manifest.toml` Schema

### Common Fields

```toml
schema_version = "1.0"
package_type = "plugin" # or "bundle"

id = "qq_auto_reply"
package_name = "QQ 自动回复"
version = "0.2.0"
package_description = "读取 QQ 消息并根据权限自动回复。"
```

### Single Plugin Package Example

```toml
schema_version = "1.0"
package_type = "plugin"

id = "qq_auto_reply"
package_name = "QQ 自动回复"
version = "0.2.0"
package_description = "读取 QQ 消息并根据权限自动回复。"
```

### Bundle Package Example

```toml
schema_version = "1.0"
package_type = "bundle"

id = "streaming_suite"
package_name = "Streaming Suite"
version = "1.0.0"
package_description = "适合直播场景的插件整合包。"

[[plugins]]
id = "qq_auto_reply"
path = "payload/plugins/qq_auto_reply"

[[plugins]]
id = "mijia"
path = "payload/plugins/mijia"
```

## `metadata.toml` Schema

Suggested fields:

```toml
[payload]
hash_algorithm = "sha256"
hash = "..."

[source]
kind = "local"
path = "plugin/plugins/qq_auto_reply"
```

Notes:

- `payload.hash` should be computed over normalized `payload/` contents, not over the entire zip file
- metadata is allowed to evolve more freely than manifest
- `package_name` and `package_description` are package summary fields, not a replacement for the plugin's own `plugin.toml`

## `sign.toml` Schema

Suggested fields:

```toml
signed = true
algorithm = "ed25519"
signer = "neko-official"
public_key_id = "official-key-1"

[target]
payload_hash = "sha256:..."

[signature]
value = "base64:..."
```

## Validation Rules

Minimal validation:

1. archive must contain `manifest.toml`
2. archive must contain `payload/plugins/`
3. each packaged plugin directory must contain `plugin.toml`
4. `package_type = "plugin"` requires exactly one plugin directory
5. `package_type = "bundle"` requires at least one plugin directory

Recommended validation:

1. `metadata.toml` exists
2. bundle `[[plugins]]` entries match packaged plugin directory names
3. `metadata.payload.hash` matches computed payload hash
4. each packaged plugin directory contains `plugin.toml`

Optional trust validation:

1. `sign.toml` exists
2. `target.payload_hash` matches `metadata.toml`
3. signature verifies against the configured public key

## Packaging Pipeline Guidance

Recommended pipeline for a single plugin:

1. Read plugin source directory
2. Parse `plugin.toml`
3. Optionally parse `pyproject.toml`
4. Extract plugin profiles into `payload/profiles/`
5. Copy runtime plugin files into `payload/plugins/<plugin_id>/`
6. Generate `manifest.toml`
7. Generate `metadata.toml`
8. Optionally generate `sign.toml`
9. Export ZIP archive with `.neko-plugin` extension

Current implementation notes:

- single-plugin builds package one supported runtime type (`plugin` or `adapter`) as `package_type = "plugin"`
- install verifies `metadata.toml` payload hash when metadata exists
- executable plugin directories use their declared identity and fail on an occupied or mismatched destination; they are never installed as suffixed copies
- the lower-level archive/profile compatibility API still accepts `rename`, but the Plugin Manager backend accepts only `fail` and replacement actions are selected through `/plugin-cli/install-plan`

Recommended pipeline for a bundle:

1. Collect multiple plugin sources or `.neko-plugin` inputs
2. Build unified `payload/plugins/`
3. Build shared `payload/profiles/`
4. Generate bundle `manifest.toml`
5. Generate bundle `metadata.toml`
6. Optionally generate `sign.toml`
7. Export ZIP archive with `.neko-bundle` extension

## Status

This document describes the current `neko_plugin_cli` package and backend contract.

Current intent:

- archive layout and identity rules are compatibility-sensitive
- backend replacement behavior is canonicalized by the install-plan and transaction implementations
