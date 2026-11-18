"""End-to-end tests for the waveform assembly API.

Each test gets an isolated SQLite file so runs never touch real data.
"""
from __future__ import annotations

import os
import tempfile
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(monkeypatch):
    tmpdir = tempfile.mkdtemp(prefix="wf-test-")
    db_path = Path(tmpdir) / "test.db"
    monkeypatch.setenv("WAVEFORM_DB_PATH", str(db_path))

    # Import after the env var is set so config picks up the test database.
    import importlib

    from app import config, database, main, service

    # Reload in dependency order so every module re-reads the test DB path.
    importlib.reload(config)
    importlib.reload(database)
    importlib.reload(service)
    importlib.reload(main)
    database.init_db()
    with TestClient(main.app) as c:
        yield c
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(db_path) + suffix)
        if p.exists():
            p.unlink()
    os.rmdir(tmpdir)


MIN = "2026-11-17T05:06:00Z"


def frag(no, rev, start, samples, station="STA1", minute=MIN):
    return {
        "station": station,
        "minute_utc": minute,
        "fragment_no": no,
        "revision": rev,
        "start_sample": start,
        "samples": samples,
    }


# --------------------------------------------------------------------------- #
# Health & validation
# --------------------------------------------------------------------------- #
def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_boundary_rejected(client):
    # start 55 + 6 samples reaches slot 61 -> out of bounds
    r = client.post("/fragments", json=frag(1, 1, 55, [1, 2, 3, 4, 5, 6]))
    assert r.status_code == 422
    assert "boundary" in r.json()["detail"]


def test_non_whole_minute_rejected(client):
    payload = frag(1, 1, 0, [1])
    payload["minute_utc"] = "2026-11-17T05:06:30Z"
    r = client.post("/fragments", json=payload)
    assert r.status_code == 422


def test_revision_must_be_positive(client):
    r = client.post("/fragments", json=frag(1, 0, 0, [1]))
    assert r.status_code == 422


def test_empty_samples_rejected(client):
    r = client.post("/fragments", json=frag(1, 1, 0, []))
    assert r.status_code == 422


# --------------------------------------------------------------------------- #
# Revision semantics
# --------------------------------------------------------------------------- #
def test_upload_and_retrieve(client):
    r = client.post("/fragments", json=frag(1, 1, 0, [10, 20]))
    assert r.status_code == 201
    body = r.json()
    assert body["status"] == "accepted"
    assert body["revision"] == 1
    assert len(body["content_hash"]) == 64

    r = client.get("/fragments", params={"station": "STA1", "minute_utc": MIN})
    assert r.status_code == 200
    assert len(r.json()) == 1


def test_lower_revision_rejected(client):
    client.post("/fragments", json=frag(1, 3, 0, [10, 20]))
    r = client.post("/fragments", json=frag(1, 2, 0, [99]))
    assert r.status_code == 409
    assert "not higher" in r.json()["detail"]
    # stored fragment untouched
    r = client.get("/fragments", params={"station": "STA1", "minute_utc": MIN})
    assert r.json()[0]["revision"] == 3


def test_same_revision_same_content_idempotent(client):
    r1 = client.post("/fragments", json=frag(1, 2, 0, [10, 20]))
    r2 = client.post("/fragments", json=frag(1, 2, 0, [10, 20]))
    assert r1.status_code == 201
    assert r2.status_code == 201
    assert r2.json()["status"] == "unchanged"
    assert r1.json()["content_hash"] == r2.json()["content_hash"]
    r = client.get("/fragments", params={"station": "STA1", "minute_utc": MIN})
    assert len(r.json()) == 1


def test_same_revision_different_content_rejected(client):
    client.post("/fragments", json=frag(1, 2, 0, [10, 20]))
    r = client.post("/fragments", json=frag(1, 2, 0, [10, 21]))
    assert r.status_code == 409
    assert "different content" in r.json()["detail"]
    r = client.get("/fragments", params={"station": "STA1", "minute_utc": MIN})
    assert r.json()[0]["samples"] == [10, 20]


