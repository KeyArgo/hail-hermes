"""Weather answered directly by the hub, without waking the agent.

"Why is the watch good for the weather?" — because it is on your wrist. A spoken agent turn costs
seconds and a model call; a forecast is one HTTP GET. This module recognises the question, fetches
Open-Meteo (free, no account, no key) and returns one plain sentence the hub can speak.
"""

from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request
from typing import Any

# Whole words only: a substring match made "training run", "brain", "constraint" and
# "set the model temperature" all look like weather questions, which hijacked real work.
_WEATHER_WORDS = (
    r"(?:weather|forecast|rain|raining|rainy|snow|snowing|sunny|umbrella|windy|temperature outside)"
)
_WEATHER_RE = re.compile(rf"\b{_WEATHER_WORDS}\b", re.IGNORECASE)
# A weather word alone is not a weather question: "say a sentence about snow", "I like the snow" and
# "tell me about weather balloons" were all answered with the local forecast, swallowing the real
# question. The sentence must also be *asking* about conditions now or soon.
_ASKING_RE = re.compile(
    r"\b(?:what|whats|what's|how|will|is it|are we|should i|do i need|going to be|"
    r"today|tomorrow|tonight|right now|currently|outside|this week|this weekend)\b",
    re.IGNORECASE,
)
# Anything really about the agent itself must never be answered by a forecast.
_NOT_WEATHER_RE = re.compile(
    r"\b(model|models|parameter|parameters|settings?|temperature to|training|train|brain|dataset)\b",
    re.IGNORECASE,
)

# "what is the weather in Denver" / "forecast for Colorado Springs". A named town must beat the
# saved home town, but only when the sentence really names one.
_PLACE_AFTER_RE = re.compile(
    rf"\b{_WEATHER_WORDS}\b(?:\s+[A-Za-z']+){{0,3}}?\s+(?:in|for|at)\s+(?P<place>[^?.!,;]+)",
    re.IGNORECASE,
)
# "Denver weather" / "Denver weather like today?" — the town has to come first.
_PLACE_BEFORE_RE = re.compile(
    rf"^(?:what(?:'s| is)?\s+)?(?:the\s+)?(?P<place>[A-Za-z][A-Za-z' .-]{{0,39}}?)\s+"
    rf"(?:weather|forecast)\b"
    rf"(?:\s+(?:like|today|now|right\s+now|this\s+(?:morning|afternoon|evening|week)))*"
    rf"\s*[?.!]*$",
    re.IGNORECASE,
)
# Words that say when or roughly where, not which town: "will it rain in the afternoon" must
# not turn into a failed geocode for a town called "afternoon".
_PLACE_STOPWORDS = {
    "a", "an", "the", "this", "right", "now", "today", "tonight", "tomorrow", "currently",
    "later", "morning", "afternoon", "evening", "night", "week", "weekend", "moment",
    "here", "there", "outside", "home", "house", "my", "our", "area", "city", "town",
    "state", "county", "region", "neighborhood", "please", "weather", "forecast", "rain",
    "raining", "snow", "snowing", "sun", "wind", "umbrella", "temperature",
}
_PLACE_TAIL_RE = re.compile(
    r"\s+(?:right\s+now|now|today|tonight|tomorrow|currently|please|"
    r"this\s+(?:morning|afternoon|evening|night|week|weekend)|at\s+the\s+moment)\b.*$",
    re.IGNORECASE,
)

ENDPOINT = "https://api.open-meteo.com/v1/forecast"
# Free, keyless, and it answered Colorado Springs correctly on this connection.
GEO_ENDPOINT = "https://ipwho.is/"
# Open-Meteo's keyless town lookup, the same client family as the forecast above.
GEOCODE_ENDPOINT = "https://geocoding-api.open-meteo.com/v1/search"
# Short on purpose: a weather question must never wait long enough to feel like a hang.
GEOCODE_TIMEOUT = 2.0

# WMO weather codes, collapsed to what a person actually needs to hear.
_CODES = {
    0: "clear", 1: "mostly clear", 2: "partly cloudy", 3: "cloudy",
    45: "foggy", 48: "freezing fog",
    51: "light drizzle", 53: "drizzle", 55: "heavy drizzle",
    61: "light rain", 63: "rain", 65: "heavy rain",
    66: "freezing rain", 67: "freezing rain",
    71: "light snow", 73: "snow", 75: "heavy snow", 77: "snow grains",
    80: "rain showers", 81: "rain showers", 82: "heavy showers",
    85: "snow showers", 86: "heavy snow showers",
    95: "thunderstorms", 96: "thunderstorms with hail", 99: "thunderstorms with hail",
}


def is_weather_question(text: str) -> bool:
    text = (text or "").strip()
    if not text:
        return False
    if _NOT_WEATHER_RE.search(text):
        return False
    if not _WEATHER_RE.search(text):
        return False
    return bool(_ASKING_RE.search(text))


