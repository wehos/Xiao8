"""Validate OpenAI chat results without flattening their content or tool calls."""
from __future__ import annotations

from copy import deepcopy

from .errors import ModelGatewayError


_FINISH_REASONS = {"stop", "length", "tool_calls", "content_filter", "function_call"}


def _invalid() -> ModelGatewayError:
    # Provider bodies may contain prompts, credentials, or arbitrary error text.
    return ModelGatewayError("invalid_upstream_response", "Model provider returned an invalid chat response", 502)


def _integer(value: object) -> bool:
    return type(value) is int and value >= 0


def _text(value: object) -> bool:
    return isinstance(value, str) and bool(value)


def _validate_usage(usage: object) -> None:
    if usage is None:
        return
    if not isinstance(usage, dict) or any(
        not _integer(usage.get(key)) for key in ("prompt_tokens", "completion_tokens", "total_tokens")
    ):
        raise _invalid()
    # Keep provider token details intact while rejecting malformed known fields.
    for key in ("prompt_tokens_details", "completion_tokens_details"):
        details = usage.get(key)
        if details is not None and (
            not isinstance(details, dict) or any(value is not None and not _integer(value) for value in details.values())
        ):
            raise _invalid()


def _choice(choices: object) -> dict:
    if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
        raise _invalid()
    choice = choices[0]
    if type(choice.get("index")) is not int or choice["index"] != 0:
        raise _invalid()
    reason = choice.get("finish_reason")
    if reason is not None and (not isinstance(reason, str) or reason not in _FINISH_REASONS):
        raise _invalid()
    if choice.get("logprobs") is not None and not isinstance(choice["logprobs"], dict):
        raise _invalid()
    return choice


def convert_response(body: dict, *, model_alias: str) -> dict:
    """Return a detached chat completion with the private model name replaced."""
    if (
        not isinstance(body, dict)
        or "error" in body
        or not _text(body.get("id"))
        or not _integer(body.get("created"))
        or body.get("object") != "chat.completion"
    ):
        raise _invalid()
    choice = _choice(body.get("choices"))
    if choice.get("finish_reason") is None:
        raise _invalid()
    message = choice.get("message")
    if not isinstance(message, dict) or message.get("role") != "assistant":
        raise _invalid()
    for key in ("content", "refusal"):
        if message.get(key) is not None and not isinstance(message[key], str):
            raise _invalid()
    calls = message.get("tool_calls")
    if calls is not None:
        if not isinstance(calls, list):
            raise _invalid()
        ids: set[str] = set()
        for call in calls:
            if not isinstance(call, dict) or not _text(call.get("id")) or call.get("type") != "function":
                raise _invalid()
            function = call.get("function")
            if (
                not isinstance(function, dict)
                or not _text(function.get("name"))
                or not isinstance(function.get("arguments"), str)
                or call["id"] in ids
            ):
                raise _invalid()
            ids.add(call["id"])
    if choice["finish_reason"] == "tool_calls" and not calls:
        raise _invalid()
    _validate_usage(body.get("usage"))
    result = deepcopy(body)
    result["model"] = model_alias
    return result


class OpenAIStreamConverter:
    """Validate incremental chunks, retaining only envelope, tool IDs, and usage.

    Text and argument deltas are forwarded immediately and never accumulated.
    ``finish`` validates the protocol terminal state separately from SSE [DONE].
    """

    def __init__(self, model_alias: str, include_usage: bool = True):
        self.model_alias = model_alias
        self.include_usage = include_usage
        self.usage: dict | None = None
        self.finished = False
        self._envelope: dict | None = None
        self._tools: dict[int, dict[str, str]] = {}

    def _normalize_envelope(self, event: dict) -> dict:
        if not isinstance(event, dict) or "error" in event:
            raise _invalid()
        result = deepcopy(event)
        if self._envelope is not None:
            for key, value in self._envelope.items():
                if key in result and result[key] != value:
                    raise _invalid()
                result.setdefault(key, value)
        if (
            not _text(result.get("id"))
            or not _integer(result.get("created"))
            or result.get("object") != "chat.completion.chunk"
        ):
            raise _invalid()
        if self._envelope is None:
            self._envelope = {key: result[key] for key in ("id", "created", "object")}
        result["model"] = self.model_alias
        return result

    def _validate_tools(self, calls: object) -> None:
        if not isinstance(calls, list):
            raise _invalid()
        indices: set[int] = set()
        for call in calls:
            if not isinstance(call, dict) or not _integer(call.get("index")):
                raise _invalid()
            index = call["index"]
            if index in indices:
                raise _invalid()
            indices.add(index)
            if call.get("type") is not None and call["type"] != "function":
                raise _invalid()
            state = self._tools.setdefault(index, {})
            call_id = call.get("id")
            if call_id is not None:
                if not _text(call_id) or ("id" in state and state["id"] != call_id):
                    raise _invalid()
                if any(other.get("id") == call_id for key, other in self._tools.items() if key != index):
                    raise _invalid()
                state["id"] = call_id
            function = call.get("function")
            if function is not None:
                if not isinstance(function, dict):
                    raise _invalid()
                name = function.get("name")
                if name is not None:
                    if not isinstance(name, str):
                        raise _invalid()
                    # Function names, like arguments, may arrive in fragments.
                    # Only remember that a nonempty name was provided.
                    if name:
                        state["name"] = "present"
                if function.get("arguments") is not None and not isinstance(function["arguments"], str):
                    raise _invalid()

    def feed(self, event: dict) -> list[dict]:
        result = self._normalize_envelope(event)
        _validate_usage(result.get("usage"))
        if result.get("usage") is not None:
            self.usage = deepcopy(result["usage"])
        choices = result.get("choices")
        if choices == []:
            if not self.finished or result.get("usage") is None:
                raise _invalid()
            return [result] if self.include_usage else []
        if self.finished:
            raise _invalid()
        choice = _choice(choices)
        delta = choice.get("delta")
        if not isinstance(delta, dict):
            raise _invalid()
        # Compatible providers may add reasoning_content or extra_content even
        # for ordinary requests. Preserve opaque extensions, as nonstreaming
        # responses do; validate only the standard fields supported here. This
        # does not promise that another protocol can translate those extensions.
        if delta.get("role") is not None and delta["role"] != "assistant":
            raise _invalid()
        for key in ("content", "refusal"):
            if delta.get(key) is not None and not isinstance(delta[key], str):
                raise _invalid()
        if delta.get("tool_calls") is not None:
            self._validate_tools(delta["tool_calls"])
        if choice.get("finish_reason") is not None:
            if any(not state.get("id") or not state.get("name") for state in self._tools.values()):
                raise _invalid()
            if choice["finish_reason"] == "tool_calls" and not self._tools:
                raise _invalid()
            self.finished = True
        if not self.include_usage:
            result.pop("usage", None)
        return [result]

    def finish(self) -> None:
        if not self.finished:
            raise ModelGatewayError("incomplete_upstream_stream", "Model provider stream ended before completion", 502)
