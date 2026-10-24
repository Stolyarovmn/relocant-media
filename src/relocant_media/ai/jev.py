"""Optional TypeSafe Jev signals. Never authoritative for editorial decisions.

JEV_ENABLED=false is a hard bypass: no credentials, client, or HTTP calls.
Only public, official-source excerpts are sent. API failures never block the pipeline.
"""

from __future__ import annotations

import math
import os
import time
from typing import Any

import httpx

ENDPOINT = "https://api.typesafe.ai/v1/systemone"
MODEL = "jev-latest"
MAX_RANK_ITEMS = 5
MAX_DUPLICATE_PAIRS = 8
MAX_EXCERPT_CHARS = 600
REQUEST_TIMEOUT_SECONDS = 3.0


def jev_enabled() -> bool:
    """The single feature switch for all Jev integration points."""
    return os.getenv("JEV_ENABLED", "false").strip().casefold() in {
        "1", "true", "yes", "on",
    }


def _noul(instructions: str) -> dict[str, str]:
    return {"type": "noul", "instructions": instructions}


def _request(
    state: dict[str, Any],
    questions: dict[str, dict[str, str]],
) -> dict[str, Any]:
    if not jev_enabled():
        return {"status": "disabled"}
    key = os.getenv("TYPESAFE_API_KEY", "").strip()
    if not key:
        return {"status": "missing_api_key"}

    started = time.monotonic()
    try:
        with httpx.Client(timeout=REQUEST_TIMEOUT_SECONDS, trust_env=False) as client:
            response = client.post(
                ENDPOINT,
                headers={"Authorization": f"Bearer {key}"},
                json={"model": MODEL, "state": state, "questions": questions},
            )
            response.raise_for_status()
            payload = response.json()

        if not isinstance(payload, dict) or not isinstance(payload.get("answers"), dict):
            raise ValueError("invalid TypeSafe response")
        answers = payload["answers"]
        probabilities: dict[str, float] = {}
        for question_id in questions:
            answer = answers.get(question_id)
            if not isinstance(answer, dict) or answer.get("type") != "noul":
                raise ValueError("missing TypeSafe noul")
            value = answer.get("noul")
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or not 0.0 <= value <= 1.0
            ):
                raise ValueError("invalid TypeSafe probability")
            probabilities[question_id] = round(float(value), 6)

        usage = payload.get("usage")
        if not isinstance(usage, dict):
            usage = {}
        model = payload.get("model")
        return {
            "status": "ok",
            "model": model if isinstance(model, str) else MODEL,
            "duration_ms": round((time.monotonic() - started) * 1000),
            "input_tokens": (
                usage.get("input_tokens")
                if type(usage.get("input_tokens")) is int else None
            ),
            "output_tokens": (
                usage.get("output_tokens")
                if type(usage.get("output_tokens")) is int else None
            ),
            "probabilities": probabilities,
        }
    except (httpx.HTTPError, ValueError, TypeError) as error:
        # Never persist exception messages: HTTP exceptions may contain sensitive data.
        return {
            "status": "unavailable",
            "error_type": type(error).__name__,
            "duration_ms": round((time.monotonic() - started) * 1000),
        }


def _public_excerpt(title: str, body: str) -> dict[str, str]:
    return {
        "title": title[:MAX_EXCERPT_CHARS],
        "body": body[:MAX_EXCERPT_CHARS],
    }


def smoke_shadow() -> dict[str, Any]:
    """Check the real adapter with the public TypeSafe API example, without newsroom data."""
    result = _request(
        {"message": "I was charged twice for my subscription this month. "
                    "Please refund the extra charge."},
        {"billing": _noul("Is this message about billing?")},
    )
    return {
        "mode": "shadow_only",
        "decision_effect": "none",
        "fixture": "typesafe-system-one-billing-example-v1",
        **result,
    }


