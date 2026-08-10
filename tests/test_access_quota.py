from __future__ import annotations

import json
import logging
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from soramimic_video import access_identity
from soramimic_video import api as api_mod

FAKE_MIDI = b"MThd" + b"\0" * 16
ACCESS_HEADERS = {"CF-Access-Jwt-Assertion": "header.payload.signature"}


def _fast_pipeline(job, config):
    output = job.dir / "result.mp4"
    output.write_bytes(b"video")
    return output


def _submit(client: TestClient, **data):
    return client.post(
        "/api/jobs",
        headers=ACCESS_HEADERS,
        files={"midi": ("song.mid", FAKE_MIDI, "audio/midi")},
        data={"wordlist": "stations", **data},
    )


def _access_env(monkeypatch, *, allowlist: str = "member@example.com") -> None:
    monkeypatch.setenv(api_mod.PUBLIC_ENV, "1")
    monkeypatch.setenv(api_mod.TRUSTED_PROXY_IPS_ENV, "127.0.0.1/32")
    monkeypatch.setenv(
        api_mod.CF_ACCESS_TEAM_DOMAIN_ENV,
        "https://team.cloudflareaccess.com",
    )
    monkeypatch.setenv(api_mod.CF_ACCESS_AUD_ENV, "audience")
    monkeypatch.setenv(api_mod.QUOTA_EXEMPT_EMAILS_ENV, allowlist)
    monkeypatch.setenv(api_mod.IP_HASH_KEY_ENV, "persistent-test-key")
    monkeypatch.setattr(api_mod, "run_pipeline", _fast_pipeline)
    monkeypatch.setattr(api_mod, "song_seconds", lambda _: 1.0)


def test_verifier_uses_fixed_jwks_and_strict_decode_options(monkeypatch):
    seen: dict[str, object] = {}
    key = SimpleNamespace(key=object())

    class FakeClient:
        def get_signing_key_from_jwt(self, assertion):
            seen["assertion"] = assertion
            return key

    def fake_client(issuer):
        seen["issuer"] = issuer
        return FakeClient()

    def fake_decode(assertion, signing_key, **kwargs):
        seen["decode"] = kwargs
        assert signing_key is key.key
        return {"email": " Member@Example.COM "}

    monkeypatch.setattr(access_identity.jwt, "get_unverified_header", lambda _: {"alg": "RS256"})
    monkeypatch.setattr(access_identity, "_jwks_client", fake_client)
    monkeypatch.setattr(access_identity.jwt, "decode", fake_decode)

    result = access_identity.verify_access_email(
        "header.payload.signature",
        issuer="https://team.cloudflareaccess.com",
        audience="expected-aud",
    )

    assert result == "member@example.com"
    assert seen["issuer"] == "https://team.cloudflareaccess.com"
    options = seen["decode"]["options"]
    assert seen["decode"]["algorithms"] == ["RS256"]
    assert seen["decode"]["audience"] == "expected-aud"
    assert seen["decode"]["issuer"] == "https://team.cloudflareaccess.com"
    assert set(options["require"]) == {"exp", "iat", "iss", "aud", "email"}
    assert all(options[name] for name in (
        "verify_signature", "verify_exp", "verify_iat", "verify_iss", "verify_aud"
    ))


def test_jwks_client_uses_fixed_endpoint_timeout_and_cache(monkeypatch):
    seen = []
    access_identity._jwks_client.cache_clear()
    monkeypatch.setattr(
        access_identity,
        "PyJWKClient",
        lambda url, **kwargs: seen.append((url, kwargs)) or object(),
    )
    issuer = "https://team.cloudflareaccess.com"
    first = access_identity._jwks_client(issuer)
    second = access_identity._jwks_client(issuer)
    access_identity._jwks_client.cache_clear()

    assert first is second
    assert seen == [
        (
            "https://team.cloudflareaccess.com/cdn-cgi/access/certs",
            {"cache_jwk_set": True, "cache_keys": True, "timeout": 3},
        )
    ]


