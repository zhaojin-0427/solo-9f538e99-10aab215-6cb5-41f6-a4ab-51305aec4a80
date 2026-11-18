#!/usr/bin/env python3
"""End-to-end smoke test against a running server.

Usage: python scripts/smoke.py [base_url]
Exits non-zero on the first failed expectation.
"""
from __future__ import annotations

import sys

import httpx

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8011"
MIN = "2026-11-17T05:06:00Z"
ST = "SMOKE"

failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"[{'PASS' if cond else 'FAIL'}] {name} {detail}")
    if not cond:
        failures.append(name)


def main() -> int:
    http = httpx.Client(base_url=BASE, timeout=10)

    def frag(no, rev, start, samples, station=ST, minute=MIN):
        return {
            "station": station,
            "minute_utc": minute,
            "fragment_no": no,
            "revision": rev,
            "start_sample": start,
            "samples": samples,
        }

    # 1. Out-of-order arrival: fragment 2 lands before fragment 1.
    r = http.post("/fragments", json=frag(2, 1, 30, list(range(30, 60))))
    check("upload fragment 2 first", r.status_code == 201, r.json().get("status", ""))
    r = http.post("/fragments", json=frag(1, 1, 0, list(range(0, 30))))
    check("upload fragment 1", r.status_code == 201)

    # 2. Assembly is complete and conflict free.
    body = http.get("/assembly", params={"station": ST, "minute_utc": MIN}).json()
    check("assembly complete", body["complete"] is True, f"coverage={body['coverage']}")
    check("no gaps", body["gaps"] == [])
    check("no conflicts", body["conflicts"] == [])

    # 3. Boundary rejection.
    r = http.post("/fragments", json=frag(9, 1, 58, [1, 2, 3]))
    check("out-of-bounds rejected", r.status_code == 422, r.json()["detail"])

    # 4. Idempotent retry: same version + same content -> unchanged, no dup row.
    r = http.post("/fragments", json=frag(1, 1, 0, list(range(0, 30))))
    check("same-version same-content retry idempotent",
          r.status_code == 201 and r.json()["status"] == "unchanged")
    rows = http.get("/fragments", params={"station": ST, "minute_utc": MIN}).json()
    check("no duplicate fragment row", len(rows) == 2)

    # 5. Same version, different content -> rejected.
    r = http.post("/fragments", json=frag(1, 1, 0, [999] + list(range(1, 30))))
    check("same-version different-content rejected", r.status_code == 409)

    # 6. Lower revision -> rejected.
    r = http.post("/fragments", json=frag(1, 0, 0, [1]))
    check("revision zero rejected at validation", r.status_code == 422)

    # 7. Publish the complete minute.
    r = http.post("/publications", params={"station": ST, "minute_utc": MIN})
    check("publish succeeds", r.status_code == 201)
    pub1 = r.json()
    check("wave is 60 samples", pub1["wave"] == list(range(60)))
    check("published as fresh", pub1["stale"] is False)
    check("pins two fragment revisions",
          sorted((i["fragment_no"], i["revision"]) for i in pub1["items"])
          == [(1, 1), (2, 1)])

    # Re-publishing the identical set returns the same record.
    r = http.post("/publications", params={"station": ST, "minute_utc": MIN})
    check("republish identical set is idempotent",
          r.status_code == 201 and r.json()["id"] == pub1["id"])

    # 8. Correction arrives: higher revision changes current assembly.
    r = http.post("/fragments", json=frag(1, 2, 0, [42] + list(range(1, 30))))
    check("higher revision accepted", r.status_code == 201
          and r.json()["status"] == "superseded")

    # 9. Old record is still readable, frozen, and marked stale.
    old = http.get(f"/publications/{pub1['id']}").json()
    check("old record still readable", old["id"] == pub1["id"])
    check("old wave frozen", old["wave"] == list(range(60)))
    check("old revisions frozen", old["items"][0]["revision"] == 1)
    check("old record marked stale", old["stale"] is True)

    # 10. Publish the new fragment set; latest points to it.
    r = http.post("/publications", params={"station": ST, "minute_utc": MIN})
    check("republish after correction", r.status_code == 201)
    pub2 = r.json()
    check("new record id differs", pub2["id"] != pub1["id"])
    check("new wave contains correction", pub2["wave"][0] == 42
          and pub2["wave"][1:] == list(range(1, 60)))
    check("new record fresh", pub2["stale"] is False)
    latest = http.get("/publications/latest",
                      params={"station": ST, "minute_utc": MIN}).json()
    check("latest endpoint returns new record", latest["id"] == pub2["id"])
    listing = http.get("/publications",
                       params={"station": ST, "minute_utc": MIN}).json()
    check("both records retained", [p["id"] for p in listing]
          == [pub1["id"], pub2["id"]])
    check("staleness flags", [p["stale"] for p in listing] == [True, False])

    # 11. Conflict reporting on a second station-minute.
    http.post("/fragments", json=frag(1, 1, 0, [7, 8, 9], station="BAD"))
    http.post("/fragments", json=frag(2, 1, 2, [1, 2], station="BAD"))
    body = http.get("/assembly", params={"station": "BAD", "minute_utc": MIN}).json()
    check("conflict located with fragment numbers",
          body["conflicts"] == [{"position": 2, "fragment_nos": [1, 2],
                                 "values": [9, 1]}])
    check("gaps listed", body["gaps"] == list(range(4, 60)))
    r = http.post("/publications", params={"station": "BAD", "minute_utc": MIN})
    check("conflicted minute cannot publish", r.status_code == 409)
    r = http.get("/publications", params={"station": "BAD"})
    check("failed publish left no rows", r.json() == [])

    # 12. Missing minute publication -> 404.
    r = http.get("/publications/latest",
                 params={"station": "NOPE", "minute_utc": MIN})
    check("unknown publication 404", r.status_code == 404)

    # 13. Withdrawal: stop supplying a polluted publication immediately.
    r = http.post(f"/publications/{pub2['id']}/withdrawal",
                  json={"reason": "equipment fault"})
    check("withdraw effective publication", r.status_code == 201)
    withdrawn = r.json()
    check("withdrawn flag and reason recorded",
          withdrawn["withdrawn"] is True
          and withdrawn["withdrawal_reason"] == "equipment fault"
          and withdrawn["withdrawn_at"] is not None)
    check("withdrawn record keeps frozen wave and items",
          withdrawn["wave"] == pub2["wave"] and len(withdrawn["items"]) == 2)

    # Current supply no longer contains the withdrawn minute, while the
    # record itself stays readable through the publication endpoints.
    supply = http.get("/supply",
                      params={"station": ST, "minute_utc": MIN}).json()
    check("withdrawn minute absent from supply", supply == [])
    still = http.get(f"/publications/{pub2['id']}").json()
    check("withdrawn record still readable",
          still["withdrawn"] is True
          and still["withdrawal_reason"] == "equipment fault"
          and still["wave"] == pub2["wave"])

    # Same id + same reason -> idempotent reuse of the original withdrawal.
    r = http.post(f"/publications/{pub2['id']}/withdrawal",
                  json={"reason": "equipment fault"})
    check("same reason retry idempotent",
          r.status_code == 200
          and r.json()["withdrawn_at"] == withdrawn["withdrawn_at"])
    # Same id + different reason -> refused, original reason untouched.
    r = http.post(f"/publications/{pub2['id']}/withdrawal",
                  json={"reason": "another reason"})
    check("different reason rejected", r.status_code == 409)
    check("original withdrawal reason preserved",
          http.get(f"/publications/{pub2['id']}").json()["withdrawal_reason"]
          == "equipment fault")
    # Blank/empty reason -> validation error.
    r = http.post(f"/publications/{pub2['id']}/withdrawal",
                  json={"reason": "   "})
    check("blank reason rejected", r.status_code == 422)
    # Unknown publication -> 404.
    r = http.post("/publications/999999/withdrawal", json={"reason": "x"})
    check("withdraw unknown id 404", r.status_code == 404)

    # Re-publishing the identical fragment set cannot restore supply.
    r = http.post("/publications", params={"station": ST, "minute_utc": MIN})
    check("republish withdrawn same set refused", r.status_code == 409)
    check("supply stays empty after refused republish",
          http.get("/supply", params={"station": ST,
                                      "minute_utc": MIN}).json() == [])

    # Only a real fragment-set change (higher revision) allows a new
    # publication, which then resumes supply.
    http.post("/fragments", json=frag(1, 3, 0, [7] + list(range(1, 30))))
    r = http.post("/publications", params={"station": ST, "minute_utc": MIN})
    check("publish after real correction", r.status_code == 201)
    pub3 = r.json()
    check("new publication id differs and is effective",
          pub3["id"] != pub2["id"] and pub3["withdrawn"] is False)
    supply = http.get("/supply",
                      params={"station": ST, "minute_utc": MIN}).json()
    check("supply resumes with corrected record",
          [p["id"] for p in supply] == [pub3["id"]])
    # Withdrawn audit trail remains available.
    audit = http.get(f"/publications/{pub2['id']}").json()
    check("withdrawn audit record retained",
          audit["withdrawn"] is True and audit["withdrawal_reason"])

    # 14. A stale publication cannot be withdrawn and never reaches supply.
    stale_minute = "2026-11-17T08:08:00Z"
    http.post("/fragments", json=frag(1, 1, 0, list(range(0, 30)),
                                      station="STALE", minute=stale_minute))
    http.post("/fragments", json=frag(2, 1, 30, list(range(30, 60)),
                                      station="STALE", minute=stale_minute))
    stale_pub = http.post(
        "/publications", params={"station": "STALE",
                                 "minute_utc": stale_minute}).json()
    http.post("/fragments", json=frag(1, 2, 0, [55] + list(range(1, 30)),
                                      station="STALE", minute=stale_minute))
    r = http.post(f"/publications/{stale_pub['id']}/withdrawal",
                  json={"reason": "too late"})
    check("stale publication cannot be withdrawn", r.status_code == 409)
    check("stale record absent from supply",
          http.get("/supply", params={"station": "STALE"}).json() == [])

    print()
    if failures:
        print(f"{len(failures)} FAILURE(S): {failures}")
        return 1
    print("all smoke checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
