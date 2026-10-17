import json
import threading
import unittest

import core
import workflow


def context_from(prompt):
    raw = prompt.split("Authoritative context for this workflow action:\n", 1)[1]
    return json.JSONDecoder().raw_decode(raw)[0]


def submission(text="result", ready=True, needs_input=False):
    return {"result": text, "ready": ready, "needs_input": needs_input}


def review(approved=True, feedback="Meets the assignment"):
    return lambda c: {"approved": approved, "feedback": feedback, "submission_id": c["submission_id"]}


def confirm(accepted=True, feedback="I agree the work is complete"):
    return lambda c: {"accepted": accepted, "feedback": feedback, "submission_id": c["submission_id"]}


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.master = dict(core.blank_bot("master"), name="Master", assignment="Check quality")
        self.workers = [dict(core.blank_bot(name), name=name, assignment=f"Task for {name}") for name in ("A", "B", "C")]
        self.messages, self.progress, self.calls = [], [], []

    def run_script(self, responses, workers=None, **kwargs):
        items = iter(responses)

        def model(bot, prompt, key, *, instruction_context):
            context = context_from(prompt)
            self.calls.append((bot["id"], context, instruction_context))
            item = next(items)
            output = item(context) if callable(item) else item
            if isinstance(output, Exception):
                raise output
            return output if isinstance(output, str) else json.dumps(output)

        return workflow.run_task_workflow("Tasks", self.master, workers or self.workers,
            [{"sender": "You", "text": "Deliver a tested plan"}], [], {}, self.messages.append,
            on_progress=self.progress.append, model_call=model, **kwargs)

    def test_all_approved_results_reach_later_workers_only_after_both_decisions(self):
        script = []
        for name in ("A", "B", "C"):
            script += [submission(f"Approved output {name}"), review(), confirm()]
        self.assertEqual(self.run_script(script, quality_criteria="Include evidence"), "workflow_complete")
        self.assertEqual([c[0] for c in self.calls], ["A", "master", "A", "B", "master", "B", "C", "master", "C"])
        inputs = self.calls[6][1]["all_previously_approved_results"]
        self.assertEqual([r["result"] for r in inputs], ["Approved output A", "Approved output B"])
        self.assertTrue(all(r["master_approved"] and r["worker_confirmed"] for r in inputs))
        self.assertEqual(self.calls[1][1]["quality_criteria"], "Include evidence")
        self.assertEqual(self.progress[-1]["phase"], "complete")
        self.assertEqual(len(self.progress[-1]["approved_results"]), 3)

    def test_master_rejection_revises_same_worker_before_handoff(self):
        self.run_script([submission("draft"), review(False, "Add evidence"),
                         submission("corrected"), review(), confirm(),
                         submission("next task"), review(), confirm()], workers=self.workers[:2])
        self.assertEqual([c[0] for c in self.calls], ["A", "master", "A", "master", "A", "B", "master", "B"])
        self.assertEqual(self.calls[2][1]["revision_feedback"], "Add evidence")
        self.assertEqual(self.calls[2][1]["previous_result"], "draft")
        self.assertEqual(self.calls[5][1]["all_previously_approved_results"][0]["result"], "corrected")

    def test_worker_disagreement_invalidates_master_approval_until_new_review(self):
        self.run_script([submission("v1"), review(), confirm(False, "I found an error"),
                         submission("v2"), review(), confirm()], workers=self.workers[:1])
        self.assertEqual([c[0] for c in self.calls], ["A", "master", "A", "A", "master", "A"])
        self.assertNotEqual(self.calls[1][1]["submission_id"], self.calls[4][1]["submission_id"])
        self.assertEqual(self.calls[3][1]["revision_feedback"], "I found an error")
        self.assertEqual(self.progress[-1]["approved_results"][0]["result"], "v2")

    def test_unready_worker_cannot_be_handed_off_even_if_master_approves(self):
        self.run_script([submission("partial", ready=False), review(),
                         submission("complete"), review(), confirm()], workers=self.workers[:1])
        self.assertEqual([c[0] for c in self.calls], ["A", "master", "A", "master", "A"])
        self.assertEqual(len(self.progress[-1]["approved_results"]), 1)

    def test_invalid_master_decisions_never_advance_to_confirmation_or_next_worker(self):
        invalid = ["Looks good, approved!", {"approved": "true", "feedback": "OK", "submission_id": "old"},
                   {"approved": True, "feedback": "OK", "submission_id": "wrong-version"},
                   {"approved": False, "feedback": "", "submission_id": "wrong-version"}]
        for bad in invalid:
            with self.subTest(bad=bad):
                self.setUp()
                with self.assertRaisesRegex(core.UserError, "No handoff"):
                    self.run_script([submission(), bad, bad, bad])
                self.assertEqual([c[0] for c in self.calls], ["A", "master", "master", "master"])
                self.assertTrue(all(not p["approved_results"] for p in self.progress))

    def test_stale_worker_confirmation_never_hands_off(self):
        bad = {"accepted": True, "feedback": "OK", "submission_id": "old"}
        with self.assertRaisesRegex(core.UserError, "No handoff"):
            self.run_script([submission(), review(), bad, bad, bad])
        self.assertNotIn("B", [c[0] for c in self.calls])
        self.assertEqual(self.progress[-1]["approved_results"], [])

    def test_missing_user_input_blocks_before_master_review(self):
        with self.assertRaisesRegex(workflow.WorkflowBlocked, "needs input"):
            self.run_script([submission("Please supply the source file", ready=False, needs_input=True)])
        self.assertEqual([c[0] for c in self.calls], ["A"])
        self.assertEqual(self.progress[-1]["approved_results"], [])

    def test_stop_during_master_review_does_not_confirm_or_handoff_later(self):
        entered, release, stop = [threading.Event() for _ in range(3)]
        calls, reasons = [], []

        def model(bot, prompt, key, **kwargs):
            calls.append(bot["id"])
            if bot["id"] == "master":
                entered.set()
                release.wait(2)
                return json.dumps(review()(context_from(prompt)))
            return json.dumps(submission())

        runner = threading.Thread(target=lambda: reasons.append(workflow.run_task_workflow(
            "Test", self.master, self.workers, [], [], {}, self.messages.append,
            stop_event=stop, on_progress=self.progress.append, model_call=model)), daemon=True)
        runner.start()
        try:
            self.assertTrue(entered.wait(1))
            stop.set()
            runner.join(1)
            self.assertFalse(runner.is_alive())
            self.assertEqual(reasons, ["stopped"])
            self.assertEqual(calls, ["A", "master"])
            self.assertEqual(self.progress[-1]["approved_results"], [])
        finally:
            release.set()

    def test_json_repair_succeeds_without_treating_invalid_reply_as_approval(self):
        self.run_script([submission(), "I agree", review(), confirm()], workers=self.workers[:1])
        self.assertEqual([c[0] for c in self.calls], ["A", "master", "master", "A"])
        self.assertEqual(len(self.messages), 3)

    def test_truncated_worker_json_is_repaired_before_review(self):
        broken = '{"result":"A long unfinished research report'
        self.run_script([broken, submission("Concise complete findings"), review(), confirm()], workers=self.workers[:1])
        self.assertEqual([c[0] for c in self.calls], ["A", "A", "master", "A"])
        repair = self.calls[1][2]
        self.assertIn("Unterminated string", repair)
        self.assertIn("shorten the result", repair)
        self.assertIn("rejected_reply", repair)
        self.assertEqual(self.messages[0]["text"], "Concise complete findings")
        self.assertEqual(len(self.progress[-1]["approved_results"]), 1)

    def test_worker_with_empty_assignment_receives_goal_and_general_work_instruction(self):
        self.workers[0]["assignment"] = ""
        self.run_script([submission(), review(), confirm()], workers=self.workers[:1])
        self.assertEqual(self.calls[0][1]["worker_assignment"], "")
        self.assertEqual(self.calls[0][1]["user_goal"], "Deliver a tested plan")
        self.assertIn("If it is empty", self.calls[0][1]["action"])

    def test_rate_limit_retries_current_action_without_handoff(self):
        waits = []
        self.run_script([core.RateLimitError("Busy", retry_after=0.01), submission(), review(), confirm()],
                        workers=self.workers[:1], on_wait=waits.append)
        self.assertEqual([c[0] for c in self.calls], ["A", "A", "master", "A"])
        self.assertTrue(any("rate limited" in message for message in waits))

    def test_stop_at_worker_confirmation_discards_late_agreement(self):
        stop = threading.Event()

        def late_confirmation(context):
            stop.set()
            return confirm()(context)

        reason = self.run_script([submission(), review(), late_confirmation], stop_event=stop)
        self.assertEqual(reason, "stopped")
        self.assertEqual([c[0] for c in self.calls], ["A", "master", "A"])
        self.assertEqual(self.progress[-1]["approved_results"], [])


if __name__ == "__main__":
    unittest.main()