def extract_place(text: str) -> str | None:
    """The town the owner named, or ``None`` when the question does not name one.

    Deliberately conservative: a wrong extraction would answer "what's the weather?" with the
    forecast for "the afternoon". A captured phrase that is only a time word, a pronoun or a
    weather word counts as unnamed, and the caller keeps using the saved home town.
    """
    text = (text or "").strip()
    if not text:
        return None
    for pattern in (_PLACE_AFTER_RE, _PLACE_BEFORE_RE):
        match = pattern.search(text)
        if match:
            place = _clean_place(match.group("place"))
            if place:
                return place
    return None


def _clean_place(raw: str) -> str | None:
    place = re.sub(r"\s+", " ", raw).strip(" \t\"'.,!?;:")
    place = re.sub(r"^(?:the|a|an)\s+", "", place, flags=re.IGNORECASE)
    place = _PLACE_TAIL_RE.sub("", place)
    place = re.sub(r"'s$", "", place.strip(), flags=re.IGNORECASE)
    place = place.strip(" \t\"'.,!?;:")
    if not place or len(place) > 40:
        return None
    if not re.fullmatch(r"[A-Za-z][A-Za-z' .-]*", place):
        return None
    words = place.split()
    if not words or len(words) > 4:
        return None
    if all(word.lower() in _PLACE_STOPWORDS for word in words):
        return None
    return place


def geocode_place(
    name: str,
    *,
    timeout: float = GEOCODE_TIMEOUT,
    fetcher=None,
) -> dict[str, Any] | None:
    """Turn a spoken town name into coordinates with Open-Meteo's keyless geocoder.

    Never raises: an unknown town or a slow lookup returns ``None`` so the caller can hand the
    question to the agent instead of answering with a wrong town's forecast. The label stays the
    name the owner said, so the spoken sentence repeats it back.
    """
    name = (name or "").strip()
    if not name:
        return None
    query = urllib.parse.urlencode({"name": name, "count": "1", "language": "en", "format": "json"})
    try:
        data = (fetcher or _get)(f"{GEOCODE_ENDPOINT}?{query}", timeout)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    results = data.get("results")
    if not isinstance(results, list) or not results:
        return None
    first = results[0]
    if not isinstance(first, dict):
        return None
    lat, lon = first.get("latitude"), first.get("longitude")
    if not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)):
        return None
    return {"label": name, "lat": float(lat), "lon": float(lon), "source": "geocode"}


def detect_home_place(*, timeout: float = 3.0, fetcher=None) -> dict[str, Any] | None:
    """Where this machine is, from its public IP. Never raises, never guesses.

    The owner should not have to type a town on a watch. If the lookup fails, the caller simply
    keeps behaving as if no home town is set.
    """
    try:
        data = (fetcher or _get)(GEO_ENDPOINT, timeout)
    except Exception:
        return None
    if not isinstance(data, dict) or data.get("success") is False:
        return None
    lat, lon = data.get("latitude"), data.get("longitude")
    city = str(data.get("city") or "").strip()
    region = str(data.get("region") or "").strip()
    if not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)):
        return None
    label = city or region
    if not label:
        return None
    return {"label": label, "lat": float(lat), "lon": float(lon), "source": "ip"}


def _get(url: str, timeout: float) -> dict[str, Any]:
    with urllib.request.urlopen(url, timeout=timeout) as response:  # noqa: S310 - fixed host
        return json.loads(response.read().decode("utf-8", errors="replace"))


def describe_weather(
    lat: float,
    lon: float,
    label: str = "",
    *,
    timeout: float = 3.0,
    fetcher=None,
) -> str | None:
    """One sentence about right now and the rest of today.

    Returns ``None`` when the forecast cannot be fetched or is malformed, so the caller can hand
    the question to the agent instead of answering a code question with a weather apology.
    """
    query = urllib.parse.urlencode({
        "latitude": f"{lat:.4f}",
        "longitude": f"{lon:.4f}",
        "current": "temperature_2m,precipitation,weather_code",
        "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max",
        "timezone": "auto",
        "forecast_days": "1",
    })
    place = label.strip() or "Here"
    try:
        data = (fetcher or _get)(f"{ENDPOINT}?{query}", timeout)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None

    current = data.get("current") or {}
    daily = data.get("daily") or {}
    temp = current.get("temperature_2m")
    code = current.get("weather_code")
    high = (daily.get("temperature_2m_max") or [None])[0]
    low = (daily.get("temperature_2m_min") or [None])[0]
    rain = (daily.get("precipitation_probability_max") or [None])[0]

    parts: list[str] = []
    if isinstance(temp, (int, float)):
        parts.append(f"{round(temp)} degrees")
    if isinstance(code, int) and code in _CODES:
        parts.append(_CODES[code])
    head = f"{place}: " + ", ".join(parts) if parts else f"{place}: no reading"

    tail: list[str] = []
    if isinstance(high, (int, float)) and isinstance(low, (int, float)):
        tail.append(f"high {round(high)}, low {round(low)}")
    if isinstance(rain, (int, float)):
        if rain >= 60:
            tail.append(f"rain likely, {round(rain)}% chance")
        elif rain >= 30:
            tail.append(f"maybe rain, {round(rain)}% chance")
        else:
            tail.append("mostly dry")
    sentence = head + ("." if not tail else ", " + ", ".join(tail) + ".")
    return sentence
