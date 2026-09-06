"""Translate the gateway's Chat Completions subset to Anthropic Messages.

Only ordinary text, images and client-side function tools cross this boundary.
Provider-specific reasoning, citations and server tools are not public features.
"""

from __future__ import annotations

import base64
import binascii
import json
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from .errors import ModelGatewayError

_IMAGE_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}
_REQUEST_FIELDS = {
    "model",
    "messages",
    "stream",
    "max_tokens",
    "max_completion_tokens",
    "temperature",
    "top_p",
    "stop",
    "tools",
    "tool_choice",
    "parallel_tool_calls",
    "response_format",
    "n",
    "stream_options",
}
_STOP_REASONS = {
    "end_turn": "stop",
    "stop_sequence": "stop",
    "tool_use": "tool_calls",
    "max_tokens": "length",
    "model_context_window_exceeded": "length",
    "refusal": "content_filter",
}


def _unsupported(param: str) -> ModelGatewayError:
    return ModelGatewayError(
        "unsupported_parameter",
        "This parameter is not supported by the Anthropic backend.",
        param=param,
    )


def _bad_response() -> ModelGatewayError:
    return ModelGatewayError(
        "invalid_upstream_response",
        "The model provider returned an invalid or incomplete response.",
        status_code=502,
    )


def _image_source(image: dict[str, Any], param: str) -> dict[str, Any]:
    if image.get("detail", "auto") != "auto":
        raise _unsupported(f"{param}.detail")
    url = image["url"]
    if url.startswith("data:"):
        header, separator, data = url.partition(",")
        media_type = header[5:].removesuffix(";base64")
        if (
            not separator
            or not header.endswith(";base64")
            or media_type not in _IMAGE_TYPES
        ):
            raise _unsupported(param)
        try:
            if not base64.b64decode(data, validate=True):
                raise ValueError
        except (ValueError, binascii.Error):
            raise ModelGatewayError(
                "invalid_request", "The image data URL is invalid.", param=param
            ) from None
        return {"type": "base64", "media_type": media_type, "data": data}
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise _unsupported(param)
    return {"type": "url", "url": url}


def _content(value: Any, param: str, *, images: bool) -> list[dict[str, Any]]:
    if value is None:
        return []
    if isinstance(value, str):
        return [{"type": "text", "text": value}] if value else []
    result = []
    for index, part in enumerate(value):
        location = f"{param}[{index}]"
        if part["type"] == "text":
            if part["text"]:
                result.append({"type": "text", "text": part["text"]})
        elif part["type"] == "image_url" and images:
            result.append(
                {
                    "type": "image",
                    "source": _image_source(part["image_url"], f"{location}.image_url"),
                }
            )
        else:
            raise _unsupported(location)
    return result


def _tool_input(arguments: str, param: str) -> dict[str, Any]:
    try:
        value = json.loads(arguments)
        if not isinstance(value, dict):
            raise ValueError
        # json.loads accepts NaN/Infinity, but neither wire protocol does.
        json.dumps(value, allow_nan=False)
    except (ValueError, TypeError):
        raise ModelGatewayError(
            "invalid_request", "Tool arguments must encode a JSON object.", param=param
        ) from None
    return value


