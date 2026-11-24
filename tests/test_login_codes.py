"""The one-time code check shared by the GitHub callback and the code exchange.

``AllowListedGitHubProvider`` looks a one-time code up, asks GitHub whose it is and deletes it when
the account is not admitted, both when GitHub redirects back and when the client redeems the code.
These tests drive the real provider built by ``server._build`` with the code store and GitHub's
token check replaced by one ``Ledger`` that records every step in order, so each outcome (no
stored code, refused, GitHub rejects the token, GitHub unavailable, admitted) is pinned down step
by step.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastmcp.server.auth.auth import AccessToken, TokenVerificationError
from fastmcp.server.auth.oauth_proxy.proxy import OAuthProxy
from mcp.server.auth.provider import AuthorizationCode, TokenError
from mcp.shared.auth import OAuthClientInformationFull
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse

from .login_support import build, login_env, private_fastmcp_home

__all__ = ["private_fastmcp_home"]  # fixture, imported for pytest

REDIRECT = "http://127.0.0.1:5555/cb"
REFUSED_LINE = "Refused a token of a user who is not on the allow-list"
NOT_PERMITTED = "This GitHub account is not permitted here"


class Ledger:
    """Stands in for the proxy's code store and for GitHub's token check, logging each call.

    ``codes`` maps a one-time code to the upstream token stored with it; ``accounts`` maps an
    upstream token GitHub recognises to the claims its verified token carries (any value, so odd
    claims can be tried); a token in ``unavailable`` makes the check raise as an outage does.
    """

    def __init__(
        self,
        codes: dict[str, str],
        accounts: dict[str, Any],
        unavailable: frozenset[str] = frozenset(),
    ) -> None:
        """Start with ``codes`` stored and ``accounts`` known to GitHub; nothing logged yet."""
        self.codes = dict(codes)
        self.accounts = dict(accounts)
        self.unavailable = unavailable
        self.events: list[tuple[str, str]] = []

    async def get(self, *, key: str) -> Any:
        """The code store's ``get``: the stored code's model, or ``None``."""
        self.events.append(("get", key))
        token = self.codes.get(key)
        return None if token is None else SimpleNamespace(idp_tokens={"access_token": token})

    async def delete(self, *, key: str) -> bool:
        """The code store's ``delete``: whether ``key`` was there."""
        self.events.append(("delete", key))
        return self.codes.pop(key, None) is not None

    async def verify_token(self, token: str) -> AccessToken | None:
        """GitHub's check: a token with the known claims, ``None`` for a token GitHub rejects.

        Raises ``TokenVerificationError`` for a token in ``unavailable``.
        """
        self.events.append(("verify", token))
        if token in self.unavailable:
            raise TokenVerificationError("GitHub token verification unavailable")
        if token not in self.accounts:
            return None
        # ``model_construct`` skips validation so that claims of any type reach the provider.
        return AccessToken.model_construct(
            token=token, client_id="x", scopes=[], expires_at=None, claims=self.accounts[token]
        )


@pytest.fixture
def provider(tmp_path: Path, private_fastmcp_home: Path) -> Any:
    """The real signed-in provider for a list that holds only ``alice``."""
    built, _ = build(login_env(tmp_path))
    return built.auth


def install(provider: Any, ledger: Ledger) -> Ledger:
    """Make ``ledger`` the provider's code store and token check; return it."""
    provider._code_store = ledger
    provider._token_validator = ledger
    return ledger


def standard_ledger() -> Ledger:
    """One code per case: alice (listed), mallory (not listed), a token GitHub rejects, GitHub
    down, a token without claims, alice's login with an integer ``sub``, and claims of odd type.
    """
    return Ledger(
        codes={
            "c-alice": "gho_alice",
            "c-mallory": "gho_mallory",
            "c-gone": "gho_gone",
            "c-down": "gho_down",
            "c-noclaims": "gho_noclaims",
            "c-intsub": "gho_intsub",
            "c-odd": "gho_odd",
        },
        accounts={
            "gho_alice": {"sub": "1001", "login": "alice"},
            "gho_mallory": {"sub": "9999", "login": "mallory"},
            "gho_noclaims": None,
            "gho_intsub": {"sub": 1001, "login": "alice"},
            "gho_odd": ["sub", "login"],
        },
        unavailable=frozenset({"gho_down"}),
    )


def refusals(caplog: pytest.LogCaptureFixture) -> int:
    """How many times the provider logged that it refused a user."""
    return sum(1 for record in caplog.records if record.getMessage() == REFUSED_LINE)


