"""OpenRouter helpers: the model list and a check of an API key.

Both use OpenRouter's public API. `http` is a function (url, headers, timeout)
returning (status code, body bytes); tests replace it.
"""

import json
import urllib.error
import urllib.request

MODELS_URL = "https://openrouter.ai/api/v1/models"
KEY_URL = "https://openrouter.ai/api/v1/key"
MODEL_ID_CHARS = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._:/-")


def _http(url, headers, timeout):
    request = urllib.request.Request(url, headers=dict(headers, **{"User-Agent": "wyzer-cam-installer"}))
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read(8 * 1024 * 1024)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(65536)


def _price(value):
    """Price per million tokens. A negative price means "varies" (routers) and counts as unknown (NaN)."""
    try:
        price = float(value) * 1e6
    except (TypeError, ValueError):
        return float("nan")
    return price if price >= 0 else float("nan")


def parse_catalog(data):
    """Image-capable, text-answering models: {"paid": [...], "free": [...]}.

    Each entry: id, name, input and output price per million tokens, reasoning
    (a model that thinks first and may use up the reply space).
    """
    paid, free = [], []
    for model in (data or {}).get("data", []):
        model_id = model.get("id", "")
        arch = model.get("architecture") or {}
        if not model_id or not all(c in MODEL_ID_CHARS for c in model_id):
            continue
        if "image" not in (arch.get("input_modalities") or []):
            continue
        if (arch.get("output_modalities") or ["text"]) != ["text"]:
            continue
        if model_id == "openrouter/free" or model_id.endswith(":batch"):
            continue
        pricing = model.get("pricing") or {}
        entry = {
            "id": model_id,
            "name": model.get("name") or model_id,
            "input": _price(pricing.get("prompt")),
            "output": _price(pricing.get("completion")),
            "reasoning": any(w in (model_id + " " + (model.get("name") or "")).lower() for w in ("reasoning", "thinking")),
        }
        is_free = model_id.endswith(":free") or (entry["input"] == 0 and entry["output"] == 0)
        (free if is_free else paid).append(entry)
    paid.sort(key=lambda e: (e["input"] != e["input"], e["input"], e["id"]))
    free.sort(key=lambda e: e["id"])
    return {"paid": paid, "free": free}


def fetch_catalog(timeout=15, http=_http):
    status, body = http(MODELS_URL, {"Accept": "application/json"}, timeout)
    if status != 200:
        raise OSError("OpenRouter answered HTTP %d for its model list" % status)
    return parse_catalog(json.loads(body))


def check_model(catalog, model_id):
    """(ok, message) for a single model id against a catalog."""
    for entry in catalog["paid"] + catalog["free"]:
        if entry["id"] == model_id:
            return True, "%s accepts images." % model_id
    return False, ("%s is not in OpenRouter's list of image-capable models. Check the spelling; "
                   "the picker shows valid ids." % model_id)


def check_key(api_key, timeout=15, http=_http):
    """Asks OpenRouter whether a key is valid. Returns (ok, message); ok is
    None when the answer could not be found out (no network, odd reply)."""
    if not api_key:
        return False, "Enter an API key first."
    try:
        status, body = http(KEY_URL, {"Authorization": "Bearer " + api_key, "Accept": "application/json"}, timeout)
    except OSError as exc:
        return None, "Could not reach OpenRouter: %s" % exc
    if status in (401, 403):
        return False, "OpenRouter does not recognise this key."
    if status != 200:
        return None, "OpenRouter answered HTTP %d, so the key could not be checked." % status
    try:
        info = (json.loads(body) or {}).get("data") or {}
    except ValueError:
        info = {}
    parts = ["The key is accepted"]
    if info.get("label"):
        parts[0] += " (%s)" % str(info["label"])[:40]
    if info.get("limit") is not None and info.get("limit_remaining") is not None:
        parts.append("$%.2f of its $%.2f limit remains" % (float(info["limit_remaining"]), float(info["limit"])))
    if info.get("is_free_tier") is True:
        parts.append("the account is on OpenRouter's free tier, so free models have a low daily limit")
    return True, "; ".join(parts) + "."