@pytest.mark.parametrize(
    "issuer",
    [
        "http://team.cloudflareaccess.com",
        "https://Team.cloudflareaccess.com",
        "https://team.cloudflareaccess.com/",
        "https://team.cloudflareaccess.com.evil.example",
        "https://bad_name.cloudflareaccess.com",
    ],
)
def test_verifier_rejects_unsafe_issuer_without_network(issuer, monkeypatch):
    monkeypatch.setattr(
        access_identity,
        "_jwks_client",
        lambda _: pytest.fail("invalid issuer must not fetch JWKS"),
    )
    assert (
        access_identity.verify_access_email(
            "header.payload.signature", issuer=issuer, audience="aud"
        )
        is None
    )


def test_verifier_failure_logs_no_sensitive_values(monkeypatch, caplog):
    sentinel_token = "header.SENTINEL_TOKEN.signature"
    sentinel_email = "sentinel-email@example.com"
    monkeypatch.setattr(
        access_identity.jwt, "get_unverified_header", lambda _: {"alg": "HS256"}
    )
    with caplog.at_level(logging.WARNING):
        assert access_identity.verify_access_email(
            sentinel_token,
            issuer="https://team.cloudflareaccess.com",
            audience=sentinel_email,
        ) is None
    text = caplog.text
    assert "verification failed" in text
    assert sentinel_token not in text
    assert "SENTINEL_TOKEN" not in text
    assert sentinel_email not in text


@pytest.mark.parametrize(
    "failure",
    [
        access_identity.jwt.ExpiredSignatureError("expired"),
        access_identity.jwt.InvalidAudienceError("aud"),
        access_identity.jwt.InvalidIssuerError("iss"),
        access_identity.jwt.InvalidSignatureError("signature"),
        RuntimeError("jwks unavailable"),
    ],
)
def test_verifier_decode_jwks_and_claim_failures_are_generic(
    failure, monkeypatch, caplog
):
    class FakeClient:
        def get_signing_key_from_jwt(self, assertion):
            if isinstance(failure, RuntimeError):
                raise failure
            return SimpleNamespace(key=object())

    monkeypatch.setattr(
        access_identity.jwt, "get_unverified_header", lambda _: {"alg": "RS256"}
    )
    monkeypatch.setattr(access_identity, "_jwks_client", lambda _: FakeClient())
    monkeypatch.setattr(
        access_identity.jwt,
        "decode",
        lambda *args, **kwargs: (_ for _ in ()).throw(failure),
    )
    with caplog.at_level(logging.WARNING):
        assert access_identity.verify_access_email(
            "header.payload.signature",
            issuer="https://team.cloudflareaccess.com",
            audience="expected",
        ) is None
    assert str(failure) not in caplog.text


def test_verifier_rejects_oversized_or_missing_claims(monkeypatch):
    monkeypatch.setattr(
        access_identity,
        "_jwks_client",
        lambda _: pytest.fail("oversized assertion must not fetch JWKS"),
    )
    assert access_identity.verify_access_email(
        "x" * (access_identity.MAX_ASSERTION_BYTES + 1),
        issuer="https://team.cloudflareaccess.com",
        audience="aud",
    ) is None

    monkeypatch.setattr(
        access_identity.jwt, "get_unverified_header", lambda _: {"alg": "RS256"}
    )
    monkeypatch.setattr(
        access_identity,
        "_jwks_client",
        lambda _: SimpleNamespace(
            get_signing_key_from_jwt=lambda assertion: SimpleNamespace(key=object())
        ),
    )
    monkeypatch.setattr(access_identity.jwt, "decode", lambda *a, **kw: {})
    assert access_identity.verify_access_email(
        "header.payload.signature",
        issuer="https://team.cloudflareaccess.com",
        audience="aud",
    ) is None


def test_valid_access_exemption_config_and_status_do_not_expose_identity(
    tmp_path, monkeypatch
):
    _access_env(monkeypatch, allowlist=" OTHER@example.com, Member@Example.com ")
    monkeypatch.setenv(api_mod.DAILY_QUOTA_ENV, "1")
    monkeypatch.setenv(api_mod.IP_DAILY_QUOTA_ENV, "1")
    monkeypatch.setattr(api_mod, "verify_access_email", lambda *a, **kw: "member@example.com")
    jobs_dir = tmp_path / "jobs"
    client = TestClient(
        api_mod.create_app(jobs_dir), client=("127.0.0.1", 50000)
    )

    conf_res = client.get("/api/config", headers=ACCESS_HEADERS)
    conf = conf_res.json()
    assert conf["quota_exempt"] is True
    assert "daily_quota" not in conf
    assert "email" not in json.dumps(conf).casefold()
    assert conf_res.headers["cache-control"] == "no-store"
    first = _submit(client)
    second = _submit(client)
    assert first.status_code == second.status_code == 200
    saved = json.loads(
        (jobs_dir / first.json()["id"] / api_mod.STATUS_FILENAME).read_text()
    )
    assert "client_hash" not in saved
    assert "member@example.com" not in json.dumps(saved)


