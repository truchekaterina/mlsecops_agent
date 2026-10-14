"""Самоподписанный сертификат стенда. Ключ лежит в data/tls и в git не входит.

Кто вызывает: entrypoint, start-local.sh и mail_client.
Почему отдельно: выпуск сертификата не должен смешиваться с чатом и с книгой.
Браузер предупредит, что сертификат не из общего списка. Это ожидаемо.
"""

from __future__ import annotations

import os
import ssl
import subprocess
from pathlib import Path

from paths import DATA

_DIR = DATA / "tls"
CERT = _DIR / "cert.pem"
KEY = _DIR / "key.pem"
_SAN_FILE = _DIR / "san.txt"


def _ips() -> list[str]:
    found = ["127.0.0.1"]
    raw = os.environ.get("STRAZH_TLS_IPS", "")
    for part in raw.split(","):
        ip = part.strip()
        if ip and ip not in found and ip != "0.0.0.0":
            found.append(ip)
    return found


def _san_text(ips: list[str]) -> str:
    names = ["DNS:localhost", *[f"IP:{ip}" for ip in ips]]
    return ",".join(names)


def ensure_cert() -> tuple[Path, Path]:
    """Создать сертификат, если его нет или сменился список адресов."""
    _DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(_DIR, 0o700)
    san = _san_text(_ips())
    previous = _SAN_FILE.read_text(encoding="ascii").strip() if _SAN_FILE.is_file() else ""
    if CERT.is_file() and KEY.is_file() and previous == san:
        os.chmod(KEY, 0o600)
        return CERT, KEY
    try:
        subprocess.run(
            [
                "openssl",
                "req",
                "-x509",
                "-newkey",
                "rsa:2048",
                "-sha256",
                "-days",
                "825",
                "-nodes",
                "-keyout",
                str(KEY),
                "-out",
                str(CERT),
                "-subj",
                "/CN=strazh-local",
                "-addext",
                f"subjectAltName={san}",
            ],
            check=True,
            capture_output=True,
        )
    except FileNotFoundError as exc:
        raise SystemExit("Для HTTPS нужен openssl.") from exc
    except subprocess.CalledProcessError as exc:
        raise SystemExit("openssl не выпустил сертификат Стража.") from exc
    _SAN_FILE.write_text(san + "\n", encoding="ascii")
    os.chmod(KEY, 0o600)
    os.chmod(CERT, 0o600)
    os.chmod(_SAN_FILE, 0o600)
    return CERT, KEY


def client_context() -> ssl.SSLContext:
    """Доверять только сертификату этого стенда, не отключать проверку целиком."""
    if not CERT.is_file():
        raise RuntimeError("Нет data/tls/cert.pem. Запустите стенд ещё раз.")
    return ssl.create_default_context(cafile=str(CERT))


def uvicorn_ssl_args() -> list[str]:
    cert, key = ensure_cert()
    return ["--ssl-certfile", str(cert), "--ssl-keyfile", str(key)]
