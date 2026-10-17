"""Only fixture execution is implemented. Supervisor never imports xtquant."""

import time

from tools.full_qmt_readonly_probe_v01.evidence import RunDeadline, RunDeadlineExceeded, clock, new_run_directory
from tools.full_qmt_readonly_probe_v01.normalize import normalize
from .real_profile import (Journal, CallbackClosure, closure_veto, PrivateRedactor, configuration,
                           manifest_for, publish, incomplete, MODE)
from .sdk_loader import RealSDKDisabled
from .windows_runtime import (PrivatePipe, create_fixture_process, cleanup_owned, receive_report,
                              fixture_health, channel_key)
from .worker import run_fixture_worker


def run_fixture(evidence_root, selection, account_id, sdk_factory, *, mode="fixture", overrides=None,
                process_behavior="normal", health_status="PASS", harness_commit=None, after_worker=None):
    started = clock()  # Includes preflight, source hashing, fixture import and final consume.
    config = configuration(overrides)
    deadline = RunDeadline(started, config["total_timeout_seconds"])
    journal = process = directory = None
    if mode != "fixture":
        raise RealSDKDisabled("Real execution is not implemented, even with supplied approvals")
    try:
        deadline.check()
        directory = new_run_directory(evidence_root)
        journal = Journal(directory, deadline)
        closure = CallbackClosure(journal)
        deadline.arm(journal)
        journal.initialize()
        key = channel_key()
        redactor = PrivateRedactor(key)
        manifest = manifest_for(journal, config, selection, redactor, deadline, harness_commit)
        journal.write("manifest.json", manifest)
        client_token = redactor.token("FIXTURE_CLIENT_IDENTITY")
        for source in ("os", "human"):
            journal.append("client_health.jsonl", fixture_health("pre", source, health_status, client_token))
        process_facts = {"started": clock(), "pid": None, "timeout": None, "controller_error": None}
        report, channel_events = {"report_missing": True}, []
        try:
            process = create_fixture_process(process_behavior)
            process_facts["pid"] = process.pid
            request = PrivatePipe()
            request.send({"mode": "fixture", "directory": str(directory), "selection": selection, "account_id": account_id,
                          "config": config, "key": key.hex(), "run_timing": deadline.timing})
            # No secret is put in argv/env. Fake process does not launch an OS child.
            process.start(request, lambda frame, pid: run_fixture_worker(frame, pid, sdk_factory, closure),
                          key, after_worker, closure=closure)
            while process.poll() is None:
                if deadline.remaining() == 0:
                    process_facts["timeout"] = {**clock(), "kind": "total"}
                    break
                time.sleep(min(0.005, deadline.remaining()))
            report, channel_events = receive_report(process, key)
        except Exception as exc:
            process_facts["controller_error"] = {"error_type": redactor.error(exc)["error_type"], "reason": "supervision_failed"}
        finally:
            cleanup = cleanup_owned(process) if process is not None else {
                "exited": False, "exitcode": None, "forced_termination": False, "cleanup_errors": [{"stage": "launch"}]}
            process_facts.update(cleanup)
            process_facts["finished"] = clock()
            process_facts["channel_ok"] = bool(channel_events) and process is not None and process.poll() == 0
            process_facts["output_counts"] = {"noise_bytes": 0, "stderr_bytes": 0, "invalid_packets": 0}
        deadline.check()
        for row in channel_events:
            journal.append("channel_events.jsonl", row)
        journal.append("process_checks.jsonl", {**clock(), **process_facts})
        journal.write("worker_result.json", report)
        # SDK health is derived from raw asset facts, not invented by OS/human fixtures.
        sdk_status = "NOT_COVERED"
        snapshots = journal.rows("snapshots.jsonl")
        account = next((row for row in snapshots if row["domain"] == "account"), None)
        if account is not None:
            sdk_status = normalize("account", account["raw"])["raw_query"]
        during = fixture_health("during_lifecycle", "sdk", sdk_status, client_token)
        during.update(basis="query_asset_snapshot", basis_step_id=account["step_id"] if account is not None else None)
        if account is not None:
            during.update({k: account[k] for k in ("timestamp_utc", "timestamp_cn", "monotonic_ns")})
        journal.append("client_health.jsonl", during)
        for source in ("os", "human"):
            journal.append("client_health.jsonl", fixture_health("post", source, health_status, client_token))
        journal.append("client_health.jsonl", {**fixture_health("post_stop", "sdk", "NOT_COVERED", client_token),
                                              "basis": "not_performed"})
        closure.persist_snapshot()
        deadline.check()
        result = publish(journal, selection, deadline)
        deadline.check()
        result = closure_veto(journal, result)
        deadline.check()
        return directory, result
    except RunDeadlineExceeded:
        deadline.revoke()
        return directory, incomplete("run_deadline_exceeded")
    except Exception:
        deadline.revoke()
        return directory, incomplete("supervisor_failed")
    finally:
        if process is not None and process.poll() is None:
            cleanup_owned(process)
        deadline.close()


def main():
    # No CLI credential/SDK options are available in this offline implementation.
    print("HARNESS_FAKE_ONLY; REAL_SDK=NOT_COVERED; REAL_FULL_QMT=NOT_COVERED; EXECUTION=OUT_OF_SCOPE")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
