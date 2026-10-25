"""Sign in with Google, Facebook or LinkedIn (OAuth 2.0 authorization-code flow).

- `state` is random, stored hashed server-side (single use, 10 minutes) and
  also set in a cookie, so a callback only completes in the browser that
  started it (no login CSRF).
- Google also uses PKCE, and its OpenID userinfo tells us whether the email is
  verified. Facebook only shares an email the user has confirmed. LinkedIn
  ("Sign In with LinkedIn using OpenID Connect") says whether it's verified too;
  it shares name, email, photo and locale only -- work history isn't available
  to apps (see app/modules/profiles/linkedin.py for importing it).
- The provider's tokens are used once to read the profile and then dropped.
"""
from __future__ import annotations

import base64
import hashlib
import secrets
from datetime import timedelta
from urllib.parse import urlencode

import httpx
from sqlalchemy.orm import Session

from app.modules.auth.models import TokenPurpose
from app.modules.auth.service import AuthError, ProviderProfile, provider_name
from app.modules.auth.settings import AuthSettings
from app.modules.auth.tokens import consume_token, issue_token

STATE_COOKIE = "jsa_oauth_state"
STATE_TTL = timedelta(minutes=10)
PROVIDERS = ("google", "facebook", "linkedin")
_TIMEOUT = 15.0

_GOOGLE_AUTH = "https://accounts.google.com/o/oauth2/v2/auth"
_GOOGLE_TOKEN = "https://oauth2.googleapis.com/token"
_GOOGLE_USERINFO = "https://openidconnect.googleapis.com/v1/userinfo"

_LINKEDIN_AUTH = "https://www.linkedin.com/oauth/v2/authorization"
_LINKEDIN_TOKEN = "https://www.linkedin.com/oauth/v2/accessToken"
_LINKEDIN_USERINFO = "https://api.linkedin.com/v2/userinfo"


def redirect_uri(provider: str) -> str:
    return f"{AuthSettings().base_url}/auth/{provider}/callback"


def _pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def authorization_url(session: Session, provider: str, next_path: str) -> tuple[str, str]:
    """(URL to send the browser to, raw state for the cookie)."""
    settings = AuthSettings()
    if provider not in PROVIDERS or not settings.provider_enabled(provider):
        raise AuthError("That sign-in option isn't available.")
    verifier, challenge = _pkce_pair()
    state = issue_token(session, TokenPurpose.OAUTH_STATE, user_id=None, ttl=STATE_TTL,
                        payload={"provider": provider, "verifier": verifier, "next": next_path})
    session.commit()
    if provider == "google":
        url = _GOOGLE_AUTH + "?" + urlencode({
            "client_id": settings.google_client_id, "redirect_uri": redirect_uri(provider), "response_type": "code",
            "scope": "openid email profile", "state": state, "code_challenge": challenge,
            "code_challenge_method": "S256", "prompt": "select_account",
        })
    elif provider == "linkedin":
        # LinkedIn only accepts PKCE from native apps; state + the client secret protect this one.
        url = _LINKEDIN_AUTH + "?" + urlencode({
            "response_type": "code", "client_id": settings.linkedin_client_id, "redirect_uri": redirect_uri(provider),
            "scope": "openid profile email", "state": state,
        })
    else:
        url = f"https://www.facebook.com/{settings.facebook_graph_version}/dialog/oauth?" + urlencode({
            "client_id": settings.facebook_client_id, "redirect_uri": redirect_uri(provider),
            "response_type": "code", "scope": "email,public_profile", "state": state,
        })
    return url, state


def complete(session: Session, provider: str, *, code: str | None, state: str | None, cookie_state: str | None,
             client: httpx.Client | None = None) -> tuple[ProviderProfile, str]:
    """Check the callback and fetch the user's profile. Returns (profile, next path)."""
    if not state or not cookie_state or not secrets.compare_digest(state, cookie_state):
        raise AuthError("Sign-in expired or was started in another browser. Please try again.")
    token = consume_token(session, TokenPurpose.OAUTH_STATE, state)
    session.commit()
    if token is None or (token.payload or {}).get("provider") != provider:
        raise AuthError("Sign-in expired. Please try again.")
    if not code:
        raise AuthError("Sign-in was cancelled.")
    payload = token.payload or {}
    http = client or httpx.Client(timeout=_TIMEOUT)
    try:
        if provider == "google":
            profile = _google_profile(http, code, payload["verifier"])
        elif provider == "linkedin":
            profile = _linkedin_profile(http, code)
        else:
            profile = _facebook_profile(http, code)
    except AuthError:
        raise
    except Exception as exc:  # network / provider error: never echo provider details to the user
        raise AuthError(f"Couldn't sign you in with {provider_name(provider)} right now. Please try again.") from exc
    return profile, payload.get("next") or "/"


