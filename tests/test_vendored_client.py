"""The vendored Frigidaire client: TLS, host allowlisting, and response parsing.

These tests are the reason the library is vendored (audit C1). They run against the
in-tree copy with no network access: the requests.Session is replaced by a recorder that
captures every call and returns canned responses.
"""

from __future__ import annotations

import gzip
import json
from typing import Any
from unittest.mock import patch

import pytest
from vendor import frigidaire
from vendor.frigidaire import rate_limit


class FakeResponse:
    """Enough of requests.Response for parse_response and the 429 handling."""

    def __init__(self, payload: Any = None, status_code: int = 200, headers: dict | None = None, content=None):
        self.status_code = status_code
        self.headers = headers or {}
        self._payload = payload
        self.content = json.dumps(payload).encode() if content is None else content

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class RecordingSession:
    """Stands in for requests.Session, recording calls and replaying queued responses."""

    def __init__(self, responses: list[FakeResponse] | None = None):
        self.calls: list[tuple[str, str, dict]] = []
        self.responses = responses or []
        self.closed = False

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if self.responses:
            return self.responses.pop(0)
        return FakeResponse({})

    def get(self, url, **kwargs):
        return self.request("GET", url, **kwargs)

    def post(self, url, **kwargs):
        return self.request("POST", url, **kwargs)

    def put(self, url, **kwargs):
        return self.request("PUT", url, **kwargs)

    def close(self):
        self.closed = True


def _client(session: RecordingSession | None = None) -> frigidaire.Frigidaire:
    """Build a client with a session key already in hand, so __init__ makes one GET."""
    session = session or RecordingSession()
    with patch.object(frigidaire.requests, "Session", return_value=session):
        client = frigidaire.Frigidaire(
            username="user@example.com",
            password="hunter2",
            session_key="cached-key",
            regional_base_url="https://api.us.ocp.electrolux.one",
        )
    client._recording_session = session  # type: ignore[attr-defined]
    return client


# --- TLS ---------------------------------------------------------------------------


def test_no_request_ever_disables_certificate_verification() -> None:
    client = _client()
    session: RecordingSession = client._recording_session

    client.get_request(client.regional_base_url, "/x", {})
    client.post_request(client.regional_base_url, "/x", {}, {"a": 1})
    client.put_request(client.regional_base_url, "/x", {}, {"a": 1})

    assert len(session.calls) >= 4  # the constructor's test_connection plus the three above
    for _method, _url, kwargs in session.calls:
        assert "verify" not in kwargs or kwargs["verify"] is True


def test_source_contains_no_verify_false() -> None:
    """A grep-level guard: the flag must not come back anywhere in the vendored package."""
    from pathlib import Path

    package = Path(frigidaire.__file__).parent
    for module in sorted(package.glob("*.py")):
        assert "verify=False" not in module.read_text(), module


def test_session_wrapper_rejects_a_request_that_disables_verification() -> None:
    wrapped = rate_limit.wrap_session_request(lambda *a, **k: FakeResponse({}), rate_limit.RateLimiter(0, 0))
    with pytest.raises(ValueError, match="cannot be disabled"):
        wrapped("GET", "https://api.us.ocp.electrolux.one/x", verify=False)


def test_every_request_carries_a_timeout_even_when_none_is_configured() -> None:
    seen: list[dict] = []

    def record(method, url, **kwargs):
        seen.append(kwargs)
        return FakeResponse({})

    wrapped = rate_limit.wrap_session_request(record, rate_limit.RateLimiter(0, 0), default_timeout=None)
    wrapped("GET", "https://api.us.ocp.electrolux.one/x")
    assert seen[0]["timeout"] == rate_limit.FALLBACK_TIMEOUT


# --- host allowlist ----------------------------------------------------------------


@pytest.mark.parametrize(
    "domain",
    [
        "attacker.example",
        "gigya.com.attacker.example",
        "attacker.example/x.gigya.com",
        "attacker.example#.gigya.com",
        "user:pass@eu1.gigya.com",
        "",
        None,
    ],
)
def test_identity_domain_outside_the_allowlist_is_rejected(domain) -> None:
    with pytest.raises(frigidaire.FrigidaireException, match="identity domain"):
        frigidaire._validate_identity_domain(domain)


@pytest.mark.parametrize("domain", ["eu1.gigya.com", "us1.gigya.com", "accounts.electrolux.one"])
def test_known_identity_domains_are_accepted(domain) -> None:
    assert frigidaire._validate_identity_domain(domain) == domain


