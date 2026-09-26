#!/usr/bin/env python3
"""Refresh the external model data snapshot at _data/external.json.

Model specs come from five key-free sources; per-provider inference prices from
three independent paths:
  - Epoch AI Capabilities Index (ECI):       https://epoch.ai/data/eci_scores.csv
  - OpenRouter model list & list pricing:    https://openrouter.ai/api/v1/models
  - OpenRouter per-provider endpoint prices: https://openrouter.ai/api/v1/models/<id>/endpoints
  - LiteLLM community price database:        model_prices_and_context_window.json
  - DeepInfra model list & pricing:          https://api.deepinfra.com/models/list
  - Novita AI model list & pricing:          https://api.novita.ai/v3/openai/models
  - Venice AI model list & pricing:          https://api.venice.ai/api/v1/models

Run manually (python3 script/fetch-external-data.py) or by the weekly
scheduled GitHub Action (.github/workflows/refresh-external-data.yml).
Standard library only; writes JSON atomically (temp file + os.replace).
All status/warning lines go to stderr so --dry-run stdout stays pure JSON.
"""

import argparse
import csv
import io
import json
import os
import re
import sys
import tempfile
import time
import urllib.request
import datetime

EPOCH_CSV_URL = "https://epoch.ai/data/eci_scores.csv"
OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"
OPENROUTER_ENDPOINTS_URL = "https://openrouter.ai/api/v1/models/{}/endpoints"
LITELLM_URL = "https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json"
DEEPINFRA_URL = "https://api.deepinfra.com/models/list"
NOVITA_URL = "https://api.novita.ai/v3/openai/models"
VENICE_URL = "https://api.venice.ai/api/v1/models"

HEADERS = {"User-Agent": "theaimetric-data-refresh/1.0 (+https://theaimetric.com)",
           "Accept": "application/json, text/csv;q=0.9,*/*;q=0.8"}

# Hosts shown on the page (curated "major" set). Everything else is still counted in
# provider_total so the footnote can state how many providers exist.
MAJOR_PROVIDERS = ["OpenAI", "Anthropic", "Google", "Amazon Bedrock", "Azure", "Alibaba",
                   "DeepSeek", "Z.AI", "Together", "DeepInfra", "Novita", "Fireworks",
                   "BaseTen", "Venice", "Cohere"]

# litellm_provider -> canonical provider name (from the observed distribution).
# Unknown values pass through under their raw litellm_provider value and are
# dropped by the curation filter - no guessing, no crash.
LITELLM_PROVIDERS = {
    "openai": "OpenAI", "chatgpt": "OpenAI", "anthropic": "Anthropic",
    "gemini": "Google", "vertex_ai": "Google", "vertex_ai-anthropic_models": "Google",
    "vertex_ai-language-models": "Google",
    "bedrock": "Amazon Bedrock", "bedrock_converse": "Amazon Bedrock", "bedrock_mantle": "Amazon Bedrock",
    "azure": "Azure", "azure_ai": "Azure",
    "dashscope": "Alibaba", "qwencloud": "Alibaba", "qwen_ai_platform": "Alibaba",
    "zai": "Z.AI", "deepseek": "DeepSeek", "cohere": "Cohere",
    "together_ai": "Together", "deepinfra": "DeepInfra", "novita": "Novita",
    "baseten": "BaseTen", "fireworks_ai": "Fireworks", "nebius": "Nebius",
    "friendliai": "Friendli", "perplexity": "Perplexity", "aihubmix": "AIHubMix",
    "scx-ai": "SCX", "moonshot": "Moonshot", "mistral": "Mistral",
}

# Epoch CSV "Model" value per tracked model; None = Epoch has not rated it
# (known absence, not a source-side rename).
EPOCH_NAMES = {
    "gpt-5-6-sol": "GPT-5.6 Sol",
    "glm-5-3": "GLM-5.3",
    "claude-opus-5-5": None,
    "deepseek-v4-1-flash": "DeepSeek V4.1 Flash",
    "qwen3-8-max-preview": "Qwen3.8 Max (0902)",
    "north-mini-code-1-0": None,
}

