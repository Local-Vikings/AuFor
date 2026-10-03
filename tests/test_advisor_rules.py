"""Weather and battery rules against the scenarios in bible 19.8 (assert subtopic and hour range)."""

import pytest

from app import advisor, pipeline, solar
from tests.fixtures import mock_data
from tests.fixtures.mock_data import make_mock_weather, make_request


def recommendations(scenario, config="default", days=3, **battery):
    request = make_request(config, days, **battery)
    weather = make_mock_weather(scenario, days)
    result = pipeline.build_forecast(request, weather)
    now = result.hourly.index[0]
    return advisor.recommend(request, result.hourly, result.daily, now.to_pydatetime()), result


def by(cards, subtopic):
    return [card for card in cards if card.subtopic == subtopic]


def test_load_profile_sums_to_12() -> None:
    assert sum(mock_data.LOAD_PROFILE) == pytest.approx(12.0)


@pytest.mark.parametrize("scenario", mock_data.SCENARIOS)
def test_every_scenario_builds_and_runs_the_pipeline(scenario) -> None:
    cards, result = recommendations(scenario)
    assert len(result.hourly) == 72 and len(result.daily) == 3
    assert isinstance(cards, list)


def test_clear_default_gets_a_use_window_around_midday() -> None:
    cards, _ = recommendations("clear")
    use = by(cards, "use")
    # solar noon in Sofia in early October is about 13:30 local time (EEST)
    assert len(use) == 1 and 11 <= use[0].hour.hour <= 14
    assert use[0].kwh_effect > 1.0 and "kWh of surplus" in use[0].reason


def test_cloudy_tomorrow_gets_a_store_card() -> None:
    cards, result = recommendations("cloudy_tomorrow")
    store = by(cards, "store")
    assert len(store) == 1
    assert "Tomorrow's forecast" in store[0].reason and store[0].kwh_effect > 0
    assert result.daily["kwh"].iloc[1] < 0.7 * result.daily["kwh"].mean()


def test_clear_days_get_no_store_card() -> None:
    assert by(recommendations("clear")[0], "store") == []


def test_no_battery_gets_use_only_no_store_or_direct() -> None:
    cards, _ = recommendations("clear", "small")
    assert [card.subtopic for card in cards if card.subtopic != "optimize"] == ["use"]
    cloudy, _ = recommendations("cloudy_tomorrow", "small")
    assert by(cloudy, "store") == [] and by(cloudy, "direct") == []


def test_partly_cloudy_gets_a_midday_warning() -> None:
    cards, _ = recommendations("partly_cloudy")
    warnings = by(cards, "warning")
    assert warnings and all(11 <= card.hour.hour <= 13 for card in warnings)
    assert warnings[0].kwh_effect < 0 and "cloud cover" in warnings[0].reason


def test_clear_days_get_no_warning() -> None:
    assert by(recommendations("clear")[0], "warning") == []


def test_north_roof_gets_an_optimize_card() -> None:
    if solar.ENGINE != "native":
        pytest.skip("native library not built")
    assert len(by(recommendations("clear", "north")[0], "optimize")) == 1


def test_night_only_has_no_use_window_just_a_no_production_message() -> None:
    cards, _ = recommendations("night_only")
    assert by(cards, "direct") == [] and by(cards, "warning") == []
    use = by(cards, "use")
    assert len(use) == 1 and use[0].title == "No solar production forecast"


def test_direct_fires_only_when_the_battery_fills_before_noon() -> None:
    small_battery, _ = recommendations("clear", capacity_kwh=2.0, initial_soc_kwh=1.0)
    direct = by(small_battery, "direct")
    assert len(direct) == 1 and direct[0].hour.hour < 12 and direct[0].kwh_effect >= 1.0
    # the default 10 kWh battery only fills at noon, which is not before noon
    assert by(recommendations("clear")[0], "direct") == []


def test_cards_are_sorted_soonest_first_with_optimize_last() -> None:
    cards, _ = recommendations("cloudy_tomorrow", "north")
    timely = [card for card in cards if card.subtopic != "optimize"]
    assert [c.hour for c in timely] == sorted(c.hour for c in timely)
    if by(cards, "optimize"):
        assert cards[-1].subtopic == "optimize"


def test_the_battery_now_moves_in_the_forecast() -> None:
    _, result = recommendations("clear")
    soc = result.hourly["soc_kwh"]
    assert soc.min() >= 1.0 - 1e-9 and soc.max() <= 10.0 + 1e-9 and soc.nunique() > 10
