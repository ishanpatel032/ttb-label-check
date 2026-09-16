"""
Reads the printed fields off a label image.

The provider is configuration, not architecture. Set PROVIDER to anthropic,
openai, azure or gemini; everything downstream is unchanged, because this
module's only contract is `extract(image) -> dict of strings`.

That matters more than which provider is chosen today. Marcus noted in the
discovery interview that outbound traffic to cloud ML endpoints is blocked on
their network, and that this is part of what killed the scanning vendor pilot.
Anything real would have to run inside the FedRAMP boundary, which for an
office already on Azure means Azure OpenAI in Azure Government. That is the
`azure` provider below: the same code path, a different endpoint and key, with
no change to the compliance rules, the tests or the interface.

The model is asked only to transcribe. It never decides whether a label
passes; matching.py does that with deterministic rules.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import re

import httpx

TIMEOUT = httpx.Timeout(30.0, connect=10.0)

FIELDS = [
    "brand_name",
    "class_type",
    "abv",
    "net_contents",
    "producer",
    "country_of_origin",
    "government_warning",
]

SYSTEM = """You transcribe text from alcohol beverage labels for a compliance review tool.

Report only what is printed on the label. Do not correct spelling, fix
capitalisation, expand abbreviations, or fill in what you expect to be there.
If a field is not visible, return an empty string for it. Accuracy of
transcription matters more than completeness.

Return a single JSON object and nothing else. No prose, no code fences.

Keys, all strings:
  brand_name         the brand as printed
  class_type         class or type designation, e.g. Kentucky Straight Bourbon Whiskey
  abv                alcohol statement exactly as printed, e.g. "45% Alc./Vol. (90 Proof)"
  net_contents       net contents exactly as printed, e.g. "750 mL"
  producer           bottler, producer or importer, with city and state if shown
  country_of_origin  country of origin if stated, otherwise ""
  government_warning the full health warning transcribed character for character,
                     preserving capitalisation exactly as printed. This one must be
                     verbatim, including whether "GOVERNMENT WARNING:" is capitalised.
  legibility         one of "clear", "partial", "poor"
  legibility_note    one short sentence if anything was hard to read, otherwise ""
