"""Safe, OpenAI-shaped errors shared by the plugin model adapters."""
from __future__ import annotations


class ModelGatewayError(Exception):
    def __init__(self, code: str, message: str, status_code: int = 400, param: str | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.param = param

    def to_dict(self) -> dict:
        return {
            "error": {
                "message": self.message,
                "type": "invalid_request_error" if self.status_code < 500 else "api_error",
                "param": self.param,
                "code": self.code,
            }
        }


def upstream_error(status_code: int) -> ModelGatewayError:
    """Do not forward vendor error bodies: they may echo headers or prompts."""
    if 300 <= status_code < 400:
        return ModelGatewayError("upstream_redirect_rejected", "Model provider redirects are not allowed", 502)
    if status_code in (401, 403):
        return ModelGatewayError("upstream_authentication_failed", "Model provider rejected the configured credentials", 502)
    if status_code == 429:
        return ModelGatewayError("upstream_rate_limited", "Model provider rate limit exceeded", 429)
    if status_code in (408, 504):
        return ModelGatewayError("upstream_timeout", "Model provider request timed out", 504)
    if 400 <= status_code < 500:
        return ModelGatewayError("upstream_request_rejected", "Model provider rejected the request", 400)
    return ModelGatewayError("upstream_error", "Model provider request failed", 502)
