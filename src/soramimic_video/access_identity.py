"""Strict Cloudflare Access JWT verification for quota exemption decisions."""

from __future__ import annotations

import logging
import re
from functools import lru_cache

import jwt
from jwt import PyJWKClient

logger = logging.getLogger(__name__)

MAX_ASSERTION_BYTES = 16 * 1024
_ISSUER_RE = re.compile(
    r"https://(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)*"
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.cloudflareaccess\.com"
)


def canonical_email(value: str) -> str:
    """Return the comparison form used for configured and asserted email addresses."""
    return value.strip().casefold()


def valid_issuer(issuer: str) -> bool:
    """Accept only the exact HTTPS Cloudflare Access team-domain issuer shape."""
    hostname = issuer.removeprefix("https://")
    return len(hostname) <= 253 and bool(_ISSUER_RE.fullmatch(issuer))


@lru_cache(maxsize=8)
def _jwks_client(issuer: str) -> PyJWKClient:
    """Keep PyJWT's bounded JWKS/key caches alive across assertions."""
    return PyJWKClient(
        f"{issuer}/cdn-cgi/access/certs",
        cache_jwk_set=True,
        cache_keys=True,
        timeout=3,
    )


def verify_access_email(assertion: str, *, issuer: str, audience: str) -> str | None:
    """Verify an Access assertion and return its canonical email, failing closed.

    Logs deliberately contain no exception, token, email, or claim values.
    """
    if (
        not assertion
        or len(assertion.encode("utf-8", errors="ignore")) > MAX_ASSERTION_BYTES
        or assertion.count(".") != 2
        or not valid_issuer(issuer)
        or not audience.strip()
    ):
        logger.warning("Cloudflare Access assertion verification failed")
        return None
    try:
        header = jwt.get_unverified_header(assertion)
        if header.get("alg") != "RS256":
            raise ValueError("unsupported signing algorithm")
        jwks = _jwks_client(issuer)
        signing_key = jwks.get_signing_key_from_jwt(assertion)
        claims = jwt.decode(
            assertion,
            signing_key.key,
            algorithms=["RS256"],
            audience=audience,
            issuer=issuer,
            options={
                "require": ["exp", "iat", "iss", "aud", "email"],
                "verify_signature": True,
                "verify_exp": True,
                "verify_iat": True,
                "verify_iss": True,
                "verify_aud": True,
            },
        )
        email = claims.get("email")
        if not isinstance(email, str) or not canonical_email(email):
            raise ValueError("missing email")
        return canonical_email(email)
    except Exception:  # noqa: BLE001 - all verifier/network failures must fail closed
        logger.warning("Cloudflare Access assertion verification failed")
        return None
