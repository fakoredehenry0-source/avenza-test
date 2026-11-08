"""Run the Avenza server.

    python run.py                       # http://localhost:8000
    python run.py --reload              # auto-reload for development
    python run.py --https               # HTTPS using certs/dev.crt + certs/dev.key
    python run.py --host 0.0.0.0        # listen on the LAN (phone testing)

WebAuthn requires a secure context. `localhost` is secure; a phone on the LAN
needs HTTPS, so run `python scripts/make_cert.py` first and then `--https`.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import uvicorn

from avenza.config import PROJECT_ROOT, load_settings


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the Avenza voice banking demo.")
    parser.add_argument("--host", default="127.0.0.1", help="bind address (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8000, help="bind port (default: 8000)")
    parser.add_argument("--reload", action="store_true", help="reload on code changes")
    parser.add_argument(
        "--https",
        action="store_true",
        help="serve HTTPS with certs/dev.crt and certs/dev.key (run scripts/make_cert.py first)",
    )
    parser.add_argument("--cert", type=Path, default=PROJECT_ROOT / "certs" / "dev.crt")
    parser.add_argument("--key", type=Path, default=PROJECT_ROOT / "certs" / "dev.key")
    parser.add_argument("--log-level", default=None, help="override LOG_LEVEL")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    settings = load_settings()

    ssl_certfile = ssl_keyfile = None
    if args.https:
        if not args.cert.exists() or not args.key.exists():
            print(
                "HTTPS certificate not found.\n"
                f"  expected: {args.cert}\n"
                f"            {args.key}\n"
                "Run: python scripts/make_cert.py",
                file=sys.stderr,
            )
            return 2
        ssl_certfile = str(args.cert)
        ssl_keyfile = str(args.key)

    scheme = "https" if args.https else "http"
    host_for_browser = "localhost" if args.host in {"0.0.0.0", "::"} else args.host
    print(f"Avenza {settings.version}")
    print(f"  bank: {settings.bank_mode}{' (simulated, no real money)' if settings.bank_is_simulated else ''}")
    print(f"  stt/tts: {settings.stt_provider}/{settings.tts_provider}")
    print(f"  database: {settings.database_url}")
    print(f"  open: {scheme}://{host_for_browser}:{args.port}/")
    if settings.allow_dev_approval and not settings.is_production:
        print("  WARNING: ALLOW_DEV_APPROVAL is on; fingerprint approval can be simulated.")
    if args.https:
        print("  On a phone, open the certificate URL once and accept it, then reload the app.")
    print()

    if args.reload:
        # Reload needs an import string so the worker can re-import the app.
        uvicorn.run(
            "avenza.main:create_app",
            factory=True,
            host=args.host,
            port=args.port,
            reload=True,
            log_level=(args.log_level or settings.log_level).lower(),
            ssl_certfile=ssl_certfile,
            ssl_keyfile=ssl_keyfile,
        )
        return 0

    from avenza.main import create_app

    uvicorn.run(
        create_app(settings),
        host=args.host,
        port=args.port,
        log_level=(args.log_level or settings.log_level).lower(),
        ssl_certfile=ssl_certfile,
        ssl_keyfile=ssl_keyfile,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
