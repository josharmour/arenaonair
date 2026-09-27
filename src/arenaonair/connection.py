"""Hosted trial, Patreon keys and custom provider discovery (no GUI dependency)."""
from __future__ import annotations

import hashlib
import json
import os
import platform
import secrets
import urllib.error
import urllib.parse
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .config import save_settings

MODEL = "glm-5.3-flash"
HOSTED_URL = "https://api.mtgacoach.com/v1"
TRIAL_ROOT = "https://mtgacoach.com/api/arenaonair"
TRIAL_URL = TRIAL_ROOT + "/v1"
SUBSCRIBE_URL = "https://mtgacoach.com/subscribe"
LOCAL_ENDPOINTS = ("http://127.0.0.1:11434/v1", "http://127.0.0.1:1234/v1",
                   "http://127.0.0.1:8000/v1", "http://127.0.0.1:8080/v1")


class ConnectionError(ValueError):
    """Safe user-facing message: never includes response bodies or credentials."""


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def endpoint(value, port=None):
    value = value.strip().rstrip("/")
    if "://" not in value:
        value = "http://" + value
    parsed = urllib.parse.urlsplit(value)
    if (parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or
            parsed.password or parsed.query or parsed.fragment):
        raise ConnectionError("Use an HTTP(S) endpoint without credentials, query, or fragment.")
    try:
        parsed.port
        host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
        netloc = f"{host}:{int(port)}" if port else parsed.netloc
        if port and not 1 <= int(port) <= 65535:
            raise ValueError()
    except ValueError:
        raise ConnectionError("Port must be between 1 and 65535.") from None
    return urllib.parse.urlunsplit((parsed.scheme, netloc, parsed.path or "/v1", "", ""))


def request_json(url, key="", body=None, timeout=8):
    headers = {"User-Agent": "ArenaOnAir/0.1", "Content-Type": "application/json"}
    if key:
        headers["Authorization"] = "Bearer " + key
    req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None,
                                 headers=headers)
    try:
        with urllib.request.build_opener(NoRedirect).open(req, timeout=timeout) as response:
            raw = response.read(131073)
        if len(raw) > 131072:
            raise ValueError()
        return json.loads(raw)
    except urllib.error.HTTPError as exc:
        messages = {401: "The key was rejected. Check your connection or get your Patreon key.",
                    402: "Your trial is used. Subscribe through Patreon or connect your own provider.",
                    403: "Access was denied. Check your Patreon membership or provider permissions.",
                    409: "This device already has a trial. Restore its trial key file or use a Patreon key.",
                    429: "Too many requests. Please try again shortly."}
        raise ConnectionError(messages.get(exc.code, f"The service returned HTTP {exc.code}. Try again later.")) from None
    except (OSError, ValueError):
        raise ConnectionError("Could not reach the service or read its response. Check the endpoint and connection.") from None


def models(base, key="", timeout=5):
    result = request_json(endpoint(base) + "/models", key, timeout=timeout)
    found = sorted({m["id"] for m in result.get("data", [])
                    if isinstance(m, dict) and isinstance(m.get("id"), str) and m["id"]})
    if not found:
        raise ConnectionError("The provider lists no models. Load a model, then try again.")
    return found


def discover_local():
    """Probe only four known loopback ports; never scan a LAN or send a saved key."""
    def probe(base):
        try:
            return base, models(base, timeout=1.5)
        except ConnectionError:
            return None
    with ThreadPoolExecutor(max_workers=4) as pool:
        return [result for result in pool.map(probe, LOCAL_ENDPOINTS) if result]


def write_key(path, key):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp-" + secrets.token_hex(4))
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(key.strip() + "\n")
    os.replace(temp, path)
    return path


def provider_key_path(directory, base):
    # A failed config save must not replace the credential still used by a
    # different endpoint. Keep each provider's key in a distinct file.
    suffix = hashlib.sha256(base.encode()).hexdigest()[:16]
    return Path(directory) / f"provider-{suffix}.key"


def start_trial(config_path):
    # Save the possession secret before contacting the server, so retries/restarts
    # cannot strand a provisioned trial. A device ID alone cannot retrieve it.
    key_path = Path(config_path).parent / "trial.key"
    key = key_path.read_text().strip() if key_path.exists() else "aoa_" + secrets.token_urlsafe(32)
    write_key(key_path, key)
    device = hashlib.sha256(f"arenaonair:{uuid.getnode()}:{platform.node()}".encode()).hexdigest()
    result = request_json(TRIAL_ROOT + "/trial", key, {"device_id": device})
    return {"base_url": TRIAL_URL, "model": MODEL, "key_file": str(key_path), "profile": "glm"}, result


def save_connection(config_path, values):
    save_settings(config_path, {None: {"narration_mode": "llm"}, "llm": values})


def connection_label(base):
    if base.rstrip("/") == TRIAL_URL:
        return "Hosted free trial · GLM 5.3"
    if base.rstrip("/") == HOSTED_URL:
        return "Patreon subscription · GLM 5.3"
    return "Your provider · " + urllib.parse.urlsplit(base).netloc if base else "Connect the generative booth"