def prepare_request(request: dict[str, Any]) -> dict[str, Any]:
    """Convert a validated gateway request with its actual model already resolved."""
    for key in request.keys() - _REQUEST_FIELDS:
        raise _unsupported(key)
    if request.get("n", 1) != 1:
        raise _unsupported("n")
    if request.get("response_format", {"type": "text"}) != {"type": "text"}:
        raise _unsupported("response_format")
    if request.get("temperature", 0) > 1:
        raise _unsupported("temperature")

    body: dict[str, Any] = {
        "model": request["model"],
        "max_tokens": request.get(
            "max_completion_tokens", request.get("max_tokens", 1024)
        ),
        "messages": [],
        "stream": request.get("stream", False),
    }
    system = []
    for index, message in enumerate(request["messages"]):
        param = f"messages[{index}]"
        if message.get("name"):
            raise _unsupported(f"{param}.name")
        role = message["role"]
        if role in {"system", "developer"}:
            if body["messages"]:
                raise _unsupported(f"{param}.role")
            system.extend(
                _content(message.get("content"), f"{param}.content", images=False)
            )
            continue
        if role == "tool":
            content = [
                {
                    "type": "tool_result",
                    "tool_use_id": message["tool_call_id"],
                    "content": _content(
                        message.get("content"), f"{param}.content", images=True
                    ),
                }
            ]
            role = "user"
        else:
            content = _content(
                message.get("content"), f"{param}.content", images=role == "user"
            )
            if role == "assistant" and message.get("refusal"):
                content.append({"type": "text", "text": message["refusal"]})
            for tool_index, call in enumerate(message.get("tool_calls", [])):
                function = call["function"]
                content.append(
                    {
                        "type": "tool_use",
                        "id": call["id"],
                        "name": function["name"],
                        "input": _tool_input(
                            function["arguments"],
                            f"{param}.tool_calls[{tool_index}].function.arguments",
                        ),
                    }
                )
        if not content:
            raise ModelGatewayError(
                "invalid_request",
                "Anthropic messages must contain content.",
                param=f"{param}.content",
            )
        if body["messages"] and body["messages"][-1]["role"] == role:
            body["messages"][-1]["content"].extend(content)
        else:
            body["messages"].append({"role": role, "content": content})
    if not body["messages"]:
        raise ModelGatewayError(
            "invalid_request",
            "At least one conversation message is required.",
            param="messages",
        )
    if system:
        body["system"] = system
    for key in ("temperature", "top_p"):
        if key in request:
            body[key] = request[key]
    if "stop" in request:
        stop = request["stop"]
        body["stop_sequences"] = [stop] if isinstance(stop, str) else stop
    if request.get("tools"):
        body["tools"] = []
        for index, tool in enumerate(request["tools"]):
            function = tool["function"]
            if function.get("strict"):
                raise _unsupported(f"tools[{index}].function.strict")
            converted = {
                "name": function["name"],
                "input_schema": function.get(
                    "parameters", {"type": "object", "properties": {}}
                ),
            }
            if "description" in function:
                converted["description"] = function["description"]
            body["tools"].append(converted)
        choice = request.get("tool_choice", "auto")
        if isinstance(choice, dict):
            body["tool_choice"] = {"type": "tool", "name": choice["function"]["name"]}
        else:
            body["tool_choice"] = {
                "type": {"auto": "auto", "required": "any", "none": "none"}[choice]
            }
        if "parallel_tool_calls" in request and body["tool_choice"]["type"] != "none":
            body["tool_choice"]["disable_parallel_tool_use"] = not request[
                "parallel_tool_calls"
            ]
    return body


def _usage(value: dict[str, Any]) -> dict[str, Any] | None:
    if "input_tokens" not in value or "output_tokens" not in value:
        return None
    keys = (
        "input_tokens",
        "output_tokens",
        "cache_read_input_tokens",
        "cache_creation_input_tokens",
    )
    for key in keys:
        count = value.get(key, 0)
        if type(count) is not int or count < 0:
            raise _bad_response()
    cached = value.get("cache_read_input_tokens", 0)
    prompt = (
        value["input_tokens"] + cached + value.get("cache_creation_input_tokens", 0)
    )
    return {
        "prompt_tokens": prompt,
        "completion_tokens": value["output_tokens"],
        "total_tokens": prompt + value["output_tokens"],
        "prompt_tokens_details": {"cached_tokens": cached},
    }


def _finish_reason(value: Any) -> str:
    if not isinstance(value, str) or value not in _STOP_REASONS:
        raise _bad_response()
    return _STOP_REASONS[value]


