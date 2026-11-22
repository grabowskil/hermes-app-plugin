
import sqlite3
from datetime import datetime, timedelta


from hermes_app import store

GATEWAY_TEXT = "⚠️ Gateway shutting down — Your current task will be interrupted."
CRON_TEXT = (
    'Cronjob Response: nightly-backup\n(job_id: a1b2c3d4)\n-------------\n\n'
    'Backup finished.\n\n'
    'To stop or manage this job, send me a new message (e.g. "stop reminder nightly-backup").'
)


def _use_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_APP_DATA_DIR", str(tmp_path))


def test_classify_gateway():
    n = store.classify(GATEWAY_TEXT)
    assert n["kind"] == "gateway"
    assert n["title"] == "Gateway"
    assert n["body"] == GATEWAY_TEXT
    assert n["job_id"] is None
    assert len(n["id"]) == 32
    datetime.fromisoformat(n["created_at"])


def test_classify_cron():
    n = store.classify(CRON_TEXT)
    assert n["kind"] == "cron"
    assert n["title"] == "nightly-backup"
    assert n["job_id"] == "a1b2c3d4"
    assert n["body"] == "Backup finished."


def test_append_and_latest(monkeypatch, tmp_path):
    _use_dir(monkeypatch, tmp_path)
    assert store.latest() == []
    ids = []
    for text in (GATEWAY_TEXT, CRON_TEXT, GATEWAY_TEXT):
        n = store.classify(text)
        ids.append(n["id"])
        store.append(n)
    with open(tmp_path / "notices.jsonl", "a", encoding="utf-8") as f:
        f.write("not json\n")
    got = store.latest()
    assert [n["id"] for n in got] == ids[::-1]
    assert [n["id"] for n in store.latest(limit=2)] == ids[::-1][:2]


def test_devices(monkeypatch, tmp_path):
    _use_dir(monkeypatch, tmp_path)
    assert store.load_devices() == []
    store.upsert_device("aa", "production")
    store.upsert_device("bb", "sandbox")
    store.upsert_device("aa", "sandbox")
    assert store.load_devices() == [
        {"token": "aa", "environment": "sandbox"},
        {"token": "bb", "environment": "sandbox"},
    ]
    store.remove_devices(["aa"])
    assert store.load_devices() == [{"token": "bb", "environment": "sandbox"}]


def test_cron_notice_session_backfill(monkeypatch, tmp_path):
    _use_dir(monkeypatch, tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    con = sqlite3.connect(tmp_path / "state.db")
    con.execute("CREATE TABLE sessions (id TEXT, started_at REAL)")
    notice = store.classify(CRON_TEXT)
    t = datetime.fromisoformat(notice["created_at"])
    for sid, dt in (("cron_a1b2c3d4_old", -3600), ("cron_a1b2c3d4_new", -30), ("cron_a1b2c3d4_later", 3600),
                    ("cron_other_x", -10)):
        con.execute("INSERT INTO sessions VALUES (?, ?)", (sid, (t + timedelta(seconds=dt)).timestamp()))
    con.commit()
    con.close()
    store.append(notice)
    store.append(store.classify(GATEWAY_TEXT))
    got = {n["kind"]: n["session_id"] for n in store.latest()}
    assert got == {"cron": "cron_a1b2c3d4_new", "gateway": None}