def test_higher_revision_supersedes(client):
    client.post("/fragments", json=frag(1, 1, 0, [10, 20]))
    r = client.post("/fragments", json=frag(1, 2, 0, [11, 21]))
    assert r.status_code == 201
    assert r.json()["status"] == "superseded"
    r = client.get("/fragments", params={"station": "STA1", "minute_utc": MIN})
    assert r.json()[0]["revision"] == 2
    assert r.json()[0]["samples"] == [11, 21]


# --------------------------------------------------------------------------- #
# Assembly: gaps and conflicts
# --------------------------------------------------------------------------- #
def test_assembly_gaps(client):
    client.post("/fragments", json=frag(1, 1, 0, [1, 2, 3]))
    r = client.get("/assembly", params={"station": "STA1", "minute_utc": MIN})
    body = r.json()
    assert body["complete"] is False
    assert body["coverage"] == 3
    assert body["gaps"] == list(range(3, 60))
    assert body["conflicts"] == []


def test_assembly_conflict_reports_position_and_fragments(client):
    client.post("/fragments", json=frag(1, 1, 0, [1, 2, 3]))
    client.post("/fragments", json=frag(2, 1, 2, [9, 4]))  # overlap at slot 2
    r = client.get("/assembly", params={"station": "STA1", "minute_utc": MIN})
    body = r.json()
    assert body["complete"] is False
    assert body["conflicts"] == [
        {"position": 2, "fragment_nos": [1, 2], "values": [3, 9]}
    ]
    assert body["gaps"] == list(range(4, 60))


def test_overlap_same_value_is_not_conflict(client):
    client.post("/fragments", json=frag(1, 1, 0, [1, 2, 3]))
    client.post("/fragments", json=frag(2, 1, 2, [3, 4]))  # slot 2 agrees
    r = client.get("/assembly", params={"station": "STA1", "minute_utc": MIN})
    body = r.json()
    assert body["conflicts"] == []
    assert body["coverage"] == 4
    assert body["gaps"] == list(range(4, 60))


def test_complete_minute_assembly(client):
    client.post("/fragments", json=frag(1, 1, 0, list(range(0, 30))))
    client.post("/fragments", json=frag(2, 1, 30, list(range(30, 60))))
    r = client.get("/assembly", params={"station": "STA1", "minute_utc": MIN})
    body = r.json()
    assert body["complete"] is True
    assert body["coverage"] == 60
    assert body["gaps"] == []
    assert body["conflicts"] == []


# --------------------------------------------------------------------------- #
# Publishing
# --------------------------------------------------------------------------- #
def test_publish_fails_with_gaps(client):
    client.post("/fragments", json=frag(1, 1, 0, [1, 2]))
    r = client.post("/publications", params={"station": "STA1", "minute_utc": MIN})
    assert r.status_code == 409
    assert "gaps" in r.json()["detail"]


def test_publish_fails_with_conflict(client):
    client.post("/fragments", json=frag(1, 1, 0, [1] + [0] * 29))
    client.post("/fragments", json=frag(2, 1, 0, [9] + [0] * 59))
    r = client.post("/publications", params={"station": "STA1", "minute_utc": MIN})
    assert r.status_code == 409
    assert "conflicts" in r.json()["detail"]
    # failed publish leaves nothing behind
    r = client.get("/publications", params={"station": "STA1"})
    assert r.json() == []


