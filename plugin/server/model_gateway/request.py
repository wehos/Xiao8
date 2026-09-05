"""Validate the public Chat Completions subset without provider side effects."""
from __future__ import annotations

import base64
import binascii
import copy
import json
import math
import re
from typing import NoReturn
from urllib.parse import urlsplit

from plugin.server.domain.model_config import ModelSlot

from .errors import ModelGatewayError

_NAME = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")
_USAGE_ID = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_MAX_OUTPUT_TOKENS = 1_000_000
_ASSISTANT_HISTORY_FIELDS = {"tool_calls", "refusal", "annotations", "audio", "function_call"}
_REQUEST_FIELDS = {
    "model", "messages", "stream", "stream_options", "tools", "tool_choice",
    "parallel_tool_calls", "max_tokens", "max_completion_tokens", "temperature",
    "top_p", "stop", "n", "response_format",
}


def _invalid(param: str, message: str, *, code: str = "invalid_request") -> NoReturn:
    # Never interpolate user-controlled values (including unknown field names).
    raise ModelGatewayError(code, message, status_code=400, param=param)


def _object(value: object, param: str, allowed: set[str]) -> dict:
    if not isinstance(value, dict):
        _invalid(param, "Expected an object.")
    if value.keys() - allowed:
        _invalid(param, "Unsupported fields in object.", code="unsupported_parameter")
    return value


def _string(value: object, param: str, *, nonempty: bool = False) -> str:
    if not isinstance(value, str) or (nonempty and not value.strip()):
        _invalid(param, "Expected a nonempty string." if nonempty else "Expected a string.")
    return value


def _name(value: object, param: str) -> str:
    if not isinstance(value, str) or _NAME.fullmatch(value) is None:
        _invalid(param, "Expected a function or message name of 1 to 64 letters, digits, underscores or hyphens.")
    return value


def _boolean(value: object, param: str) -> None:
    if type(value) is not bool:
        _invalid(param, "Expected a boolean.")


def _number(value: object, param: str, minimum: float, maximum: float) -> None:
    if type(value) not in (int, float) or not minimum <= value <= maximum or not math.isfinite(value):
        _invalid(param, "Numeric parameter is outside its supported range.")


def _json_object(value: object, param: str) -> dict:
    if not isinstance(value, dict):
        _invalid(param, "Expected a JSON object.")
    try:
        json.dumps(value, allow_nan=False)
    except (ValueError, TypeError, OverflowError, RecursionError):
        _invalid(param, "Expected a serializable JSON object with finite numbers.")
    return value


def _image(value: object, param: str) -> None:
    image = _object(value, param, {"url", "detail"})
    url = _string(image.get("url"), param + ".url", nonempty=True)
    if image.get("detail", "auto") not in ("auto", "low", "high"):
        _invalid(param + ".detail", "Unsupported image detail.")
    if url.startswith("data:"):
        header, separator, encoded = url.partition(",")
        if not separator or header not in {
            "data:image/jpeg;base64", "data:image/png;base64",
            "data:image/webp;base64", "data:image/gif;base64",
        }:
            _invalid(param + ".url", "Expected a supported base64 image data URL.")
        try:
            if not base64.b64decode(encoded, validate=True):
                _invalid(param + ".url", "Image data must not be empty.")
        except (ValueError, binascii.Error):
            _invalid(param + ".url", "Image data must use valid base64 encoding.")
        return
    try:
        parsed = urlsplit(url)
        _ = parsed.port
        valid = (
            parsed.scheme in {"http", "https"} and parsed.hostname
            and parsed.username is None and parsed.password is None
            and not any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in url)
        )
    except ValueError:
        valid = False
    if not valid:
        _invalid(param + ".url", "Expected an HTTP(S) image URL or supported image data URL.")


def _content(value: object, param: str, *, images: bool = False) -> bool:
    """Validate ordered content; return whether it includes an image."""
    if isinstance(value, str):
        return False
    if not isinstance(value, list) or not value:
        _invalid(param, "Expected text or a nonempty content block list.")
    has_image = False
    for index, item in enumerate(value):
        part_path = f"{param}[{index}]"
        part = _object(item, part_path, {"type", "text", "image_url"})
        if part.get("type") == "text":
            _object(part, part_path, {"type", "text"})
            _string(part.get("text"), part_path + ".text")
        elif part.get("type") == "image_url" and images:
            _object(part, part_path, {"type", "image_url"})
            _image(part.get("image_url"), part_path + ".image_url")
            has_image = True
        else:
            _invalid(part_path, "Content type is not supported for this message role.", code="unsupported_modality")
    return has_image


