# -*- coding: utf-8 -*-
import json
import unittest
from unittest.mock import patch

from ds_vision import (
    DeepSeekVisionError,
    completion,
    create_session,
    fork_files_for_model,
    parse_completion_stream,
    wait_for_files_ready,
)


def event(value, path=None):
    chunk = {"v": value}
    if path is not None:
        chunk["p"] = path
    return "data: " + json.dumps(chunk)


class CompletionStreamTests(unittest.TestCase):
    def test_create_session_matches_current_web_payload(self):
        class Response:
            def raise_for_status(self):
                pass

            def json(self):
                return {
                    "code": 0,
                    "data": {"biz_data": {"chat_session": {"id": "session-id"}}},
                }

        class Client:
            request_json = None

            def post(self, _url, **kwargs):
                self.request_json = kwargs["json"]
                return Response()

        client = Client()

        session_id = create_session(client, "test-token")

        self.assertEqual(session_id, "session-id")
        self.assertEqual(client.request_json, {})

    def test_initial_snapshot_and_pathless_deltas(self):
        lines = [
            event({"response": {"fragments": [
                {"type": "RESPONSE", "content": "The"}
            ], "message_id": 42}}),
            event(" answer", "response/fragments/-1/content"),
            event(" is"),
            event(" "),
            event("42"),
            event("FINISHED", "response/status"),
        ]

        result = parse_completion_stream(lines)

        self.assertEqual(result["text"], "The answer is 42")
        self.assertEqual(result["thinking"], "")
        self.assertEqual(result["message_id"], 42)

    def test_thinking_and_response_fragments_are_separated(self):
        lines = [
            event({"response": {"fragments": [
                {"type": "THINK", "content": "inspect"},
                {"type": "RESPONSE", "content": "answer"},
            ]}}),
            event(" complete"),
            "data: [DONE]",
        ]

        result = parse_completion_stream(lines)

        self.assertEqual(result["text"], "answer complete")
        self.assertEqual(result["thinking"], "inspect")

    @patch("ds_vision.get_pow_response", return_value="pow-response")
    def test_completion_sends_parent_message_id(self, _get_pow):
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                pass

            def raise_for_status(self):
                pass

            def iter_lines(self):
                return [event({"response": {
                    "message_id": 100,
                    "fragments": [{"type": "RESPONSE", "content": "next"}],
                }})]

        class Client:
            payload = None

            def stream(self, _method, _url, **kwargs):
                self.payload = kwargs["json"]
                return Response()

        client = Client()

        result = completion(
            client,
            "token",
            "chat-session",
            "follow up",
            [],
            parent_message_id=99,
        )

        self.assertEqual(client.payload["parent_message_id"], 99)
        self.assertEqual(client.payload["model_type"], "vision")
        self.assertEqual(result["message_id"], 100)

    def test_fork_files_for_vision_model(self):
        class Response:
            def raise_for_status(self):
                pass

            def json(self):
                return {
                    "code": 0,
                    "data": {
                        "biz_code": 0,
                        "biz_data": {"id": "file-vision"},
                    },
                }

        class Client:
            payload = None

            def post(self, _url, **kwargs):
                self.payload = kwargs["json"]
                return Response()

        client = Client()
        result = fork_files_for_model(client, "token", ["file-normal"], "vision")

        self.assertEqual(result, ["file-vision"])
        self.assertEqual(
            client.payload,
            {"file_id": "file-normal", "to_model_type": "vision"},
        )

    def test_wait_for_files_ready_polls_until_success(self):
        class Response:
            def __init__(self, status):
                self.status = status

            def raise_for_status(self):
                pass

            def json(self):
                return {
                    "code": 0,
                    "data": {
                        "biz_code": 0,
                        "biz_data": {
                            "files": [{"id": "file-1", "status": self.status}]
                        },
                    },
                }

        class Client:
            def __init__(self):
                self.statuses = iter(["PARSING", "SUCCESS"])
                self.calls = 0

            def get(self, *_args, **_kwargs):
                self.calls += 1
                return Response(next(self.statuses))

        client = Client()
        wait_for_files_ready(client, "token", ["file-1"], poll_interval=0)

        self.assertEqual(client.calls, 2)

    def test_wait_for_files_ready_reports_parse_failure(self):
        class Response:
            def raise_for_status(self):
                pass

            def json(self):
                return {
                    "code": 0,
                    "data": {
                        "biz_code": 0,
                        "biz_data": {
                            "files": [{
                                "id": "file-1",
                                "status": "PARSE_FAILED",
                                "error_code": "bad_image",
                            }]
                        },
                    },
                }

        class Client:
            def get(self, *_args, **_kwargs):
                return Response()

        with self.assertRaisesRegex(DeepSeekVisionError, "bad_image"):
            wait_for_files_ready(Client(), "token", ["file-1"], poll_interval=0)

    def test_wait_for_files_ready_accepts_content_empty_for_visual_only_image(self):
        class Response:
            def raise_for_status(self):
                pass

            def json(self):
                return {
                    "code": 0,
                    "data": {
                        "biz_code": 0,
                        "biz_data": {
                            "files": [{"id": "file-1", "status": "CONTENT_EMPTY"}]
                        },
                    },
                }

        class Client:
            calls = 0

            def get(self, *_args, **_kwargs):
                self.calls += 1
                return Response()

        client = Client()
        wait_for_files_ready(client, "token", ["file-1"], poll_interval=0)
        self.assertEqual(client.calls, 1)

    def test_wait_for_files_ready_queries_each_file_id_separately(self):
        class Response:
            def __init__(self, file_id):
                self.file_id = file_id

            def raise_for_status(self):
                pass

            def json(self):
                return {
                    "code": 0,
                    "data": {
                        "biz_code": 0,
                        "biz_data": {
                            "files": [{"id": self.file_id, "status": "SUCCESS"}]
                        },
                    },
                }

        class Client:
            def __init__(self):
                self.requested_ids = []

            def get(self, *_args, **kwargs):
                file_id = kwargs["params"]["file_ids"]
                self.requested_ids.append(file_id)
                return Response(file_id)

        client = Client()
        wait_for_files_ready(
            client,
            "token",
            ["file-2", "file-1"],
            poll_interval=0,
        )

        self.assertEqual(client.requested_ids, ["file-1", "file-2"])


if __name__ == "__main__":
    unittest.main()