def _tool_output(block: dict[str, Any]) -> dict[str, Any]:
    if (
        not isinstance(block.get("id"), str)
        or not block["id"]
        or not isinstance(block.get("name"), str)
        or not block["name"]
    ):
        raise _bad_response()
    if not isinstance(block.get("input"), dict):
        raise _bad_response()
    try:
        arguments = json.dumps(
            block["input"], ensure_ascii=False, separators=(",", ":"), allow_nan=False
        )
    except (ValueError, TypeError):
        raise _bad_response() from None
    return {
        "id": block["id"],
        "type": "function",
        "function": {"name": block["name"], "arguments": arguments},
    }


def convert_response(body: dict[str, Any], *, model_alias: str) -> dict[str, Any]:
    """Return only the supported OpenAI response fields, without provider payloads."""
    if (
        not isinstance(body, dict)
        or body.get("type") != "message"
        or body.get("role") != "assistant"
        or not isinstance(body.get("id"), str)
        or not body["id"]
    ):
        raise _bad_response()
    if not isinstance(body.get("content"), list) or not isinstance(
        body.get("usage", {}), dict
    ):
        raise _bad_response()
    text, calls = [], []
    for block in body["content"]:
        if not isinstance(block, dict) or not isinstance(block.get("type"), str):
            raise _bad_response()
        if block.get("type") == "text" and isinstance(block.get("text"), str):
            text.append(block["text"])
        elif block.get("type") == "tool_use":
            calls.append(_tool_output(block))
        elif block.get("type") not in {"thinking", "redacted_thinking"}:
            raise _bad_response()
    message: dict[str, Any] = {
        "role": "assistant",
        "content": "".join(text) if text else None,
    }
    if calls:
        message["tool_calls"] = calls
    return {
        "id": body["id"],
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model_alias,
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": _finish_reason(body.get("stop_reason")),
                "logprobs": None,
            }
        ],
        "usage": _usage(body.get("usage", {})),
    }


@dataclass
class _Block:
    kind: str
    tool_index: int = -1
    initial_arguments: str = "{}"
    has_arguments: bool = False
    closed: bool = False


