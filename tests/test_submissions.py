"""Cancellation cannot lose a job between browser submission and its acknowledgement."""
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from soramimic_video import api
from soramimic_video.submissions import TTL_SECONDS, Submissions
from test_public_mode import public_app as public_app_fixture
from test_public_mode import submit, wait_done

public_app = public_app_fixture


def reserve(client):
    response = client.post('/api/submissions')
    assert response.status_code == 200, response.text
    assert response.headers['cache-control'] == 'private, no-store'
    return response.json()['id']


def registered(client, sid):
    return submit(client, submission_id=sid, registered_submission='true')


def cancel(client, sid):
    return client.post(f'/api/submissions/{sid}/cancel')


def test_status_recovers_accepted_job_without_canceling_it(public_app):
    client = TestClient(public_app)
    sid = reserve(client)
    url = f'/api/submissions/{sid}'
    for _ in range(2):
        response = client.get(url)
        assert response.status_code == 200
        assert response.headers['cache-control'] == 'private, no-store'
        assert response.json() == {'id': None, 'status': 'pending'}
    job_id = registered(client, sid).json()['id']
    assert client.get(url).json()['id'] == job_id
    assert wait_done(client, job_id)['status'] == 'done'
    assert client.get(url).json() == {'id': job_id, 'status': 'done'}
    assert len(public_app.state.manager.jobs) == 1
    assert not public_app.state.manager.jobs[job_id].cancel_event.is_set()
    assert TestClient(public_app).get(url).status_code == 410


def test_status_requires_valid_owned_reservation_and_auth(public_app, monkeypatch):
    client = TestClient(public_app)
    sid = reserve(client)
    url = f'/api/submissions/{sid}'
    assert TestClient(public_app).get(url).status_code == 404
    assert client.get('/api/submissions/invalid').status_code == 404
    assert cancel(client, sid).status_code == 200
    assert client.get(url).status_code == 410
    monkeypatch.setenv(api.API_KEY_ENV, 'secret')
    assert client.get(url).status_code == 401


def test_cancel_before_admission_remains_revoked_after_restart(public_app):
    client = TestClient(public_app)
    sid = reserve(client)
    assert cancel(client, sid).json() == {'id': None, 'status': 'canceled'}
    # Repeating after a lost cancel acknowledgement is safe and creates nothing.
    assert cancel(client, sid).json() == {'id': None, 'status': 'canceled'}
    root = public_app.state.submissions.root
    with pytest.raises(HTTPException) as exc:
        Submissions(root).require(sid, client.cookies[api.SESSION_COOKIE])
    assert exc.value.status_code == 410
    assert registered(client, sid).status_code == 410
    assert not public_app.state.manager.jobs


def test_cancel_during_validation_prevents_the_later_admission(public_app, monkeypatch):
    client = TestClient(public_app)
    sid = reserve(client)
    entered, release = Event(), Event()
    store = public_app.state.submissions
    original = store.require

    def validate(*args):
        original(*args)
        if not entered.is_set():
            entered.set()
            assert release.wait(5)

    monkeypatch.setattr(store, 'require', validate)
    with ThreadPoolExecutor(1) as executor:
        pending = executor.submit(registered, client, sid)
        try:
            assert entered.wait(5)
            assert cancel(client, sid).json()['status'] == 'canceled'
        finally:
            release.set()
        assert pending.result(timeout=5).status_code == 410
    assert not public_app.state.manager.jobs


def test_cancel_waiting_for_admission_cancels_the_accepted_job(public_app, monkeypatch):
    client = TestClient(public_app)
    sid = reserve(client)
    entered, release = Event(), Event()
    manager = public_app.state.manager
    original = manager.create

    def create(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return original(*args, **kwargs)

    def pipeline(job, config):
        job.cancel_event.wait(5)
        raise api.runproc.Cancelled()

    monkeypatch.setattr(manager, 'create', create)
    monkeypatch.setattr(api, 'run_pipeline', pipeline)
    with ThreadPoolExecutor(2) as executor:
        pending = executor.submit(registered, client, sid)
        try:
            assert entered.wait(5)
            cancellation = executor.submit(cancel, client, sid)
        finally:
            release.set()
        job_id = pending.result(timeout=5).json()['id']
        assert cancellation.result(timeout=5).json()['id'] == job_id
    assert wait_done(client, job_id)['status'] == 'canceled'
    assert len(manager.jobs) == 1


def test_cancel_after_finished_does_not_claim_the_job_was_stopped(public_app):
    client = TestClient(public_app)
    sid = reserve(client)
    job_id = registered(client, sid).json()['id']
    assert wait_done(client, job_id)['status'] == 'done'
    result = cancel(client, sid).json()
    assert result == {'id': job_id, 'status': 'done'}
    assert registered(client, sid).json()['id'] == job_id
    assert len(public_app.state.manager.jobs) == 1


def test_other_owner_cannot_revoke_reservation_or_cancel_job(public_app, monkeypatch):
    client, other = TestClient(public_app), TestClient(public_app)
    sid = reserve(client)
    assert cancel(other, sid).status_code == 404
    assert registered(other, sid).status_code == 404

    def pipeline(job, config):
        job.cancel_event.wait(5)
        raise api.runproc.Cancelled()

    monkeypatch.setattr(api, 'run_pipeline', pipeline)
    job_id = registered(client, sid).json()['id']
    assert cancel(other, sid).json().get('id') is None
    assert not public_app.state.manager.jobs[job_id].cancel_event.is_set()
    assert cancel(client, sid).json()['id'] == job_id
    assert wait_done(client, job_id)['status'] == 'canceled'


def test_reservations_expire_are_bounded_and_require_api_auth(tmp_path, monkeypatch):
    store = Submissions(tmp_path)
    ids = [store.create('owner', 'ip') for _ in range(8)]
    with pytest.raises(HTTPException) as exc:
        store.create('another-owner', 'ip')
    assert exc.value.status_code == 429
    store.remove(ids[0], 'owner')
    sid = store.create('owner', 'ip')
    store.cleanup(now=time.time() + TTL_SECONDS + 1)
    assert not list(tmp_path.iterdir())
    with pytest.raises(HTTPException):
        store.require(sid, 'owner')
    monkeypatch.setenv(api.API_KEY_ENV, 'secret')
    client = TestClient(api.create_app(jobs_dir=tmp_path / 'jobs'))
    assert client.post('/api/submissions').status_code == 401
    assert cancel(client, sid).status_code == 401
