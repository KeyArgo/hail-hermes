"""The weather fast path: one GET, one sentence, no agent turn."""

import pytest

from hail_hermes import weather
from hail_hermes.settings import update_settings

from conftest import Client, running_hub

FORECAST = {
    "current": {"temperature_2m": 11.6, "precipitation": 0.0, "weather_code": 3},
    "daily": {
        "temperature_2m_max": [17.9],
        "temperature_2m_min": [5.2],
        "precipitation_probability_max": [72],
    },
}


def _stub(url, timeout):
    return FORECAST


def _broken(url, timeout):
    raise OSError("no route to host")


def test_recognises_the_question_people_actually_ask():
    assert weather.is_weather_question("will it rain today?")
    assert weather.is_weather_question("Do I need an umbrella")
    assert not weather.is_weather_question("summarise my repo")


def test_one_sentence_with_temperatures_and_a_rain_line():
    text = weather.describe_weather(39.7, -105.0, "Denver", fetcher=_stub)
    assert text.startswith("Denver: 12 degrees, cloudy")
    assert "high 18" in text and "low 5" in text
    assert "rain likely" in text


def test_a_failed_fetch_returns_nothing_so_the_agent_can_answer():
    # Better to hand the question to the agent than to answer a real question with an apology
    # about the weather service.
    assert weather.describe_weather(39.7, -105.0, "Denver", fetcher=_broken) is None
    assert weather.describe_weather(39.7, -105.0, "Denver", fetcher=lambda u, t: ["not", "a", "dict"]) is None


async def test_hub_answers_weather_without_waking_the_agent(state_dir, monkeypatch):
    update_settings(state_dir, {"home_place": {"label": "Denver", "lat": 39.7, "lon": -105.0}})
    monkeypatch.setattr(
        weather, "describe_weather",
        lambda lat, lon, label="", **kw: "Denver: 12 degrees, cloudy, rain likely.",
    )
    async with running_hub(state_dir) as hub, Client.open(hub) as c:
        await c.pair()
        await c.send("text", text="will it rain today?")
        said = None
        for _ in range(8):  # the user echo arrives first
            msg = await c.recv()
            if msg.get("type") == "transcript" and msg.get("role") == "assistant":
                said = msg
                break
        assert said is not None, "the hub never spoke the forecast"
        assert "rain likely" in said["text"]
        assert hub.agent.calls == [], "the fast path must not wake the agent"


def test_detects_the_town_from_the_network():
    payload = {"success": True, "city": "Colorado Springs", "region": "Colorado",
               "latitude": 38.83, "longitude": -104.82}
    place = weather.detect_home_place(fetcher=lambda url, timeout: payload)
    assert place["label"] == "Colorado Springs"
    assert round(place["lat"], 1) == 38.8 and place["source"] == "ip"


def test_detection_failing_is_not_an_error():
    def broken(url, timeout):
        raise OSError("no route")

    assert weather.detect_home_place(fetcher=broken) is None
    assert weather.detect_home_place(fetcher=lambda u, t: {"success": False}) is None


async def test_first_weather_question_learns_the_town(state_dir, monkeypatch):
    update_settings(state_dir, {"home_place": {}})
    monkeypatch.setattr(
        weather, "detect_home_place",
        lambda **kw: {"label": "Colorado Springs", "lat": 38.83, "lon": -104.82, "source": "ip"},
    )
    monkeypatch.setattr(
        weather, "describe_weather",
        lambda lat, lon, label="", **kw: f"{label}: 5 degrees, clear.",
    )
    async with running_hub(state_dir) as hub, Client.open(hub) as c:
        await c.pair()
        await c.send("text", text="what's the weather?")
        said = None
        for _ in range(8):
            msg = await c.recv()
            if msg.get("type") == "transcript" and msg.get("role") == "assistant":
                said = msg
                break
        assert said is not None and "Colorado Springs" in said["text"]
        from hail_hermes.settings import read_settings

        assert read_settings(state_dir)["home_place"]["label"] == "Colorado Springs"


def test_model_questions_are_not_weather():
    assert not weather.is_weather_question("set the model temperature to 0.2")
    assert not weather.is_weather_question("what did the training run do")
    assert not weather.is_weather_question("check the constraint on my brain")  # 'rain' inside words


async def test_a_broken_forecast_falls_through_to_the_agent(state_dir, monkeypatch):
    update_settings(state_dir, {"home_place": {"label": "Denver", "lat": 39.7, "lon": -105.0}})
    monkeypatch.setattr(weather, "describe_weather", lambda *a, **k: None)
    async with running_hub(state_dir) as hub, Client.open(hub) as c:
        await c.pair()
        await c.send("text", text="what is the weather?")
        for _ in range(10):
            msg = await c.recv()
            if msg.get("type") == "transcript" and msg.get("role") == "assistant":
                break
        assert hub.agent.calls, "with no forecast the agent must still be asked"


