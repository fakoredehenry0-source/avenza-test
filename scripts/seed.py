"""Seed the demo database.

    python scripts/seed.py
    python scripts/seed.py --dev-user "Demo Customer"
    python scripts/seed.py --reset

Safe to run repeatedly: mock bank accounts are upserted by account number and
a dev customer is only created when --dev-user is given. The dev customer has
no WebAuthn key, so it can only approve when ALLOW_DEV_APPROVAL is on.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from avenza.audit import AuditService  # noqa: E402
from avenza.bank import BANK_NAMES, DEMO_ACCOUNTS, seed_mock_bank  # noqa: E402
from avenza.config import load_settings  # noqa: E402
from avenza.db import init_db, make_engine, make_session_factory  # noqa: E402
from avenza.security import WebAuthnService  # noqa: E402
from avenza.services import DeviceService  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Seed Avenza demo data.")
    parser.add_argument("--dev-user", default=None, help="create a dev-enrolled customer with this display name")
    parser.add_argument("--language", default="en", help="language for the dev customer (default: en)")
    parser.add_argument(
        "--reset",
        action="store_true",
        help="delete an existing SQLite file first (refuses for non-SQLite databases)",
    )
    args = parser.parse_args(argv)

    settings = load_settings()
    if args.reset:
        if not settings.database_url.startswith("sqlite:///"):
            print("--reset only supports SQLite databases", file=sys.stderr)
            return 2
        path = Path(settings.database_url.replace("sqlite:///", "", 1))
        if not path.is_absolute():
            path = ROOT / path
        if path.exists():
            path.unlink()
            print(f"deleted {path}")
        else:
            print(f"no database at {path}")

    engine = make_engine(settings.database_url)
    init_db(engine)
    session_factory = make_session_factory(engine)
    seed_mock_bank(session_factory)

    print(f"database: {settings.database_url}")
    print(f"bank: {settings.bank_mode}" + (" (simulated)" if settings.bank_is_simulated else ""))
    print()
    print("Mock bank accounts:")
    for code, number, name in DEMO_ACCOUNTS:
        print(f"  {BANK_NAMES.get(code, code):<14} {number}  {name}")

    if args.dev_user:
        audit = AuditService(session_factory, engine.dialect.name)
        devices = DeviceService(session_factory, audit, WebAuthnService(settings), settings)
        if not devices.dev_tools_allowed():
            print(
                "\nDev enrolment is disabled (DEV_TOOLS must be true and APP_ENV must not be production).",
                file=sys.stderr,
            )
            engine.dispose()
            return 2
        registered = devices.dev_register(display_name=args.dev_user, language=args.language)
        print()
        print("Dev customer created (no fingerprint key):")
        print(f"  user_id:      {registered['user_id']}")
        print(f"  device_id:    {registered['device_id']}")
        print(f"  device_token: {registered['device_token']}")
        print()
        print("Open the PWA and use its enrolment flow instead if you want a real fingerprint.")

    engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
