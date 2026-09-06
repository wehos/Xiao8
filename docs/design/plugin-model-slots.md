# Plugin model slots

The host stores plugin-only model settings in the selected runtime root's
`config/plugin_models.json`. This configuration is independent of Main/Agent
model slots and does not trigger `/api/config/core_api` session reloads.

Configuration, binding management, model adapters, authenticated plugin SDK
access, execution/accounting policies and the Plugin API configuration UI form
one independent plugin-model workflow.

## User workflow

Open **Plugin API** from the API settings page or the plugin manager navigation
(`/model-api` in the manager). Create a named slot, choose its protocol, enter
the provider's API prefix, model and key, then select the model capabilities.
Save the slot before testing it. A connection test makes a short model request;
ordinary save operations only change configuration.

In a plugin's details, its manifest-declared model usages appear as binding
controls. Select a compatible saved slot for each required usage and optionally
bind additional usages. Several plugins or usages can share a slot. The slot
page lists those bindings in reverse and prevents deleting a slot still in use.
Use **Unbind** beside a listed consumer to remove a binding, including one left
behind after uninstalling a plugin or removing a usage from its manifest.
Plugins without declarations retain their existing management workflow.

Changing a slot or binding applies to subsequent calls. Active calls retain
their resolved configuration snapshot. Plugin-model settings do not close a
Main conversation, reload its roles, or change Agent inference configuration.