# Per-model source identifiers. None = that source has no row for the model
# (never fuzzy-match). litellm_exclude: substrings that disqualify a LiteLLM key
# (different model or variant). litellm_spec: the LiteLLM key of the model's own
# author, used only for the specs table.
MODEL_SOURCES = {
    "gpt-5-6-sol": {"openrouter": "openai/gpt-5.6-sol", "litellm": "gpt-5.6-sol",
                    "litellm_exclude": ["-pro"], "litellm_spec": "gpt-5.6-sol",
                    "deepinfra": None, "novita": None, "venice": None},
    "glm-5-3": {"openrouter": "z-ai/glm-5.3", "litellm": "glm-5.3",
                "litellm_exclude": ["flash", "coding-glm"], "litellm_spec": "zai/glm-5.3",
                "deepinfra": "zai-org/GLM-5.3", "novita": "zai-org/glm-5.3", "venice": "z-ai-glm-5-3"},
    "claude-opus-5-5": {"openrouter": "anthropic/claude-opus-5.5", "litellm": "claude-opus-5-5",
                        "litellm_exclude": [], "litellm_spec": "claude-opus-5-5",
                        "deepinfra": None, "novita": None, "venice": "claude-opus-5-5"},
    "deepseek-v4-1-flash": {"openrouter": "deepseek/deepseek-v4.1-flash", "litellm": "deepseek-v4.1-flash",
                            "litellm_exclude": [], "litellm_spec": None,
                            "deepinfra": "deepseek-ai/DeepSeek-V4.1-Flash",
                            "novita": "deepseek/deepseek-v4.1-flash", "venice": "deepseek-v4-1-flash"},
    "qwen3-8-max-preview": {"openrouter": "qwen/qwen3.8-max-0902", "litellm": "qwen3.8-max",
                            "litellm_exclude": [], "litellm_spec": "dashscope/qwen3.8-max",
                            "deepinfra": "Qwen/Qwen3.8-Max", "novita": "qwen/qwen3.8-max", "venice": None},
    "north-mini-code-1-0": {"openrouter": "cohere/north-mini-code:free", "litellm": "north-mini-code",
                            "litellm_exclude": [], "litellm_spec": None,
                            "deepinfra": None, "novita": None, "venice": None},
}

# Vendor-populated catalog: the newest AUTO_LIMIT base ids per tracked vendor
# are appended to _data/models.yml from the vendor's own catalog row
# (OpenRouter /models). Entries already tracked via MODEL_SOURCES are skipped;
# scores and usage momentum are OpenCode-only and stay absent until
# hand-authored.
AUTO_VENDORS = {
    "openai": "OpenAI",
    "anthropic": "Anthropic",
    "z-ai": "Zhipu",
    "deepseek": "DeepSeek",
    "qwen": "Qwen (Alibaba)",
    "cohere": "Cohere",
}
AUTO_LIMIT = 10

MODALITY_NAMES = {"text": "Text", "image": "Image", "file": "Pdf", "video": "Video", "audio": "Audio"}


def fetch_text(url, timeout):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8")


def fetch_json(url, timeout):
    return json.loads(fetch_text(url, timeout))


def slugify(name):
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def fmt_tokens(n):
    if n >= 1_000_000:
        s = f"{n / 1e6:.2f}".rstrip("0").rstrip(".")
        return s + "M"
    if n >= 1_000:
        return f"{n / 1000:.0f}K"
    return str(n)


def to_per_m(per_token):
    """USD per 1M tokens from a USD-per-token string, or None."""
    if per_token is None:
        return None
    return round(float(per_token) * 1e6, 4)


def price_cell(per_million):
    """Catalog pricing cell: usd value plus the rendered label."""
    if per_million is None:
        return {"usd": None, "label": "Not published"}
    v = float(per_million)
    label = f"${v:.2f}" if v >= 0.01 else "$" + f"{v:.4f}".rstrip("0")
    return {"usd": round(v, 4), "label": label}


def parse_catalog_keys(text):
    """Top-level keys of _data/models.yml without a YAML dependency."""
    keys = set()
    for line in text.splitlines():
        m = re.match(r"^([A-Za-z0-9][A-Za-z0-9_-]*):\s*$", line)
        if m:
            keys.add(m.group(1))
    return keys


def first_sentence(text):
    text = (text or "").strip().replace("\n", " ")
    m = re.match(r"(.+?[.!?])(?:\s|$)", text)
    return (m.group(1) if m else text).strip()


