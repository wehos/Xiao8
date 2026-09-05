# Plugin model slots

The host stores plugin-only model settings in the selected runtime root's
`config/plugin_models.json`. This configuration is independent of Main/Agent
model slots and does not trigger `/api/config/core_api` session reloads.

Configuration, binding management, model adapters and authenticated plugin SDK
access are implemented. The overall deadline, fallback/accounting policies and
configuration UI follow separately.

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

## Internal Chat Completions execution

`ModelGatewayService` accepts an already-resolved `ModelSlot` and an OpenAI-shaped
request. The authenticated route resolves the plugin's declared usage and
binding before calling it; supplying an arbitrary slot is not a plugin API.

- `await service.complete(slot, request)` returns a Chat Completion dictionary.
- `service.stream(slot, request)` yields SSE bytes for `stream=true`, ending with
  one `[DONE]` only when the upstream protocol finishes successfully. A consumer
  that stops early must close the iterator. Cancellation releases HTTP resources.
- Request `model` is the usage alias; only the outgoing request contains the
  configured real model. Responses and chunks use the requested alias.

Both backends support text, ordered user image parts (HTTP(S) or base64 data
URLs), ordinary function tools, complete tool-result histories, and streaming.
The service does not execute tools or add Main's role prompts, history or tools.
Standard empty fields from OpenAI SDK assistant messages can be replayed in a
subsequent tool round. Images are not downloaded, resized or converted to text.

The accepted request fields are `model`, `messages`, `stream`, `stream_options`
(`include_usage`), `tools`, `tool_choice`, `parallel_tool_calls`, `max_tokens`,
`max_completion_tokens`, `temperature`, `top_p`, `stop`, `n` (only 1), and
`response_format`. Explicit request values override slot defaults. An omitted
output budget defaults to 1024 tokens. Unknown fields, unsupported modalities,
unmatched tool results and missing declared slot capabilities are rejected.

The OpenAI adapter forwards the supported Chat Completions request and preserves
response/chunk extensions without claiming that they work across providers.
The Anthropic adapter converts to native Messages and maps text, tool calls,
finish reasons and usage back. Anthropic-specific reasoning and citations are
not exposed. Message names, non-auto image detail, strict function schemas,
non-text `response_format` and temperature above 1 have no supported mapping and
are explicitly rejected for that backend. OpenAI JSON output is passed through
only when the selected upstream model supports it.

OpenAI `base_url` is the API prefix (typically ending in `/v1`). Anthropic bases
may include or omit the final `/v1`. Credentials are placed only in the relevant
upstream authentication header; redirects are never followed. Vendor error
bodies are replaced with safe OpenAI-shaped errors.

Each attempt owns and closes its HTTP client. There are no automatic retries,
fallback, usage persistence or accounting hooks yet. The HTTP inactivity timeout
uses the slot setting; the overall deadline, bounded scheduling and accounting
are a separate planned increment. Upstream streaming usage is requested for
OpenAI, but forwarded to the caller only when `include_usage=true`. Missing usage
remains unknown. Requests and responses are bounded to 16 MiB and SSE events to
1 MiB, including unterminated lines. These are transport limits, not token-budget
estimates.

## Plugin SDK and instance access

Inside an async plugin handler, use the public context without importing host
configuration or `utils`:

```python
client = await self.ctx.models.get_client()
response = await client.chat.completions.create(
    model="analysis",  # manifest usage ID, not a slot ID or provider model name
    messages=[{"role": "user", "content": "Analyze this text"}],
)
text = response.choices[0].message.content
```

This is an official `AsyncOpenAI` client pointing to the plugin HTTP service's
`/api/models/v1` base. The sole execution route is `POST /chat/completions`.
Other OpenAI SDK endpoints are not implemented. Streaming uses the same method
with `stream=True`; close its stream context if stopping consumption early:

```python
stream = await client.chat.completions.create(
    model="analysis",
    messages=[{"role": "user", "content": "Analyze this text"}],
    stream=True,
)
async with stream:
    async for chunk in stream:
        # Consume content/tool-call deltas in the plugin's own business logic.
        pass
```

The host passes a fresh instance token through process startup arguments. It is
not a supplier key and is never stored in plugin config or environment variables.
The gateway uses it to identify the plugin, then checks its current manifest,
binding and slot capabilities. A restart replaces the prior token. Failed
startup, stop, freeze and process death invalidate access and cancel associated
requests without touching a newer instance's token. This follows the existing
local plugin trust model; it is not a filesystem sandbox for Python plugins.

Get the client inside the current handler's event loop. The SDK reuses it only
within that loop, disables automatic retries and environment HTTP proxies, and
uses a 360-second transport timeout. Host lifecycle wrappers close clients before
temporary or command loops exit. Final context closure prevents new clients.
The plugin HTTP service owns supplier requests; its existing separate event loop
is used in embedded mode. Main sessions and Agent inference loops are not used.

Authentication failures return OpenAI-shaped 401 errors. Undeclared/unbound
usages return 403, and incompatible bindings return 409. Supplier/validation
failures before the first stream event retain their HTTP error status; failures
after headers produce an SDK-readable SSE error without a success terminator.
Disconnect and revocation release upstream resources, including during stream
prefetch and response handoff. Supplier errors never echo raw error bodies.