def test_full_publish_lifecycle(client):
    wave_a = list(range(60))
    client.post("/fragments", json=frag(1, 1, 0, wave_a[:30]))
    client.post("/fragments", json=frag(2, 1, 30, wave_a[30:]))

    r = client.post("/publications", params={"station": "STA1", "minute_utc": MIN})
    assert r.status_code == 201
    pub = r.json()
    pub_id = pub["id"]
    assert pub["wave"] == wave_a
    assert pub["stale"] is False
    assert sorted(it["fragment_no"] for it in pub["items"]) == [1, 2]
    assert {it["revision"] for it in pub["items"]} == {1}
    # revisions and per-fragment waveforms are pinned in the record
    pinned = {it["fragment_no"]: it["samples"] for it in pub["items"]}
    assert pinned[1] == wave_a[:30]
    assert pinned[2] == wave_a[30:]

    # publishing the same fragment set again is idempotent
    r2 = client.post("/publications", params={"station": "STA1", "minute_utc": MIN})
    assert r2.json()["id"] == pub_id

    # a later correction changes current assembly but must not mutate the record
    client.post("/fragments", json=frag(1, 2, 0, [100] + wave_a[1:30]))
    r = client.get(f"/publications/{pub_id}")
    old = r.json()
    assert old["wave"] == wave_a  # frozen waveform
    assert old["stale"] is True
    assert old["items"][0]["revision"] == 1  # frozen revisions

    # the new fragment set can itself be published
    new_wave = [100] + wave_a[1:]
    r = client.post("/publications", params={"station": "STA1", "minute_utc": MIN})
    assert r.status_code == 201
    pub2 = r.json()
    assert pub2["id"] != pub_id
    assert pub2["wave"] == new_wave
    assert pub2["stale"] is False

    # both records remain readable; latest endpoint returns the new one
    r = client.get(
        "/publications/latest",
        params={"station": "STA1", "minute_utc": MIN},
    )
    assert r.json()["id"] == pub2["id"]
    r = client.get("/publications", params={"station": "STA1", "minute_utc": MIN})
    ids = [p["id"] for p in r.json()]
    assert ids == [pub_id, pub2["id"]]
    assert [p["stale"] for p in r.json()] == [True, False]


def test_supersede_marks_previous_publication_stale(client):
    client.post("/fragments", json=frag(1, 1, 0, list(range(0, 30))))
    client.post("/fragments", json=frag(2, 1, 30, list(range(30, 60))))
    pub = client.post(
        "/publications", params={"station": "STA1", "minute_utc": MIN}
    ).json()
    assert pub["stale"] is False

    # replacement fragment at same number, higher revision, different span
    client.post("/fragments", json=frag(2, 2, 29, [77] + list(range(30, 60))))
    r = client.get(f"/publications/{pub['id']}")
    assert r.json()["stale"] is True


def test_idempotent_retry_does_not_stale_publication(client):
    client.post("/fragments", json=frag(1, 1, 0, list(range(0, 30))))
    client.post("/fragments", json=frag(2, 1, 30, list(range(30, 60))))
    pub = client.post(
        "/publications", params={"station": "STA1", "minute_utc": MIN}
    ).json()

    # same version, same content retransmission: fragment set unchanged
    client.post("/fragments", json=frag(2, 1, 30, list(range(30, 60))))
    r = client.get(f"/publications/{pub['id']}")
    assert r.json()["stale"] is False
    # no duplicate publication either
    again = client.post(
        "/publications", params={"station": "STA1", "minute_utc": MIN}
    ).json()
    assert again["id"] == pub["id"]


# --------------------------------------------------------------------------- #
# Restart durability
# --------------------------------------------------------------------------- #
def test_data_survives_restart(client, monkeypatch, tmp_path):
    client.post("/fragments", json=frag(1, 1, 0, list(range(0, 30))))
    client.post("/fragments", json=frag(2, 1, 30, list(range(30, 60))))
    pub = client.post(
        "/publications", params={"station": "STA1", "minute_utc": MIN}
    ).json()

    # Simulate a process restart: rebuild app objects against the same file.
    import importlib

    from app import config, database, main, service

    importlib.reload(config)
    importlib.reload(database)
    importlib.reload(service)
    importlib.reload(main)
    with TestClient(main.app) as restarted:
        r = restarted.get(f"/publications/{pub['id']}")
        assert r.status_code == 200
        body = r.json()
        assert body["wave"] == list(range(60))
        assert body["stale"] is False
        r = restarted.get(
            "/assembly", params={"station": "STA1", "minute_utc": MIN}
        )
        assert r.json()["complete"] is True