@pytest.mark.parametrize(
    ("peer", "trusted", "verified_email"),
    [
        (("127.0.0.1", 50000), "", "member@example.com"),
        (("192.0.2.10", 50000), "192.0.2.10/32", "member@example.com"),
        (("127.0.0.1", 50000), "127.0.0.1/32", "other@example.com"),
        (("127.0.0.1", 50000), "127.0.0.1/32", None),
    ],
)
def test_access_exemption_fails_closed(peer, trusted, verified_email, tmp_path, monkeypatch):
    _access_env(monkeypatch)
    monkeypatch.setenv(api_mod.TRUSTED_PROXY_IPS_ENV, trusted)
    monkeypatch.setattr(api_mod, "verify_access_email", lambda *a, **kw: verified_email)
    client = TestClient(api_mod.create_app(tmp_path / "jobs"), client=peer)
    headers = {
        **ACCESS_HEADERS,
        "CF-Access-Authenticated-User-Email": "member@example.com",
    }
    conf = client.get("/api/config", headers=headers).json()
    assert conf["quota_exempt"] is False
    assert conf["daily_quota"] == api_mod.DEFAULT_DAILY_QUOTA


@pytest.mark.parametrize(
    "team_domain",
    [
        "team.cloudflareaccess.com",
        "https://https://team.cloudflareaccess.com",
        "https://team.cloudflareaccess.com//",
    ],
)
def test_access_team_domain_must_be_a_full_single_url(
    team_domain, tmp_path, monkeypatch
):
    _access_env(monkeypatch)
    monkeypatch.setenv(api_mod.CF_ACCESS_TEAM_DOMAIN_ENV, team_domain)
    monkeypatch.setattr(
        api_mod,
        "verify_access_email",
        lambda *args, **kwargs: pytest.fail("invalid issuer must not be verified"),
    )
    client = TestClient(
        api_mod.create_app(tmp_path / "jobs"), client=("127.0.0.1", 50000)
    )
    conf = client.get("/api/config", headers=ACCESS_HEADERS).json()
    assert conf["quota_exempt"] is False
    assert conf["daily_quota"] == api_mod.DEFAULT_DAILY_QUOTA


def test_access_team_domain_allows_one_trailing_slash(tmp_path, monkeypatch):
    _access_env(monkeypatch)
    monkeypatch.setenv(
        api_mod.CF_ACCESS_TEAM_DOMAIN_ENV,
        "  https://team.cloudflareaccess.com/  ",
    )
    monkeypatch.setattr(
        api_mod, "verify_access_email", lambda *args, **kwargs: "member@example.com"
    )
    client = TestClient(
        api_mod.create_app(tmp_path / "jobs"), client=("127.0.0.1", 50000)
    )
    assert client.get("/api/config", headers=ACCESS_HEADERS).json()["quota_exempt"] is True


def test_nonexempt_ip_quota_survives_cookie_change_and_restart(tmp_path, monkeypatch):
    _access_env(monkeypatch, allowlist="")
    monkeypatch.setenv(api_mod.DAILY_QUOTA_ENV, "10")
    monkeypatch.setenv(api_mod.IP_DAILY_QUOTA_ENV, "1")
    jobs_dir = tmp_path / "jobs"
    first = TestClient(api_mod.create_app(jobs_dir), client=("127.0.0.1", 50000))
    result = _submit(first)
    assert result.status_code == 200
    job_id = result.json()["id"]
    saved = json.loads((jobs_dir / job_id / api_mod.STATUS_FILENAME).read_text())
    assert len(saved["client_hash"]) == 16
    assert "127.0.0.1" not in json.dumps(saved)

    restarted = TestClient(api_mod.create_app(jobs_dir), client=("127.0.0.1", 50001))
    assert _submit(restarted).status_code == 429