The same page exposes recent request statuses and token usage. Its totals cover
retained local history and distinguish reported, partial and unknown usage;
they are not a complete provider billing report. For a runnable developer
example, see [text, image and streaming calls](https://github.com/Project-N-E-K-O/N.E.K.O/blob/main/docs/examples/model_api_example/README.md).

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
`max_output_tokens`. These fields configure subsequent execution; saving does not
make a model request or prove provider capabilities.

All endpoints are served by the plugin HTTP app under `/api/model-config` and
use its existing management access policy.

| Method | Path | Purpose |
| --- | --- | --- |
| GET / POST | `/slots` | List / create slots |
| GET / PATCH / DELETE | `/slots/{slot_id}` | Read / edit / delete a slot |
| POST | `/slots/{slot_id}/test` | Test the saved slot with a short text request |
| GET | `/plugins/{plugin_id}/bindings` | Declarations, bindings and readiness |
| PUT | `/plugins/{plugin_id}/bindings/{usage_id}` | Bind with `{"slot_id":"..."}` |
| DELETE | `/plugins/{plugin_id}/bindings/{usage_id}` | Remove a binding |

Slot responses include `id` and `bound_by` (plugin/usage references). Nonempty
keys are replaced with `__NEKO_SECRET_MASKED__`; `api_key_preview` contains only
the first six and last four characters separated by `......`. Keys of ten or
fewer characters are fully masked. The UI displays this preview, and copying
returns masked text only. A PATCH with a display preview, that sentinel or
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

### Saved-slot connection tests

`POST /api/model-config/slots/{slot_id}/test` uses the same management access
dependency as the settings routes. It reads only the saved slot; request-body
fields cannot override its endpoint, model or credential. The request contains
`Reply with OK.` and `max_completion_tokens: 16` (mapped to Anthropic's
`max_tokens`). Its total timeout is the shorter of the slot timeout and 15
seconds, and fallback is disabled so another
model cannot disguise this slot's failure. This tests text connectivity, not
every selected capability or exact wording of the returned text.

Tests use the application model executor, sharing admission limits, cancellation,
safe errors and token accounting with plugin requests. Browser disconnects
cancel the probe and release upstream resources. Successful responses contain
`slot_id`, `status: "success"`, `duration_ms`, `usage_status`, and normalized
`usage` (or `null` if unavailable); provider response text is not returned.
Failures use the management API's safe `detail.code`/`detail.message` format.

Local history identifies these explicit user actions as
`plugin_id: "@host:model_probe"`, `usage_id: "connection_test"`. The reserved
identity cannot match a valid installed plugin ID and requires no plugin
registry entry or binding. Tests are included in slot usage and ordinary token
totals, while remaining distinguishable from actual plugin calls.

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

Each low-level attempt owns and closes its HTTP client without SDK retries.
The HTTP route wraps attempts in the execution policy described below. Upstream
streaming usage is requested for OpenAI, but forwarded to the caller only when
`include_usage=true`; internal observation is independent of that presentation.
Requests and responses are bounded to 16 MiB and SSE events to 1 MiB, including
unterminated lines. These are transport limits, not token-budget estimates.

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

## Deadline, concurrency and fallback

After authentication, body parsing and binding resolution, one monotonic
deadline is established from the primary slot's `timeout_seconds`. Queueing,
request preparation, provider I/O, generation, backpressure and fallback share
that budget. HTTP response sends use the same deadline, so a connected client
that stops reading cannot hold a send indefinitely. Only a final error frame
gets a bounded 100 ms flush window beyond that deadline; model work, fallback
and ordinary content sends never use it. Upstream resource cleanup remains
protected finalization work. Local accounting is queued separately and never
delays response delivery or cancellation. If
an error cannot be flushed in time, the SDK sees a closed response stream rather
than a success terminator.

The default executor admits four active requests and sixteen waiting requests.
Overflow returns `model_gateway_busy` (429); a deadline expiring in the queue
returns `gateway_timeout` (504) without starting an upstream attempt. The limiter
belongs to the plugin HTTP application's loop, not Main or Agent inference.

Streaming uses one producer task and a one-item queue. The task owns its timeout
and provider iterator across route prefetch and ASGI response tasks. It reports
completion/errors independently of queue capacity, and emits `[DONE]` after
successful upstream completion, cleanup and accounting enqueue. It does not wait
for accounting to reach disk. Consumer cancellation and service shutdown
cancel that producer and await its cleanup.

A request can try its configured fallback slot once, only for connection,
upstream timeout, rate-limit or server failures. It never retries the primary
automatically. Authentication, invalid input, redirects and malformed responses
do not trigger fallback. Both slot configurations are captured together. The
fallback must cover the primary capabilities and must pass protocol/request
validation again. No fallback occurs after the first chunk has been yielded to
the HTTP response path; unobserved primary chunks are discarded before switching.

## Local usage history

`GET /api/model-config/usage` returns recent request records and a summary. It
accepts optional `plugin_id`, `slot_id`, and `limit` (1–1000, default 100). The
summary covers the entire retained matching window, not just the displayed
page. It explicitly reports `window: "recent_retained"`; it is not an unlimited
per-plugin billing history.

`config/plugin_model_usage.json` retains at most 1000 logical requests. Each has
one server-generated request ID and up to two attempt IDs, with plugin/usage/
slot identity, configured protocol/model, timestamps, duration, execution status
and safe error codes. Attempt counts distinguish actual send attempts from
validation and queue failures. Bodies, URLs, headers, keys, instance tokens and
arbitrary provider fields are never included. Execution success is not an
acknowledgement that the plugin consumed every response byte.

Usage has three states:

- `reported`: a complete nonstreaming usage report or a completed stream.
- `partial`: the last valid cumulative snapshot from an incomplete stream.
- `unknown`: no usable counters were received; values remain absent, not zero.

Snapshots replace previous snapshots rather than being summed per chunk. The
local summary adds known counters and separately reports completeness counts.
Only `reported` usage from started upstream attempts is sent to the existing
`TokenTracker`, under generic `plugin_model` / `plugin_gateway` labels. Request,
plugin and slot identifiers remain local. Failed attempts with complete usage
are counted too. Bounded request-ID deduplication prevents repeated finalizers
from incrementing totals again.

The OpenAI SDK statistics hook bypasses only the exact local `/api/models/v1`
endpoint. This also handles forked plugin processes inheriting Agent's hook;
ordinary Main/Agent and external provider requests keep their existing tracking.
A standalone gateway starts a periodic tracker saver only if none is active and
stops only the task it owns. Usage persistence failure is logged without changing
the model result; corrupted history is preserved instead of overwritten.

Each executor owns one background accounting writer and a queue of at most 256
pending records, plus the current write. Completed model calls enqueue once;
there is no accounting retry that could double-count them. Recent usage is
eventually visible, so an immediate read may precede persistence. If the queue
fills, further records are dropped with a warning and a shutdown drop count;
model requests still complete normally.

Shutdown first closes model requests so their final records can be queued, then
allows up to two seconds in total for the accounting writer and recorder to
finish. Pending records left at that deadline are reported as unconfirmed and
no longer awaited. An interrupted response or stopped plugin does not wait for
that shutdown drain. An abrupt process exit can lose records still in memory.

The serial writer performs file writes in a daemon thread with inherited
storage-transaction context. This prevents a blocked accounting write from
holding up the event loop's default-executor shutdown. Python cannot interrupt
an OS filesystem operation already in progress: that write may finish later,
and any storage lock it owns remains held until it returns. General Main/Agent
TokenTracker periodic-save and exit behavior is unchanged by this policy.
