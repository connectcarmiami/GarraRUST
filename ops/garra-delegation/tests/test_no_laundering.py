#!/usr/bin/env python3
"""Anti-laundering: a non-existent/invented id must NEVER appear verbatim in a
delegation tool's output (otherwise the runtime output guard would harvest it
from that echo and 'launder' an invented id into verified). Real/existing ids
ARE still echoed (so legitimate ids are harvestable and pass the guard).

Hermetic: throwaway tasks DB.
"""
import os
import re
import sys
import tempfile

_tmp = tempfile.NamedTemporaryFile(prefix="launder_", suffix=".db", delete=False)
_tmp.close()
os.environ["GARRA_TASKS_DB"] = _tmp.name

sys.path.insert(0, "/home/connect-car/.config/garraia")
from garra_delegation import taskstore as ts   # noqa: E402

FAKE = "t-aaaabbbbcccc"          # 12-hex, id-shaped, but NOT in the store
_fail = []


def check(name, cond, extra=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" — {extra}" if extra else ""))
    if not cond:
        _fail.append(name)


def harvestable(text):
    """Mimic the runtime guard scanner: would it find a real-id-shaped token?"""
    return bool(re.search(r't-[0-9a-f]{8,16}(?![0-9a-f])', text)
                or re.search(r'corr-[0-9a-z]{6,16}(?![0-9a-z])', text))


def main():
    ts.init_db()

    # verify_identifier / audit_metadata must not echo the raw fake id
    v = ts.verify_identifier(FAKE)
    import json
    check("verify_identifier(fake): verdict UNVERIFIED, raw id NOT present",
          v["verdict"] == "UNVERIFIED" and FAKE not in json.dumps(v))
    a = ts.audit_metadata(FAKE)
    check("audit_metadata(fake): exists False, raw id NOT present",
          a["exists"] is False and FAKE not in json.dumps(a))

    # the masked rendering must NOT be harvestable as a real id
    masked = ts.mask_taskid(FAKE)
    check("mask_taskid(fake) is non-harvestable", not harvestable(masked), f"masked={masked}")

    # a REAL id must still be echoed verbatim (so the guard can harvest legit ids)
    t, _ = ts.create_task("garra", "flash", "engineering", "real task")
    tid = t["task_id"]
    ts.mark_accepted(tid, 1); ts.mark_running(tid); ts.mark_succeeded(tid, "ok")
    vp = ts.verify_identifier(tid)
    ap = ts.audit_metadata(tid)
    check("verify_identifier(real): PASS + real id present (harvestable)",
          vp["verdict"] == "PASS" and tid in json.dumps(vp) and harvestable(json.dumps(vp)))
    check("audit_metadata(real): real id present", ap["task_id"] == tid)

    print()
    if _fail:
        print(f"RESULT: {len(_fail)} FAIL -> {_fail}")
        return 1
    print("RESULT: ALL PASS")
    return 0


if __name__ == "__main__":
    try:
        rc = main()
    finally:
        for suf in ("", "-wal", "-shm"):
            try:
                os.unlink(_tmp.name + suf)
            except OSError:
                pass
    sys.exit(rc)
