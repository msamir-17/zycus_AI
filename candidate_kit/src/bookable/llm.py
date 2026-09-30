"""
src/bookable/llm.py

Unified Vision LLM Provider with Automatic Fallback & Resumable Caching:
- Primary: Google Gemini (gemini-3.1-flash-lite / gemini-2.5-flash-lite)
- Fallback: OpenAI (gpt-4o-mini) via httpx
- Configurable image detail ("low" for classification, "high" for extraction)
- Automatic transient retry for OpenAI HTTP 429 with Retry-After and jitter (max 3 retries)
- Circuit-breaker on Gemini daily quota / RESOURCE_EXHAUSTED
- Content-addressed caching using SHA-256 (prompt + version + detail + image bytes)
- Detailed API usage tracking per call
"""

import os
import io
import re
import json
import time
import random
import base64
import hashlib
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from PIL import Image
import httpx
from dotenv import load_dotenv

load_dotenv()

# Track circuit-breaker state across the session
_GEMINI_CIRCUIT_BROKEN = False

SCHEMA_VERSION = "v4_quality_fixes"
CACHE_DIR = Path(__file__).resolve().parent.parent.parent / "cache" / "llm"
CACHE_DIR.mkdir(parents=True, exist_ok=True)


def get_gemini_circuit_broken() -> bool:
    """Return whether the Gemini circuit breaker is currently active."""
    return _GEMINI_CIRCUIT_BROKEN


def set_gemini_circuit_broken(broken: bool) -> None:
    """Explicitly set the circuit-breaker state (useful for tests or resets)."""
    global _GEMINI_CIRCUIT_BROKEN
    _GEMINI_CIRCUIT_BROKEN = broken


def compute_cache_key(
    prompt: str,
    images: List[Image.Image],
    text_hints: Optional[List[str]] = None,
    detail: str = "high"
) -> str:
    """Compute SHA-256 hash representing the exact input payload, detail mode, and schema version."""
    hasher = hashlib.sha256()
    hasher.update(SCHEMA_VERSION.encode("utf-8"))
    hasher.update(detail.encode("utf-8"))
    hasher.update(prompt.encode("utf-8"))
    if text_hints:
        for th in text_hints:
            hasher.update(th.encode("utf-8"))
    for img in images:
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=80)
        hasher.update(buf.getvalue())
    return hasher.hexdigest()


def _image_to_base64(img: Image.Image) -> str:
    """Convert PIL image to base64 jpeg string."""
    buf = io.BytesIO()
    if img.mode != "RGB":
        img = img.convert("RGB")
    img.save(buf, format="JPEG", quality=85)
    return base64.b64encode(buf.getvalue()).decode("utf-8")


def _parse_retry_delay(resp: httpx.Response, default_delay: float = 2.0) -> float:
    """
    Parse retry delay from HTTP 429 response.
    Checks Retry-After, retry-after-ms headers, or error message body.
    """
    hdr = resp.headers.get("retry-after")
    if hdr:
        try:
            return float(hdr)
        except ValueError:
            pass

    hdr_ms = resp.headers.get("retry-after-ms")
    if hdr_ms:
        try:
            return float(hdr_ms) / 1000.0
        except ValueError:
            pass

    try:
        body = resp.text
        m_s = re.search(r"try again in ([\d\.]+)s", body, re.IGNORECASE)
        if m_s:
            return float(m_s.group(1))
        m_ms = re.search(r"try again in ([\d\.]+)ms", body, re.IGNORECASE)
        if m_ms:
            return float(m_ms.group(1)) / 1000.0
    except Exception:
        pass

    return default_delay


