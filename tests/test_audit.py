from __future__ import annotations

from sqlalchemy import select

from avenza.audit import GENESIS_HASH, AuditService, compute_row_hash
from avenza.models import AuditLog
from avenza.timeutil import iso


def test_chain_links_and_verifies(services):
    audit = services["audit"]
    for index in range(5):
        audit.record_standalone("test.event", details={"index": index})

    with services["session_factory"]() as db:
        rows = db.scalars(select(AuditLog).order_by(AuditLog.seq.asc())).all()
        assert len(rows) == 5
        assert rows[0].previous_hash == GENESIS_HASH
        for previous, current in zip(rows, rows[1:]):
            assert current.previous_hash == previous.row_hash
        result = audit.verify(db)
        assert result["ok"] is True
        assert result["entries"] == 5
        assert result["head_hash"] == rows[-1].row_hash


def test_tampered_details_are_detected(services):
    audit = services["audit"]
    audit.record_standalone("test.event", details={"amount_kobo": 500_000})
    audit.record_standalone("test.event", details={"amount_kobo": 900_000})

    with services["session_factory"]() as db:
        row = db.scalars(select(AuditLog).order_by(AuditLog.seq.asc())).first()
        row.details = {"amount_kobo": 1}
        db.commit()

    with services["session_factory"]() as db:
        result = audit.verify(db)
        assert result["ok"] is False
        assert result["reason"] == "row_hash_mismatch"
        assert result["first_bad_seq"] is not None


def test_tampered_previous_hash_is_detected(services):
    audit = services["audit"]
    audit.record_standalone("one")
    audit.record_standalone("two")

    with services["session_factory"]() as db:
        row = db.scalars(select(AuditLog).order_by(AuditLog.seq.desc())).first()
        row.previous_hash = "f" * 64
        db.commit()

    with services["session_factory"]() as db:
        result = audit.verify(db)
        assert result["ok"] is False
        assert result["reason"] == "previous_hash_mismatch"


def test_deleted_row_breaks_chain(services):
    audit = services["audit"]
    for index in range(3):
        audit.record_standalone("event", details={"index": index})

    with services["session_factory"]() as db:
        middle = db.scalars(select(AuditLog).order_by(AuditLog.seq.asc())).all()[1]
        db.delete(middle)
        db.commit()

    with services["session_factory"]() as db:
        result = audit.verify(db)
        assert result["ok"] is False
        assert result["reason"] == "previous_hash_mismatch"


def test_row_hash_is_deterministic():
    from avenza.timeutil import utcnow

    created = utcnow()
    kwargs = dict(
        entry_id="abc",
        event="test",
        transfer_id="tr_1",
        session_id="ses_1",
        details={"a": 1, "b": [2, 3]},
        previous_hash=GENESIS_HASH,
        created_at=created,
    )
    assert compute_row_hash(**kwargs) == compute_row_hash(**kwargs)
    assert compute_row_hash(**kwargs) != compute_row_hash(**{**kwargs, "event": "other"})


def test_audit_uses_naive_utc_iso():
    from avenza.timeutil import utcnow

    assert iso(utcnow()).endswith("Z")