def test_credentials_are_never_posted_to_an_off_allowlist_identity_host() -> None:
    """The password POST must not happen at all when the identity response is tampered with."""
    session = RecordingSession(
        [
            FakeResponse({"accessToken": "client-credentials-token"}),
            FakeResponse([{"domain": "attacker.example", "apiKey": "k", "httpRegionalBaseUrl": "https://x"}]),
        ]
    )
    with patch.object(frigidaire.requests, "Session", return_value=session):
        with pytest.raises(frigidaire.FrigidaireException, match="identity domain"):
            frigidaire.Frigidaire(username="user@example.com", password="hunter2")

    bodies = [kwargs.get("data", "") for _m, _u, kwargs in session.calls]
    assert not any("hunter2" in str(body) for body in bodies)
    assert not any("attacker.example" in url for _m, url, _k in session.calls)


@pytest.mark.parametrize(
    "base_url",
    [
        "http://api.us.ocp.electrolux.one",  # not https
        "https://api.attacker.example",
        "https://api.us.ocp.electrolux.one.attacker.example",
        "https://api.us.ocp.electrolux.one:8443",
        "https://user:pass@api.us.ocp.electrolux.one",
        "https://attacker.example/api.us.ocp.electrolux.one",
        "",
    ],
)
def test_api_base_url_outside_the_allowlist_is_rejected(base_url) -> None:
    with pytest.raises(frigidaire.FrigidaireException):
        frigidaire._validate_api_base_url(base_url)


def test_api_base_url_on_a_known_host_is_accepted() -> None:
    assert frigidaire._validate_api_base_url("https://api.us.ocp.electrolux.one/") == (
        "https://api.us.ocp.electrolux.one"
    )


def test_a_request_to_an_unexpected_host_is_refused_before_it_is_sent() -> None:
    client = _client()
    session: RecordingSession = client._recording_session
    before = len(session.calls)

    with pytest.raises(frigidaire.FrigidaireException, match="unexpected URL"):
        client.get_request("https://attacker.example", "/appliances", {})

    assert len(session.calls) == before


def test_a_tampered_cached_base_url_is_dropped_rather_than_trusted() -> None:
    session = RecordingSession(
        [
            FakeResponse({"accessToken": "client-credentials-token"}),
            FakeResponse([{"domain": "attacker.example", "apiKey": "k", "httpRegionalBaseUrl": "https://x"}]),
        ]
    )
    with patch.object(frigidaire.requests, "Session", return_value=session):
        # The bad cached URL must not be used; the client falls back to full
        # authentication, which then trips the identity allowlist.
        with pytest.raises(frigidaire.FrigidaireException):
            frigidaire.Frigidaire(
                username="user@example.com",
                password="hunter2",
                session_key="cached-key",
                regional_base_url="https://attacker.example",
            )
    assert not any("attacker.example" in url for _m, url, _k in session.calls)


# --- response parsing --------------------------------------------------------------


def test_parse_response_reads_plain_json() -> None:
    assert frigidaire.Frigidaire.parse_response(FakeResponse({"a": 1})) == {"a": 1}


def test_parse_response_decompresses_a_gzip_body() -> None:
    body = gzip.compress(json.dumps({"a": 1}).encode())
    response = FakeResponse(status_code=200, headers={"Content-Encoding": "gzip"}, content=body)
    assert frigidaire.Frigidaire.parse_response(response) == {"a": 1}


def test_parse_response_tolerates_a_gzip_header_on_plain_json() -> None:
    """The server mislabels plain JSON as gzip; the client must not fail on it."""
    response = FakeResponse({"a": 1}, headers={"Content-Encoding": "gzip"})
    assert frigidaire.Frigidaire.parse_response(response) == {"a": 1}


def test_parse_response_treats_an_empty_body_as_an_empty_dict() -> None:
    assert frigidaire.Frigidaire.parse_response(FakeResponse(content=b"")) == {}


def test_parse_response_surfaces_the_platform_error_code() -> None:
    response = FakeResponse({"error": "cas_3403", "message": "too many sessions"}, status_code=429)
    with pytest.raises(frigidaire.FrigidaireException) as excinfo:
        frigidaire.Frigidaire.parse_response(response)
    assert excinfo.value.status_code == 429
    assert excinfo.value.error_code == "cas_3403"


def test_parse_response_raises_on_an_unparseable_body() -> None:
    response = FakeResponse(content=b"<html>nope</html>")
    with pytest.raises(frigidaire.FrigidaireException, match="unexpected response"):
        frigidaire.Frigidaire.parse_response(response)


