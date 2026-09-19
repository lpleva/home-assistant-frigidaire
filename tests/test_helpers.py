"""is_auth_failure: only a structural credential error asks for the password."""

import requests
from vendor import frigidaire

from custom_components.frigidaire.helpers import is_auth_failure


def _wrapped_in(cause: BaseException, message: str) -> frigidaire.FrigidaireException:
    err = frigidaire.FrigidaireException(message)
    err.__cause__ = cause
    return err


def test_a_wrong_password_is_an_auth_failure() -> None:
    assert is_auth_failure(
        frigidaire.FrigidaireException("Failed to authenticate", status_code=401, error_code="invalid_credentials")
    )
    assert is_auth_failure(frigidaire.FrigidaireException("Request failed with status 401", status_code=401))


def test_the_session_cap_is_not() -> None:
    assert not is_auth_failure(frigidaire.FrigidaireException("Request failed", status_code=403, error_code="cas_3403"))


def test_auth_wording_without_a_code_is_not() -> None:
    """A missing sessionInfo with no errorCode, or no identity provider, is a bad response, not a bad password."""
    assert not is_auth_failure(
        frigidaire.FrigidaireException(
            "Failed to authenticate: sessionInfo missing or incomplete (response keys: [], errorCode: None)"
        )
    )
    assert not is_auth_failure(frigidaire.FrigidaireException("Failed to authenticate: no identity provider returned"))


def test_a_transport_failure_in_the_chain_is_never_an_auth_failure() -> None:
    outage = requests.exceptions.ConnectionError("Name or service not known")
    assert not is_auth_failure(_wrapped_in(outage, "Error processing request (ConnectionError):\nPOST /login"))
    assert not is_auth_failure(_wrapped_in(requests.exceptions.ReadTimeout("read timed out"), "Failed to authenticate"))
    # Even a 401 stamped on the wrapper does not outrank a transport failure underneath it.
    err = frigidaire.FrigidaireException("Failed to authenticate", status_code=401)
    err.__context__ = ConnectionError("reset")
    assert not is_auth_failure(err)