"""

USER_TEXT = "Transcribe this label. Return the JSON object described in your instructions."

_MEDIA = {
    "jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
    "gif": "image/gif", "webp": "image/webp",
}


def media_type(filename: str, fallback: str | None = None) -> str:
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    return _MEDIA.get(ext) or fallback or "image/jpeg"


def provider() -> str:
    return os.environ.get("PROVIDER", "anthropic").strip().lower()


def _need(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(
            f"{name} is not set. Copy .env.example to .env and fill it in."
        )
    return value


# --------------------------------------------------------------------------
# one request builder per provider
# --------------------------------------------------------------------------

def _anthropic(b64: str, mime: str) -> tuple[str, dict, dict]:
    return (
        "https://api.anthropic.com/v1/messages",
        {
            "x-api-key": _need("ANTHROPIC_API_KEY"),
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        {
            "model": os.environ.get("MODEL", "claude-sonnet-5"),
            "max_tokens": 900,
            "system": SYSTEM,
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "image",
                     "source": {"type": "base64", "media_type": mime, "data": b64}},
                    {"type": "text", "text": USER_TEXT},
                ],
            }],
        },
    )


def _openai_body(model: str, b64: str, mime: str) -> dict:
    return {
        "model": model,
        "max_tokens": 900,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": [
                {"type": "text", "text": USER_TEXT},
                {"type": "image_url",
                 "image_url": {"url": f"data:{mime};base64,{b64}"}},
            ]},
        ],
    }


def _openai(b64: str, mime: str) -> tuple[str, dict, dict]:
    return (
        "https://api.openai.com/v1/chat/completions",
        {"Authorization": f"Bearer {_need('OPENAI_API_KEY')}",
         "content-type": "application/json"},
        _openai_body(os.environ.get("MODEL", "gpt-4o"), b64, mime),
    )


def _azure(b64: str, mime: str) -> tuple[str, dict, dict]:
    """Azure OpenAI, including Azure Government.

    AZURE_OPENAI_ENDPOINT is the resource URL. In Azure Government that host
    ends in .azure.us rather than .azure.com, which is the whole difference
    between this running commercially and running inside the boundary.
    """
    endpoint = _need("AZURE_OPENAI_ENDPOINT").rstrip("/")
    deployment = _need("AZURE_OPENAI_DEPLOYMENT")
    version = os.environ.get("AZURE_OPENAI_API_VERSION", "2024-10-21")
    body = _openai_body(deployment, b64, mime)
    body.pop("model", None)  # on Azure the deployment name carries this
    return (
        f"{endpoint}/openai/deployments/{deployment}/chat/completions"
        f"?api-version={version}",
        {"api-key": _need("AZURE_OPENAI_API_KEY"), "content-type": "application/json"},
        body,
    )


def _gemini(b64: str, mime: str) -> tuple[str, dict, dict]:
    model = os.environ.get("MODEL", "gemini-3.6-flash")

    # Transcription needs no reasoning, and the Gemini 3 family thinks at a
    # high level unless told otherwise, which costs far more time than the
    # five second budget allows. Gemini 3 takes a thinking level; 2.5 takes a
    # token budget instead, and sending both is rejected.
    config: dict = {"maxOutputTokens": 900, "responseMimeType": "application/json"}
    level = os.environ.get("THINKING_LEVEL", "low")
    if "gemini-3" in model:
        config["thinkingConfig"] = {"thinkingLevel": level}
    elif "flash" in model:
        config["thinkingConfig"] = {"thinkingBudget": 0}

    return (
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
        {"x-goog-api-key": _need("GEMINI_API_KEY"), "content-type": "application/json"},
        {
            "system_instruction": {"parts": [{"text": SYSTEM}]},
            "contents": [{"parts": [
                {"inline_data": {"mime_type": mime, "data": b64}},
                {"text": USER_TEXT},
            ]}],
            "generationConfig": config,
        },
    )


BUILDERS = {
    "anthropic": _anthropic,
    "openai": _openai,
    "azure": _azure,
    "gemini": _gemini,
}


# --------------------------------------------------------------------------
# one response reader per provider
# --------------------------------------------------------------------------

def _text_from(name: str, data: dict) -> str:
    if name == "anthropic":
        return "".join(
            b.get("text", "") for b in data.get("content", [])
            if b.get("type") == "text"
        )
    if name == "gemini":
        parts = data["candidates"][0]["content"]["parts"]
        return "".join(p.get("text", "") for p in parts)
    return data["choices"][0]["message"]["content"] or ""


def _parse(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text).strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            raise ValueError("The model did not return readable JSON.")
        data = json.loads(text[start:end + 1])

    out = {k: (data.get(k) or "") for k in FIELDS}
    out["legibility"] = data.get("legibility") or "clear"
    out["legibility_note"] = data.get("legibility_note") or ""
    return {k: (v.strip() if isinstance(v, str) else v) for k, v in out.items()}


RETRY_ON = {429, 500, 502, 503, 504}
MAX_ATTEMPTS = 3


async def extract(image_bytes: bytes, filename: str,
                  content_type: str | None = None) -> dict:
    name = provider()
    build = BUILDERS.get(name)
    if not build:
        raise RuntimeError(
            f"PROVIDER is set to {name!r}. Use one of: {', '.join(BUILDERS)}."
        )

    url, headers, body = build(
        base64.b64encode(image_bytes).decode(),
        media_type(filename, content_type),
    )

    # A shared API returns 503 when it is busy and 429 when we are going too
    # fast. Both clear on their own, so a couple of short retries turn a
    # visible failure into a slightly slower success. The backoff stays small
    # to protect the latency budget, and anything still failing after three
    # attempts is reported rather than hidden.
    last: RuntimeError | None = None
    async with httpx.AsyncClient(timeout=TIMEOUT) as http:
        for attempt in range(MAX_ATTEMPTS):
            try:
                response = await http.post(url, headers=headers, json=body)
            except httpx.RequestError as e:
                last = RuntimeError(f"Could not reach the {name} API: {e}")
                await asyncio.sleep(0.5 * (attempt + 1))
                continue

            if response.status_code < 400:
                try:
                    return _parse(_text_from(name, response.json()))
                except (KeyError, IndexError, TypeError) as e:
                    raise ValueError(f"Unexpected response shape from {name}: {e}")

            detail = response.text[:300]
            if response.status_code in (401, 403):
                raise RuntimeError(
                    f"The {name} API rejected the credentials. {detail}"
                )
            if response.status_code not in RETRY_ON:
                raise RuntimeError(
                    f"The {name} API returned {response.status_code}. {detail}"
                )

            last = RuntimeError(
                f"The {name} API returned {response.status_code} after "
                f"{MAX_ATTEMPTS} attempts. {detail}"
            )
            if attempt < MAX_ATTEMPTS - 1:
                await asyncio.sleep(0.5 * (attempt + 1))

    raise last or RuntimeError(f"The {name} API could not be reached.")
