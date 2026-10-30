from csv_cleaner.engine import CleaningOptions, clean, parse_csv
from csv_cleaner.state import SessionStore


def test_idle_expiry_refreshes_only_on_use():
    now = [0]
    store = SessionStore(ttl=30, clock=lambda: now[0])
    store.put("one", parse_csv(b"a\nx\n"))
    now[0] = 25
    assert store.get("one") is not None
    now[0] = 54
    store.prune()
    assert "one" in store._jobs
    now[0] = 55
    assert store.get("one") is None


def test_reset_or_replacement_cannot_be_undone_by_stale_preview():
    store = SessionStore()
    data = parse_csv(b"a\nx\n")
    store.put("one", data)
    old_job = store.get("one")
    result = clean(data, CleaningOptions())
    store.discard("one")
    assert not store.set_result("one", old_job, result)
    store.put("one", data)
    assert not store.set_result("one", old_job, result)
    assert store.get("one").result is None
