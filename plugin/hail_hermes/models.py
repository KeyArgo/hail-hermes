"""Which models this machine can see, and which ones the watch may switch to.

Design rule (owner, 2026-11-07): the watch never gets its own credentials. It may only pick
from models Hermes can already use — the configured default, or a local server on this machine
that needs no key at all. If Hermes cannot use a model, the watch is not offered it.

Design rule (catalog, 2026-11-11): this module must never read a credential store. ``.env`` and
``auth.json`` are Hermes' business, and a plugin that parses them is refused (rule 11), so the
list is built from configuration, model *names* Hermes has cached, and unauthenticated probes of
local endpoints. A name here means "visible", not "authorised": Hermes answers that question
when it answers the turn.
"""

from __future__ import annotations

import json
import os
import time
import urllib.request
from urllib.parse import urlparse
from pathlib import Path
from typing import Any

# Providers that run on this machine and need no credential.
LOCAL_HOSTS = ("127.0.0.1", "localhost", "0.0.0.0", "::1")
# "custom" is deliberately NOT here: a custom endpoint is usually remote and needs a key.
LOCAL_PROVIDERS = {"lmstudio", "ollama", "vllm", "llamacpp"}


def hermes_home() -> Path:
    """The active Hermes home (profile aware), or the Windows/Linux default."""
    env = os.environ.get("HERMES_HOME", "").strip()
    if env:
        return Path(env)
    if os.name == "nt":
        local = os.environ.get("LOCALAPPDATA", "").strip()
        if local and (Path(local) / "hermes").exists():
            return Path(local) / "hermes"
    return Path.home() / ".hermes"


def _config(home: Path) -> dict[str, Any]:
    """Parse the two config blocks we need, with or without PyYAML installed.

    The hub may run where ``yaml`` is absent (it is not a hard dependency of the watch path),
    so a tiny reader handles ``model:`` and ``providers:`` rather than silently returning
    nothing and telling the owner they have no models.
    """
    try:
        text = (home / "config.yaml").read_text(encoding="utf-8")
    except OSError:
        return {}
    try:
        import yaml  # type: ignore

        return yaml.safe_load(text) or {}
    except ImportError:
        pass
    except ValueError:
        return {}

    return _config_without_yaml(text)


def _config_without_yaml(text: str) -> dict[str, Any]:
    """Minimal reader for the shape Hermes writes: two-space nesting, scalar values."""
    model: dict[str, str] = {}
    providers: dict[str, dict[str, str]] = {}
    section = ""
    current = ""
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        line = raw.strip()
        if indent == 0:
            section = line[:-1] if line.endswith(":") else ""
            current = ""
            continue
        if ":" not in line:
            continue
        name, _, value = line.partition(":")
        name, value = name.strip(), value.strip().strip('"').strip("'")
        if section == "model" and indent >= 2:
            if name in ("default", "model", "provider"):
                model[name] = value
        elif section == "providers":
            if indent <= 2 and not value:
                current = name
                providers.setdefault(current, {})
            elif indent >= 4 and current:
                providers[current][name] = value
    out: dict[str, Any] = {}
    if model:
        out["model"] = model
    if providers:
        out["providers"] = providers
    return out