# --------------------------------------------------------------------------- #
# Concurrency: interleaved uploads and publishes serialize on one snapshot
# --------------------------------------------------------------------------- #
def test_concurrent_uploads_and_publishes(monkeypatch):
    """Hit a real uvicorn server with parallel uploads/publishes.

    Every publication must come from one committed fragment set and failed
    publishes must never leave partial rows behind.
    """
    import socket
    import time

    import httpx
    import uvicorn

    tmpdir = tempfile.mkdtemp(prefix="wf-live-")
    db_path = Path(tmpdir) / "live.db"
    monkeypatch.setenv("WAVEFORM_DB_PATH", str(db_path))

    import importlib

    from app import config, database, main, service

    importlib.reload(config)
    importlib.reload(database)
    importlib.reload(service)
    importlib.reload(main)
    database.init_db()

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]

    server = uvicorn.Server(
        uvicorn.Config(main.app, host="127.0.0.1", port=port, log_level="error")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"
    deadline = time.time() + 15
    while time.time() < deadline:
        try:
            if httpx.get(base + "/health", timeout=1).status_code == 200:
                break
        except httpx.TransportError:
            time.sleep(0.1)
    else:  # pragma: no cover
        raise RuntimeError("test server did not start")

    errors: list[Exception] = []
    barrier = threading.Barrier(6)

    def worker(idx: int) -> None:
        try:
            no = idx + 1
            barrier.wait()
            r = httpx.post(
                base + "/fragments",
                json=frag(no, 1, (no - 1) * 10, [no * 100] * 10),
                timeout=10,
            )
            assert r.status_code == 201, r.text
            rp = httpx.post(
                base + "/publications",
                params={"station": "STA1", "minute_utc": MIN},
                timeout=10,
            )
            # incomplete snapshots fail cleanly; the complete one succeeds —
            # a 500/half-written row would be a correctness bug
            assert rp.status_code in (201, 409), rp.text
        except Exception as exc:  # pragma: no cover - failure reporting
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors

    with httpx.Client(base_url=base, timeout=10) as http:
        r = http.get("/fragments", params={"station": "STA1", "minute_utc": MIN})
        assert len(r.json()) == 6
        pubs = http.post(
            "/publications", params={"station": "STA1", "minute_utc": MIN}
        ).json()
        assert pubs["wave"] == [
            v for no in range(1, 7) for v in [no * 100] * 10
        ]
        listing = http.get(
            "/publications", params={"station": "STA1", "minute_utc": MIN}
        ).json()
        # only one publication row for a single fragment set
        assert len(listing) == 1

    server.should_exit = True
    thread.join(timeout=10)


def test_station_isolation(client):
    client.post("/fragments", json=frag(1, 1, 0, [1, 2, 3], station="STA1"))
    client.post("/fragments", json=frag(1, 1, 0, [4, 5, 6], station="STA2"))
    r = client.get("/fragments", params={"station": "STA2", "minute_utc": MIN})
    assert r.json()[0]["samples"] == [4, 5, 6]
    r = client.get("/assembly", params={"station": "STA1", "minute_utc": MIN})
    assert r.json()["coverage"] == 3


# --------------------------------------------------------------------------- #
# Withdrawal and current supply
# --------------------------------------------------------------------------- #
def _publish_complete(client, station="STA1", minute=MIN, wave=None):
    if wave is None:
        wave = list(range(60))
    client.post(
        "/fragments", json=frag(1, 1, 0, wave[:30], station=station, minute=minute)
    )
    client.post(
        "/fragments", json=frag(2, 1, 30, wave[30:], station=station, minute=minute)
    )
    r = client.post(
        "/publications", params={"station": station, "minute_utc": minute}
    )
    assert r.status_code == 201
    return r.json()


def test_withdraw_stops_supply_but_record_stays_readable(client):
    pub = _publish_complete(client)

    # Before withdrawal the publication is supplied.
    supply = client.get("/supply").json()
    assert [p["id"] for p in supply] == [pub["id"]]

    r = client.post(
        f"/publications/{pub['id']}/withdrawal",
        json={"reason": "equipment fault pollutes this minute"},
    )
    assert r.status_code == 201
    body = r.json()
    assert body["id"] == pub["id"]
    assert body["withdrawn"] is True
    assert body["withdrawal_reason"] == "equipment fault pollutes this minute"
    assert body["withdrawn_at"] is not None
    # frozen waveform and fragment provenance are still present
    assert body["wave"] == list(range(60))
    assert sorted((i["fragment_no"], i["revision"]) for i in body["items"]) == [
        (1, 1),
        (2, 1),
    ]

    # Current supply no longer contains it (also with explicit filters).
    assert client.get("/supply").json() == []
    assert (
        client.get(
            "/supply", params={"station": "STA1", "minute_utc": MIN}
        ).json()
        == []
    )

    # The withdrawn record stays fully readable via every publication endpoint.
    got = client.get(f"/publications/{pub['id']}").json()
    assert got["withdrawn"] is True
    assert got["withdrawal_reason"] == "equipment fault pollutes this minute"
    assert got["wave"] == list(range(60))
    assert len(got["items"]) == 2
    listing = client.get(
        "/publications", params={"station": "STA1", "minute_utc": MIN}
    ).json()
    assert listing[0]["id"] == pub["id"]
    assert listing[0]["withdrawn"] is True
    latest = client.get(
        "/publications/latest", params={"station": "STA1", "minute_utc": MIN}
    ).json()
    assert latest["id"] == pub["id"] and latest["withdrawn"] is True


def test_withdraw_same_id_same_reason_is_idempotent(client):
    pub = _publish_complete(client)
    payload = {"reason": "sensor failure"}
    r1 = client.post(f"/publications/{pub['id']}/withdrawal", json=payload)
    assert r1.status_code == 201
    ts = r1.json()["withdrawn_at"]

    r2 = client.post(f"/publications/{pub['id']}/withdrawal", json=payload)
    assert r2.status_code == 200
    body = r2.json()
    # The original withdrawal is reused, not re-recorded.
    assert body["withdrawn"] is True
    assert body["withdrawn_at"] == ts
    assert body["withdrawal_reason"] == "sensor failure"


def test_withdraw_same_id_different_reason_rejected(client):
    pub = _publish_complete(client)
    r = client.post(
        f"/publications/{pub['id']}/withdrawal", json={"reason": "reason A"}
    )
    assert r.status_code == 201

    r = client.post(
        f"/publications/{pub['id']}/withdrawal", json={"reason": "reason B"}
    )
    assert r.status_code == 409
    # original withdrawal untouched
    got = client.get(f"/publications/{pub['id']}").json()
    assert got["withdrawal_reason"] == "reason A"
    assert client.get("/supply").json() == []


def test_withdraw_unknown_id_404(client):
    r = client.post("/publications/9999/withdrawal", json={"reason": "x"})
    assert r.status_code == 404
    assert "9999" in r.json()["detail"]


def test_withdraw_requires_non_blank_reason(client):
    pub = _publish_complete(client)
    r = client.post(f"/publications/{pub['id']}/withdrawal", json={"reason": "   "})
    assert r.status_code == 422
    # still effective
    assert client.get("/supply").json()[0]["id"] == pub["id"]


def test_stale_publication_cannot_be_withdrawn(client):
    pub = _publish_complete(client)
    # Fragment set changes: the publication becomes stale.
    client.post("/fragments", json=frag(1, 2, 0, [100] + list(range(1, 30))))
    r = client.post(
        f"/publications/{pub['id']}/withdrawal", json={"reason": "fault"}
    )
    assert r.status_code == 409
    got = client.get(f"/publications/{pub['id']}").json()
    assert got["stale"] is True and got["withdrawn"] is False


def test_withdrawn_set_cannot_republish_same_set(client):
    pub = _publish_complete(client)
    client.post(
        f"/publications/{pub['id']}/withdrawal",
        json={"reason": "equipment fault"},
    )
    # Re-publishing the identical fragment set must not restore the supply.
    r = client.post(
        "/publications", params={"station": "STA1", "minute_utc": MIN}
    )
    assert r.status_code == 409
    assert "withdrawn" in r.json()["detail"]
    # No new publication row, supply stays empty.
    listing = client.get(
        "/publications", params={"station": "STA1", "minute_utc": MIN}
    ).json()
    assert [p["id"] for p in listing] == [pub["id"]]
    assert client.get("/supply").json() == []


def test_supply_resumes_only_after_fragment_set_changes(client):
    wave = list(range(60))
    pub = _publish_complete(client, wave=wave)
    client.post(
        f"/publications/{pub['id']}/withdrawal",
        json={"reason": "equipment fault"},
    )

    # Higher revision really changes the fragment set -> a new publication is
    # allowed and supply resumes.
    corrected = [42] + wave[1:]
    client.post("/fragments", json=frag(1, 2, 0, corrected[:30]))
    r = client.post(
        "/publications", params={"station": "STA1", "minute_utc": MIN}
    )
    assert r.status_code == 201
    new_pub = r.json()
    assert new_pub["id"] != pub["id"]
    assert new_pub["withdrawn"] is False
    assert new_pub["wave"] == corrected

    supply = client.get(
        "/supply", params={"station": "STA1", "minute_utc": MIN}
    ).json()
    assert [p["id"] for p in supply] == [new_pub["id"]]
    # The withdrawn record remains readable for audit.
    old = client.get(f"/publications/{pub['id']}").json()
    assert old["withdrawn"] is True
    assert old["wave"] == wave


def test_backfill_can_restore_supply_after_withdrawal(client):
    # Publish a minute assembled from fragments 1..6 (10 slots each).
    wave = [v for no in range(1, 7) for v in [no * 100] * 10]
    for no in range(1, 7):
        client.post(
            "/fragments",
            json=frag(no, 1, (no - 1) * 10, [no * 100] * 10),
        )
    pub = client.post(
        "/publications", params={"station": "STA1", "minute_utc": MIN}
    ).json()
    client.post(
        f"/publications/{pub['id']}/withdrawal",
        json={"reason": "equipment fault"},
    )
    assert client.get("/supply").json() == []

    # A backfilled replacement fragment with a higher revision changes the set.
    client.post("/fragments", json=frag(3, 2, 20, [777] * 10))
    r = client.post(
        "/publications", params={"station": "STA1", "minute_utc": MIN}
    )
    assert r.status_code == 201
    assert r.json()["id"] != pub["id"]
    supply = client.get("/supply").json()
    assert len(supply) == 1 and supply[0]["wave"][20] == 777


def test_supply_filters_include_station_and_minute(client):
    _publish_complete(client, station="STA1", minute=MIN)
    other_minute = "2026-11-17T05:07:00Z"
    pub2 = _publish_complete(client, station="STA2", minute=other_minute)

    all_supply = client.get("/supply").json()
    assert len(all_supply) == 2

    by_station = client.get("/supply", params={"station": "STA2"}).json()
    assert [p["id"] for p in by_station] == [pub2["id"]]
    by_minute = client.get(
        "/supply", params={"minute_utc": other_minute}
    ).json()
    assert [p["id"] for p in by_minute] == [pub2["id"]]
    assert (
        client.get(
            "/supply",
            params={"station": "STA1", "minute_utc": other_minute},
        ).json()
        == []
    )


def test_stale_records_never_supplied(client):
    pub = _publish_complete(client)
    # Change the fragment set without publishing: the record goes stale and
    # must leave the supply even without an explicit withdrawal.
    client.post("/fragments", json=frag(1, 2, 0, [9] + list(range(1, 30))))
    assert client.get("/supply").json() == []
    got = client.get(f"/publications/{pub['id']}").json()
    assert got["stale"] is True and got["withdrawn"] is False


def test_withdrawal_is_atomic_when_conflicting(client):
    pub = _publish_complete(client)
    # First withdrawal succeeds; the conflicting retry must roll back fully.
    client.post(
        f"/publications/{pub['id']}/withdrawal", json={"reason": "first"}
    )
    r = client.post(
        f"/publications/{pub['id']}/withdrawal", json={"reason": "second"}
    )
    assert r.status_code == 409
    got = client.get(f"/publications/{pub['id']}").json()
    assert got["withdrawn"] is True
    assert got["withdrawal_reason"] == "first"
    # Only one row exists, no half-written second record.
    assert len(client.get("/publications").json()) == 1


def test_legacy_sqlite_volume_defaults_to_not_withdrawn(monkeypatch, tmp_path):
    """A database file created by the previous release migrates cleanly."""
    import sqlite3

    db_path = tmp_path / "legacy.db"
    # Reproduce the pre-withdrawal schema (no withdrawn* columns).
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE fragments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            station TEXT NOT NULL,
            minute_utc TEXT NOT NULL,
            fragment_no INTEGER NOT NULL,
            revision INTEGER NOT NULL,
            start_sample INTEGER NOT NULL,
            samples TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            UNIQUE (station, minute_utc, fragment_no)
        );
        CREATE TABLE publications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            station TEXT NOT NULL,
            minute_utc TEXT NOT NULL,
            wave TEXT NOT NULL,
            fragment_set_hash TEXT NOT NULL,
            stale INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
        );
        CREATE TABLE publication_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            publication_id INTEGER NOT NULL,
            fragment_no INTEGER NOT NULL,
            revision INTEGER NOT NULL,
            start_sample INTEGER NOT NULL,
            samples TEXT NOT NULL,
            content_hash TEXT NOT NULL
        );
        """
    )
    # A genuine legacy volume held real, complete fragments and a matching
    # fragment-set hash on its publication.
    conn.execute(
        "INSERT INTO fragments (station, minute_utc, fragment_no, revision, "
        "start_sample, samples, content_hash) VALUES "
        "('LEGACY', ?, 1, 1, 0, ?, 'h1'), "
        "('LEGACY', ?, 2, 1, 30, ?, 'h2')",
        (
            MIN,
            str(list(range(0, 30))),
            MIN,
            str(list(range(30, 60))),
        ),
    )
    conn.execute(
        "INSERT INTO publications (station, minute_utc, wave, "
        "fragment_set_hash, stale) VALUES (?, ?, ?, ?, 0)",
        ("LEGACY", MIN, str(list(range(60))), "legacy-set-hash"),
    )
    conn.execute(
        "INSERT INTO publication_items (publication_id, fragment_no, revision, "
        "start_sample, samples, content_hash) VALUES "
        "(1, 1, 1, 0, ?, 'h1'), (1, 2, 1, 30, ?, 'h2')",
        (str(list(range(0, 30))), str(list(range(30, 60)))),
    )
    conn.commit()
    conn.close()

    monkeypatch.setenv("WAVEFORM_DB_PATH", str(db_path))
    import importlib

    from app import config, database, main, service

    importlib.reload(config)
    importlib.reload(database)
    importlib.reload(service)
    importlib.reload(main)
    database.init_db()  # must attach the new columns without failing

    # Backfill the real fragment-set hash the legacy release would have stored.
    import sqlite3 as _sqlite3

    with _sqlite3.connect(str(db_path)) as fix:
        fix.row_factory = _sqlite3.Row
        rows = fix.execute(
            "SELECT * FROM fragments WHERE station = 'LEGACY'"
        ).fetchall()
        real_hash = service._fragment_set_hash(rows)
        fix.execute(
            "UPDATE publications SET fragment_set_hash = ? WHERE id = 1",
            (real_hash,),
        )
        fix.commit()

    with TestClient(main.app) as c:
        # Legacy publication is effective by default: supplied, not withdrawn.
        supply = c.get("/supply", params={"station": "LEGACY"}).json()
        assert len(supply) == 1
        assert supply[0]["withdrawn"] is False
        assert supply[0]["withdrawal_reason"] is None
        got = c.get("/publications/1").json()
        assert got["withdrawn"] is False
        # It can now be withdrawn through the new API.
        r = c.post(
            "/publications/1/withdrawal", json={"reason": "post-upgrade fault"}
        )
        assert r.status_code == 201
        assert r.json()["withdrawn"] is True


def test_concurrent_withdrawals_one_wins_others_follow_idempotency(
    monkeypatch,
):
    """Same id + same reason under concurrency: exactly one 201, rest 200."""
    import time

    tmpdir = tempfile.mkdtemp(prefix="wf-wd-")
    db_path = Path(tmpdir) / "wd.db"
    monkeypatch.setenv("WAVEFORM_DB_PATH", str(db_path))

    import importlib

    from app import config, database, main, service

    importlib.reload(config)
    importlib.reload(database)
    importlib.reload(service)
    importlib.reload(main)
    database.init_db()

    with TestClient(main.app) as c:
        pub = _publish_complete(c)
        results: list[tuple[int, str]] = []
        lock = threading.Lock()
        barrier = threading.Barrier(8)

        def worker(reason: str) -> None:
            barrier.wait()
            r = c.post(
                f"/publications/{pub['id']}/withdrawal",
                json={"reason": reason},
            )
            with lock:
                results.append((r.status_code, r.json().get("withdrawn_at", "")))

        threads = [
            threading.Thread(
                target=worker, args=("shared reason" if i < 6 else "other reason",)
            )
            for i in range(8)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        statuses = sorted(code for code, _ in results)
        # Exactly one withdrawal is created; same-reason retries are 200,
        # different-reason retries are 409 — never a 500 or a double record.
        assert statuses.count(201) == 1
        assert statuses.count(200) == 5
        assert statuses.count(409) == 2
        timestamps = {ts for code, ts in results if code in (200, 201)}
        assert timestamps and len(timestamps) == 1  # one shared withdrawal
        assert len(c.get("/publications").json()) == 1
        assert c.get("/supply").json() == []

    for suffix in ("", "-wal", "-shm"):
        p = Path(str(db_path) + suffix)
        if p.exists():
            p.unlink()


def test_concurrent_withdraw_and_republish_same_set(monkeypatch):
    """Withdraw vs re-publish of the same set interleave on one committed state."""
    tmpdir = tempfile.mkdtemp(prefix="wf-inter-")
    db_path = Path(tmpdir) / "inter.db"
    monkeypatch.setenv("WAVEFORM_DB_PATH", str(db_path))

    import importlib

    from app import config, database, main, service

    importlib.reload(config)
    importlib.reload(database)
    importlib.reload(service)
    importlib.reload(main)
    database.init_db()

    with TestClient(main.app) as c:
        pub = _publish_complete(c)
        barrier = threading.Barrier(10)
        outcomes: list[str] = []
        lock = threading.Lock()

        def withdrawer() -> None:
            barrier.wait()
            r = c.post(
                f"/publications/{pub['id']}/withdrawal",
                json={"reason": "fault"},
            )
            with lock:
                outcomes.append(f"w{r.status_code}")

        def republisher() -> None:
            barrier.wait()
            r = c.post(
                "/publications",
                params={"station": "STA1", "minute_utc": MIN},
            )
            with lock:
                outcomes.append(f"p{r.status_code}")

        threads = [threading.Thread(target=withdrawer) for _ in range(5)]
        threads += [threading.Thread(target=republisher) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Every outcome is one of the documented committed states — no 500s,
        # no partial rows.
        assert set(outcomes) <= {"w200", "w201", "w409", "p201", "p409"}
        listing = c.get(
            "/publications", params={"station": "STA1", "minute_utc": MIN}
        ).json()
        assert len(listing) == 1  # same fragment set never gets a second row
        # Final state is consistent: either withdrawn (supply empty) or not.
        if listing[0]["withdrawn"]:
            assert c.get("/supply").json() == []
        else:
            assert [p["id"] for p in c.get("/supply").json()] == [pub["id"]]

    for suffix in ("", "-wal", "-shm"):
        p = Path(str(db_path) + suffix)
        if p.exists():
            p.unlink()