# --- a city named in the question ---------------------------------------------------------

GEOCODE = {
    "results": [
        {"name": "Denver", "latitude": 39.7392, "longitude": -104.9903,
         "admin1": "Colorado", "country": "United States"},
    ]
}


def test_extracts_a_named_city_and_ignores_time_words():
    assert weather.extract_place("what is the weather in Denver right now?") == "Denver"
    assert weather.extract_place("forecast for Colorado Springs") == "Colorado Springs"
    assert weather.extract_place("Denver weather") == "Denver"
    assert weather.extract_place("what's the weather?") is None
    assert weather.extract_place("will it rain in the afternoon?") is None
    assert weather.extract_place("what's the weather at home?") is None


def test_geocodes_the_named_city_with_a_short_timeout():
    seen = {}

    def fetcher(url, timeout):
        seen["url"] = url
        seen["timeout"] = timeout
        return GEOCODE

    place = weather.geocode_place("Denver", fetcher=fetcher)
    assert place["label"] == "Denver"
    assert round(place["lat"], 1) == 39.7 and round(place["lon"], 1) == -105.0
    assert "name=Denver" in seen["url"]
    assert seen["timeout"] <= weather.GEOCODE_TIMEOUT <= 2.0


def test_an_unknown_or_slow_geocode_returns_nothing():
    assert weather.geocode_place("Xyzzyville", fetcher=lambda u, t: {"results": []}) is None
    assert weather.geocode_place("Xyzzyville", fetcher=_broken) is None
    assert weather.geocode_place("Xyzzyville", fetcher=lambda u, t: ["not", "a", "dict"]) is None


async def test_hub_answers_for_the_city_the_owner_names(state_dir, monkeypatch):
    # The saved home town is Colorado Springs; the question names Denver. The spoken sentence
    # must name Denver, not the home town.
    update_settings(state_dir, {"home_place": {"label": "Colorado Springs", "lat": 38.83, "lon": -104.82}})
    asked = []

    def fake_geocode(name, **kw):
        asked.append(name)
        return {"label": name, "lat": 39.74, "lon": -104.99, "source": "geocode"}

    monkeypatch.setattr(weather, "geocode_place", fake_geocode)
    monkeypatch.setattr(
        weather, "describe_weather",
        lambda lat, lon, label="", **kw: f"{label}: 9 degrees, clear.",
    )
    async with running_hub(state_dir) as hub, Client.open(hub) as c:
        await c.pair()
        await c.send("text", text="what is the weather in Denver right now?")
        said = None
        for _ in range(8):
            msg = await c.recv()
            if msg.get("type") == "transcript" and msg.get("role") == "assistant":
                said = msg
                break
        assert said is not None, "the hub never spoke the forecast"
        assert "Denver" in said["text"], said["text"]
        assert "Colorado Springs" not in said["text"]
        assert asked == ["Denver"], asked
        assert hub.agent.calls == [], "the fast path must not wake the agent"


async def test_an_unknown_town_falls_through_to_the_agent(state_dir, monkeypatch):
    # A home town is set, but the question names a town the geocoder cannot find. Answering
    # from the home town would be wrong, so the agent gets the question.
    update_settings(state_dir, {"home_place": {"label": "Denver", "lat": 39.7, "lon": -105.0}})
    monkeypatch.setattr(weather, "geocode_place", lambda name, **kw: None)
    monkeypatch.setattr(weather, "describe_weather", lambda *a, **k: "should not be used")
    async with running_hub(state_dir) as hub, Client.open(hub) as c:
        await c.pair()
        await c.send("text", text="what is the weather in Xyzzyville?")
        for _ in range(10):
            msg = await c.recv()
            if msg.get("type") == "transcript" and msg.get("role") == "assistant":
                break
        assert hub.agent.calls, "an unknown town must not be answered from the home town"


def test_weather_model_is_still_not_a_weather_question():
    assert not weather.is_weather_question("what is the weather model?")
    assert weather.extract_place("what is the weather model?") is None


def test_a_weather_word_alone_does_not_hijack_the_question():
    """A topic word is not a question.

    Live: "Say one short sentence about snow." was answered with the local forecast instead of the
    agent's sentence, and the same happened to "I like the snow." and "Tell me about weather
    balloons". The fast path may only take sentences that ask about conditions.
    """
    from hail_hermes.weather import is_weather_question

    for sentence in (
        "Say one short sentence about snow.",
        "Is snow cold?",
        "I like the snow.",
        "Tell me about weather balloons",
    ):
        assert is_weather_question(sentence) is False, sentence

    for sentence in (
        "What's the weather today?",
        "Will it snow tomorrow?",
        "What is the weather in Denver?",
        "Should I bring an umbrella today?",
    ):
        assert is_weather_question(sentence) is True, sentence
