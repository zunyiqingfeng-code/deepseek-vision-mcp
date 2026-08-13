import json
import inspect
import errno
import concurrent.futures
import threading
import time
import tempfile
import unittest
from pathlib import Path
from unittest.mock import ANY, patch

import ds_vision_mcp as server


class SharedSessionTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.state_path = Path(self.temp_dir.name) / ".shared_session.json"
        self.lock_path = self.state_path.with_name(self.state_path.name + ".lock")
        self.paths = patch.multiple(
            server,
            SESSION_STATE_FILE=self.state_path,
            SESSION_LOCK_FILE=self.lock_path,
        )
        self.paths.start()

    def tearDown(self):
        self.paths.stop()
        self.temp_dir.cleanup()

    def test_two_initial_calls_share_one_web_session(self):
        first = {
            "text": "first",
            "message_id": 2,
            "chat_session_id": "chat-1",
        }
        second = {
            "text": "second",
            "message_id": 4,
            "chat_session_id": "chat-1",
        }
        with patch.object(server, "analyze_images", return_value=first) as create, \
                patch.object(server, "continue_vision_session", return_value=second) as follow:
            result1 = server._start_analysis(["one.png"], "read one", False, False)
            result2 = server._start_analysis(["two.png"], "read two", False, False)

        self.assertEqual(result1["answer"], "first")
        self.assertEqual(result2["answer"], "second")
        self.assertEqual(result1["turn"], 1)
        self.assertEqual(result2["turn"], 1)
        self.assertNotEqual(result1["session_id"], result2["session_id"])
        self.assertEqual(result1["web_chat_session_id"], "chat-1")
        self.assertEqual(result2["web_chat_session_id"], "chat-1")
        create.assert_called_once_with(["one.png"], "read one", thinking=False, search=False)
        follow.assert_called_once_with(
            "chat-1",
            2,
            "read two",
            image_paths=["two.png"],
            thinking=False,
            search=False,
        )
        self.assertEqual(
            json.loads(self.state_path.read_text(encoding="utf-8")),
            {
                "version": 2,
                "chat_session_id": "chat-1",
                "anchor_message_id": 4,
                "anchor_turn": 2,
                "branches": {
                    result1["session_id"]: {
                        "parent_message_id": 2,
                        "turn": 1,
                        "updated_at": ANY,
                    },
                    result2["session_id"]: {
                        "parent_message_id": 4,
                        "turn": 1,
                        "updated_at": ANY,
                    },
                },
            },
        )

    def test_continue_reads_state_after_server_restart(self):
        self.state_path.write_text(
            json.dumps(
                {
                    "version": 2,
                    "chat_session_id": "chat-1",
                    "anchor_message_id": 4,
                    "anchor_turn": 2,
                    "branches": {
                        "branch-1": {
                            "parent_message_id": 4,
                            "turn": 2,
                            "updated_at": 1.0,
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        result = {"text": "third", "message_id": 6, "chat_session_id": "chat-1"}
        with patch.object(server, "continue_vision_session", return_value=result) as follow:
            output = server._continue_analysis("branch-1", "read again", [], False, False)

        self.assertEqual(
            output,
            {
                "answer": "third",
                "session_id": "branch-1",
                "turn": 3,
                "web_chat_session_id": "chat-1",
            },
        )
        follow.assert_called_once_with(
            "chat-1",
            4,
            "read again",
            image_paths=[],
            thinking=False,
            search=False,
        )

    def test_end_does_not_clear_shared_session(self):
        self.state_path.write_text(
            json.dumps(
                {"chat_session_id": "chat-1", "parent_message_id": 4, "turn": 2}
            ),
            encoding="utf-8",
        )
        output = server.end_analysis_session("chat-1")

        self.assertFalse(output["ended"])
        self.assertTrue(output["shared"])
        self.assertTrue(self.state_path.exists())

    def test_thinking_is_enabled_by_default(self):
        self.assertTrue(inspect.signature(server.analyze_image).parameters["thinking"].default)
        self.assertTrue(inspect.signature(server.analyze_multiple_images).parameters["thinking"].default)
        self.assertFalse(inspect.signature(server.analyze_image_set).parameters["thinking"].default)
        self.assertTrue(inspect.signature(server.continue_analysis).parameters["thinking"].default)
        self.assertTrue(inspect.signature(server.capture_screen).parameters["thinking"].default)

    def test_empty_thinking_answer_retries_in_same_session(self):
        first = {
            "text": "",
            "thinking": "visual reasoning",
            "message_id": 2,
            "chat_session_id": "chat-1",
        }
        fallback = {
            "text": "final answer",
            "message_id": 4,
            "chat_session_id": "chat-1",
        }
        with patch.object(server, "analyze_images", return_value=first), \
                patch.object(server, "continue_vision_session", return_value=fallback) as follow:
            output = server._start_analysis(["one.png"], "inspect", True, False)

        self.assertEqual(output["answer"], "final answer")
        self.assertEqual(output["thinking"], "visual reasoning")
        self.assertTrue(output["thinking_fallback"])
        self.assertEqual(output["web_chat_session_id"], "chat-1")
        self.assertEqual(output["turn"], 1)
        self.assertEqual(follow.call_args.args[:2], ("chat-1", 2))
        self.assertEqual(follow.call_args.kwargs["image_paths"], [])
        self.assertFalse(follow.call_args.kwargs["thinking"])

    def test_capture_screen_removes_temporary_image(self):
        screenshot = Path(self.temp_dir.name) / "screen.png"
        screenshot.write_bytes(b"png")
        metadata = {"target": "screen", "monitor": 0, "width": 100, "height": 50}
        with patch.object(server, "_capture_screenshot", return_value=(screenshot, metadata)), \
                patch.object(
                    server,
                    "_start_analysis",
                    return_value={"answer": "desktop", "session_id": "chat-1", "turn": 3},
                ) as analyze:
            output = server.capture_screen("inspect screen")

        self.assertFalse(screenshot.exists())
        self.assertEqual(output["capture"], metadata)
        self.assertEqual(analyze.call_args.args[0], [str(screenshot)])
        self.assertTrue(analyze.call_args.args[2])

    def test_windows_file_lock_retries_deadlock_error(self):
        if server.os.name != "nt":
            self.skipTest("Windows lock behavior")
        import msvcrt

        deadlock = OSError(errno.EDEADLK, "Resource deadlock avoided")
        with patch("msvcrt.locking", side_effect=[deadlock, None, None]) as locking, \
                patch.object(server.time, "sleep") as sleep:
            with server._shared_state_lock():
                pass

        self.assertEqual(locking.call_count, 3)
        sleep.assert_called_once_with(0.1)
        self.assertEqual(locking.call_args_list[1].args[1], msvcrt.LK_NBLCK)
        self.assertEqual(locking.call_args_list[2].args[1], msvcrt.LK_UNLCK)

    def test_webp_is_converted_and_temporary_file_is_removed(self):
        from PIL import Image

        source = Path(self.temp_dir.name) / "asset.webp"
        Image.new("RGBA", (32, 24), (10, 20, 30, 128)).save(source, "WEBP")
        with server._prepared_image_paths([str(source)]) as prepared:
            converted = Path(prepared[0])
            self.assertEqual(converted.suffix.lower(), ".jpg")
            self.assertTrue(converted.exists())
            with Image.open(converted) as image:
                self.assertEqual(image.mode, "RGB")
                self.assertEqual(image.size, (32, 24))

        self.assertFalse(converted.exists())
        self.assertTrue(source.exists())

    def test_analyze_image_set_expands_globs_and_reports_matches(self):
        from PIL import Image

        before = Path(self.temp_dir.name) / "before"
        after = Path(self.temp_dir.name) / "after"
        before.mkdir()
        after.mkdir()
        first = before / "a.png"
        second = after / "b.jpg"
        Image.new("RGB", (16, 16), "red").save(first)
        Image.new("RGB", (16, 16), "blue").save(second)
        (after / "ignore.txt").write_text("not an image", encoding="utf-8")

        def response_for_batch(paths, question, *_args):
            is_first = Path(paths[0]).name == "a.png"
            self.assertIn(Path(paths[0]).name, question)
            return {
                "answer": "first review" if is_first else "second review",
                "session_id": "branch-1" if is_first else "branch-2",
                "web_chat_session_id": "chat-1",
                "turn": 1,
            }

        state = server.SharedVisionSessionState("chat-1", 4)
        with patch.object(server, "_start_analysis", side_effect=response_for_batch) as analyze, \
                patch.object(server, "_state_snapshot", return_value=state):
            output = server.analyze_image_set(
                ["before/*.png", "after/*", "before/a.png"],
                "compare screenshots",
                base_directory=self.temp_dir.name,
                batch_size=1,
                thinking=False,
            )

        self.assertEqual(output["matched_count"], 2)
        self.assertEqual(output["matched_images"], [str(first.resolve()), str(second.resolve())])
        self.assertEqual(output["batch_count"], 2)
        self.assertEqual(output["web_chat_session_id"], "chat-1")
        analyzed_batches = {tuple(call.args[0]) for call in analyze.call_args_list}
        self.assertEqual(
            analyzed_batches,
            {(str(first.resolve()),), (str(second.resolve()),)},
        )
        self.assertTrue(all(not call.args[2] for call in analyze.call_args_list))

    def test_analyze_image_set_runs_batches_in_parallel(self):
        from PIL import Image

        image_paths = []
        for index in range(2):
            image_path = Path(self.temp_dir.name) / f"{index}.png"
            Image.new("RGB", (8, 8), "white").save(image_path)
            image_paths.append(image_path)

        barrier = threading.Barrier(2, timeout=3)

        def parallel_response(paths, *_args):
            barrier.wait()
            return {
                "answer": Path(paths[0]).name,
                "session_id": f"branch-{Path(paths[0]).stem}",
                "web_chat_session_id": "chat-1",
                "turn": 1,
            }

        state = server.SharedVisionSessionState("chat-1", 4)
        with patch.object(server, "_start_analysis", side_effect=parallel_response), \
                patch.object(server, "_state_snapshot", return_value=state):
            output = server.analyze_image_set(
                ["*.png"],
                "inspect",
                base_directory=self.temp_dir.name,
                batch_size=1,
                thinking=False,
            )

        self.assertEqual(output["batch_count"], 2)
        self.assertEqual([item["batch"] for item in output["batch_results"]], [1, 2])

    def test_analyze_image_set_rejects_large_batch_size(self):
        with self.assertRaisesRegex(server.DeepSeekVisionError, "batch_size 必须是 1-4"):
            server.analyze_image_set(
                ["*.png"],
                "inspect",
                base_directory=self.temp_dir.name,
                batch_size=5,
            )

    def test_analyze_image_set_rejects_too_many_matches(self):
        from PIL import Image

        for index in range(3):
            Image.new("RGB", (8, 8), "white").save(
                Path(self.temp_dir.name) / f"{index}.png"
            )

        with self.assertRaisesRegex(server.DeepSeekVisionError, "超过 max_images=2"):
            server.analyze_image_set(
                ["*.png"],
                "inspect",
                base_directory=self.temp_dir.name,
                max_images=2,
            )

    def test_independent_branches_enter_network_in_parallel(self):
        self.state_path.write_text(
            json.dumps(
                {
                    "version": 2,
                    "chat_session_id": "chat-1",
                    "anchor_message_id": 4,
                    "anchor_turn": 2,
                    "branches": {},
                }
            ),
            encoding="utf-8",
        )
        barrier = threading.Barrier(2, timeout=3)

        def parallel_response(_chat, _parent, prompt, **_kwargs):
            barrier.wait()
            number = 6 if prompt == "first" else 8
            return {"text": prompt, "message_id": number, "chat_session_id": "chat-1"}

        with patch.object(server, "continue_vision_session", side_effect=parallel_response):
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                futures = [
                    pool.submit(server._start_analysis, ["one.png"], "first", False, False),
                    pool.submit(server._start_analysis, ["two.png"], "second", False, False),
                ]
                outputs = [future.result(timeout=5) for future in futures]

        self.assertEqual({output["answer"] for output in outputs}, {"first", "second"})
        self.assertEqual(len({output["session_id"] for output in outputs}), 2)
        self.assertEqual({output["web_chat_session_id"] for output in outputs}, {"chat-1"})

    def test_request_pool_runs_two_at_a_time(self):
        self.state_path.write_text(
            json.dumps(
                {
                    "version": 2,
                    "chat_session_id": "chat-1",
                    "anchor_message_id": 4,
                    "anchor_turn": 2,
                    "branches": {},
                }
            ),
            encoding="utf-8",
        )
        counter_lock = threading.Lock()
        active = 0
        max_active = 0
        next_message_id = 10

        def bounded_response(_chat, _parent, prompt, **_kwargs):
            nonlocal active, max_active, next_message_id
            with counter_lock:
                active += 1
                max_active = max(max_active, active)
                message_id = next_message_id
                next_message_id += 2
            time.sleep(0.05)
            with counter_lock:
                active -= 1
            return {"text": prompt, "message_id": message_id, "chat_session_id": "chat-1"}

        with patch.object(server, "continue_vision_session", side_effect=bounded_response):
            with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
                outputs = list(
                    pool.map(
                        lambda index: server._start_analysis(
                            [f"{index}.png"], f"answer-{index}", False, False
                        ),
                        range(4),
                    )
                )

        self.assertEqual(len(outputs), 4)
        self.assertEqual(max_active, server.MAX_PARALLEL_REQUESTS)


if __name__ == "__main__":
    unittest.main()
