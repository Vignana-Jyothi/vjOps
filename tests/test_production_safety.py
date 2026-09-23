"""Production-hardening checks: refuse to start with default secrets, and
fail closed (not open) on an unconfigured webhook secret once ENVIRONMENT is
"production". None of this fires by default (ENVIRONMENT defaults to
"development") — these tests construct their own Settings instances rather
than touching the process-wide `settings` singleton other tests depend on.
"""

from app.config import Settings, check_production_safety
from app.security import verify_github_signature


def _settings(**overrides) -> Settings:
    base = dict(
        environment="production",
        jwt_secret="a-real-random-secret-not-the-default",
        bootstrap_admin_password="a-real-random-password",
        database_url="postgresql+psycopg://viljaops:S0methingReal@db:5432/viljaops",
        github_webhook_secret="a-real-webhook-secret",
        cors_allowed_origins="https://ops.example.edu",
    )
    base.update(overrides)
    return Settings(**base)


def test_development_mode_skips_the_check_entirely():
    # Every default left in place, but environment is the default "development".
    assert check_production_safety(Settings()) == []


def test_production_with_every_default_still_in_place_refuses():
    # conftest.py sets JWT_SECRET/BOOTSTRAP_ADMIN_PASSWORD env vars for the
    # rest of the suite, which would otherwise mask this — pass the actual
    # documented defaults explicitly so this test means what it says.
    s = Settings(
        environment="production",
        jwt_secret="change-me-in-production",
        bootstrap_admin_password="changeme123",
        database_url="postgresql+psycopg://viljaops:viljaops@localhost:5432/viljaops",
        github_webhook_secret="",
        cors_allowed_origins="",
    )
    problems = check_production_safety(s)
    assert any("jwt_secret" in p for p in problems)
    assert any("bootstrap_admin_password" in p for p in problems)
    assert any("Postgres password" in p for p in problems)
    assert any("webhook" in p for p in problems)
    assert any("cors" in p.lower() for p in problems)


def test_production_with_everything_configured_passes():
    assert check_production_safety(_settings()) == []


def test_production_catches_each_problem_independently():
    assert any("jwt_secret" in p for p in check_production_safety(_settings(jwt_secret="change-me-in-production")))
    assert any("bootstrap_admin_password" in p for p in check_production_safety(_settings(bootstrap_admin_password="changeme123")))
    assert any("webhook" in p for p in check_production_safety(_settings(github_webhook_secret="")))
    assert any("cors" in p.lower() for p in check_production_safety(_settings(cors_allowed_origins="")))


def test_webhook_signature_fails_closed_in_production_when_secret_missing(monkeypatch):
    import app.security as security_module

    monkeypatch.setattr(security_module.settings, "github_webhook_secret", "")
    monkeypatch.setattr(security_module.settings, "environment", "production")
    try:
        assert verify_github_signature(b"payload", None) is False
        assert verify_github_signature(b"payload", "sha256=whatever") is False
    finally:
        monkeypatch.setattr(security_module.settings, "environment", "development")


def test_webhook_signature_still_permissive_in_development_when_secret_missing(monkeypatch):
    import app.security as security_module

    monkeypatch.setattr(security_module.settings, "github_webhook_secret", "")
    monkeypatch.setattr(security_module.settings, "environment", "development")
    assert verify_github_signature(b"payload", None) is True


def test_webhook_signature_still_verifies_correctly_when_secret_is_set(monkeypatch):
    import hashlib
    import hmac

    import app.security as security_module

    monkeypatch.setattr(security_module.settings, "github_webhook_secret", "s3cr3t")
    body = b'{"hello":"world"}'
    good_sig = "sha256=" + hmac.new(b"s3cr3t", body, hashlib.sha256).hexdigest()
    assert verify_github_signature(body, good_sig) is True
    assert verify_github_signature(body, "sha256=deadbeef" + "0" * 58) is False
    assert verify_github_signature(body, None) is False