def build_auto_catalog(key, vendor, or_id, src):
    """Catalog entry built from the vendor's own catalog row (OpenRouter)."""
    name = src["name"]
    if ": " in name:
        name = name.split(": ", 1)[1].strip()
    arch = src.get("architecture") or {}
    params = src.get("supported_parameters") or []
    top = src.get("top_provider") or {}
    pricing = src.get("pricing") or {}
    ctx = src.get("context_length") or 0
    out_tokens = top.get("max_completion_tokens")
    tagline = first_sentence(src.get("description")) or f"{name} — {vendor} catalog entry."
    return {
        "key": key,
        "name": name,
        "vendor": vendor,
        "data_url": None,
        "model_url": f"https://openrouter.ai/{or_id}",
        "tagline": tagline,
        "context_tokens": ctx,
        "context_display": fmt_tokens(ctx) if ctx else "",
        "output_tokens": out_tokens,
        "output_display": fmt_tokens(out_tokens) if out_tokens else None,
        "knowledge_cutoff": "Unknown",
        "released": datetime.datetime.fromtimestamp(
            src.get("created") or 0, datetime.timezone.utc).strftime("%b %Y"),
        "reasoning": "reasoning" in params or "include_reasoning" in params,
        "inputs": [MODALITY_NAMES.get(x, str(x).title()) for x in (arch.get("input_modalities") or [])],
        "outputs": [MODALITY_NAMES.get(x, str(x).title()) for x in (arch.get("output_modalities") or [])],
        "weights_url": f"https://huggingface.co/{src['hugging_face_id']}" if src.get("hugging_face_id") else None,
        "pricing": {"input": price_cell(to_per_m(pricing.get("prompt"))),
                    "output": price_cell(to_per_m(pricing.get("completion"))),
                    "cached": price_cell(to_per_m(pricing.get("input_cache_read")))},
        "scores": {},
        "momentum": {"has_usage": False},
    }


def yaml_entry_block(key, m):
    """Emit one catalog entry as YAML text (JSON scalars are valid YAML)."""
    j = json.dumps
    lines = [f"{key}:",
             f"  key: {j(m['key'])}",
             f"  name: {j(m['name'])}",
             f"  vendor: {j(m['vendor'])}",
             "  data_url:",
             f"  model_url: {j(m['model_url'])}",
             f"  tagline: {j(m['tagline'])}",
             f"  context_tokens: {m['context_tokens'] if m['context_tokens'] else ''}",
             f"  context_display: {j(m['context_display'])}",
             f"  output_tokens: {m['output_tokens'] if m['output_tokens'] is not None else ''}",
             f"  output_display: {j(m['output_display']) if m['output_display'] is not None else ''}",
             f"  knowledge_cutoff: {j(m['knowledge_cutoff'])}",
             f"  released: {j(m['released'])}",
             f"  reasoning: {'true' if m['reasoning'] else 'false'}",
             f"  inputs: {j(m['inputs'])}",
             f"  outputs: {j(m['outputs'])}",
             "  weights_url:",
             "  pricing:",
             f"    input: {j(m['pricing']['input'])}",
             f"    output: {j(m['pricing']['output'])}",
             f"    cached: {j(m['pricing']['cached'])}",
             "  scores: {}",
             "  momentum:",
             "    has_usage: false"]
    return "\n".join(lines)


def write_catalog_entries(path, text, entries):
    """Append generated catalog entries, preserving every existing byte."""
    if not text.endswith("\n"):
        text += "\n"
    if "# --- vendor-populated entries" not in text:
        text += ("\n# --- vendor-populated entries below (script/fetch-external-data.py) ---\n"
                 "# Generated from each vendor's published model list. Scores and usage\n"
                 "# momentum are OpenCode-only and stay absent until hand-authored.\n")
    text += "\n" + "\n\n".join(yaml_entry_block(k, e) for k, e in entries) + "\n"
    tmp = tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=os.path.dirname(os.path.abspath(path)), delete=False, suffix=".tmp"
    )
    try:
        with tmp:
            tmp.write(text)
        os.replace(tmp.name, path)
    except BaseException:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# Epoch AI (ECI) - contributes the epoch object and eci_ranking; no price rows.
# ---------------------------------------------------------------------------

def parse_epoch(text):
    """Return dict keyed by exact Model-column value."""
    rows = {}
    for row in csv.DictReader(io.StringIO(text)):
        rows[row["Model"]] = row
    return rows


def build_epoch(entry):
    slug = slugify(entry["Display name"])
    return {
        "display": entry["Display name"],
        "eci": float(entry["eci"]),
        "ci_low": float(entry["eci_ci_low"]),
        "ci_high": float(entry["eci_ci_high"]),
        "as_of": entry["date"],
        "organization": entry["Organization"],
        "country": entry["Country (of organization)"],
        "access": entry["Accessibility group"],
        "page": f"https://epoch.ai/models/{slug}",
    }