def rank_shadow(candidates: list[dict[str, Any]], *, city: str) -> dict[str, Any]:
    """Observe local relevance and practical value; never reorder or drop candidates."""
    if not jev_enabled():
        return {"mode": "shadow_only", "decision_effect": "none", "status": "disabled"}

    selected: list[dict[str, Any]] = []
    for candidate in candidates:
        source = candidate.get("source")
        evidence = candidate.get("evidence")
        raw_id = candidate.get("raw_item_id")
        if (
            not isinstance(source, dict)
            or source.get("ownership") != "official"
            or not isinstance(evidence, dict)
            or type(raw_id) is not int
            or not isinstance(evidence.get("title"), str)
            or not isinstance(evidence.get("body"), str)
        ):
            continue
        selected.append({
            "raw_item_id": raw_id,
            **_public_excerpt(evidence["title"], evidence["body"]),
        })
        if len(selected) == MAX_RANK_ITEMS:
            break

    summary: dict[str, Any] = {
        "mode": "shadow_only",
        "decision_effect": "none",
        "evaluated": len(selected),
    }
    if not selected:
        return {**summary, "status": "no_public_official_candidates"}

    state = {
        "city": city,
        "candidates": [
            {"title": item["title"], "body": item["body"]}
            for item in selected
        ],
    }
    questions: dict[str, dict[str, str]] = {}
    for index in range(len(selected)):
        questions[f"local_{index}"] = _noul(
            f"Is candidates[{index}] a concrete, current or upcoming local news item "
            "relevant to residents of the city named in state.city? "
            "A generic advertisement or an unrelated city is not locally relevant."
        )
        questions[f"practical_{index}"] = _noul(
            f"Does candidates[{index}] offer residents actionable information "
            "(such as a specific service change, deadline, place, route or event)? "
            "Generic promotional copy without actionable details does not count."
        )

    result = _request(state, questions)
    if result["status"] != "ok":
        return {**summary, **result}
    probabilities = result.pop("probabilities")
    return {
        **summary,
        **result,
        "signals": [
            {
                "raw_item_id": item["raw_item_id"],
                "local_relevance_probability": probabilities[f"local_{index}"],
                "practical_value_probability": probabilities[f"practical_{index}"],
            }
            for index, item in enumerate(selected)
        ],
    }


def duplicate_shadow(pairs: list[dict[str, Any]], *, city: str) -> dict[str, Any]:
    """Judge a bounded lexical shortlist; never merge, discard, or block events."""
    if not jev_enabled():
        return {"mode": "shadow_only", "decision_effect": "none", "status": "disabled"}

    selected = pairs[:MAX_DUPLICATE_PAIRS]
    summary: dict[str, Any] = {
        "mode": "shadow_only",
        "decision_effect": "none",
        "evaluated": len(selected),
    }
    if not selected:
        return {**summary, "status": "no_public_official_pairs"}

    state = {
        "city": city,
        "pairs": [
            {
                "left": _public_excerpt(pair["left_title"], pair["left_body"]),
                "right": _public_excerpt(pair["right_title"], pair["right_body"]),
            }
            for pair in selected
        ],
    }
    questions = {
        f"same_{index}": _noul(
            f"Do pairs[{index}].left and pairs[{index}].right report the SAME "
            "specific real-world event, with compatible dates, places, entities and "
            "actions? Related topics, annual repeats, opposing actions or different "
            "dates are NOT the same event. Answer from the text only, not as fact proof."
        )
        for index in range(len(selected))
    }
    result = _request(state, questions)
    if result["status"] != "ok":
        return {**summary, **result}
    probabilities = result.pop("probabilities")
    return {
        **summary,
        **result,
        "signals": [
            {
                "left_raw_item_id": pair["left_raw_item_id"],
                "right_raw_item_id": pair["right_raw_item_id"],
                "same_event_probability": probabilities[f"same_{index}"],
            }
            for index, pair in enumerate(selected)
        ],
    }
