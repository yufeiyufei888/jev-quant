from datetime import datetime, timezone

import pytest

from jevquant.store import EventStore


def test_event_store_is_idempotent_and_survives_reopen(tmp_path):
    path = tmp_path / "account.sqlite"
    at = datetime(2024, 1, 2, tzinfo=timezone.utc)
    with EventStore(path) as store:
        first = store.append("evt-1", "BUY_FILL", {"shares": 100, "price": "1500.00"}, at)
        repeated = store.append("evt-1", "BUY_FILL", {"shares": 100, "price": "1500.00"}, at)
        assert repeated.sequence == first.sequence
        store.append("evt-2", "MARK", {"price": "1510.00"}, at)
        assert store.verify()
    with EventStore(path) as restored:
        assert [e.event_id for e in restored.events()] == ["evt-1", "evt-2"]
        assert restored.verify()


def test_event_id_cannot_be_reused_for_different_payload(tmp_path):
    with EventStore(tmp_path / "account.sqlite") as store:
        store.append("evt-1", "BUY_FILL", {"shares": 100})
        with pytest.raises(ValueError, match="different event content"):
            store.append("evt-1", "BUY_FILL", {"shares": 200})