# ---------------------------------------------------------------------------
# OpenRouter: specs from /models; per-provider price rows from /endpoints.
# ---------------------------------------------------------------------------

def or_spec(entry, url):
    """Spec row for a model from its /models catalog entry."""
    provider = entry.get("top_provider") or {}
    spec = {"source": "openrouter", "url": url,
            "context": entry["context_length"],
            "context_display": fmt_tokens(entry["context_length"])}
    if provider.get("max_completion_tokens"):
        spec["max_output"] = provider["max_completion_tokens"]
        spec["max_output_display"] = fmt_tokens(provider["max_completion_tokens"])
    spec["tools"] = "tools" in (entry.get("supported_parameters") or [])
    return spec


def or_endpoint_row(ep):
    """Price row for one OpenRouter endpoint, or None when unusable.

    Rows are keyed by (provider_name, tag); status == 0 means live, batch
    variants are skipped. pricing.* are charged USD-per-token strings (the
    discount is already applied); pricing.discount is informational.
    """
    tag = ep.get("tag") or ""
    if ep.get("status") != 0 or "batch" in tag:
        return None
    pricing = ep.get("pricing") or {}
    prompt, completion = pricing.get("prompt"), pricing.get("completion")
    if prompt is None or completion is None:
        return None
    row = {
        "provider": ep["provider_name"],
        "tier": tag.split("/", 1)[1] if "/" in tag else None,
        "input": round(float(prompt) * 1e6, 4),
        "output": round(float(completion) * 1e6, 4),
        "sources": ["openrouter"],
    }
    cached = pricing.get("input_cache_read")
    if cached is not None:
        row["cached"] = round(float(cached) * 1e6, 4)
    discount = pricing.get("discount")
    if discount:
        row["discount"] = discount
        row["list_input"] = round(row["input"] / (1 - discount), 4)
        row["list_output"] = round(row["output"] / (1 - discount), 4)
    ctx = ep.get("context_length")
    if ctx:
        row["context"] = ctx
        row["context_display"] = fmt_tokens(ctx)
    quant = ep.get("quantization")
    if quant:
        row["quantization"] = quant
    return row


def or_model_rows(or_id, timeout, warnings):
    """Fetch per-provider endpoint rows for one OpenRouter model id."""
    rows = []
    try:
        payload = fetch_json(OPENROUTER_ENDPOINTS_URL.format(or_id), timeout)
        endpoints = (payload.get("data") or {}).get("endpoints") or []
        for ep in endpoints:
            row = or_endpoint_row(ep)
            if row:
                rows.append(row)
    except Exception as exc:
        warnings.append(f"openrouter: endpoints for {or_id!r} failed: {exc}")
    return rows


# ---------------------------------------------------------------------------
# LiteLLM: community price database. Key identity rules below; the cheapest
# input-price key per canonical provider is kept.
# ---------------------------------------------------------------------------

def litellm_spec_row(entry):
    spec = {"source": "litellm", "url": "https://github.com/BerriAI/litellm"}
    if entry.get("max_input_tokens"):
        spec["context"] = entry["max_input_tokens"]
        spec["context_display"] = fmt_tokens(entry["max_input_tokens"])
    if entry.get("max_output_tokens"):
        spec["max_output"] = entry["max_output_tokens"]
        spec["max_output_display"] = fmt_tokens(entry["max_output_tokens"])
    fcall = entry.get("supports_function_calling")
    if fcall is not None:
        spec["tools"] = bool(fcall)
    return spec


def source_litellm(litellm_json, cfg):
    """Return (spec, rows, hits) for one model; hits = catalog keys containing the
    needle regardless of row filters (a substring matching nothing is a warning)."""
    spec = None
    spec_key = cfg.get("litellm_spec")
    if spec_key and spec_key in litellm_json:
        spec = litellm_spec_row(litellm_json[spec_key])

    needle = cfg["litellm"].lower()
    excludes = [x.lower() for x in cfg.get("litellm_exclude") or []]
    per_provider = {}
    hits = 0
    for key, entry in litellm_json.items():
        low = key.lower()
        if needle not in low:
            continue
        hits += 1
        if any(x in low for x in excludes):
            continue
        if entry.get("mode") != "chat" or ":batch" in key:
            continue
        in_cost, out_cost = entry.get("input_cost_per_token"), entry.get("output_cost_per_token")
        if in_cost is None or out_cost is None:
            continue
        raw_provider = entry.get("litellm_provider")
        if raw_provider == "openrouter":
            continue
        provider = LITELLM_PROVIDERS.get(raw_provider, raw_provider)
        row = {
            "provider": provider,
            "tier": None,
            "input": round(float(in_cost) * 1e6, 4),
            "output": round(float(out_cost) * 1e6, 4),
            "sources": ["litellm"],
        }
        cached = entry.get("cache_read_input_token_cost")
        if cached is not None:
            row["cached"] = round(float(cached) * 1e6, 4)
        best = per_provider.get(provider)
        if best is None or (row["input"], row["output"]) < (best["input"], best["output"]):
            per_provider[provider] = row
    return spec, list(per_provider.values()), hits


