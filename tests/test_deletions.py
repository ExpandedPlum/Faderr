"""The delete routine's safety rules, and the deletion job queue."""
import asyncio
from datetime import timedelta

import httpx
import pytest

import models
from config import config
from services import deletion_service, triage_service
from tests.conftest import add_artists, all_artists, all_jobs

BAND_FILES = ["/music/Band/Album/01.flac"]


@pytest.fixture
def routing(fake_plex, fake_lidarr):
    fake_plex.files["42"] = BAND_FILES
    fake_lidarr.artists = [{"id": 5, "artistName": "Band", "path": "/data/Band"}]
    return fake_plex, fake_lidarr


def run(name="Band", key="42", mbid=None):
    return asyncio.run(deletion_service.execute(name, key, mbid))


# ── Routing: which system deletes, and when nothing is deleted ───────────────

def test_matched_artist_deleted_via_lidarr_only(routing):
    plex, lidarr = routing
    outcome = run()
    assert (outcome.route, outcome.lidarr_id, outcome.folder) == ("lidarr", 5, "Band")
    assert lidarr.deleted == [5] and plex.deleted == []


def test_lidarr_unreachable_deletes_nothing(routing):
    plex, lidarr = routing
    lidarr.list_error = RuntimeError("connection refused")
    with pytest.raises(deletion_service.DeletionError):
        run()
    assert lidarr.deleted == [] and plex.deleted == []


def test_ambiguous_match_deletes_nothing(routing):
    plex, lidarr = routing
    lidarr.artists = [{"id": 5, "artistName": "Band", "path": "/data/Some Other Band"}]
    with pytest.raises(deletion_service.DeletionError):
        run()
    assert lidarr.deleted == [] and plex.deleted == []


def test_mbid_conflict_deletes_nothing(routing):
    plex, lidarr = routing
    lidarr.artists = [
        {"id": 5, "artistName": "Band", "path": "/data/Band", "foreignArtistId": "aaa"},
        {"id": 6, "artistName": "Band (other)", "path": "/data/Band (other)", "foreignArtistId": "bbb"},
    ]
    with pytest.raises(deletion_service.DeletionError, match="MusicBrainz"):
        run(mbid="bbb")  # Plex says it's artist bbb, but the files are in Band's folder
    assert lidarr.deleted == [] and plex.deleted == []


def test_artist_not_in_lidarr_falls_back_to_plex(routing):
    plex, lidarr = routing
    lidarr.artists = [{"id": 9, "artistName": "Someone Else", "path": "/data/Someone Else"}]
    assert run().route == "plex"
    assert lidarr.deleted == [] and plex.deleted == ["42"]


def test_lidarr_delete_fails_but_unmonitor_works_then_plex(routing):
    plex, lidarr = routing
    lidarr.delete_error = RuntimeError("500")
    assert run().route == "plex_after_unmonitor"
    assert lidarr.unmonitored == [5] and plex.deleted == ["42"]


def test_lidarr_delete_and_unmonitor_fail_deletes_nothing(routing):
    plex, lidarr = routing
    lidarr.delete_error = RuntimeError("500")
    lidarr.unmonitor_error = RuntimeError("500")
    with pytest.raises(deletion_service.DeletionError):
        run()
    assert plex.deleted == []


def test_failed_request_but_artist_gone_counts_as_deleted(routing):
    plex, lidarr = routing
    lidarr.delete_error = RuntimeError("502 from reverse proxy")
    lidarr.exists = False
    assert run().route == "lidarr_already_gone"
    assert lidarr.unmonitored == [] and plex.deleted == []


def test_timeout_while_lidarr_still_deleting_does_not_race_it(routing):
    plex, lidarr = routing
    lidarr.delete_error = httpx.ReadTimeout("timed out")
    with pytest.raises(deletion_service.DeletionError, match="may still be deleting"):
        run()
    assert lidarr.unmonitored == [] and plex.deleted == []


def test_cannot_confirm_after_failure_deletes_nothing_more(routing):
    plex, lidarr = routing
    lidarr.delete_error = RuntimeError("500")
    lidarr.exists_error = RuntimeError("connection refused")
    with pytest.raises(deletion_service.DeletionError):
        run()
    assert lidarr.unmonitored == [] and plex.deleted == []


# ── Queue: grace period, undo, worker, retry ──────────────────────────────────

def decide(artist_id, decision):
    return asyncio.run(triage_service.make_decision(artist_id, decision))


def run_due():
    return asyncio.run(deletion_service.worker.run_due())


def make_due():
    """End every pending job's grace period now."""
    async def _go():
        async with models.async_session() as session:
            async with session.begin():
                for job in (await session.execute(models.Deletion.__table__.select())).all():
                    await session.execute(
                        models.Deletion.__table__.update().where(models.Deletion.id == job.id)
                        .values(run_after=models.utcnow() - timedelta(seconds=1))
                    )
    asyncio.run(_go())


def test_delete_is_queued_with_grace_period_and_not_run_early(routing):
    plex, lidarr = routing
    (aid,) = add_artists({"artist_name": "Band", "plex_artist_key": "42"})
    result = decide(aid, "delete")
    assert result["decision"] == "delete"
    job = result["deletion"]
    assert job["status"] == "pending" and job["cancellable"]
    assert run_due() == 0  # still in the grace period
    assert lidarr.deleted == [] and plex.deleted == []


