# Plugin model slots

The host stores plugin-only model settings in the selected runtime root's
`config/plugin_models.json`. This configuration is independent of Main/Agent
model slots and does not trigger `/api/config/core_api` session reloads.

This first increment provides configuration and binding management only.
Model execution, the plugin SDK client and configuration UI follow separately.

## Manifest declarations

```toml
[plugin.models.analysis]
label = "Content analysis"
required = true
capabilities = ["image_input"]
```

Usage IDs are stable, lowercase identifiers, up to 64 characters. Supported
capability declarations are `text`, `image_input`, `tool_calling`, and `streaming`.
Omitted capabilities default to `text`; omitted `required` defaults to true.
Declarations come from the installed manifest, not plugin runtime overrides.
Existing plugins without declarations continue to work.

## Storage and management API

The version-1 document contains `slots` keyed by host-generated stable IDs and
`bindings` keyed by plugin ID, then usage ID. Each binding points to one slot ID.
A slot contains `name`, `protocol` (`openai_chat` or `anthropic_messages`),
`base_url`, `model`, `api_key`, `capabilities`, `defaults`, `timeout_seconds`, and
an optional `fallback_slot_id`. Defaults currently accept `temperature` and
`max_output_tokens`. These fields describe future execution; saving does not
make a model request or prove provider capabilities.

All endpoints are served by the plugin HTTP app under `/api/model-config` and
use its existing management access policy.

| Method | Path | Purpose |
| --- | --- | --- |
| GET / POST | `/slots` | List / create slots |
| GET / PATCH / DELETE | `/slots/{slot_id}` | Read / edit / delete a slot |
| GET | `/plugins/{plugin_id}/bindings` | Declarations, bindings and readiness |
| PUT | `/plugins/{plugin_id}/bindings/{usage_id}` | Bind with `{"slot_id":"..."}` |
| DELETE | `/plugins/{plugin_id}/bindings/{usage_id}` | Remove a binding |

Slot responses include `id` and `bound_by` (plugin/usage references). Nonempty
keys are replaced with `__NEKO_SECRET_MASKED__`; a PATCH with that sentinel or
an omitted key preserves the stored value. An explicit empty string clears it.
Changing the endpoint or protocol of a credentialed slot requires an explicit
key update, including an empty string for an unauthenticated endpoint. URLs
cannot contain credentials, a query, or a fragment.

Bindings must reference declared usages and meet their capabilities. Renaming
a slot preserves its ID and bindings. Removing a used slot returns 409 until
bindings and fallback references are removed. Cleanup of stale bindings is
allowed after uninstall or manifest changes; disabling a plugin does not erase
its bindings. Readiness describes configuration, not network availability, and
does not change plugin startup behavior.

Writes use the existing storage-maintenance transaction and atomic JSON writer.
Malformed or unsupported stored documents are reported without overwriting
them. Whole-root migrations carry the file; legacy imports keep an existing
target file intact instead of merging credentials and endpoints. These local
credentials are not added to character cloud-save exports.