# ---------------------------------------------------------------------------
# DeepInfra / Novita / Venice: direct model lists.
# ---------------------------------------------------------------------------

def source_deepinfra(rows, cfg):
    """Return (spec, row) for one model, or (None, None) when absent.

    cents_per_*_token are the LIST price (cents/token); the published discount
    must be applied to get the charged price: charged = list * (1 - discount).
    """
    model = next((r for r in rows if r.get("model_name") == cfg["deepinfra"]), None)
    if model is None:
        return None, None
    pricing = model.get("pricing") or {}
    spec = None
    if model.get("max_tokens"):
        spec = {"source": "deepinfra", "url": f"https://deepinfra.com/{model['model_name']}",
                "context": model["max_tokens"],
                "context_display": fmt_tokens(model["max_tokens"])}
        spec["tools"] = "tools" in (model.get("tags") or [])
    ci, co = pricing.get("cents_per_input_token"), pricing.get("cents_per_output_token")
    if ci is None or co is None:
        return spec, None
    discount = pricing.get("discount")
    list_input = round(float(ci) / 100 * 1e6, 4)
    list_output = round(float(co) / 100 * 1e6, 4)
    row = {
        "provider": "DeepInfra",
        "tier": model.get("quantization") or None,
        "input": round(list_input * (1 - (discount or 0)), 4),
        "output": round(list_output * (1 - (discount or 0)), 4),
        "sources": ["deepinfra"],
    }
    rate = pricing.get("rate_per_input_token_cached")
    if rate is not None:
        row["cached"] = round(row["input"] * float(rate), 4)
    if discount:
        row["discount"] = discount
        row["list_input"] = list_input
        row["list_output"] = list_output
    if model.get("quantization"):
        row["quantization"] = model["quantization"]
    row["context"] = model["max_tokens"]
    row["context_display"] = fmt_tokens(model["max_tokens"])
    return spec, row


def source_novita(rows, cfg):
    """Return (spec, row) for one model, or (None, None) when absent.

    price_per_m_decimal strings are already USD per 1M tokens.
    """
    model = next((r for r in rows if r.get("id") == cfg["novita"]), None)
    if model is None:
        return None, None
    spec = None
    if model.get("context_size"):
        spec = {"source": "novita", "url": "https://novita.ai/models",
                "context": model["context_size"],
                "context_display": fmt_tokens(model["context_size"])}
        if model.get("max_output_tokens"):
            spec["max_output"] = model["max_output_tokens"]
            spec["max_output_display"] = fmt_tokens(model["max_output_tokens"])
        spec["tools"] = "function-calling" in (model.get("features") or [])
    pricing = model.get("pricing") or {}
    prompt = (pricing.get("prompt") or {}).get("price_per_m_decimal")
    completion = (pricing.get("completion") or {}).get("price_per_m_decimal")
    if prompt is None or completion is None:
        return spec, None
    row = {
        "provider": "Novita",
        "tier": None,
        "input": round(float(prompt), 4),
        "output": round(float(completion), 4),
        "sources": ["novita"],
    }
    cache = (pricing.get("input_cache_read") or {}).get("price_per_m_decimal")
    if cache is not None:
        row["cached"] = round(float(cache), 4)
    row["context"] = model["context_size"]
    row["context_display"] = fmt_tokens(model["context_size"])
    return spec, row