def test_persisted_owner_job_without_client_hash_counts_for_session_quota(
    tmp_path, monkeypatch
):
    _access_env(monkeypatch)
    monkeypatch.setenv(api_mod.DAILY_QUOTA_ENV, "1")
    monkeypatch.setenv(api_mod.IP_DAILY_QUOTA_ENV, "30")
    monkeypatch.setattr(
        api_mod, "verify_access_email", lambda *args, **kwargs: "member@example.com"
    )
    jobs_dir = tmp_path / "jobs"
    exempt = TestClient(
        api_mod.create_app(jobs_dir), client=("127.0.0.1", 50000)
    )
    first = _submit(exempt)
    assert first.status_code == 200
    session = exempt.cookies[api_mod.SESSION_COOKIE]
    saved = json.loads(
        (jobs_dir / first.json()["id"] / api_mod.STATUS_FILENAME).read_text()
    )
    assert saved["owner"] == session
    assert "client_hash" not in saved

    monkeypatch.setattr(api_mod, "verify_access_email", lambda *args, **kwargs: None)
    restarted = TestClient(
        api_mod.create_app(jobs_dir), client=("127.0.0.1", 50001)
    )
    restarted.cookies.set(api_mod.SESSION_COOKIE, session)
    response = _submit(restarted)
    assert response.status_code == 429
    assert "1日に作れる本数" in response.json()["detail"]


def test_exemption_does_not_bypass_turnstile_queue_or_duration(tmp_path, monkeypatch):
    _access_env(monkeypatch)
    monkeypatch.setattr(api_mod, "verify_access_email", lambda *a, **kw: "member@example.com")
    monkeypatch.setenv(api_mod.TURNSTILE_SECRET_ENV, "secret")
    monkeypatch.setattr(api_mod, "verify_turnstile", lambda token, ip=None: token == "good")
    client = TestClient(api_mod.create_app(tmp_path / "turnstile"), client=("127.0.0.1", 1))
    assert _submit(client).status_code == 403

    monkeypatch.delenv(api_mod.TURNSTILE_SECRET_ENV)
    monkeypatch.setenv(api_mod.QUEUE_LIMIT_ENV, "1")
    app = api_mod.create_app(tmp_path / "queue")
    app.state.manager.active_count = lambda: 1
    assert _submit(TestClient(app, client=("127.0.0.1", 1))).status_code == 429

    monkeypatch.setenv(api_mod.QUEUE_LIMIT_ENV, "0")
    monkeypatch.setenv(api_mod.MAX_SONG_SECONDS_ENV, "1")
    monkeypatch.setattr(api_mod, "song_seconds", lambda _: 2.0)
    duration = TestClient(api_mod.create_app(tmp_path / "duration"), client=("127.0.0.1", 1))
    assert _submit(duration).status_code == 400

def test_access_assertion_does_not_authorize_ops(tmp_path, monkeypatch):
    _access_env(monkeypatch)
    monkeypatch.setattr(api_mod, "verify_access_email", lambda *a, **kw: "member@example.com")
    client = TestClient(api_mod.create_app(tmp_path / "jobs"), client=("127.0.0.1", 1))
    assert client.get("/readyz", headers=ACCESS_HEADERS).status_code == 404


def test_readiness_reports_only_boolean_security_checks(tmp_path, monkeypatch):
    _access_env(monkeypatch)
    monkeypatch.setenv(api_mod.OPS_TOKEN_ENV, "ops-secret")
    client = TestClient(api_mod.create_app(tmp_path / "jobs"), client=("127.0.0.1", 1))
    response = client.get(
        "/readyz",
        headers={"X-Soramimic-Ops-Token": "ops-secret"},
    )
    assert response.status_code == 200
    assert response.json()["checks"] == {
        "jobs_dir_writable": True,
        "persistent_ip_hash": True,
        "access": True,
    }
    assert "team.cloudflareaccess.com" not in response.text
    assert "member@example.com" not in response.text


def test_ui_has_exact_unlimited_quota_message():
    html = (api_mod.STATIC_DIR / "index.html").read_text(encoding="utf-8")
    assert 'limits.push("検証アカウント：1日の回数制限なし")' in html
    assert "conf.quota_exempt === true" in html