def _tool_calls(value: object, param: str) -> set[str]:
    if not isinstance(value, list) or not value:
        _invalid(param, "Expected a nonempty tool call list.")
    ids: set[str] = set()
    for index, item in enumerate(value):
        path = f"{param}[{index}]"
        call = _object(item, path, {"id", "type", "function"})
        call_id = _string(call.get("id"), path + ".id", nonempty=True)
        if len(call_id) > 256 or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in call_id):
            _invalid(path + ".id", "Invalid tool call identifier.")
        if call_id in ids:
            _invalid(path + ".id", "Tool call identifiers must be unique within a message.")
        ids.add(call_id)
        if call.get("type") != "function":
            _invalid(path + ".type", "Only function tool calls are supported.")
        function = _object(call.get("function"), path + ".function", {"name", "arguments"})
        _name(function.get("name"), path + ".function.name")
        arguments_path = path + ".function.arguments"
        arguments = _string(function.get("arguments"), arguments_path)
        try:
            parsed = json.loads(arguments)
        except (ValueError, RecursionError):
            _invalid(arguments_path, "Tool arguments must be a JSON object encoded as a string.")
        _json_object(parsed, arguments_path)
    return ids


def _messages(value: object) -> set[str]:
    if not isinstance(value, list) or not value:
        _invalid("messages", "Expected a nonempty message list.")
    capabilities: set[str] = set()
    pending_calls: set[str] = set()
    seen_conversation = False
    for index, item in enumerate(value):
        path = f"messages[{index}]"
        message = _object(item, path, {"role", "content", "name", "tool_call_id"} | _ASSISTANT_HISTORY_FIELDS)
        role = message.get("role")
        if role not in ("system", "developer", "user", "assistant", "tool"):
            _invalid(path + ".role", "Unsupported message role.")
        allowed = {"role", "content", "name"}
        if role == "assistant":
            allowed.update(_ASSISTANT_HISTORY_FIELDS)
        elif role == "tool":
            allowed = {"role", "content", "tool_call_id"}
        _object(message, path, allowed)
        if "name" in message:
            _name(message["name"], path + ".name")
        if role != "tool" and pending_calls:
            _invalid(path, "All preceding tool calls require results before the next message.")
        if role in ("system", "developer"):
            if seen_conversation:
                _invalid(path + ".role", "System and developer messages must precede conversation messages.")
            _content(message.get("content"), path + ".content")
            continue
        seen_conversation = True
        if role == "tool":
            capabilities.add("tool_calling")
            call_id = _string(message.get("tool_call_id"), path + ".tool_call_id", nonempty=True)
            if call_id not in pending_calls:
                _invalid(path + ".tool_call_id", "Tool result does not match an outstanding tool call.")
            pending_calls.remove(call_id)
            _content(message.get("content"), path + ".content")
        elif role == "assistant":
            # SDK model_dump() includes standard optional fields even when the
            # provider did not return them. Accept only their empty forms here.
            for field in ("audio", "function_call"):
                if message.get(field) is not None:
                    _invalid(path + "." + field, "This assistant content is not supported.", code="unsupported_parameter")
            if message.get("annotations") is not None and message["annotations"] != []:
                _invalid(path + ".annotations", "Assistant annotations are not supported.", code="unsupported_parameter")
            if message.get("refusal") is not None:
                _string(message["refusal"], path + ".refusal")
            calls = message.get("tool_calls")
            if calls is not None and calls != []:
                pending_calls = _tool_calls(calls, path + ".tool_calls")
                capabilities.add("tool_calling")
            content = message.get("content")
            if content is None:
                if not pending_calls and not message.get("refusal"):
                    _invalid(path + ".content", "Assistant content may be null only with tool calls or refusal text.")
            else:
                _content(content, path + ".content")
        elif _content(message.get("content"), path + ".content", images=True):
            capabilities.add("image_input")
    if pending_calls:
        _invalid("messages", "Tool calls require matching results before requesting a completion.")
    return capabilities


