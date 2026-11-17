from __future__ import annotations

import argparse
import json
import math
import time
from typing import Callable, Protocol, Sequence

import httpx

from ..config import get_config
from ..models import Code, CodeExemplar

EXAMPLE_WORDS = 80
MAX_HUMAN_EXAMPLES = 3
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
# Tried in order. The live probe of 2026-11-13 returned {"type": "noul", "noul": 0.03};
# the rest are fallbacks in case the API renames the field.
PROBABILITY_FIELDS = ("noul", "probability", "p", "answer")


class JevError(RuntimeError):
    """`status` is Jev's HTTP status, or None for a network error or a missing key."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class JevClient(Protocol):
    def judge(self, body: dict) -> dict:
        """The decoded Jev response for one request body."""
        ...


def _clip(text: str) -> str:
    return " ".join(text.split()[:EXAMPLE_WORDS])


def build_instructions(
    code: Code,
    exemplars: Sequence[CodeExemplar],
    accepted: Sequence[str],
    rejected: Sequence[str],
    include: Sequence[str] = (),
    exclude: Sequence[str] = (),
    name: str | None = None,
    definition: str | None = None,
    starred: Sequence[str] = (),
) -> str:
    """Noul instructions for one code. `accepted` and `rejected` are newest first. `name` and
    `definition`, when given, replace the code's own (to score a proposal). `starred` texts are
    pinned "fits" examples, not limited by MAX_HUMAN_EXAMPLES."""
    name = code.name if name is None else name
    definition = ((code.definition if definition is None else definition) or "").strip()
    lines = [f"Does this highlight fit the code \"{name}\"?"]
    if definition:
        lines.append(f"Definition: {definition}")
    if include:
        lines.append("Counts when:")
        lines.extend(f"- {r}" for r in include)
    if exclude:
        lines.append("Does not count when:")
        lines.extend(f"- {r}" for r in exclude)

    fits = (
        [_clip(e.text) for e in exemplars]
        + [_clip(t) for t in starred]
        + [_clip(t) for t in accepted[:MAX_HUMAN_EXAMPLES]]
    )
    not_fits = [_clip(t) for t in rejected[:MAX_HUMAN_EXAMPLES]]
    if fits:
        lines.append("Examples that fit:")
        lines.extend(f"- {t}" for t in fits)
    if not_fits:
        lines.append("Examples that do not fit:")
        lines.extend(f"- {t}" for t in not_fits)
    return "\n".join(lines)


def question_key(code_id: int) -> str:
    return f"c{code_id}"


def build_request(highlight: dict, codes: Sequence[tuple[int, str]], model: str) -> dict:
    """One Noul question per (code_id, instructions) pair, all against one highlight."""
    return build_keyed_request(
        highlight, [(question_key(code_id), instructions) for code_id, instructions in codes], model
    )


def build_keyed_request(highlight: dict, questions: Sequence[tuple[str, str]], model: str) -> dict:
    """One Noul question per (key, instructions) pair, all against one highlight."""
    state = {
        "about": get_config().jev.state_note,
        "highlight": {"id": highlight["id"], "text": highlight["text"]},
    }
    return {
        "model": model,
        "state": state,
        "questions": {key: {"type": "noul", "instructions": instructions} for key, instructions in questions},
    }


def _probability(answer: object) -> float | None:
    if not isinstance(answer, dict):
        return None
    for field in PROBABILITY_FIELDS:
        value = answer.get(field)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        value = float(value)
        if math.isfinite(value) and 0.0 <= value <= 1.0:
            return value
        return None
    return None


def parse_noul(response: dict, code_ids: Sequence[int]) -> tuple[dict[int, float], list[int]]:
    """Probabilities by code id, plus ids with no usable answer (absent or out of range)."""
    by_key, missing = parse_keyed_noul(response, [question_key(c) for c in code_ids])
    probabilities = {code_id: by_key[question_key(code_id)] for code_id in code_ids if question_key(code_id) in by_key}
    return probabilities, [code_id for code_id in code_ids if question_key(code_id) in missing]


def parse_keyed_noul(response: dict, keys: Sequence[str]) -> tuple[dict[str, float], list[str]]:
    """Probabilities by question key, plus keys with no usable answer (absent or out of range)."""
    answers = response.get("answers") or {}
    probabilities: dict[str, float] = {}
    missing: list[str] = []
    for key in keys:
        p = _probability(answers.get(key))
        if p is None:
            missing.append(key)
        else:
            probabilities[key] = p
    return probabilities, missing


class HttpJevClient:
    """Paid. Tests pass an httpx.MockTransport."""

    def __init__(
        self,
        api_key: str | None = None,
        url: str | None = None,
        max_attempts: int | None = None,
        backoff_start_seconds: float | None = None,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        timeout: float = 60.0,
    ) -> None:
        config = get_config()
        self._api_key = api_key or config.typesafe_api_key()
        if not self._api_key:
            raise JevError("TYPESAFE_API_KEY is not set in the environment, Tagging/.env or Codestormer/.env.")
        self._url = url or config.jev.url
        self._max_attempts = max_attempts if max_attempts is not None else config.jev.max_attempts
        self._backoff = (
            backoff_start_seconds if backoff_start_seconds is not None else config.jev.backoff_start_seconds
        )
        self._sleep = sleep
        self._http = httpx.Client(transport=transport, timeout=timeout)

    def judge(self, body: dict) -> dict:
        delay = self._backoff
        for attempt in range(self._max_attempts):
            last = attempt == self._max_attempts - 1
            try:
                resp = self._http.post(self._url, json=body, headers={"Authorization": f"Bearer {self._api_key}"})
            except httpx.TransportError as exc:
                if last:
                    raise JevError(f"Jev request failed: {type(exc).__name__}: {exc}") from exc
            else:
                if resp.status_code == 200:
                    return resp.json()
                if resp.status_code not in RETRY_STATUSES or last:
                    raise JevError(f"Jev returned {resp.status_code}: {resp.text[:300]}", resp.status_code)
            self._sleep(delay)
            delay *= 2
        raise JevError("Jev was not called: max_attempts is below 1.")


def _probe() -> None:
    from sqlalchemy import select

    from ..db import CorticoSession
    from ..models_cortico import Highlight

    with CorticoSession() as db_c:
        row = db_c.execute(select(Highlight).order_by(Highlight.id).limit(1)).scalar_one()
        highlight = {"id": row.id, "text": row.text}

    codes = [
        Code(id=1, codebook_id=0, name="Feeling unheard by officials",
             definition="The speaker says people in power do not listen to them or their community."),
        Code(id=2, codebook_id=0, name="Cooking and recipes",
             definition="The speaker talks about preparing food or a specific recipe."),
    ]
    config = get_config()
    body = build_request(
        highlight, [(c.id, build_instructions(c, [], [], [])) for c in codes], config.jev.model
    )
    response = HttpJevClient().judge(body)
    print(json.dumps({"answers": response.get("answers"), "usage": response.get("usage")}, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Jev client")
    parser.add_argument("--probe", action="store_true", help="Send one paid request and print answers and usage.")
    args = parser.parse_args()
    if args.probe:
        _probe()
    else:
        parser.print_help()
