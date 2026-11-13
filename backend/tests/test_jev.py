from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from app.config import get_config
from app.models import Code, CodeExemplar
from app.services import jev

FIXTURE = Path(__file__).parent / "fixtures" / "jev_noul_response.json"


def _code(definition: str = "People describe a neighbour helping out.") -> Code:
    return Code(id=7, codebook_id=1, name="Neighbourly help", definition=definition)


def test_instructions_order_and_caps():
    exemplars = [CodeExemplar(code_id=7, text="exemplar one")]
    accepted = [f"accepted {i}" for i in range(5)]  # newest first
    rejected = [f"rejected {i}" for i in range(5)]
    text = jev.build_instructions(_code(), exemplars, accepted, rejected)

    definition_at = text.index("People describe a neighbour helping out.")
    fit_at = text.index("Examples that fit:")
    not_fit_at = text.index("Examples that do not fit:")
    assert definition_at < fit_at < not_fit_at

    fits = text[fit_at:not_fit_at]
    assert fits.index("exemplar one") < fits.index("accepted 0") < fits.index("accepted 1")
    assert "accepted 2" in fits and "accepted 3" not in text and "accepted 4" not in text

    not_fits = text[not_fit_at:]
    assert not_fits.index("rejected 0") < not_fits.index("rejected 1") < not_fits.index("rejected 2")
    assert "rejected 3" not in text


def test_instructions_clip_examples_to_80_words():
    long = " ".join(f"w{i}" for i in range(200))
    text = jev.build_instructions(_code(), [], [long], [])
    assert "w79" in text and "w80" not in text


def test_instructions_without_definition():
    exemplars = [CodeExemplar(code_id=7, text="exemplar one")]
    for definition in ("", None):
        text = jev.build_instructions(_code(definition), exemplars, ["accepted 0"], ["rejected 0"])
        assert "Neighbourly help" in text
        assert "exemplar one" in text and "accepted 0" in text and "rejected 0" in text
        assert "None" not in text


def test_request_has_one_noul_question_per_code():
    highlight = {"id": 42, "text": "My neighbour shovelled my drive."}
    body = jev.build_request(highlight, [(7, "instr seven"), (9, "instr nine")], "jev-latest")

    assert body["model"] == "jev-latest"
    assert body["state"] == {"about": get_config().jev.state_note, "highlight": highlight}
    assert set(body["questions"]) == {"c7", "c9"}
    assert body["questions"]["c7"]["type"] == "noul"
    assert body["questions"]["c7"]["instructions"] == "instr seven"
    assert body["questions"]["c9"]["instructions"] == "instr nine"


def test_parse_noul_reports_missing():
    response = {"answers": {"c9": {"probability": 0.8}}}
    probs, missing = jev.parse_noul(response, [7, 9])
    assert probs == {9: 0.8}
    assert missing == [7]


def test_parse_noul_rejects_out_of_range():
    response = {"answers": {"c7": {"probability": 1.3}, "c9": {"probability": -0.1}}}
    probs, missing = jev.parse_noul(response, [7, 9])
    assert probs == {}
    assert missing == [7, 9]


def test_parse_noul_fallback_fields():
    response = {"answers": {"c1": {"p": 0.2}, "c2": {"answer": 0.6}, "c3": {"answer": "yes"}}}
    probs, missing = jev.parse_noul(response, [1, 2, 3])
    assert probs == {1: 0.2, 2: 0.6}
    assert missing == [3]


def test_parse_noul_live_fixture():
    response = json.loads(FIXTURE.read_text())
    probs, missing = jev.parse_noul(response, [1, 2])
    assert probs == {1: 0.03, 2: 0.01}
    assert missing == []
    assert response["usage"]["input_tokens"] == 422


def _client(statuses: list[int], sleeps: list[float], max_retries: int = 5) -> tuple[jev.HttpJevClient, list]:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        status = statuses[min(len(seen) - 1, len(statuses) - 1)]
        if status == 200:
            return httpx.Response(200, json={"answers": {"c7": {"probability": 0.5}}, "usage": {"input_tokens": 3}})
        return httpx.Response(status, text="nope")

    client = jev.HttpJevClient(
        api_key="test-key",
        url="https://jev.test/v1/systemone",
        max_retries=max_retries,
        backoff_start_seconds=2.0,
        transport=httpx.MockTransport(handler),
        sleep=sleeps.append,
    )
    return client, seen


def test_http_client_retries_then_succeeds():
    sleeps: list[float] = []
    client, seen = _client([429, 503, 200], sleeps)
    result = client.judge({"model": "m"})
    assert result["answers"]["c7"]["probability"] == 0.5
    assert len(seen) == 3
    assert sleeps == [2.0, 4.0]
    assert seen[0].headers["Authorization"] == "Bearer test-key"


def test_http_client_gives_up_after_max_retries():
    sleeps: list[float] = []
    client, seen = _client([500], sleeps, max_retries=3)
    with pytest.raises(jev.JevError, match="500"):
        client.judge({"model": "m"})
    assert len(seen) == 3
    assert sleeps == [2.0, 4.0]


def test_http_client_does_not_retry_client_errors():
    sleeps: list[float] = []
    client, seen = _client([400], sleeps)
    with pytest.raises(jev.JevError, match="400"):
        client.judge({"model": "m"})
    assert len(seen) == 1
    assert sleeps == []
