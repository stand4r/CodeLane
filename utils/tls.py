"""TLS trust configuration for outbound HTTPS calls.

The AI client talks to providers with :mod:`urllib`, which validates the
server certificate against the operating system trust store.  On Windows that
store is sometimes trimmed down (some system images ship only a handful of
roots) or HTTPS is intercepted by a corporate proxy / antivirus.  Both cases
end with::

    [SSL: CERTIFICATE_VERIFY_FAILED] unable to get local issuer certificate

To keep CodeLane working on such machines the context built here trusts:

* the system store (corporate roots installed by an administrator),
* the Mozilla CA bundle shipped with ``certifi`` (a complete, regularly
  updated set of public roots), and
* an optional user-supplied CA file (PEM or DER) for TLS interception.

Verification can be switched off explicitly for networks that cannot be fixed
otherwise, but that is a last resort and is never the default.
"""
from __future__ import annotations

import os
import ssl
from pathlib import Path
from typing import Optional

__all__ = [
    "TLSProblem",
    "build_ssl_context",
    "describe_tls_error",
    "mozilla_ca_bundle",
]

# Human-readable hints for the most common OpenSSL verification failures.
_VERIFY_HINTS = {
    "unable to get local issuer certificate":
        "в хранилище нет корневого сертификата, которым подписан сертификат сервера",
    "unable to get issuer certificate":
        "сервер прислал неполную цепочку сертификатов",
    "self-signed certificate in certificate chain":
        "соединение перехватывает прокси или антивирус с самоподписанным сертификатом",
    "self-signed certificate":
        "сервер использует самоподписанный сертификат",
    "certificate has expired":
        "срок действия сертификата сервера истёк",
    "certificate is not yet valid":
        "системные часы отстают от начала срока действия сертификата",
    "hostname mismatch":
        "имя в сертификате не совпадает с адресом сервера (проверьте базовый URL)",
}


class TLSProblem(Exception):
    """The configured CA file cannot be used (missing, unreadable, invalid)."""


def mozilla_ca_bundle() -> Optional[Path]:
    """Path to the ``certifi`` CA bundle, or ``None`` when it is unavailable."""
    try:
        import certifi  # local import: the app must work without it too
    except Exception:  # noqa: BLE001 - a broken optional dependency is not fatal
        return None
    try:
        path = Path(certifi.where())
    except Exception:  # noqa: BLE001
        return None
    return path if path.is_file() else None


def build_ssl_context(verify: bool = True, ca_bundle: str = "") -> ssl.SSLContext:
    """Create the TLS context used for outbound HTTPS requests.

    Parameters
    ----------
    verify:
        ``False`` disables certificate and hostname verification.  Only use it
        when the user explicitly asked for it.
    ca_bundle:
        Optional path to a PEM bundle or a DER certificate.  It is *added* to
        the system and Mozilla roots, so a corporate proxy certificate can be
        trusted without hiding the public roots.

    Raises
    ------
    TLSProblem
        When *ca_bundle* is set but cannot be read or parsed.
    """
    if not verify:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        return context

    context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH)
    bundle = mozilla_ca_bundle()
    if bundle is not None:
        try:
            context.load_verify_locations(cafile=str(bundle))
        except (OSError, ssl.SSLError):
            # A damaged optional bundle must not hide the system roots.
            pass

    extra = str(ca_bundle or "").strip().strip('"')
    if not extra:
        return context
    path = Path(os.path.expandvars(extra)).expanduser()
    if path.is_dir():
        try:
            context.load_verify_locations(capath=str(path))
        except (OSError, ssl.SSLError) as exc:
            raise TLSProblem(
                f"Не удалось прочитать папку сертификатов CA «{extra}»: {exc}") from None
        return context
    if not path.is_file():
        raise TLSProblem(
            f"Файл сертификата CA не найден: {extra}. "
            "Укажите PEM- или DER-файл в Настройки → ИИ-агент → «Сертификат CA».")
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise TLSProblem(f"Не удалось открыть файл сертификата CA «{extra}»: {exc}") from None
    try:
        if b"BEGIN CERTIFICATE" in data:
            context.load_verify_locations(cafile=str(path))
        else:
            # DER exported by the Windows certificate manager.
            context.load_verify_locations(cadata=data)
    except (OSError, ssl.SSLError) as exc:
        raise TLSProblem(
            f"Файл «{extra}» не похож на сертификат CA (PEM или DER): {exc}") from None
    return context


def describe_tls_error(exc: BaseException, *, host: str = "", verify: bool = True,
                       ca_bundle: str = "") -> str:
    """Turn a low-level TLS failure into an actionable Russian message."""
    detail = str(getattr(exc, "verify_message", "") or exc).strip()
    target = host or "сервер API"
    if not verify:
        return (f"Не удалось установить TLS-соединение с {target}: {detail}. "
                "Проверка сертификата отключена, поэтому причина в сети: прокси, VPN, "
                "файрвол или неверный базовый URL.")
    if not isinstance(exc, ssl.SSLCertVerificationError):
        return (f"Не удалось установить защищённое TLS-соединение с {target}: {detail}. "
                "Проверьте базовый URL, прокси/VPN и настройки TLS в Настройки → ИИ-агент."
                + (f" Сейчас указан файл сертификата: {ca_bundle}." if ca_bundle else ""))
    hint = _VERIFY_HINTS.get(detail)
    reason = f" ({hint})" if hint else ""
    lines = [
        f"Сертификат сервера {target} не прошёл проверку: {detail}{reason}.",
        "Обычно помогает добавить корневой сертификат: Настройки → ИИ-агент → "
        "«Сертификат CA» (PEM или DER; для корпоративного прокси или антивируса — "
        "его корневой сертификат).",
        "CodeLane также автоматически доверяет набору корневых сертификатов Mozilla "
        "из пакета certifi — установите его (pip install certifi), если он отсутствует.",
        "Крайняя мера — снять флажок «Проверять TLS-сертификат» в тех же настройках "
        "(небезопасно: трафик можно прочитать).",
    ]
    if ca_bundle:
        lines.insert(1, f"Сейчас указан файл сертификата: {ca_bundle}.")
    return " ".join(lines)