class AnthropicStreamConverter:
    """Stateful Messages SSE conversion; transport owns I/O and the [DONE] marker."""

    def __init__(self, model_alias: str, include_usage: bool = True) -> None:
        self.model_alias = model_alias
        self.include_usage = include_usage
        self.done = False
        self._id: str | None = None
        self._created = int(time.time())
        self._blocks: dict[int, _Block] = {}
        self._tool_count = 0
        self._raw_usage: dict[str, Any] = {}
        self._finish_reason: str | None = None

    @property
    def usage(self) -> dict[str, Any] | None:
        return _usage(self._raw_usage)

    def _chunk(
        self, delta: dict[str, Any], finish_reason: str | None = None
    ) -> dict[str, Any]:
        chunk = {
            "id": self._id,
            "object": "chat.completion.chunk",
            "created": self._created,
            "model": self.model_alias,
            "choices": [
                {
                    "index": 0,
                    "delta": delta,
                    "finish_reason": finish_reason,
                    "logprobs": None,
                }
            ],
        }
        if self.include_usage:
            chunk["usage"] = None
        return chunk

    def feed(self, event: dict[str, Any]) -> list[dict[str, Any]]:
        if not isinstance(event, dict) or self.done:
            raise _bad_response()
        kind = event.get("type")
        if not isinstance(kind, str):
            raise _bad_response()
        if kind == "error":
            raise ModelGatewayError(
                "upstream_error",
                "The model provider reported a streaming error.",
                status_code=502,
            )
        if kind == "ping":
            return []
        if kind == "message_start":
            message = event.get("message", {})
            if (
                self._id is not None
                or not isinstance(message, dict)
                or not isinstance(message.get("id"), str)
                or not message["id"]
            ):
                raise _bad_response()
            if message.get("role") != "assistant" or message.get("content", []) != []:
                raise _bad_response()
            self._id = message["id"]
            self._update_usage(message.get("usage", {}))
            return [self._chunk({"role": "assistant", "content": ""})]
        if self._id is None:
            raise _bad_response()
        if kind == "message_delta":
            delta = event.get("delta")
            if not isinstance(delta, dict) or any(
                not block.closed for block in self._blocks.values()
            ):
                raise _bad_response()
            self._update_usage(event.get("usage", {}))
            if delta.get("stop_reason") is not None:
                reason = _finish_reason(delta["stop_reason"])
                if self._finish_reason is not None and reason != self._finish_reason:
                    raise _bad_response()
                self._finish_reason = reason
            return []
        if kind == "message_stop":
            if self._finish_reason is None or any(
                not block.closed for block in self._blocks.values()
            ):
                raise _bad_response()
            chunks = [self._chunk({}, self._finish_reason)]
            if self.include_usage:
                usage_chunk = self._chunk({})
                usage_chunk["choices"] = []
                usage_chunk["usage"] = self.usage
                chunks.append(usage_chunk)
            self.done = True
            return chunks
        if kind not in {
            "content_block_start",
            "content_block_delta",
            "content_block_stop",
        }:
            return []  # Anthropic permits additive event types.
        index = event.get("index")
        if type(index) is not int or index < 0 or self._finish_reason is not None:
            raise _bad_response()
        if kind == "content_block_start":
            if index in self._blocks:
                raise _bad_response()
            value = event.get("content_block")
            if not isinstance(value, dict) or not isinstance(value.get("type"), str):
                raise _bad_response()
            block = _Block(kind=value.get("type", ""))
            self._blocks[index] = block
            if block.kind == "text":
                if not isinstance(value.get("text"), str):
                    raise _bad_response()
                return (
                    [self._chunk({"content": value["text"]})] if value["text"] else []
                )
            if block.kind == "tool_use":
                tool = _tool_output(value)
                block.tool_index = self._tool_count
                self._tool_count += 1
                block.initial_arguments = tool["function"]["arguments"]
                tool["index"] = block.tool_index
                tool["function"]["arguments"] = ""
                return [self._chunk({"tool_calls": [tool]})]
            if block.kind not in {"thinking", "redacted_thinking"}:
                raise _bad_response()
            return []
        block = self._blocks.get(index)
        if block is None or block.closed:
            raise _bad_response()
        if kind == "content_block_stop":
            block.closed = True
            if block.kind == "tool_use" and not block.has_arguments:
                return [
                    self._chunk(
                        {
                            "tool_calls": [
                                {
                                    "index": block.tool_index,
                                    "function": {"arguments": block.initial_arguments},
                                }
                            ]
                        }
                    )
                ]
            return []
        delta = event.get("delta")
        if not isinstance(delta, dict) or not isinstance(delta.get("type"), str):
            raise _bad_response()
        if block.kind == "text":
            if delta.get("type") == "citations_delta":
                return []
            if delta.get("type") != "text_delta" or not isinstance(
                delta.get("text"), str
            ):
                raise _bad_response()
            return [self._chunk({"content": delta["text"]})]
        if block.kind == "tool_use":
            if delta.get("type") != "input_json_delta" or not isinstance(
                delta.get("partial_json"), str
            ):
                raise _bad_response()
            if not delta["partial_json"]:
                return []
            if block.initial_arguments != "{}":
                raise _bad_response()
            block.has_arguments = True
            return [
                self._chunk(
                    {
                        "tool_calls": [
                            {
                                "index": block.tool_index,
                                "function": {"arguments": delta["partial_json"]},
                            }
                        ]
                    }
                )
            ]
        return []  # Thinking/signature content is not part of the public contract.

    def _update_usage(self, value: Any) -> None:
        if not isinstance(value, dict):
            raise _bad_response()
        self._raw_usage.update(value)
        _usage(self._raw_usage)

    def finish(self) -> None:
        """Reject EOF without a complete message, never synthesize a successful end."""
        if not self.done:
            raise _bad_response()
