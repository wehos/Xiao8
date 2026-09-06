"""Copy into a development plugin directory before explicitly running an entry.

This documentation example performs no startup calls and owns no provider keys.
"""
from __future__ import annotations

from plugin.sdk.plugin import NekoPluginBase, Ok, neko_plugin, plugin_entry

_TEXT_INPUT = {
    "type": "object",
    "properties": {"text": {"type": "string", "description": "Text to summarize"}},
    "required": ["text"],
}


@neko_plugin
class ModelApiExample(NekoPluginBase):
    @plugin_entry(
        id="summarize", name="Summarize text", timeout=360,
        description="Summarize supplied text using the analysis binding.",
        input_schema=_TEXT_INPUT,
    )
    async def summarize(self, text: str, **_):
        client = await self.ctx.models.get_client()
        response = await client.chat.completions.create(
            model="analysis",  # The manifest usage, never a real provider model.
            messages=[
                {"role": "system", "content": "Summarize the supplied text briefly."},
                {"role": "user", "content": text},
            ],
            max_completion_tokens=256,
        )
        return Ok({"text": response.choices[0].message.content or ""})

    @plugin_entry(
        id="describe_image", name="Describe an image", timeout=360,
        description="Describe an HTTP(S) or base64 data image using the optional vision binding.",
        input_schema={
            "type": "object",
            "properties": {
                "image_url": {"type": "string", "description": "HTTP(S) image URL or base64 image data URL"},
            },
            "required": ["image_url"],
        },
    )
    async def describe_image(self, image_url: str, **_):
        client = await self.ctx.models.get_client()
        response = await client.chat.completions.create(
            model="vision",
            messages=[{
                "role": "user",
                "content": [
                    {"type": "text", "text": "Describe this image briefly."},
                    {"type": "image_url", "image_url": {"url": image_url}},
                ],
            }],
            max_completion_tokens=256,
        )
        return Ok({"text": response.choices[0].message.content or ""})

    @plugin_entry(
        id="stream_summary", name="Summarize with streaming", timeout=360,
        description="Consume streamed text and final usage from the analysis binding.",
        input_schema=_TEXT_INPUT,
    )
    async def stream_summary(self, text: str, **_):
        client = await self.ctx.models.get_client()
        stream = await client.chat.completions.create(
            model="analysis",
            messages=[
                {"role": "system", "content": "Summarize the supplied text briefly."},
                {"role": "user", "content": text},
            ],
            max_completion_tokens=256,
            stream=True,
            stream_options={"include_usage": True},
        )
        parts = []
        usage = None
        # Close this response stream on cancellation or an early break. The host
        # owns the reusable client; do not close it after each entry invocation.
        async with stream:
            async for chunk in stream:
                if chunk.choices and chunk.choices[0].delta.content:
                    delta = chunk.choices[0].delta.content
                    # An application can update its own UI here as deltas arrive.
                    parts.append(delta)
                if chunk.usage is not None:
                    usage = chunk.usage.model_dump()
        return Ok({"text": "".join(parts), "usage": usage})