def assert_nothing_logged_about(caplog: pytest.LogCaptureFixture, *secrets: str) -> None:
    """No log record, at any level, mentions any of ``secrets`` (an id, a login, a token)."""
    for record in caplog.records:
        text = record.getMessage() + (record.exc_text or "")
        assert not any(secret in text for secret in secrets), text


@pytest.fixture
def logs(caplog: pytest.LogCaptureFixture) -> pytest.LogCaptureFixture:
    """Capture every log record at every level, the provider's own included."""
    caplog.set_level(logging.DEBUG)
    caplog.set_level(logging.DEBUG, logger="boe_insights_mcp.login")
    return caplog


# --- The code exchange ---------------------------------------------------------------------------


async def exchange(provider: Any, code: str) -> Any:
    """Redeem ``code`` at the provider as the token endpoint would."""
    client = OAuthClientInformationFull(client_id="mcp-client", redirect_uris=[REDIRECT])  # type: ignore[arg-type]
    grant = AuthorizationCode(
        code=code,
        scopes=[],
        expires_at=time.time() + 300,
        client_id="mcp-client",
        code_challenge="x" * 43,
        redirect_uri=REDIRECT,  # type: ignore[arg-type]
        redirect_uri_provided_explicitly=True,
    )
    return await provider.exchange_authorization_code(client, grant)