def _cached_models(home: Path) -> dict[str, list[str]]:
    try:
        raw = json.loads((home / "provider_models_cache.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    out: dict[str, list[str]] = {}
    for provider, entry in (raw or {}).items():
        models = entry.get("models") if isinstance(entry, dict) else entry
        if not isinstance(models, list):
            continue
        ids: list[str] = []
        for item in models:
            mid = item.get("id") if isinstance(item, dict) else item
            if isinstance(mid, str) and mid and mid not in ids:
                ids.append(mid)
        if ids:
            out[str(provider)] = ids
    return out


def _is_local_url(base_url: str) -> bool:
    """Compare hostnames, not substrings: a URL with 127.0.0.1 in a query is not local."""
    try:
        host = (urlparse(base_url or "").hostname or "").strip().lower()
    except ValueError:
        return False
    return host in LOCAL_HOSTS


def _tag(provider: str, base_url: str) -> str:
    """``local`` when the endpoint is on this machine, ``subscription`` otherwise."""
    if provider.lower() in LOCAL_PROVIDERS or _is_local_url(base_url):
        return "local"
    return "subscription"


# Live model lists, so a provider with no cache still offers real models. Kept short-lived:
# a fresh server should appear without restarting the hub.
_PROBE_CACHE: dict[str, tuple[float, list[str]]] = {}
_PROBE_TTL_S = 300.0


# Not everything an endpoint lists can hold a conversation. Offering an embedding model or a
# batch job to the owner as a "quick pick" is worse than offering nothing.
_NOT_CHAT = ("embed", "batch", "rerank", "whisper", "tts", "image", "moderation", "audio")


def _chat_only(ids: list[str]) -> list[str]:
    out = [i for i in ids if not any(bad in i.lower() for bad in _NOT_CHAT)]
    return out


def probe_models(base_url: str, api_key: str = "", *, timeout: float = 5.0) -> list[str]:
    """Ask an OpenAI-compatible endpoint what it serves. Never raises."""
    cache_key = f"{base_url}|{bool(api_key)}"
    hit = _PROBE_CACHE.get(cache_key)
    if hit and time.monotonic() - hit[0] < _PROBE_TTL_S:
        return hit[1]
    url = base_url.rstrip("/") + "/models"
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    ids: list[str] = []
    try:
        request = urllib.request.Request(url, headers=headers)  # noqa: S310 - configured endpoint
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8", errors="replace"))
        for item in payload.get("data") or []:
            mid = item.get("id") if isinstance(item, dict) else item
            if isinstance(mid, str) and mid and mid not in ids:
                ids.append(mid)
        ids = _chat_only(ids)
    except Exception:
        ids = []
    _PROBE_CACHE[cache_key] = (time.monotonic(), ids)
    return ids


def available_models(home: Path | None = None) -> list[dict[str, str]]:
    """Models this computer can see, as ``{source, id, label, tag, verified}``.

    **No credential file is read.** Catalog rule 11 forbids reading ``.env`` and ``auth.json``, and
    a disclosure cannot turn a prohibited read into an allowed one — so discovery here uses only
    what is not a secret:

    * ``config.yaml`` — provider endpoints and the configured default (config, not credentials);
    * Hermes' own model-name cache — names only, no keys;
    * a live ``/v1/models`` probe of **local** endpoints, which need no key at all;
    * the configured default, so the model Hermes actually answers with is always offered.

    A name in this list is therefore *visible*, not *authorised*: only Hermes can say whether the
    account may use it, and it does that when it answers. ``verified`` records which is which so a
    caller can be honest in the UI instead of promising something we cannot check.
    """
    home = home or hermes_home()
    cfg = _config(home)
    cached = _cached_models(home)

    providers = cfg.get("providers") or {}
    if not isinstance(providers, dict):
        providers = {}

    model_cfg = cfg.get("model") if isinstance(cfg.get("model"), dict) else {}
    default_provider = str(model_cfg.get("provider") or "") if model_cfg else ""
    default_model = str(model_cfg.get("default") or model_cfg.get("model") or "") if model_cfg else ""

    out: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()

    for name, entry in providers.items():
        entry = entry if isinstance(entry, dict) else {}
        provider = str(name)
        base_url = str(entry.get("base_url") or "")
        tag = _tag(provider, base_url)
        ids = list(cached.get(provider, []))
        verified = "unverified"
        if base_url and tag == "local":
            # Local endpoints take no key, so asking them is both allowed and accurate.
            live = probe_models(base_url, "")
            if live:
                ids = live
                verified = "local"
        default_for_provider = str(entry.get("default_model") or "")
        if default_for_provider and default_for_provider not in ids:
            ids.insert(0, default_for_provider)
        if provider == default_provider and default_model and default_model not in ids:
            ids.insert(0, default_model)
        for mid in ids:
            if (provider, mid) in seen:
                continue
            seen.add((provider, mid))
            out.append({
                "source": provider,
                "id": mid,
                "label": f"{provider} · {mid}",
                "tag": tag,
                "verified": verified,
            })

    # The model Hermes is configured to answer with, even when its provider is not in the list:
    # offering nothing on a fresh install made the panel say "no model" about a working machine.
    if default_provider and default_model and (default_provider, default_model) not in seen:
        out.insert(0, {
            "source": default_provider,
            "id": default_model,
            "label": f"{default_provider} · {default_model}",
            "tag": _tag(default_provider, ""),
            "verified": "configured",
        })

    return out


def preset_options(selected: Any, available: list[dict[str, str]]) -> list[dict[str, str]]:
    """Resolve the owner's ``watch_model_presets`` against what is actually available.

    Unknown or de-authorised entries are dropped rather than shown and failing later, and the
    list is capped at five because that is what fits on a wrist without scrolling forever.
    """
    by_key = {f"{m['source']}::{m['id']}": m for m in available}
    out: list[dict[str, str]] = []
    for item in selected if isinstance(selected, list) else []:
        key = item if isinstance(item, str) else ""
        if not key:
            if isinstance(item, dict):
                key = f"{item.get('source', '')}::{item.get('id', '')}"
        model = by_key.get(key)
        if model and model not in out:
            # Only models this machine can still see. A hand-picked entry whose model has since
            # disappeared is exactly the trap that broke the watch once ("nous · default").
            out.append(model)
        if len(out) >= 5:
            break
    return out
