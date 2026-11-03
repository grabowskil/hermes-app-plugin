import cron.jobs
import hermes_state_registry

from hermes_app import cron_sessions

JOBS = {
    "nojob1": {"id": "nojob1", "name": "Nightly cleanup", "no_agent": True, "script": "clean.sh"},
    "agent1": {"id": "agent1", "name": "Brief", "no_agent": False},
}


class FakeDB:
    # The CI interpreter cannot write a real state.db; the gateway runtime can (checked by hand).
    def __init__(self):
        self.sessions, self.messages = {}, []

    def create_session(self, session_id, source, **kwargs):
        self.sessions[session_id] = {"source": source, **kwargs}

    def set_session_title(self, session_id, title):
        self.sessions[session_id]["title"] = title

    def append_message(self, session_id, role, content, **kwargs):
        self.messages.append((session_id, role, content))

    def end_session(self, session_id, reason):
        self.sessions[session_id]["end_reason"] = reason


def _setup(monkeypatch):
    db = FakeDB()
    monkeypatch.setattr(cron.jobs, "get_job", JOBS.get)
    monkeypatch.setattr(hermes_state_registry, "acquire", lambda: db)
    monkeypatch.setattr(hermes_state_registry, "release_or_close", lambda _: None)
    return db


def _notice(job_id, body="Removed 3 items."):
    return {"kind": "cron", "job_id": job_id, "title": "x", "body": body}


def test_no_agent_run_becomes_a_cron_session(monkeypatch):
    db = _setup(monkeypatch)
    session_id = cron_sessions.record(_notice("nojob1"))
    assert session_id.startswith("cron_nojob1_")
    row = db.sessions[session_id]
    assert row["source"] == "cron"
    assert row["title"].startswith("Nightly cleanup · ")
    assert row["end_reason"] == "cron_complete"
    assert [(r, c) for _, r, c in db.messages][1] == ("assistant", "Removed 3 items.")


def test_agent_jobs_and_gateway_notices_are_skipped(monkeypatch):
    db = _setup(monkeypatch)
    assert cron_sessions.record(_notice("agent1")) is None
    assert cron_sessions.record(_notice("missing")) is None
    assert cron_sessions.record({"kind": "gateway", "body": "Gateway online"}) is None
    assert db.sessions == {}
