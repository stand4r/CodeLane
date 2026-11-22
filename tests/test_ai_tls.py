"""TLS trust for the AI agent.

The regression these tests guard against is the real-world failure on Windows
machines with a trimmed root store::

    [SSL: CERTIFICATE_VERIFY_FAILED] unable to get local issuer certificate

CodeLane must add the Mozilla bundle from ``certifi`` to the system store and
explain the failure instead of reporting it as "no internet connection".
"""
import ssl
import sys
import urllib.error
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def test_mozilla_bundle_is_trusted_when_available():
    from utils.tls import build_ssl_context, mozilla_ca_bundle

    if mozilla_ca_bundle() is None:
        # certifi is an optional runtime dependency; without it the system
        # store is still used.  requirements.txt ships it for real installs.
        return
    context = build_ssl_context()
    assert context.cert_store_stats()["x509_ca"] >= 100


def test_verification_can_be_disabled_explicitly():
    from utils.tls import build_ssl_context

    context = build_ssl_context(False)
    assert context.verify_mode == ssl.CERT_NONE
    assert context.check_hostname is False


def test_missing_ca_bundle_is_reported():
    from services.ai_service import AIError, AIService

    service = AIService(config={
        "ai_base_url": "https://api.deepseek.com",
        "ai_tls_ca_bundle": str(ROOT / "no-such-ca-bundle.pem"),
    })
    try:
        service._ssl_context()
    except AIError as exc:
        assert "Файл сертификата CA не найден" in str(exc)
    else:
        raise AssertionError("expected AIError for a missing CA bundle")


def test_broken_ca_bundle_is_reported(tmp_path):
    from services.ai_service import AIError, AIService

    path = tmp_path / "broken.pem"
    path.write_text("this is not a certificate", encoding="utf-8")
    service = AIService(config={
        "ai_base_url": "https://api.deepseek.com",
        "ai_tls_ca_bundle": str(path),
    })
    try:
        service._ssl_context()
    except AIError as exc:
        assert "не похож на сертификат" in str(exc)
    else:
        raise AssertionError("expected AIError for an invalid CA bundle")


def test_certificate_failure_is_explained_not_called_offline():
    from utils.tls import describe_tls_error

    error = ssl.SSLCertVerificationError(1, "unable to get local issuer certificate")
    text = describe_tls_error(error, host="api.deepseek.com")
    assert "api.deepseek.com" in text
    assert "Сертификат CA" in text
    assert "certifi" in text
    assert "Нет соединения" not in text


def test_generic_tls_failure_is_not_blamed_on_the_certificate():
    from utils.tls import describe_tls_error

    text = describe_tls_error(ssl.SSLError(1, "TLSV1_ALERT_PROTOCOL_VERSION"),
                              host="api.deepseek.com")
    assert "TLS-соединение" in text
    assert "не прошёл проверку" not in text


def test_ai_service_turns_ssl_error_into_actionable_message(monkeypatch):
    from services import ai_service

    def fail(*_args, **_kwargs):
        raise urllib.error.URLError(
            ssl.SSLCertVerificationError(1, "unable to get local issuer certificate"))

    monkeypatch.setattr(ai_service.urllib.request, "urlopen", fail)
    service = ai_service.AIService(config={
        "ai_base_url": "https://api.deepseek.com",
        "ai_model": "deepseek-flash",
        "ai_api_key": "sk-" + "a" * 32,
    })
    try:
        service.complete("hi")
    except ai_service.AIError as exc:
        text = str(exc)
        assert "Нет соединения" not in text
        assert "сертификат" in text.lower()
    else:
        raise AssertionError("expected AIError")


def test_tls_settings_have_safe_defaults():
    from utils.settings_manager import DEFAULT_SETTINGS

    assert DEFAULT_SETTINGS["ai_tls_verify"] is True
    assert DEFAULT_SETTINGS["ai_tls_ca_bundle"] == ""


def test_tls_settings_are_read_from_config():
    from services.ai_service import AIService

    service = AIService(config={"ai_tls_verify": "false",
                                "ai_tls_ca_bundle": "  C:/certs/corp.pem  "})
    assert service.tls_verify is False
    assert service.tls_ca_bundle == "C:/certs/corp.pem"
    assert service._ssl_context().verify_mode == ssl.CERT_NONE
