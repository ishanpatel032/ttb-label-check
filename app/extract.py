"""
Reads the printed fields off a label image.

One model call per label, returning JSON only. The model is asked to
transcribe, never to judge: it reports what is printed, and matching.py
decides whether that is acceptable. Keeping the judgement out of the model
keeps the compliance rules reviewable and keeps the call short, which is
what holds the round trip inside the five-second budget.
"""

from __future__ import annotations

import base64
import json
import os
import re

from anthropic import AsyncAnthropic

MODEL = os.environ.get("MODEL", "claude-sonnet-5")
MAX_TOKENS = 900

_client: AsyncAnthropic | None = None


def client() -> AsyncAnthropic:
    global _client
    if _client is None:
        key = os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            raise RuntimeError(
                "ANTHROPIC_API_KEY is not set. Copy .env.example to .env and add a key."
            )
        _client = AsyncAnthropic(api_key=key)
    return _client


FIELDS = [
    "brand_name",
    "class_type",
    "abv",
    "net_contents",
    "producer",
    "country_of_origin",
    "government_warning",
]

SYSTEM = f"""You transcribe text from alcohol beverage labels for a compliance review tool.

Report only what is printed on the label. Do not correct spelling, fix
capitalisation, expand abbreviations, or fill in what you expect to be there.
If a field is not visible, return an empty string for it. Accuracy of
transcription matters more than completeness.

Return a single JSON object and nothing else. No prose, no code fences.

Keys, all strings:
  brand_name         the brand as printed
  class_type         class or type designation, e.g. Kentucky Straight Bourbon Whiskey
  abv               alcohol statement exactly as printed, e.g. "45% Alc./Vol. (90 Proof)"
  net_contents      net contents exactly as printed, e.g. "750 mL"
  producer          bottler, producer, importer, with city and state if shown
  country_of_origin country of origin if stated, otherwise ""
  government_warning the full health warning transcribed character for character,
                     preserving capitalisation exactly as printed. This one must be
                     verbatim, including whether "GOVERNMENT WARNING:" is capitalised.

Also include:
  legibility        one of "clear", "partial", "poor"
  legibility_note   one short sentence if anything was hard to read, otherwise ""
"""

USER_TEXT = (
    "Transcribe this label. Return the JSON object described in your instructions."
)

_MEDIA = {
    "jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
    "gif": "image/gif", "webp": "image/webp",
}


def media_type(filename: str, fallback: str | None = None) -> str:
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    return _MEDIA.get(ext) or fallback or "image/jpeg"


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


async def extract(image_bytes: bytes, filename: str, content_type: str | None = None) -> dict:
    message = await client().messages.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        system=SYSTEM,
        messages=[{
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": media_type(filename, content_type),
                        "data": base64.b64encode(image_bytes).decode(),
                    },
                },
                {"type": "text", "text": USER_TEXT},
            ],
        }],
    )
    text = "".join(b.text for b in message.content if b.type == "text")
    return _parse(text)
