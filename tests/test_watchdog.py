from unittest.mock import patch

import usage_watchdog as watchdog


ISSUE = {"id": "issue-1", "assignee_id": "agent-1"}


def test_only_latest_quota_failure_is_eligible():
    quota_failure = {
        "id": "run-2", "agent_id": "agent-1", "created_at": "2026-10-29T12:00:00Z",
        "status": "failed", "failure_reason": "agent_error.provider_quota_limit",
    }
    older_success = {
        "id": "run-1", "agent_id": "agent-1", "created_at": "2026-10-29T11:00:00Z",
        "status": "completed",
    }
    with patch.object(watchdog, "run_multica", return_value=[older_success, quota_failure]):
        assert watchdog.quota_failed_run(ISSUE) == (True, "")

    with patch.object(watchdog, "run_multica", return_value=[quota_failure, {**older_success, "created_at": "2026-10-29T13:00:00Z"}]):
        assert watchdog.quota_failed_run(ISSUE)[0] is False

    with patch.object(watchdog, "run_multica", return_value=[quota_failure, {**older_success, "status": "running"}]):
        assert watchdog.quota_failed_run(ISSUE)[0] is False

    with patch.object(watchdog, "run_multica", return_value=[{**quota_failure, "agent_id": "other-agent"}]):
        assert watchdog.quota_failed_run(ISSUE)[0] is False


def test_watchdog_uses_rerun_and_records_only_a_new_run():
    issue = {
        **ISSUE, "identifier": "MU-1", "title": "test", "assignee_type": "agent",
        "last_activity_at": "2020-01-01T00:00:00Z",
    }
    def multica(*args):
        if args == ("agent", "list"):
            return [{"id": "agent-1", "runtime_id": "runtime-1"}]
        if args == ("runtime", "list"):
            return [{"id": "runtime-1", "provider": "codex"}]
        if args[:2] == ("issue", "list"):
            return {"issues": [issue], "has_more": False}
        if args == ("issue", "runs", "issue-1"):
            return [{"id": "old", "agent_id": "agent-1", "status": "failed",
                     "failure_reason": "agent_error.provider_quota_limit"}]
        if args == ("issue", "rerun", "issue-1"):
            return {"id": "new", "status": "queued"}
        raise AssertionError(f"unexpected Multica call: {args}")

    state = {}
    with patch.object(watchdog, "run_multica", side_effect=multica) as run, \
         patch.object(watchdog, "current_mode", return_value="apply"), \
         patch.object(watchdog, "quota_recovered", return_value=(True, "用量0%")), \
         patch.object(watchdog, "load_state", return_value=state), \
         patch.object(watchdog, "save_state") as save, \
         patch.object(watchdog, "prune_log"), patch.object(watchdog, "log"):
        watchdog.main()
    run.assert_any_call("issue", "rerun", "issue-1")
    assert len(save.call_args.args[0]["issue-1"]["revives"]) == 1


def test_answered_member_question_requires_quota_failed_human_reply():
    issue = {**ISSUE, "assignee_type": "member", "assignee_id": "owner-1"}
    detail = {**issue, "labels": [{"name": "等我回答", "id": "label-1"}]}
    failure = {
        "id": "run-1", "agent_id": "agent-1", "status": "failed",
        "failure_reason": "agent_error.provider_quota_limit", "kind": "comment",
        "trigger_comment_id": "reply-1", "attribution": {"initiator": {"id": "owner-1"}},
    }
    with patch.object(watchdog, "run_multica", side_effect=[detail, [failure]]):
        assert watchdog.answered_question_failure(issue) == ("agent-1", "label-1", "run-1")
    with patch.object(watchdog, "run_multica", side_effect=[detail, [{**failure, "kind": "direct"}]]):
        assert watchdog.answered_question_failure(issue) is None
    with patch.object(watchdog, "run_multica", side_effect=[detail, [{**failure, "status": "running"}]]):
        assert watchdog.answered_question_failure(issue) is None
