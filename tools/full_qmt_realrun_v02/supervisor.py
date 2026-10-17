"""Final authority: preflight, owned worker, raw-fact decision, publication.

This module never loads xtquant. The executable interface is deliberately absent
until a separately authorized real-run profile/loader is implemented and reviewed.
"""

import os
from pathlib import Path
import secrets
import time
import uuid

from . import profile as contract
from .windows_runtime import start_fake_worker


def write(directory, name, value, deadline):
    deadline.check()
    with (directory / name).open("xb") as stream:
        stream.write(contract.encode(value))
        stream.flush()
    deadline.check()


def publish(directory, manifest, facts, process, deadline):
    """Publish complete FAIL/NOT_COVERED too; never publish unknown child exit."""
    if process["exit_confirmed"] is not True:
        raise ValueError("worker_exit_unconfirmed")
    deadline.check()
    candidate = contract.decision(facts, process, manifest)
    write(directory, "manifest.json", manifest, deadline)
    write(directory, "process.json", process, deadline)
    write(directory, "decision.candidate.json", candidate, deadline)
    if set(p.name for p in directory.iterdir()) != set(contract.PAYLOAD):
        raise ValueError("required_files")
    hashes = {}
    for name in contract.PAYLOAD:
        deadline.check()
        contract.reject_links(directory / name)
        hashes[name] = contract.sha((directory / name).read_bytes())
        deadline.check()
    write(directory, "hashes.json", hashes, deadline)
    marker = {"run_id": manifest["run_id"], "hashes_sha256": contract.sha((directory / "hashes.json").read_bytes()),
              "deadline_ns": deadline.end, "published_ns": time.perf_counter_ns()}
    write(directory, "COMPLETE.tmp", marker, deadline)
    deadline.check()
    os.rename(directory / "COMPLETE.tmp", directory / "COMPLETE")
    deadline.check()
    result = contract.consume(directory, manifest, deadline)
    deadline.check()
    if result["completion"] != "COMPLETE":
        raise ValueError("consume_verification")
    return result


def run_fake(profile, account_id, approved_evidence_root, scenario="pass"):
    # This is the run's very first operation, not merely a worker timeout.
    deadline = contract.Deadline(profile.get("run_timeout", 15))
    directory, child = None, None
    try:
        repo = Path(__file__).absolute().parents[2]
        contract.validate_profile(profile, repo)
        deadline.check()
        if type(account_id) is not str or not account_id.strip() or len(account_id) > 256:
            raise ValueError("invalid_private_account")
        if scenario not in contract.SCENARIOS:
            raise ValueError("unknown_fixture")
        root = Path(approved_evidence_root).absolute()
        contract.reject_links(root)
        if not root.is_dir():
            raise ValueError("approved_root_must_exist")
        # Additional guard against accidental production evidence writes. The
        # caller must still obtain approval for any non-test evidence directory.
        denied = [Path("D:/positionx_data"), Path("D:/qmt"), repo / "data"]
        denied.extend(Path(v) for k, v in profile["selection"].items()
                      if k in ("sdk_root", "userdata"))
        if any(root == p or root.is_relative_to(p) for p in denied):
            raise ValueError("production_or_sdk_evidence_root")
        run_id = uuid.uuid4().hex
        directory = root / ("fake_" + run_id)
        directory.mkdir()  # Never reuse or overwrite a run.
        key = secrets.token_bytes(32)
        manifest = {"version": contract.VERSION, "run_id": run_id,
                    "started_ns": deadline.started, "deadline_ns": deadline.end,
                    "account_token": contract.token(key, account_id),
                    "profile": contract.public_profile(profile, key)}
        request = {"mode": profile["execution_mode"], "key": key.hex(), "account": account_id,
                   "account_type": profile["account_type"], "session_id": profile["session_id"],
                   "scenario": scenario, "directory": str(directory), "deadline_ns": deadline.end,
                   "observation_seconds": profile["observation_seconds"]}
        deadline.check()
        child = start_fake_worker(profile, request, directory)
        process = child.finish(deadline)
        deadline.check()
        facts = contract.load(directory / "facts.json")
        result = publish(directory, manifest, facts, process, deadline)
        deadline.check()
        return {"directory": directory, "manifest": manifest, "decision": result}
    except BaseException:
        # No str/repr/traceback, unknown exception name or sensitive path emitted.
        if child is not None and child._process.poll() is None:
            child.terminate_and_wait(1)
        if directory is not None:
            try:
                (directory / "COMPLETE").unlink(missing_ok=True)
            except OSError:
                pass  # OS invalidation failure is a documented real-run limit.
        return {"directory": directory, "manifest": None,
                "decision": {"overall": "FAIL", "completion": "INCOMPLETE",
                             "failures": ["supervisor_or_deadline_failure"]}}