# --- malformed records, log hygiene, lifecycle -------------------------------------


def test_appliance_survives_a_record_missing_appliance_data() -> None:
    appliance = frigidaire.Appliance({"applianceId": "DH-1", "properties": {"reported": {"targetHumidity": 45}}})
    assert appliance.appliance_id == "DH-1"
    assert appliance.nickname == "DH-1"  # falls back to the id rather than raising
    assert appliance.destination == frigidaire.Destination.DEHUMIDIFIER


def test_appliance_without_an_id_is_rejected() -> None:
    with pytest.raises(ValueError, match="applianceId"):
        frigidaire.Appliance({"applianceData": {"modelName": "DH", "applianceName": "x"}})


def test_one_unparseable_record_does_not_lose_the_others(caplog) -> None:
    good = {
        "applianceId": "DH-1",
        "applianceData": {"modelName": "DH", "applianceName": "Basement"},
        "properties": {"reported": {}},
    }
    client = _client()
    with patch.object(frigidaire.Frigidaire, "_fetch_raw_appliances", return_value=[{"nope": True}, good]):
        appliances = client.get_appliances()

    assert [a.appliance_id for a in appliances] == ["DH-1"]
    assert "Skipping unparseable appliance record" in caplog.text


def test_a_failed_login_reports_key_names_not_the_response_body() -> None:
    client = _client()
    login_response = {
        "errorCode": 403042,
        "errorMessage": "Invalid LoginID",
        "sessionInfo": None,
        "regToken": "SECRET-REG-TOKEN",
    }
    session = RecordingSession(
        [
            FakeResponse({"accessToken": "client-credentials-token"}),
            FakeResponse(
                [
                    {
                        "domain": "us1.gigya.com",
                        "apiKey": "gigya-key",
                        "httpRegionalBaseUrl": "https://api.us.ocp.electrolux.one",
                    }
                ]
            ),
            FakeResponse({"gmid": "g", "ucid": "u"}),
            FakeResponse(login_response),
        ]
    )
    client._session = session
    client.session_key = None
    client.regional_base_url = None

    with pytest.raises(frigidaire.FrigidaireException) as excinfo:
        client.authenticate()

    message = str(excinfo.value)
    assert "SECRET-REG-TOKEN" not in message
    assert "sessionInfo" in message
    # Classified structurally, so callers do not have to match on the wording.
    assert excinfo.value.error_code == "invalid_credentials"
    assert excinfo.value.status_code == 401


def test_authenticating_without_a_password_asks_for_reauth_instead_of_logging_in() -> None:
    client = _client()
    session = RecordingSession()
    client._session = session
    client.password = None
    client.session_key = None
    client.regional_base_url = None

    with pytest.raises(frigidaire.FrigidaireException) as excinfo:
        client.authenticate()

    assert excinfo.value.error_code == "reauth_required"
    assert session.calls == []


def test_rate_limiter_does_not_hold_its_lock_while_sleeping() -> None:
    limiter = rate_limit.RateLimiter(min_interval=0.05, jitter=0)
    limiter.wait()  # arms the next slot
    limiter.wait()  # must sleep for it
    assert limiter._lock.acquire(blocking=False)
    limiter._lock.release()


def test_the_shared_limiter_is_not_keyed_by_the_account_email() -> None:
    _client()
    assert not any("user@example.com" in key for key in frigidaire._SCOPED_LIMITERS)
    assert frigidaire._scope_key("user@example.com") in frigidaire._SCOPED_LIMITERS


def test_close_releases_the_http_session() -> None:
    client = _client()
    client.close()
    assert client._recording_session.closed


def test_requests_do_not_follow_redirects() -> None:
    """A 307/308 replays the body — for /accounts.login, that body is the password."""
    client = _client()
    session: RecordingSession = client._recording_session

    client.get_request(client.regional_base_url, "/x", {})
    client.post_request(client.regional_base_url, "/x", {}, {"a": 1})
    client.put_request(client.regional_base_url, "/x", {}, {"a": 1})

    assert len(session.calls) >= 4
    for _method, _url, kwargs in session.calls:
        assert kwargs["allow_redirects"] is False


def test_a_redirect_is_reported_as_a_failure_rather_than_followed() -> None:
    response = FakeResponse({}, status_code=307, headers={"Location": "https://attacker.example/"})
    with pytest.raises(frigidaire.FrigidaireException) as excinfo:
        frigidaire.Frigidaire.parse_response(response)
    assert excinfo.value.status_code == 307
