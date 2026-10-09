"""Real loopback HTTP proof of normal retries and independently paced fallback transitions.

The responses below are explicit test fixtures, never a production-provider health result.
Run as a script with --home/--report to exercise an installed server interpreter as well.
"""

from __future__ import annotations

import argparse
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


def exercise_http_fallback(home: Path, status: int = 429) -> dict:
    os.environ["HERMES_HOME"] = str(home)
    from hermes_cli.config import save_config
    from run_agent import AIAgent

    requests = []
    routes = ["primary", "fallback-one", "fallback-two", "fallback-three"]

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            body = json.dumps({"object": "list", "data": [
                {"id": model, "context_length": 128000} for model in routes
            ]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
            model = payload["model"]
            requests.append({"model": model, "at": time.monotonic(), "stream": bool(payload.get("stream"))})
            if model != routes[-1]:
                body = json.dumps({"error": {"message": "fixture QPM throttle" if status == 429 else "fixture internal server error", "type": "rate_limit_error" if status == 429 else "server_error"}}).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            chunk = {"id": "fixture-completion", "object": "chat.completion.chunk", "created": 1,
                     "model": model, "choices": [{"index": 0, "delta": {"role": "assistant", "content": "FIXTURE_OK"}, "finish_reason": None}]}
            terminal = {**chunk, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
            body = ("data: " + json.dumps(chunk) + "\n\ndata: " + json.dumps(terminal) + "\n\ndata: [DONE]\n\n").encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    worker = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    worker.start()
    root = f"http://127.0.0.1:{server.server_port}"
    chain = [{"provider": "custom", "model": model, "base_url": f"{root}/{model}/v1",
              "api_key": "fixture-only-key", "api_mode": "chat_completions"} for model in routes[1:]]
    save_config({"fallback": {"inter_switch_backoff_seconds": 1},
                 "agent": {"api_max_retries": 3, "auto_recovery_cycles": 0},
                 "model": {"context_length": 128000}, "environment_probe": False}, merge_existing=True)
    agent = None
    try:
        agent = AIAgent(api_key="fixture-only-key", provider="custom", model=routes[0],
                        base_url=f"{root}/{routes[0]}/v1", api_mode="chat_completions",
                        enabled_toolsets=[], quiet_mode=True, skip_context_files=True,
                        skip_memory=True, fallback_model=chain, max_iterations=1)
        result = agent.run_conversation("Return FIXTURE_OK only.")
        assert result["completed"] is True, result.get("error")
        assert result["final_response"] == "FIXTURE_OK"
        groups = []
        for request in requests:
            if not groups or groups[-1]["model"] != request["model"]:
                groups.append({"model": request["model"], "first": request["at"], "last": request["at"], "requests": 1, "stream_requests": int(request["stream"])})
            else:
                groups[-1]["last"] = request["at"]
                groups[-1]["requests"] += 1
                groups[-1]["stream_requests"] += int(request["stream"])
        assert [g["model"] for g in groups] == routes
        gaps = [groups[i+1]["first"] - groups[i]["last"] for i in range(len(groups)-1)]
        for gap, expected in zip(gaps, (1, 2, 4)):
            assert gap >= expected, (gap, expected)
        expected_requests = 1 if status == 429 else agent._api_max_retries
        assert all(g["stream_requests"] == expected_requests for g in groups[:-1]), groups
        assert agent._api_max_retries == 3
        return {"test_fixture": True, "failure_status": status, "completed": True, "response": result["final_response"],
                "routes": routes, "requests_per_route": [g["requests"] for g in groups],
                "stream_requests_per_route": [g["stream_requests"] for g in groups],
                "transition_gap_seconds": [round(g, 3) for g in gaps],
                "hermes_retry_budget": agent._api_max_retries,
                "loaded_source": str(Path(__import__("run_agent").__file__).resolve())}
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=3)
        assert not worker.is_alive()
        if agent is not None and agent.client is not None:
            agent.client.close()


def test_loopback_http_fallback_obeys_spacing_and_model_retries(tmp_path, monkeypatch, status):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    evidence = exercise_http_fallback(tmp_path, status=status)
    assert evidence["completed"]


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--home", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--status", type=int, choices=[429, 500], default=429)
    args = parser.parse_args()
    args.home.mkdir(parents=True, exist_ok=True)
    evidence = exercise_http_fallback(args.home, status=args.status)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(evidence, indent=2) + "\n")
    print(json.dumps(evidence))
else:
    import pytest
    pytestmark = pytest.mark.real_retry_backoff
    test_loopback_http_fallback_obeys_spacing_and_model_retries = pytest.mark.parametrize("status", [429, 500])(test_loopback_http_fallback_obeys_spacing_and_model_retries)