def _google_profile(http: httpx.Client, code: str, verifier: str) -> ProviderProfile:
    settings = AuthSettings()
    tokens = http.post(_GOOGLE_TOKEN, data={
        "code": code, "client_id": settings.google_client_id, "client_secret": settings.google_client_secret,
        "redirect_uri": redirect_uri("google"), "grant_type": "authorization_code", "code_verifier": verifier,
    })
    tokens.raise_for_status()
    info = http.get(_GOOGLE_USERINFO, headers={"Authorization": f"Bearer {tokens.json()['access_token']}"})
    info.raise_for_status()
    data = info.json()
    return ProviderProfile(
        provider="google", subject=str(data["sub"]), email=data.get("email"),
        email_verified=bool(data.get("email_verified")),
        first_name=data.get("given_name") or "", last_name=data.get("family_name") or "",
    )


def _facebook_profile(http: httpx.Client, code: str) -> ProviderProfile:
    settings = AuthSettings()
    graph = f"https://graph.facebook.com/{settings.facebook_graph_version}"
    tokens = http.get(f"{graph}/oauth/access_token", params={
        "client_id": settings.facebook_client_id, "client_secret": settings.facebook_client_secret,
        "redirect_uri": redirect_uri("facebook"), "code": code,
    })
    tokens.raise_for_status()
    access_token = tokens.json()["access_token"]
    proof = hmac_sha256(settings.facebook_client_secret or "", access_token)
    me = http.get(f"{graph}/me", params={
        "fields": "id,first_name,last_name,email", "access_token": access_token, "appsecret_proof": proof,
    })
    me.raise_for_status()
    data = me.json()
    return ProviderProfile(
        provider="facebook", subject=str(data["id"]), email=data.get("email"),
        email_verified=bool(data.get("email")),  # Facebook only returns confirmed addresses
        first_name=data.get("first_name") or "", last_name=data.get("last_name") or "",
    )


def _linkedin_profile(http: httpx.Client, code: str) -> ProviderProfile:
    settings = AuthSettings()
    tokens = http.post(_LINKEDIN_TOKEN, data={
        "grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri("linkedin"),
        "client_id": settings.linkedin_client_id, "client_secret": settings.linkedin_client_secret,
    })
    tokens.raise_for_status()
    info = http.get(_LINKEDIN_USERINFO, headers={"Authorization": f"Bearer {tokens.json()['access_token']}"})
    info.raise_for_status()
    data = info.json()
    first, last = data.get("given_name") or "", data.get("family_name") or ""
    if not (first or last) and data.get("name"):
        first, _, last = str(data["name"]).strip().partition(" ")
    return ProviderProfile(
        provider="linkedin", subject=str(data["sub"]), email=data.get("email"),
        # Missing counts as unverified: an unverified address must never take over an account.
        email_verified=data.get("email_verified") is True,
        first_name=first.strip(), last_name=last.strip(), country=_linkedin_country(data.get("locale")),
    )


def _linkedin_country(locale) -> str | None:
    """LinkedIn's locale is {"country": "CA", "language": "en"} (or, from older
    apps, a string like "en_CA") -> "CA"."""
    if isinstance(locale, dict):
        country = locale.get("country")
    elif isinstance(locale, str) and "_" in locale:
        country = locale.rsplit("_", 1)[1]
    else:
        country = None
    country = (country or "").strip().upper()
    return country if len(country) == 2 and country.isalpha() else None


def hmac_sha256(key: str, message: str) -> str:
    import hmac

    return hmac.new(key.encode(), message.encode(), hashlib.sha256).hexdigest()
