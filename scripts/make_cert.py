"""Create a local self-signed certificate for HTTPS testing on a phone.

    python scripts/make_cert.py

Writes certs/dev.crt and certs/dev.key, covering localhost, 127.0.0.1, ::1 and
this machine's LAN addresses. Then run:

    python run.py --host 0.0.0.0 --https

and set WEBAUTHN_ORIGIN to the exact URL the phone shows, for example
https://192.168.1.20:8000. WebAuthn checks the origin exactly, so a mismatch
will make passkey registration fail.

This certificate is for development only. It is not trusted by browsers; accept
the warning once on the phone. Never use it in production.
"""

from __future__ import annotations

import datetime as dt
import ipaddress
import socket
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "certs"


def local_ipv4_addresses() -> list[str]:
    addresses: set[str] = set()
    try:
        hostname = socket.gethostname()
        for info in socket.getaddrinfo(hostname, None, socket.AF_INET):
            addresses.add(info[4][0])
    except OSError:
        pass
    # The UDP connect trick reveals the address used for outbound traffic.
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            addresses.add(sock.getsockname()[0])
    except OSError:
        pass
    return sorted(addr for addr in addresses if not addr.startswith("127."))


def build_certificate() -> tuple[x509.Certificate, rsa.RSAPrivateKey, list[str]]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    san: list[x509.GeneralName] = [
        x509.DNSName("localhost"),
        x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
        x509.IPAddress(ipaddress.ip_address("::1")),
    ]
    lan = local_ipv4_addresses()
    for address in lan:
        san.append(x509.IPAddress(ipaddress.ip_address(address)))

    now = dt.datetime.now(dt.timezone.utc)
    subject = issuer = x509.Name(
        [
            x509.NameAttribute(NameOID.COMMON_NAME, "Avenza local development"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Avenza development only"),
        ]
    )
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + dt.timedelta(days=365))
        .add_extension(x509.SubjectAlternativeName(san), critical=False)
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    return certificate, key, lan


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    certificate, key, lan = build_certificate()

    cert_path = OUT / "dev.crt"
    key_path = OUT / "dev.key"
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )

    print(f"wrote {cert_path}")
    print(f"wrote {key_path}")
    print("certificate covers: localhost, 127.0.0.1, ::1" + (", " + ", ".join(lan) if lan else ""))
    if lan:
        print()
        print("For phone testing set, for example:")
        print(f"  WEBAUTHN_ORIGIN=https://{lan[0]}:8000")
        print("then run: python run.py --host 0.0.0.0 --https")
    print()
    print("Development certificate only. Browsers will warn; accept once on the phone.")


if __name__ == "__main__":
    main()
