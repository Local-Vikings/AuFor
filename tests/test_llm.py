"""app/llm.py and POST /api/explain: the AI explanation never fails the page (bible 15, T54)."""

import json
import re

import httpx
import pytest
from fastapi.testclient import TestClient

from app import llm, solar
from main import app
from tests.test_pipeline import BODY

client = TestClient(app)


def sentences(text: str) -> list[str]:
    return re.split(r"(?<=[.!?])\s+(?=[A-Z])", text.strip())


@pytest.fixture
def llm_on(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("app.config.LLM_ENABLED", True)
    monkeypatch.setattr("app.config.LLM_API_KEY", "test-key")


def summary_of(body=None) -> dict:
    """The summary the server would send to the model, read back through its id."""
    data = client.post("/api/forecast", json=body or BODY).json()
    assert data["explain_id"], "the LLM must be switched on for this helper"
    return llm.recall(data["explain_id"])


# ---- the template: always available and made only of the data ----

def test_forecast_always_carries_a_template_explanation() -> None:
    data = client.post("/api/forecast", json=BODY).json()
    assert data["explanation_source"] == "template" and data["explain_id"] is None  # LLM is off by default
    assert "kWh" in data["explanation"] and 3 <= len(sentences(data["explanation"])) <= 5


def test_every_number_in_the_template_comes_from_the_data(llm_on) -> None:
    summary = summary_of()
    assert llm.is_grounded(llm.template_explanation(summary), summary)


def test_template_describes_clouds_when_they_matter_and_stays_quiet_when_they_do_not(llm_on) -> None:
    summary = summary_of()
    clear = json.loads(json.dumps(summary))
    clear["energy"].update(lost_to_clouds_pct=1, average_cloud_cover_pct=5, lost_to_clouds_kwh=0.4)
    assert "barely reduce" in llm.template_explanation(clear)
    cloudy = json.loads(json.dumps(summary))
    cloudy["energy"].update(lost_to_clouds_pct=42, average_cloud_cover_pct=88, lost_to_clouds_kwh=8.3)
    text = llm.template_explanation(cloudy)
    assert "mostly cloudy" in text and "8.3 kWh" in text and "42%" in text


@pytest.mark.skipif(solar.ENGINE != "native", reason="the Optimize rule needs the native library")
def test_template_names_the_timely_action_and_the_orientation_fix_for_a_bad_roof(llm_on) -> None:
    north = {**BODY, "panel": {**BODY["panel"], "azimuth": 0}}
    summary = summary_of(north)
    assert {r["type"] for r in summary["recommendations"]} >= {"use", "optimize"}
    text = llm.template_explanation(summary)
    assert "Most useful next steps:" in text and "Run flexible loads" in text and "Turn panels to tilt" in text
    assert "a year with clear skies" in text and 3 <= len(sentences(text)) <= 5


def test_template_works_with_no_recommendations_and_for_monthly_views(llm_on) -> None:
    summary = summary_of({**BODY, "days": 90, "resolution": "monthly"})
    assert "month" in llm.template_explanation(summary) and summary["period"]["days"] == 90
    summary["recommendations"] = []
    assert "Most useful next step" not in llm.template_explanation(summary)


def test_summary_is_small_and_matches_the_response(llm_on) -> None:
    data = client.post("/api/forecast", json=BODY).json()
    summary = llm.recall(data["explain_id"])
    assert summary["energy"]["total_kwh"] == pytest.approx(sum(d["kwh"] for d in data["daily"]), abs=0.06)
    assert summary["system"] == {"solar_kwp": 4.0, "battery_kwh": 10, "daily_load_kwh": 12}
    assert len(json.dumps(summary)) < 3000 and len(summary["recommendations"]) <= llm.MAX_RECOMMENDATIONS


# ---- the grounding check ----

def test_numbers_are_accepted_within_rounding_and_rejected_when_invented() -> None:
    summary = {"energy": {"total_kwh": 57.13, "best": {"label": "2026-10-03", "kwh": 19.1}}}
    assert llm.is_grounded("About 57.1 kWh, best day 2026-10-03 with 19,1 kWh.", summary)
    assert llm.is_grounded("It produces 57 kWh.", summary)
    assert not llm.is_grounded("It produces 90 kWh.", summary)
    assert not llm.is_grounded("Saves 12.5 euro.", summary)
    assert llm.is_grounded("No numbers at all here.", summary)


# ---- explain(): fails soft in every way ----

def test_without_the_switch_or_the_key_the_model_is_never_called(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(_summary):
        raise AssertionError("the model must not be called")

    monkeypatch.setattr(llm, "_call_model", boom)
    summary = {"x": 1}
    monkeypatch.setattr("app.config.LLM_ENABLED", False)
    monkeypatch.setattr("app.config.LLM_API_KEY", "key")
    assert not llm.available()
    monkeypatch.setattr("app.config.LLM_ENABLED", True)
    monkeypatch.setattr("app.config.LLM_API_KEY", "")
    assert not llm.available()


def test_a_grounded_answer_is_used(llm_on, monkeypatch: pytest.MonkeyPatch) -> None:
    summary = summary_of()
    total = summary["energy"]["total_kwh"]
    monkeypatch.setattr(llm, "_call_model", lambda s: f"Your system should make {total} kWh. Run big loads at midday.")
    result = llm.explain(summary)
    assert result.source == "llm" and str(total) in result.text


@pytest.mark.parametrize("failure", [
    httpx.TimeoutException("slow"), httpx.ConnectError("no network"), RuntimeError("anything"),
    KeyError("content"), ValueError("bad json"), httpx.HTTPStatusError("429", request=None, response=None),
])
def test_any_failure_returns_the_template_and_never_raises(llm_on, monkeypatch: pytest.MonkeyPatch, failure) -> None:
    summary = summary_of()

    def broken(_summary):
        raise failure

    monkeypatch.setattr(llm, "_call_model", broken)
    result = llm.explain(summary)
    assert result.source == "template" and result.text == llm.template_explanation(summary)


@pytest.mark.parametrize("answer", ["", "   ", "You will make 9999 kWh and save 500 euro."])
def test_empty_or_invented_answers_fall_back_to_the_template(llm_on, monkeypatch: pytest.MonkeyPatch, answer) -> None:
    summary = summary_of()
    monkeypatch.setattr(llm, "_call_model", lambda s: answer)
    assert llm.explain(summary).source == "template"


def test_the_request_sent_to_the_model(llm_on, monkeypatch: pytest.MonkeyPatch) -> None:
    sent = {}

    class FakeResponse:
        def raise_for_status(self): return None
        def json(self): return {"content": [{"type": "text", "text": "  Fine.  "}]}

    class FakeClient:
        def __init__(self, *, timeout): sent["timeout"] = timeout
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def post(self, url, headers, json): sent.update(url=url, headers=headers, body=json); return FakeResponse()

    monkeypatch.setattr(httpx, "Client", FakeClient)
    summary = {"energy": {"total_kwh": 5}}
    assert llm._call_model(summary) == "Fine."
    assert sent["url"] == llm.ANTHROPIC_URL and sent["timeout"] == 10.0
    assert sent["headers"]["x-api-key"] == "test-key" and "anthropic-version" in sent["headers"]
    assert "Do not invent numbers. Use only the data given." in sent["body"]["system"]
    assert json.loads(sent["body"]["messages"][0]["content"]) == summary


# ---- remembering summaries ----

def test_remember_and_recall_with_expiry_and_a_size_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    key = llm.remember({"a": 1})
    assert llm.recall(key) == {"a": 1} and llm.recall("nope") is None
    monkeypatch.setattr("app.config.EXPLAIN_CACHE_SIZE", 3)
    keys = [llm.remember({"n": i}) for i in range(5)]
    assert llm.recall(keys[0]) is None and llm.recall(keys[-1]) == {"n": 4}
    monkeypatch.setattr("app.config.EXPLAIN_CACHE_SECONDS", -1.0)
    assert llm.recall(keys[-1]) is None


# ---- the endpoint ----

def test_explain_endpoint_returns_the_ai_text_for_a_recent_forecast(llm_on, monkeypatch: pytest.MonkeyPatch) -> None:
    forecast = client.post("/api/forecast", json=BODY).json()
    total = llm.recall(forecast["explain_id"])["energy"]["total_kwh"]
    monkeypatch.setattr(llm, "_call_model", lambda s: f"About {total} kWh are expected.")
    answer = client.post("/api/explain", json={"explain_id": forecast["explain_id"]})
    assert answer.status_code == 200 and answer.json()["explanation_source"] == "llm"
    assert str(total) in answer.json()["explanation"]


def test_explain_endpoint_falls_back_with_200_when_the_model_fails(llm_on, monkeypatch: pytest.MonkeyPatch) -> None:
    forecast = client.post("/api/forecast", json=BODY).json()
    monkeypatch.setattr(llm, "_call_model", lambda s: (_ for _ in ()).throw(httpx.TimeoutException("slow")))
    answer = client.post("/api/explain", json={"explain_id": forecast["explain_id"]})
    assert answer.status_code == 200 and answer.json()["explanation_source"] == "template"
    assert answer.json()["explanation"] == forecast["explanation"]


def test_explain_endpoint_rejects_unknown_ids_and_bad_bodies() -> None:
    assert client.post("/api/explain", json={"explain_id": "nope"}).status_code == 404
    assert client.post("/api/explain", json={}).status_code == 422