def source_venice(rows, cfg):
    """Return (spec, row) for one model, or (None, None) when absent.

    model_spec.pricing.*.usd values are already USD per 1M tokens.
    """
    model = next((r for r in rows if r.get("id") == cfg["venice"]), None)
    if model is None:
        return None, None
    meta = model.get("model_spec") or {}
    pricing = meta.get("pricing") or {}
    spec = None
    if meta.get("availableContextTokens"):
        spec = {"source": "venice", "url": "https://venice.ai/models",
                "context": meta["availableContextTokens"],
                "context_display": fmt_tokens(meta["availableContextTokens"])}
        if meta.get("maxCompletionTokens"):
            spec["max_output"] = meta["maxCompletionTokens"]
            spec["max_output_display"] = fmt_tokens(meta["maxCompletionTokens"])
        fcall = (meta.get("capabilities") or {}).get("supportsFunctionCalling")
        if fcall is not None:
            spec["tools"] = bool(fcall)
    pin = (pricing.get("input") or {}).get("usd")
    pout = (pricing.get("output") or {}).get("usd")
    if pin is None or pout is None:
        return spec, None
    row = {
        "provider": "Venice",
        "tier": None,
        "input": round(float(pin), 4),
        "output": round(float(pout), 4),
        "sources": ["venice"],
    }
    cache = (pricing.get("cache_input") or {}).get("usd")
    if cache is not None:
        row["cached"] = round(float(cache), 4)
    row["context"] = meta["availableContextTokens"]
    row["context_display"] = fmt_tokens(meta["availableContextTokens"])
    return spec, row


# ---------------------------------------------------------------------------
# Merge + curation
# ---------------------------------------------------------------------------

def merge_rows(openrouter_rows, direct_rows, litellm_rows):
    """Merge one model's price rows.

    openrouter rows key on (provider, tier) and stand as-is; a duplicate
    (provider, tier) keeps the first. A direct-source row (deepinfra/novita/
    venice, tier = own quantization or None) attaches its source tag to the
    same-provider openrouter row with the same tier, else to the provider's
    first openrouter row, else becomes its own (provider, None) row. A
    litellm row attaches to a same-provider (provider, None) row, creates one
    when the provider has no openrouter row, and is dropped otherwise
    (LiteLLM never overrides tiered openrouter rows). Price values follow the
    precedence openrouter -> deepinfra/novita/venice -> litellm.
    """
    rows = {}          # (provider, tier) -> row
    provider_keys = {} # provider -> [(provider, tier)]

    def first_key(provider):
        keys = provider_keys.get(provider) or []
        return keys[0] if keys else None

    for row in openrouter_rows:
        key = (row["provider"], row.get("tier"))
        if key in rows:
            continue  # duplicate provider+tier keeps the first
        provider_keys.setdefault(row["provider"], []).append(key)
        rows[key] = dict(row)

    for source in ("deepinfra", "novita", "venice"):
        row = direct_rows.get(source)
        if row is None:
            continue
        provider = row["provider"]
        key = next((k for k in provider_keys.get(provider, []) if k[1] == row.get("tier")), None)
        if key is None and provider_keys.get(provider):
            key = provider_keys[provider][0]
        if key is None:
            key = (provider, row.get("tier"))
            provider_keys.setdefault(provider, []).append(key)
            rows[key] = dict(row)
        else:
            if source not in rows[key]["sources"]:
                rows[key]["sources"].append(source)
            # direct prices agree with openrouter once discounts are applied;
            # precedence keeps the openrouter values as-is.

    for row in litellm_rows:
        provider = row["provider"]
        key = (provider, None)
        if key in rows:
            if "litellm" not in rows[key]["sources"]:
                rows[key]["sources"].append("litellm")
        elif not provider_keys.get(provider):
            provider_keys[provider] = [key]
            rows[key] = dict(row)
        # else: provider already covered by tiered openrouter rows -> drop

    out = []
    for row in rows.values():
        row["sources"] = sorted(set(row["sources"]))
        out.append(row)
    return out


