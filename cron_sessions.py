"""Chat sessions for ``no_agent`` cron runs.

A ``no_agent`` job runs its script and delivers stdout; the gateway opens no session for it, so the run
only shows up as a notice and cannot be answered. When such a delivery reaches this adapter, the run is
written into ``state.db`` the way the scheduler writes an agent cron run: a ``cron`` session titled
``<job name> · <Mon DD HH:MM>``, the job as the user turn and the output as the reply. The app lists it
under CRON, and a reply there continues it like any other session.
"""
import logging

logger = logging.getLogger(__name__)


def record(notice: dict) -> str | None:
    """Write the session for a ``no_agent`` cron notice. Returns its id, else None. Never raises."""
    job_id = notice.get("job_id")
    if notice.get("kind") != "cron" or not job_id or not notice.get("body"):
        return None
    try:
        from cron.jobs import get_job

        job = get_job(job_id)
        if not job or not job.get("no_agent"):
            return None
        return _write(job, notice["body"])
    except Exception:
        logger.exception("hermes_app: no session for cron job %s", job_id)
        return None


def _write(job: dict, output: str) -> str:
    from hermes_state_registry import acquire, release_or_close
    from hermes_time import now, safe_strftime

    try:
        from hermes_cli.profiles import get_active_profile_name

        profile = get_active_profile_name()
    except Exception:
        profile = None
    stamp = now()
    job_id = job["id"]
    name = " ".join((job.get("name") or "").split())[:60] or f"cron {job_id}"
    session_id = f"cron_{job_id}_{stamp.strftime('%Y%m%d_%H%M%S')}"
    db = acquire()
    try:
        db.create_session(session_id=session_id, source="cron", profile_name=profile)
        try:
            db.set_session_title(session_id, f"{name} · {safe_strftime(stamp, '%b %d %H:%M')}")
        except ValueError:  # same job twice in one minute: the id still tells them apart
            pass
        script = job.get("script") or "its script"
        db.append_message(session_id, "user", f'Run the cron job "{name}" ({script}, no agent).')
        db.append_message(session_id, "assistant", output, finish_reason="stop")
        db.end_session(session_id, "cron_complete")
    finally:
        release_or_close(db)
    return session_id