def test_undo_during_grace_restores_previous_decision(routing):
    plex, lidarr = routing
    (aid,) = add_artists({"artist_name": "Band", "plex_artist_key": "42", "decision": "keep"})
    decide(aid, "delete")
    result = asyncio.run(triage_service.undo_decision(aid))
    assert result["decision"] == "keep"
    assert all_jobs()[0]["status"] == "cancelled"
    make_due()
    assert run_due() == 0
    assert lidarr.deleted == [] and plex.deleted == []


def test_worker_runs_due_job_and_records_outcome(routing):
    plex, lidarr = routing
    (aid,) = add_artists({"artist_name": "Band", "plex_artist_key": "42"})
    decide(aid, "delete")
    make_due()
    assert run_due() == 1
    (job,) = all_jobs()
    assert job["status"] == "done"
    assert (job["route"], job["lidarr_id"], job["lidarr_folder"]) == ("lidarr", 5, "Band")
    assert job["attempts"] == 1
    assert all_artists()[0]["lidarr_id"] == 5
    assert asyncio.run(triage_service.undo_decision(aid))["status_code"] == 409


def test_failed_job_can_be_retried(routing):
    plex, lidarr = routing
    lidarr.list_error = RuntimeError("connection refused")
    (aid,) = add_artists({"artist_name": "Band", "plex_artist_key": "42"})
    decide(aid, "delete")
    make_due()
    run_due()
    (job,) = all_jobs()
    assert job["status"] == "failed" and "unreachable" in job["error"]
    assert all_artists()[0]["decision"] == "delete"  # still waiting on the user

    lidarr.list_error = None
    assert asyncio.run(deletion_service.retry(job["id"])).status == "pending"
    assert run_due() == 1
    assert all_jobs()[0]["status"] == "done" and all_jobs()[0]["attempts"] == 2
    assert lidarr.deleted == [5]


def test_failed_job_can_be_cancelled(routing):
    plex, lidarr = routing
    lidarr.list_error = RuntimeError("down")
    (aid,) = add_artists({"artist_name": "Band", "plex_artist_key": "42"})
    decide(aid, "delete")
    make_due()
    run_due()
    job = all_jobs()[0]
    assert asyncio.run(triage_service.cancel_deletion(job["id"]))["status"] == "cancelled"
    assert all_artists()[0]["decision"] is None


def test_only_done_or_failed_jobs_cannot_be_retried(routing):
    (aid,) = add_artists({"artist_name": "Band", "plex_artist_key": "42"})
    decide(aid, "delete")
    assert asyncio.run(deletion_service.retry(all_jobs()[0]["id"])) is None


def test_zero_grace_runs_immediately(routing, monkeypatch):
    monkeypatch.setattr(config, "DELETE_GRACE_SECONDS", 0)
    (aid,) = add_artists({"artist_name": "Band", "plex_artist_key": "42"})
    decide(aid, "delete")
    assert run_due() == 1


def test_a_job_is_claimed_only_once(routing):
    (aid,) = add_artists({"artist_name": "Band", "plex_artist_key": "42"})
    decide(aid, "delete")
    make_due()

    async def two_workers():
        return await asyncio.gather(deletion_service.claim_next(), deletion_service.claim_next())
    claims = asyncio.run(two_workers())
    assert sorted(c is not None for c in claims) == [False, True]


def test_interrupted_job_is_marked_failed_not_rerun(routing):
    plex, lidarr = routing
    (aid,) = add_artists({"artist_name": "Band", "plex_artist_key": "42"})
    decide(aid, "delete")
    make_due()
    job_id = asyncio.run(deletion_service.claim_next())

    async def age_heartbeat():
        async with models.async_session() as session:
            async with session.begin():
                job = await session.get(models.Deletion, job_id)
                job.heartbeat_at = models.utcnow() - timedelta(minutes=10)
    asyncio.run(age_heartbeat())

    assert run_due() == 0
    job = all_jobs()[0]
    assert job["status"] == "failed" and "Interrupted" in job["error"]
    assert lidarr.deleted == [] and plex.deleted == []


def test_database_allows_one_active_job_per_artist(routing):
    (aid,) = add_artists({"artist_name": "Band", "plex_artist_key": "42"})

    async def two_jobs():
        async with models.async_session() as session:
            artist = await session.get(models.TriageArtist, aid)
            session.add_all([deletion_service.new_job(artist, None), deletion_service.new_job(artist, None)])
            await session.commit()
    with pytest.raises(Exception, match="UNIQUE"):
        asyncio.run(two_jobs())


def test_no_other_decision_while_delete_pending(routing):
    (aid,) = add_artists({"artist_name": "Band", "plex_artist_key": "42"})
    decide(aid, "delete")
    assert decide(aid, "keep")["status_code"] == 409
    assert decide(aid, "delete")["status_code"] == 409
    assert len(all_jobs()) == 1


def test_concurrent_keep_and_delete_leave_one_consistent_result(routing):
    (aid,) = add_artists({"artist_name": "Band", "plex_artist_key": "42"})

    async def both():
        return await asyncio.gather(
            triage_service.make_decision(aid, "delete"),
            triage_service.make_decision(aid, "keep"),
        )
    asyncio.run(both())
    final = all_artists()[0]["decision"]
    jobs = all_jobs()
    # Whichever ran second either kept a delete-free artist or was refused
    assert (final == "delete" and len(jobs) == 1) or (final == "keep" and jobs == [])