def curate(entry):
    """Attach prices / prices_best / provider_total to one model entry."""
    prices = [r for r in entry["rows"] if r["provider"] in MAJOR_PROVIDERS]
    prices.sort(key=lambda r: (r["output"], r["input"]))
    best = {}
    for r in prices:
        cur = best.get(r["provider"])
        if cur is None or (r["output"], r["input"]) < (cur["output"], cur["input"]):
            best[r["provider"]] = r
    entry["prices"] = prices
    entry["prices_best"] = sorted(best.values(), key=lambda r: (r["output"], r["input"]))
    entry["provider_total"] = len({r["provider"] for r in entry["rows"]})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="_data/external.json")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--timeout", type=int, default=30)
    args = parser.parse_args()

    models_path = os.path.join(os.path.dirname(os.path.abspath(args.out)), "models.yml")
    with open(models_path, encoding="utf-8") as fh:
        models_text = fh.read()
    catalog_keys = parse_catalog_keys(models_text)

    warnings = []
    matched = {"epoch": 0, "openrouter": 0, "litellm": 0}
    models = {}
    epoch_ranked = []

    # --- fetch + parse every source; any failure aborts with nothing written ---
    data = {}
    for name, url in (("epoch", EPOCH_CSV_URL),
                      ("openrouter", OPENROUTER_MODELS_URL),
                      ("litellm", LITELLM_URL),
                      ("deepinfra", DEEPINFRA_URL),
                      ("novita", NOVITA_URL),
                      ("venice", VENICE_URL)):
        try:
            text = fetch_text(url, args.timeout)
            if name == "openrouter":
                data[name] = {m["id"]: m for m in json.loads(text)["data"]}
            elif name in ("novita", "venice"):
                data[name] = json.loads(text)["data"]
            elif name == "epoch":
                data[name] = parse_epoch(text)
            else:
                data[name] = json.loads(text)
        except Exception as exc:
            print(f"error: {name} fetch/parse failed: {exc}", file=sys.stderr)
            return 1

    # --- per-model specs and price rows, in source order ---
    for key, cfg in MODEL_SOURCES.items():
        entry = {"specs": []}

        # epoch
        epoch_name = EPOCH_NAMES.get(key)
        if epoch_name is not None:
            if epoch_name in data["epoch"]:
                entry["epoch"] = build_epoch(data["epoch"][epoch_name])
                matched["epoch"] += 1
                epoch_ranked.append((key, entry["epoch"]["eci"]))
            else:
                warnings.append(f"epoch: no row for {epoch_name!r} ({key})")

        # openrouter: spec row from /models + price rows from /endpoints
        or_rows = []
        or_id = cfg["openrouter"]
        if or_id in data["openrouter"]:
            entry["specs"].append(
                or_spec(data["openrouter"][or_id], f"https://openrouter.ai/{or_id}"))
            matched["openrouter"] += 1
            or_rows = or_model_rows(or_id, args.timeout, warnings)
        else:
            warnings.append(f"openrouter: no id {or_id!r} ({key})")

        # litellm (spec row second, per documented source order)
        litellm_spec, litellm_rows, litellm_hits = source_litellm(data["litellm"], cfg)
        if litellm_spec:
            entry["specs"].append(litellm_spec)
        if litellm_hits:
            matched["litellm"] += 1
        else:
            warnings.append(f"litellm: substring {cfg['litellm']!r} matched nothing ({key})")

        # direct sources
        direct = {}
        for source, fetcher in (("deepinfra", source_deepinfra),
                                ("novita", source_novita),
                                ("venice", source_venice)):
            if cfg[source] is None:
                continue
            spec, row = fetcher(data[source], cfg)
            direct[source] = row
            if spec:
                entry["specs"].append(spec)
            if row is None:
                warnings.append(f"{source}: no id {cfg[source]!r} ({key})")

        entry["rows"] = merge_rows(or_rows, direct, litellm_rows)
        curate(entry)
        del entry["rows"]
        models[key] = entry

    # --- vendor-populated catalog entries: newest AUTO_LIMIT per tracked vendor ---
    tracked_bases = {cfg["openrouter"].split(":", 1)[0] for cfg in MODEL_SOURCES.values()}
    auto_catalog = []   # (key, catalog dict, raw litellm needle)
    used_keys = set()
    for prefix, vendor in AUTO_VENDORS.items():
        bases = {}
        for src in data["openrouter"].values():
            or_id = src.get("id") or ""
            if not or_id.startswith(prefix + "/") or or_id.startswith("~"):
                continue
            base = or_id.split(":", 1)[0]
            created = src.get("created") or 0
            cur = bases.get(base)
            prefer = cur is None or (":" not in or_id and ":" in cur[0]) or (
                (":" not in or_id) == (":" not in cur[0]) and created > cur[1])
            if prefer:
                bases[base] = (or_id, created)
        picked = 0
        for base, (or_id, _created) in sorted(bases.items(), key=lambda kv: (-kv[1][1], kv[0])):
            if picked >= AUTO_LIMIT:
                break
            if base in tracked_bases:
                continue
            key = slugify(base.split("/", 1)[1])
            if key in catalog_keys or key in used_keys:
                key = slugify(base)
            if key in catalog_keys or key in used_keys:
                continue
            src = data["openrouter"][or_id]
            auto_catalog.append((key, build_auto_catalog(key, vendor, or_id, src), or_id, base.split("/", 1)[1]))
            used_keys.add(key)
            picked += 1

    for key, catalog_entry, or_id, litellm_needle in auto_catalog:
        ext_entry = {"specs": [or_spec(data["openrouter"][or_id], f"https://openrouter.ai/{or_id}")]}
        or_rows = or_model_rows(or_id, args.timeout, warnings)
        time.sleep(0.2)
        cfg = {"openrouter": or_id, "litellm": litellm_needle,
               "litellm_exclude": [] if litellm_needle.endswith("-pro") else ["-pro"],
               "litellm_spec": None, "deepinfra": None, "novita": None, "venice": None}
        litellm_spec, litellm_rows, litellm_hits = source_litellm(data["litellm"], cfg)
        if litellm_spec:
            ext_entry["specs"].append(litellm_spec)
        if not litellm_hits:
            warnings.append(f"litellm: substring {cfg['litellm']!r} matched nothing ({key})")
        ext_entry["rows"] = merge_rows(or_rows, {}, litellm_rows)
        curate(ext_entry)
        del ext_entry["rows"]
        models[key] = ext_entry

    epoch_ranked.sort(key=lambda kv: (-kv[1], kv[0]))
    payload = {
        "generated": datetime.date.today().isoformat(),
        "sources": {
            "epoch": {
                "name": "Epoch AI",
                "dataset": "Epoch Capabilities Index (ECI)",
                "url": EPOCH_CSV_URL,
                "page": "https://epoch.ai/benchmarks",
            },
            "openrouter": {
                "name": "OpenRouter",
                "dataset": "Model list & list pricing",
                "url": OPENROUTER_MODELS_URL,
                "page": "https://openrouter.ai/models",
            },
            "litellm": {
                "name": "LiteLLM (community price database)",
                "dataset": "model_prices_and_context_window.json",
                "url": LITELLM_URL,
                "page": "https://github.com/BerriAI/litellm",
            },
            "deepinfra": {
                "name": "DeepInfra",
                "dataset": "Model list & pricing",
                "url": DEEPINFRA_URL,
                "page": "https://deepinfra.com/models",
            },
            "novita": {
                "name": "Novita AI",
                "dataset": "Model list & pricing",
                "url": NOVITA_URL,
                "page": "https://novita.ai/models",
            },
            "venice": {
                "name": "Venice AI",
                "dataset": "Model list & pricing",
                "url": VENICE_URL,
                "page": "https://venice.ai/models",
            },
        },
        "eci_ranking": [k for k, _ in epoch_ranked],
        "models": models,
    }

    curated_total = sum(len(m["prices"]) for m in models.values())
    provider_total = sum(m["provider_total"] for m in models.values())

    # --- status/warnings to stderr so --dry-run stdout stays pure JSON ---
    for w in warnings:
        print(f"warning: {w}", file=sys.stderr)
    print(f"epoch matched {matched['epoch']}/{len(MODEL_SOURCES)}", file=sys.stderr)
    print(f"openrouter matched {matched['openrouter']}/{len(MODEL_SOURCES)}", file=sys.stderr)
    print(f"litellm matched {matched['litellm']}/{len(MODEL_SOURCES)}", file=sys.stderr)
    print(f"prices: {curated_total} rows across {len(models)} models "
          f"({provider_total} providers found)", file=sys.stderr)

    if not any(m["prices"] for m in models.values()):
        print("error: curated price set is empty; aborting", file=sys.stderr)
        return 1

    if args.dry_run:
        print(json.dumps(payload, indent=2, sort_keys=True) + "\n", end="")
        print("dry run - nothing written", file=sys.stderr)
        return 0

    out_dir = os.path.dirname(os.path.abspath(args.out))
    tmp = tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=out_dir, delete=False, suffix=".tmp"
    )
    try:
        with tmp:
            json.dump(payload, tmp, indent=2, sort_keys=True)
            tmp.write("\n")
        os.replace(tmp.name, args.out)
    except BaseException:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass
        raise
    print(f"wrote {args.out}", file=sys.stderr)

    if auto_catalog and not args.dry_run:
        try:
            write_catalog_entries(models_path, models_text, [(k, e) for k, e, _, _ in auto_catalog])
            print(f"populated {len(auto_catalog)} vendor entries into {models_path}", file=sys.stderr)
        except Exception as exc:
            print(f"error: {models_path} append failed: {exc}", file=sys.stderr)
            return 1
    elif auto_catalog:
        print(f"dry run: would populate {len(auto_catalog)} vendor entries into {models_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
