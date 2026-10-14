"""Subagent observable lifecycle records, with intact original cases."""

import json
import unittest
from unittest.mock import MagicMock, patch

from tools.delegate_tool import delegate_task
from tests.tools.test_delegate import _make_mock_parent


class TestDelegateObservability(unittest.TestCase):
    """Tests for enriched metadata returned by _run_single_child."""

    def test_observability_fields_present(self):
        """Completed child should return tool_trace, tokens, model, exit_reason."""
        parent = _make_mock_parent(depth=0)

        with patch("run_agent.AIAgent") as MockAgent:
            mock_child = MagicMock()
            mock_child.model = "claude-sonnet-4-6"
            mock_child.session_prompt_tokens = 5000
            mock_child.session_completion_tokens = 1200
            mock_child.run_conversation.return_value = {
                "final_response": "done",
                "completed": True,
                "interrupted": False,
                "api_calls": 3,
                "messages": [
                    {"role": "user", "content": "do something"},
                    {"role": "assistant", "tool_calls": [
                        {"id": "tc_1", "function": {"name": "web_search", "arguments": '{"query": "test"}'}}
                    ]},
                    {"role": "tool", "tool_call_id": "tc_1", "content": '{"results": [1,2,3]}'},
                    {"role": "assistant", "content": "done"},
                ],
            }
            MockAgent.return_value = mock_child

            result = json.loads(delegate_task(goal="Test observability", parent_agent=parent))
            entry = result["results"][0]

            # Core observability fields
            self.assertEqual(entry["model"], "claude-sonnet-4-6")
            self.assertEqual(entry["exit_reason"], "completed")
            self.assertEqual(entry["tokens"]["input"], 5000)
            self.assertEqual(entry["tokens"]["output"], 1200)

            # Tool trace
            self.assertEqual(len(entry["tool_trace"]), 1)
            self.assertEqual(entry["tool_trace"][0]["tool"], "web_search")
            self.assertIn("args_bytes", entry["tool_trace"][0])
            self.assertIn("result_bytes", entry["tool_trace"][0])
            self.assertEqual(
                entry["tool_trace"][0]["input_summary"],
                {"argument_keys": ["query"], "targets": {}},
            )
            self.assertEqual(entry["tool_trace"][0]["status"], "ok")

    def test_tool_trace_handles_list_content_blocks(self):
        """Tool-result content blocks should not crash observability metadata."""
        parent = _make_mock_parent(depth=0)

        with patch("run_agent.AIAgent") as MockAgent:
            mock_child = MagicMock()
            mock_child.model = "claude-sonnet-4-6"
            mock_child.session_prompt_tokens = 0
            mock_child.session_completion_tokens = 0
            mock_child.run_conversation.return_value = {
                "final_response": "done",
                "completed": True,
                "interrupted": False,
                "api_calls": 1,
                "messages": [
                    {"role": "assistant", "tool_calls": [
                        {"id": "tc_1", "function": {"name": "image_generate", "arguments": '{"prompt": "x"}'}}
                    ]},
                    {"role": "tool", "tool_call_id": "tc_1", "content": [
                        {"type": "text", "text": '{"success": true}'},
                    ]},
                ],
            }
            MockAgent.return_value = mock_child

            result = json.loads(delegate_task(goal="Test list content", parent_agent=parent))
            trace = result["results"][0]["tool_trace"]
            self.assertEqual(trace[0]["tool"], "image_generate")
            self.assertEqual(trace[0]["status"], "ok")
            self.assertGreater(trace[0]["result_bytes"], 0)

    def test_parallel_tool_calls_paired_correctly(self):
        """Parallel tool calls should each get their own result via tool_call_id matching."""
        parent = _make_mock_parent(depth=0)

        with patch("run_agent.AIAgent") as MockAgent:
            mock_child = MagicMock()
            mock_child.model = "claude-sonnet-4-6"
            mock_child.session_prompt_tokens = 3000
            mock_child.session_completion_tokens = 800
            mock_child.run_conversation.return_value = {
                "final_response": "done",
                "completed": True,
                "interrupted": False,
                "api_calls": 1,
                "messages": [
                    {"role": "assistant", "tool_calls": [
                        {"id": "tc_a", "function": {"name": "web_search", "arguments": '{"q": "a"}'}},
                        {"id": "tc_b", "function": {"name": "web_search", "arguments": '{"q": "b"}'}},
                        {"id": "tc_c", "function": {"name": "terminal", "arguments": '{"cmd": "ls"}'}},
                    ]},
                    {"role": "tool", "tool_call_id": "tc_a", "content": '{"ok": true}'},
                    {"role": "tool", "tool_call_id": "tc_b", "content": "Error: rate limited"},
                    {"role": "tool", "tool_call_id": "tc_c", "content": "file1.txt\nfile2.txt"},
                    {"role": "assistant", "content": "done"},
                ],
            }
            MockAgent.return_value = mock_child

            result = json.loads(delegate_task(goal="Test parallel", parent_agent=parent))
            trace = result["results"][0]["tool_trace"]

            # All three tool calls should have results
            self.assertEqual(len(trace), 3)

            # First: web_search → ok
            self.assertEqual(trace[0]["tool"], "web_search")
            self.assertEqual(trace[0]["status"], "ok")
            self.assertIn("result_bytes", trace[0])

            # Second: web_search → error
            self.assertEqual(trace[1]["tool"], "web_search")
            self.assertEqual(trace[1]["status"], "error")
            self.assertIn("result_bytes", trace[1])

            # Third: terminal → ok
            self.assertEqual(trace[2]["tool"], "terminal")
            self.assertEqual(trace[2]["status"], "ok")
            self.assertIn("result_bytes", trace[2])

    def test_empty_sentinel_marks_status_failed(self):
        """Regression: a child that returns the literal '(empty)' sentinel
        (emitted by run_agent.py when the LLM returns empty responses after
        retries — e.g. transport misrouting) must be reported as failed, not
        silently accepted as a completed delegation. Otherwise the parent
        surfaces an empty string as if the subagent succeeded."""
        parent = _make_mock_parent(depth=0)

        with patch("run_agent.AIAgent") as MockAgent:
            mock_child = MagicMock()
            mock_child.model = "claude-sonnet-4-6"
            mock_child.session_prompt_tokens = 0
            mock_child.session_completion_tokens = 0
            mock_child.run_conversation.return_value = {
                "final_response": "(empty)",
                "completed": True,
                "interrupted": False,
                "api_calls": 4,
                "messages": [],
            }
            MockAgent.return_value = mock_child

            result = json.loads(delegate_task(goal="Test empty sentinel", parent_agent=parent))
            self.assertEqual(result["results"][0]["status"], "failed")

    def test_failed_child_with_error_summary_marks_status_failed(self):
        """Regression: a child whose loop gave up on a structured failure
        (``failed=True``, ``completed=False``, e.g. "API call failed after 3
        retries: HTTP 524") returns that error message as final_response.
        Status was derived from summary alone, so the non-empty error text
        made the batch report show the task as ✓ status=completed. The
        ``failed`` flag must win over a non-empty summary."""
        parent = _make_mock_parent(depth=0)

        with patch("run_agent.AIAgent") as MockAgent:
            mock_child = MagicMock()
            mock_child.model = "claude-sonnet-4-6"
            mock_child.session_prompt_tokens = 0
            mock_child.session_completion_tokens = 0
            mock_child.run_conversation.return_value = {
                "final_response": (
                    "API call failed after 3 retries: HTTP 524 — origin timeout"
                ),
                "completed": False,
                "failed": True,
                "error": "HTTP 524 — origin timeout",
                "failure_reason": "server_error",
                "interrupted": False,
                "api_calls": 3,
                "messages": [],
            }
            MockAgent.return_value = mock_child

            result = json.loads(
                delegate_task(goal="Test failed child", parent_agent=parent)
            )
            entry = result["results"][0]
            self.assertEqual(entry["status"], "failed")
            # The classified reason must survive into the batch entry so the
            # parent can tell a quota wall from a real task error.
            self.assertEqual(entry["failure_reason"], "server_error")
            self.assertEqual(entry["error"], "HTTP 524 — origin timeout")
            # A structured failure is not budget truncation.
            self.assertEqual(entry["exit_reason"], "error")
            self.assertFalse(entry["truncated"])

    def test_successful_child_still_completed(self):
        """Control for the failed-flag check: a child that succeeds
        (``completed=True``, no ``failed`` flag) must keep reporting
        status=completed — the fix must not change success behavior."""
        parent = _make_mock_parent(depth=0)

        with patch("run_agent.AIAgent") as MockAgent:
            mock_child = MagicMock()
            mock_child.model = "claude-sonnet-4-6"
            mock_child.session_prompt_tokens = 0
            mock_child.session_completion_tokens = 0
            mock_child.run_conversation.return_value = {
                "final_response": "All done.",
                "completed": True,
                "interrupted": False,
                "api_calls": 2,
                "messages": [],
            }
            MockAgent.return_value = mock_child

            result = json.loads(
                delegate_task(goal="Test success control", parent_agent=parent)
            )
            entry = result["results"][0]
            self.assertEqual(entry["status"], "completed")
            self.assertEqual(entry["exit_reason"], "completed")
            self.assertNotIn("failure_reason", entry)
