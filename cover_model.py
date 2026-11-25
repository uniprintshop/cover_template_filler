"""HTTP transport, explicit inference failures, and request-level diagnostics."""

import json
import logging
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

LOG = logging.getLogger("thesis-cover")


class ModelError(RuntimeError):
    """The model did not return a usable field extraction."""


class ModelUnavailable(ModelError):
    """Transient backend failure: retry later without moving the PDF to failed/."""


class InvalidModelResponse(ModelError):
    """Malformed, empty, or truncated output, or an invalid API request."""


@dataclass(frozen=True)
class ModelOutcome:
    status: str = "ok"
    error: str = ""


def request_json(request, timeout: int) -> dict:
    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read(4096).decode("utf-8", errors="replace")
        error = ModelUnavailable if exc.code == 429 or exc.code >= 500 else InvalidModelResponse
        raise error(f"Model HTTP {exc.code}: {detail}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ModelUnavailable(f"Model request failed: {exc}") from exc
    except (ValueError, UnicodeError) as exc:
        raise InvalidModelResponse(f"Model response is not JSON: {exc}") from exc
    if not isinstance(body, dict):
        raise InvalidModelResponse("Model API returned a non-object response")
    metrics = {k: body[k] for k in (
        "load_duration", "prompt_eval_count", "prompt_eval_duration", "eval_count",
        "eval_duration", "done_reason", "usage",
    ) if k in body}
    metrics["wall_seconds"] = round(time.monotonic() - started, 3)
    LOG.info("Inference metrics: %s", json.dumps(metrics, ensure_ascii=False))
    if body.get("error"):
        raise InvalidModelResponse(f"Model API error: {body['error']}")
    return body


def require_content(content, reason="", thinking="") -> str:
    if reason in ("length", "max_tokens"):
        raise InvalidModelResponse("Model response exhausted its output-token budget")
    if not isinstance(content, str) or not content.strip():
        raise InvalidModelResponse(
            f"Model returned empty content (reason={reason!r}, reasoning_chars={len(thinking or '')})"
        )
    return content
