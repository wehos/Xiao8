# Plugin Model API example

This is a documentation example, outside the plugin scan directories. It is not
installed or started automatically. Copy its Python code and model declarations
into your own development plugin, or place this directory at
`plugin/plugins/model_api_example/` when intentionally trying the complete example.
Keep the manifest entry aligned with the installed directory name.

1. Open **Plugin API** from API settings or the plugin manager.
2. Create and save an OpenAI-compatible Chat Completions or Anthropic Messages
   slot. Select only capabilities the configured model really supports.
3. Optionally click **Test connection**. This sends a short real model request;
   saving a slot alone does not make a request.
4. In this plugin's details, bind `analysis` to a slot supporting text and
   streaming. Bind optional `vision` to a slot supporting image input when needed.
   Both usages may share the same suitable slot.
5. Start the plugin and invoke one of its entries explicitly:

| Entry | Input | Model usage |
| --- | --- | --- |
| `summarize` | `{"text":"The text to summarize"}` | `analysis` |
| `describe_image` | `{"image_url":"data:image/png;base64,..."}` | `vision` |
| `stream_summary` | `{"text":"The text to summarize"}` | `analysis` |

Replace the image placeholder with real base64 data or an HTTP(S) image URL that
the selected provider can access. The host preserves the image input; it does
not resize the image, download the URL, or replace it with explanatory text.

The plugin imports only the public `plugin.sdk.plugin` interface. All three
entries use `await self.ctx.models.get_client()` and the same OpenAI-style
`client.chat.completions.create()` method for both backends. No provider URL,
model ID, API key, `utils` import, or Anthropic client belongs in this plugin.

The `model` argument is the manifest usage ID. Slot names and real model names
are user settings, so renaming or rebinding does not require changing code.
Calling `describe_image` before binding optional `vision` returns an explicit
unbound-usage error. Optional means the plugin can operate without that feature,
not that the host silently chooses another model.

The SDK reuses its client within the current event loop. Obtain it inside the
handler and close each response stream with `async with`; the host closes the
client on lifecycle shutdown. Cancellation should propagate so the gateway can
release its upstream request. The example lets standard OpenAI SDK errors reach
the plugin entry caller instead of retrying or hiding them. Entry timeouts and
model-slot timeouts are separate: the 360-second entry limit leaves room for the
host's maximum 300-second model budget and cleanup.

`stream_summary` consumes deltas as they arrive and collects them for its entry
result; update your plugin's own UI inside that loop if incremental display is
needed. Final usage is optional. A missing report remains `null`, not a claim of
zero token consumption. The Plugin API usage view also records cancelled calls,
fallback attempts, and partial or unknown usage.

The supported model API is Chat Completions with text, images, ordinary function
tools, and streaming. Responses, files/PDF, audio, video, generated images,
Realtime, and Anthropic-specific options are outside this API's scope. See the
[full configuration and protocol contract](../../design/plugin-model-slots.md).