@pytest.fixture
def base_exchange(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Replace the proxy's own code exchange; record the codes that reach it."""
    reached: list[str] = []

    async def exchange_authorization_code(
        _self: object, _client: object, grant: AuthorizationCode
    ) -> str:
        """Stand-in for the base class: records the code and hands back a marker."""
        reached.append(grant.code)
        return "issued"

    monkeypatch.setattr(OAuthProxy, "exchange_authorization_code", exchange_authorization_code)
    return reached


async def test_an_unknown_code_falls_through_to_the_base_class_untouched(provider: Any) -> None:
    ledger = install(provider, standard_ledger())

    with pytest.raises(TokenError) as caught:
        await exchange(provider, "c-unknown")

    # The base class's own error, not the allow-list's: nothing was decided here.
    assert caught.value.error == "invalid_grant"
    assert caught.value.error_description == "Authorization code not found"
    # One look-up by the provider, then the base class's own; no check, no deletion.
    assert ledger.events == [("get", "c-unknown"), ("get", "c-unknown")]


async def test_the_exchange_looks_an_unknown_code_up_once_and_passes_it_on(
    provider: Any, base_exchange: list[str]
) -> None:
    ledger = install(provider, standard_ledger())

    assert await exchange(provider, "c-unknown") == "issued"
    assert base_exchange == ["c-unknown"]
    assert ledger.events == [("get", "c-unknown")]


async def test_the_exchange_hands_an_admitted_code_to_the_base_class(
    provider: Any, base_exchange: list[str], logs: pytest.LogCaptureFixture
) -> None:
    ledger = install(provider, standard_ledger())

    assert await exchange(provider, "c-alice") == "issued"
    assert base_exchange == ["c-alice"]
    assert ledger.events == [("get", "c-alice"), ("verify", "gho_alice")]
    assert refusals(logs) == 0


async def test_the_exchange_refuses_and_deletes_an_unlisted_users_code(
    provider: Any, base_exchange: list[str], logs: pytest.LogCaptureFixture
) -> None:
    ledger = install(provider, standard_ledger())

    with pytest.raises(TokenError) as caught:
        await exchange(provider, "c-mallory")

    assert (caught.value.error, caught.value.error_description) == ("invalid_grant", NOT_PERMITTED)
    assert ledger.events == [
        ("get", "c-mallory"),
        ("verify", "gho_mallory"),
        ("delete", "c-mallory"),
    ]
    assert base_exchange == []
    assert refusals(logs) == 1
    assert_nothing_logged_about(logs, "9999", "mallory", "gho_mallory")


async def test_the_exchange_refuses_and_deletes_a_code_github_cannot_vouch_for(
    provider: Any, base_exchange: list[str], logs: pytest.LogCaptureFixture
) -> None:
    ledger = install(provider, standard_ledger())

    with pytest.raises(TokenError) as caught:
        await exchange(provider, "c-gone")

    assert (caught.value.error, caught.value.error_description) == ("invalid_grant", NOT_PERMITTED)
    assert ledger.events == [("get", "c-gone"), ("verify", "gho_gone"), ("delete", "c-gone")]
    assert base_exchange == []
    # No identity was checked against the list, so no refusal of a user is logged.
    assert refusals(logs) == 0
    assert_nothing_logged_about(logs, "gho_gone")


@pytest.mark.parametrize(
    ("code", "token"),
    [
        pytest.param("c-noclaims", "gho_noclaims", id="claims-none"),
        pytest.param("c-intsub", "gho_intsub", id="listed-login-integer-sub"),
    ],
)
async def test_the_exchange_refuses_a_token_without_a_usable_identity(
    provider: Any, base_exchange: list[str], logs: pytest.LogCaptureFixture, code: str, token: str
) -> None:
    ledger = install(provider, standard_ledger())

    with pytest.raises(TokenError) as caught:
        await exchange(provider, code)

    assert caught.value.error_description == NOT_PERMITTED
    assert ledger.events == [("get", code), ("verify", token), ("delete", code)]
    assert base_exchange == []
    assert refusals(logs) == 1
    assert_nothing_logged_about(logs, token)


async def test_the_exchange_lets_an_outage_at_github_propagate_and_keeps_the_code(
    provider: Any, base_exchange: list[str]
) -> None:
    ledger = install(provider, standard_ledger())

    with pytest.raises(TokenVerificationError):
        await exchange(provider, "c-down")

    assert ledger.events == [("get", "c-down"), ("verify", "gho_down")]
    assert "c-down" in ledger.codes
    assert base_exchange == []


async def test_the_exchange_lets_claims_of_an_odd_type_raise_and_keeps_the_code(
    provider: Any, base_exchange: list[str]
) -> None:
    # Pinned as it behaves today: GitHub's verifier always builds a dict, so a truthy non-dict
    # ``claims`` cannot come from it; if one did, the error propagates and nothing is issued.
    ledger = install(provider, standard_ledger())

    with pytest.raises(AttributeError):
        await exchange(provider, "c-odd")

    assert ledger.events == [("get", "c-odd"), ("verify", "gho_odd")]
    assert "c-odd" in ledger.codes
    assert base_exchange == []


# --- The GitHub callback -------------------------------------------------------------------------


@pytest.fixture
def base_callback(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Replace the proxy's own callback handler; it answers with ``answer["response"]``."""
    answer: dict[str, Any] = {}

    async def handle_idp_callback(_self: object, _request: Request) -> Any:
        """Stand-in for the base handler: returns the response the test chose."""
        return answer["response"]

    monkeypatch.setattr(OAuthProxy, "_handle_idp_callback", handle_idp_callback)
    return answer


async def callback(provider: Any, base_callback: dict[str, Any], response: Any) -> Any:
    """Run the provider's callback handler with the base handler answering ``response``."""
    base_callback["response"] = response
    return await provider._handle_idp_callback(Request({"type": "http"}))


def redirect_with(code: str) -> RedirectResponse:
    """The base handler's success redirect to the client, carrying ``code``."""
    return RedirectResponse(f"{REDIRECT}?code={code}&state=s")


async def test_the_callback_passes_on_a_response_that_is_not_a_redirect(
    provider: Any, base_callback: dict[str, Any]
) -> None:
    ledger = install(provider, standard_ledger())
    error_page = HTMLResponse("upstream failed", status_code=500)

    assert await callback(provider, base_callback, error_page) is error_page
    assert ledger.events == []


async def test_the_callback_passes_on_an_error_redirect_without_a_code(
    provider: Any, base_callback: dict[str, Any]
) -> None:
    ledger = install(provider, standard_ledger())
    redirect = RedirectResponse(f"{REDIRECT}?error=access_denied&state=s")

    assert await callback(provider, base_callback, redirect) is redirect
    assert ledger.events == []


async def test_the_callback_passes_on_a_code_that_is_not_stored_and_deletes_nothing(
    provider: Any, base_callback: dict[str, Any]
) -> None:
    ledger = install(provider, standard_ledger())
    redirect = redirect_with("c-unknown")

    assert await callback(provider, base_callback, redirect) is redirect
    assert ledger.events == [("get", "c-unknown")]


async def test_the_callback_passes_on_an_admitted_users_redirect(
    provider: Any, base_callback: dict[str, Any], logs: pytest.LogCaptureFixture
) -> None:
    ledger = install(provider, standard_ledger())
    redirect = redirect_with("c-alice")

    assert await callback(provider, base_callback, redirect) is redirect
    assert ledger.events == [("get", "c-alice"), ("verify", "gho_alice")]
    assert refusals(logs) == 0


async def test_the_callback_refuses_an_unlisted_user_naming_their_id(
    provider: Any, base_callback: dict[str, Any], logs: pytest.LogCaptureFixture
) -> None:
    ledger = install(provider, standard_ledger())

    answered = await callback(provider, base_callback, redirect_with("c-mallory"))

    assert isinstance(answered, HTMLResponse)
    assert answered.status_code == 403
    page = bytes(answered.body).decode()
    assert "id:9999" in page and "mallory" in page
    assert ledger.events == [
        ("get", "c-mallory"),
        ("verify", "gho_mallory"),
        ("delete", "c-mallory"),
    ]
    assert refusals(logs) == 1
    # The page names the account for the operator; the server's log does not.
    assert_nothing_logged_about(logs, "9999", "mallory", "gho_mallory")


async def test_the_callback_refuses_a_code_github_cannot_vouch_for_without_naming_anyone(
    provider: Any, base_callback: dict[str, Any], logs: pytest.LogCaptureFixture
) -> None:
    ledger = install(provider, standard_ledger())

    answered = await callback(provider, base_callback, redirect_with("c-gone"))

    assert isinstance(answered, HTMLResponse)
    assert answered.status_code == 403
    page = bytes(answered.body).decode()
    assert "Ask the operator to add it to the allow-list." in page
    assert "id:" not in page
    assert ledger.events == [("get", "c-gone"), ("verify", "gho_gone"), ("delete", "c-gone")]
    assert refusals(logs) == 0
    assert_nothing_logged_about(logs, "gho_gone")


@pytest.mark.parametrize(
    ("code", "token", "named"),
    [
        pytest.param("c-noclaims", "gho_noclaims", None, id="claims-none"),
        pytest.param("c-intsub", "gho_intsub", "id:1001", id="listed-login-integer-sub"),
    ],
)
async def test_the_callback_refuses_a_token_without_a_usable_identity(
    provider: Any,
    base_callback: dict[str, Any],
    logs: pytest.LogCaptureFixture,
    code: str,
    token: str,
    named: str | None,
) -> None:
    ledger = install(provider, standard_ledger())

    answered = await callback(provider, base_callback, redirect_with(code))

    assert isinstance(answered, HTMLResponse)
    assert answered.status_code == 403
    page = bytes(answered.body).decode()
    if named is None:
        assert "Ask the operator to add it to the allow-list." in page
    else:
        # The page shows whatever id GitHub sent; it is the list that refuses a non-string one.
        assert named in page
    assert ledger.events == [("get", code), ("verify", token), ("delete", code)]
    assert refusals(logs) == 1
    assert_nothing_logged_about(logs, token)


async def test_the_callback_lets_an_outage_at_github_propagate_and_keeps_the_code(
    provider: Any, base_callback: dict[str, Any]
) -> None:
    ledger = install(provider, standard_ledger())

    with pytest.raises(TokenVerificationError):
        await callback(provider, base_callback, redirect_with("c-down"))

    assert ledger.events == [("get", "c-down"), ("verify", "gho_down")]
    assert "c-down" in ledger.codes


async def test_the_callback_lets_claims_of_an_odd_type_raise_and_keeps_the_code(
    provider: Any, base_callback: dict[str, Any]
) -> None:
    # Pinned as it behaves today; see the matching exchange test.
    ledger = install(provider, standard_ledger())

    with pytest.raises(AttributeError):
        await callback(provider, base_callback, redirect_with("c-odd"))

    assert ledger.events == [("get", "c-odd"), ("verify", "gho_odd")]
    assert "c-odd" in ledger.codes


# --- The shared check itself ---------------------------------------------------------------------


async def test_the_check_decides_nothing_about_a_code_that_is_not_stored(provider: Any) -> None:
    ledger = install(provider, standard_ledger())

    assert await provider._screen_code("c-unknown") is None
    assert ledger.events == [("get", "c-unknown")]


async def test_the_check_admits_a_listed_user_and_keeps_the_code(provider: Any) -> None:
    ledger = install(provider, standard_ledger())

    admitted, upstream = await provider._screen_code("c-alice")

    assert admitted is True
    assert upstream.claims == {"sub": "1001", "login": "alice"}
    assert ledger.events == [("get", "c-alice"), ("verify", "gho_alice")]
    assert "c-alice" in ledger.codes


async def test_the_check_refuses_an_unlisted_user_deletes_the_code_and_returns_who(
    provider: Any,
) -> None:
    ledger = install(provider, standard_ledger())

    admitted, upstream = await provider._screen_code("c-mallory")

    assert admitted is False
    assert upstream.claims == {"sub": "9999", "login": "mallory"}
    assert ledger.events == [
        ("get", "c-mallory"),
        ("verify", "gho_mallory"),
        ("delete", "c-mallory"),
    ]
    assert "c-mallory" not in ledger.codes


async def test_the_check_refuses_a_code_github_cannot_vouch_for_and_deletes_it(
    provider: Any,
) -> None:
    ledger = install(provider, standard_ledger())

    assert await provider._screen_code("c-gone") == (False, None)
    assert ledger.events == [("get", "c-gone"), ("verify", "gho_gone"), ("delete", "c-gone")]
    assert "c-gone" not in ledger.codes