def _tools(request: dict) -> bool:
    names: set[str] = set()
    if "tools" in request:
        if not isinstance(request["tools"], list):
            _invalid("tools", "Expected a function tool list.")
        for index, item in enumerate(request["tools"]):
            path = f"tools[{index}]"
            tool = _object(item, path, {"type", "function"})
            if tool.get("type") != "function":
                _invalid(path + ".type", "Only function tools are supported.")
            function = _object(tool.get("function"), path + ".function", {"name", "description", "parameters", "strict"})
            name = _name(function.get("name"), path + ".function.name")
            if name in names:
                _invalid(path + ".function.name", "Function names must be unique.")
            names.add(name)
            if "description" in function:
                _string(function["description"], path + ".function.description")
            if "parameters" in function:
                _json_object(function["parameters"], path + ".function.parameters")
            if "strict" in function:
                _boolean(function["strict"], path + ".function.strict")
    if "tool_choice" in request:
        choice = request["tool_choice"]
        if isinstance(choice, str):
            if choice not in ("auto", "none", "required"):
                _invalid("tool_choice", "Unsupported tool choice.")
            if choice != "none" and not names:
                _invalid("tool_choice", "Tool choice requires tool definitions.")
        else:
            choice = _object(choice, "tool_choice", {"type", "function"})
            if choice.get("type") != "function":
                _invalid("tool_choice.type", "Only function tool choice is supported.")
            function = _object(choice.get("function"), "tool_choice.function", {"name"})
            name = _name(function.get("name"), "tool_choice.function.name")
            if name not in names:
                _invalid("tool_choice.function.name", "Chosen function must be declared in tools.")
    if "parallel_tool_calls" in request:
        _boolean(request["parallel_tool_calls"], "parallel_tool_calls")
        if not names:
            _invalid("parallel_tool_calls", "Parallel tool calls require tool definitions.")
    return bool(names)


def _response_format(value: object) -> None:
    response_format = _object(value, "response_format", {"type", "json_schema"})
    kind = response_format.get("type")
    if kind in ("text", "json_object"):
        _object(response_format, "response_format", {"type"})
    elif kind == "json_schema":
        schema = _object(response_format.get("json_schema"), "response_format.json_schema", {"name", "description", "schema", "strict"})
        _name(schema.get("name"), "response_format.json_schema.name")
        _json_object(schema.get("schema"), "response_format.json_schema.schema")
        if "description" in schema:
            _string(schema["description"], "response_format.json_schema.description")
        if "strict" in schema:
            _boolean(schema["strict"], "response_format.json_schema.strict")
    else:
        _invalid("response_format.type", "Unsupported response format.")


def prepare_chat_request(slot: ModelSlot, body: object) -> dict:
    """Return a validated copy with slot defaults and the actual upstream model.

    The caller retains the original ``body['model']`` as the plugin's usage alias.
    No user message, content order, tool association or explicit budget is repaired.
    """
    source = _object(body, "body", _REQUEST_FIELDS)
    usage_id = _string(source.get("model"), "model", nonempty=True)
    if _USAGE_ID.fullmatch(usage_id) is None:
        _invalid("model", "Model must be a declared plugin usage identifier.")
    needed = _messages(source.get("messages"))
    if _tools(source):
        needed.add("tool_calling")
    stream = source.get("stream", False)
    _boolean(stream, "stream")
    if stream:
        needed.add("streaming")
    if "stream_options" in source:
        options = _object(source["stream_options"], "stream_options", {"include_usage"})
        if not stream:
            _invalid("stream_options", "Stream options require streaming.")
        if "include_usage" in options:
            _boolean(options["include_usage"], "stream_options.include_usage")
    if needed - set(slot.capabilities):
        _invalid("model", "Bound model slot does not support the requested capabilities.", code="unsupported_capability")
    if "max_tokens" in source and "max_completion_tokens" in source:
        _invalid("max_completion_tokens", "Specify only one output token budget.")
    for field in ("max_tokens", "max_completion_tokens"):
        if field in source:
            if type(source[field]) is not int or not 1 <= source[field] <= _MAX_OUTPUT_TOKENS:
                _invalid(field, "Output token budget must be an integer from 1 to 1000000.")
    for field, maximum in (("temperature", 2), ("top_p", 1)):
        if field in source:
            _number(source[field], field, 0, maximum)
    if "n" in source and (type(source["n"]) is not int or source["n"] != 1):
        _invalid("n", "Only one completion per request is supported.")
    if "stop" in source:
        stop = source["stop"]
        if not isinstance(stop, str) and not (
            isinstance(stop, list) and 1 <= len(stop) <= 4 and all(isinstance(item, str) for item in stop)
        ):
            _invalid("stop", "Expected a string or a list of 1 to 4 strings.")
    if "response_format" in source:
        _response_format(source["response_format"])
    request = copy.deepcopy(source)
    for message in request["messages"]:
        if message["role"] == "assistant":
            for field in ("audio", "function_call", "annotations"):
                message.pop(field, None)
            if message.get("refusal") is None:
                message.pop("refusal", None)
            if message.get("tool_calls") is None or message["tool_calls"] == []:
                message.pop("tool_calls", None)
    request["model"] = slot.model
    request["stream"] = stream
    if "temperature" not in request and slot.defaults.temperature is not None:
        request["temperature"] = slot.defaults.temperature
    if not {"max_tokens", "max_completion_tokens"} & request.keys():
        budget = slot.defaults.max_output_tokens or 1024
        if budget > _MAX_OUTPUT_TOKENS:
            _invalid("max_completion_tokens", "Configured output token budget exceeds the supported limit.")
        request["max_completion_tokens"] = budget
    return request