def call_openai_vision(
    prompt: str,
    images: List[Image.Image],
    text_hints: Optional[List[str]] = None,
    model_name: Optional[str] = None,
    detail: str = "high",
    max_retries: int = 3
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """
    Execute vision prompt against OpenAI API using httpx with automatic 429 retry.
    
    Args:
        prompt: Instruction text + schema.
        images: List of PIL page images.
        text_hints: Extracted text layer snippets.
        model_name: Optional model override (defaults to OPENAI_MODEL or gpt-4o-mini).
        detail: "low" (85 tokens/image, ideal for classification) or "high" (detail tile mode for extraction).
        max_retries: Maximum transient 429 retries before giving up.
    """
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise ValueError("OPENAI_API_KEY is not set or empty in environment.")

    if not api_key.startswith("sk-"):
        api_key = f"sk-proj-{api_key}"

    model = model_name or os.getenv("OPENAI_MODEL", "gpt-4o-mini")

    content_parts: List[Dict[str, Any]] = [
        {"type": "text", "text": prompt}
    ]
    if text_hints:
        for th in text_hints:
            content_parts.append({"type": "text", "text": f"\n[Text Layer Evidence]:\n{th}"})

    for img in images:
        b64 = _image_to_base64(img)
        content_parts.append({
            "type": "image_url",
            "image_url": {
                "url": f"data:image/jpeg;base64,{b64}",
                "detail": detail
            }
        })

    payload = {
        "model": model,
        "messages": [
            {"role": "user", "content": content_parts}
        ],
        "response_format": {"type": "json_object"},
        "temperature": 0.0
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }

    retries_done = 0
    for attempt in range(max_retries + 1):
        start_t = time.time()
        resp = httpx.post(
            "https://api.openai.com/v1/chat/completions",
            headers=headers,
            json=payload,
            timeout=90.0
        )
        duration = time.time() - start_t

        if resp.status_code == 200:
            resp_json = resp.json()
            text = resp_json["choices"][0]["message"]["content"]
            data = json.loads(text)
            usage_info = {
                "provider": "openai",
                "model": model,
                "detail": detail,
                "fallback_used": True,
                "duration_s": round(duration, 2),
                "tokens": resp_json.get("usage", {}),
                "retries": retries_done
            }
            return data, usage_info

        # Handle 429 rate limit (TPM or RPM)
        if resp.status_code == 429 and attempt < max_retries:
            retries_done += 1
            base_wait = 2.0 * (1.5 ** attempt)
            wait_s = _parse_retry_delay(resp, default_delay=base_wait)
            jitter = random.uniform(0.1, 0.4)
            total_wait = max(wait_s, 0.5) + jitter
            print(f"[OpenAI 429] Rate limit hit. Waiting {total_wait:.2f}s before retry {attempt+1}/{max_retries}...")
            time.sleep(total_wait)
            continue

        raise RuntimeError(f"OpenAI API error {resp.status_code}: {resp.text}")


def call_gemini_vision(
    prompt: str,
    images: List[Image.Image],
    text_hints: Optional[List[str]] = None,
    model_name: Optional[str] = None
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Execute vision prompt against Google Gemini API."""
    from google import genai
    from google.genai import types

    client = genai.Client()
    model = model_name or os.getenv("GEMINI_MODEL", "gemini-3.1-flash-lite")

    contents: List[Any] = [prompt]
    if text_hints:
        for th in text_hints:
            contents.append(f"\n[Text Layer Evidence]:\n{th}")
    for img in images:
        contents.append(img)

    config = types.GenerateContentConfig(
        response_mime_type="application/json",
        temperature=0.0
    )

    start_t = time.time()
    response = client.models.generate_content(
        model=model,
        contents=contents,
        config=config
    )
    duration = time.time() - start_t

    data = json.loads(response.text)
    usage_info = {
        "provider": "gemini",
        "model": model,
        "detail": "native",
        "fallback_used": False,
        "duration_s": round(duration, 2)
    }
    return data, usage_info


def call_vision_llm(
    prompt: str,
    images: List[Image.Image],
    text_hints: Optional[List[str]] = None,
    gemini_model: Optional[str] = None,
    openai_model: Optional[str] = None,
    detail: str = "high",
    use_cache: bool = True
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """
    Main resilient vision LLM entrypoint:
    1. Checks SHA-256 content cache (factoring in detail setting).
    2. Calls Gemini (if circuit breaker is not active).
    3. On RESOURCE_EXHAUSTED / 429 daily quota exhaustion, immediately activates circuit breaker & routes to OpenAI.
    4. Caches valid output.
    """
    global _GEMINI_CIRCUIT_BROKEN

    cache_key = compute_cache_key(prompt, images, text_hints, detail=detail)
    cache_path = CACHE_DIR / f"{cache_key}.json"

    if use_cache and cache_path.exists():
        try:
            with open(cache_path, "r", encoding="utf-8") as f:
                cached_entry = json.load(f)
            cached_data = cached_entry["data"]
            cached_meta = cached_entry["meta"]
            cached_meta["cache_hit"] = True
            return cached_data, cached_meta
        except Exception:
            pass  # Fall through to live execution if corrupt

    # If Gemini circuit is already broken, route directly to OpenAI
    if _GEMINI_CIRCUIT_BROKEN:
        print(f"[LLM] Gemini circuit breaker is ACTIVE. Routing directly to OpenAI fallback (detail={detail})...")
        data, meta = call_openai_vision(
            prompt, images, text_hints, model_name=openai_model, detail=detail
        )
        meta["cache_hit"] = False
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump({"data": data, "meta": meta}, f, indent=2, ensure_ascii=False)
        return data, meta

    # Attempt Gemini with intelligent retry
    retries = 0
    max_transient_retries = 2
    last_error = None

    for attempt in range(max_transient_retries + 1):
        try:
            data, meta = call_gemini_vision(prompt, images, text_hints, model_name=gemini_model)
            meta["cache_hit"] = False
            meta["retries"] = retries
            # Cache success
            with open(cache_path, "w", encoding="utf-8") as f:
                json.dump({"data": data, "meta": meta}, f, indent=2, ensure_ascii=False)
            return data, meta

        except Exception as e:
            err_msg = str(e)
            last_error = e
            # Check for permanent / daily quota exhaustion
            if "RESOURCE_EXHAUSTED" in err_msg or "GenerateRequestsPerDay" in err_msg or "quota" in err_msg.lower():
                print(f"[LLM] Gemini DAILY QUOTA EXHAUSTED. Activating circuit breaker & switching to OpenAI...")
                _GEMINI_CIRCUIT_BROKEN = True
                break

            # Transient 503 / spike
            if attempt < max_transient_retries and ("503" in err_msg or "UNAVAILABLE" in err_msg or "TEMPORARY" in err_msg):
                retries += 1
                wait = (attempt + 1) * 3
                print(f"[LLM] Gemini transient error ({err_msg[:60]}). Retrying in {wait}s...")
                time.sleep(wait)
            else:
                print(f"[LLM] Gemini call failed: {err_msg[:120]}. Falling back to OpenAI...")
                break

    # Execute OpenAI Fallback
    try:
        data, meta = call_openai_vision(
            prompt, images, text_hints, model_name=openai_model, detail=detail
        )
        meta["cache_hit"] = False
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump({"data": data, "meta": meta}, f, indent=2, ensure_ascii=False)
        return data, meta
    except Exception as fallback_err:
        raise RuntimeError(f"Both Gemini and OpenAI fallback failed. Gemini err: {last_error}; OpenAI err: {fallback_err}")
