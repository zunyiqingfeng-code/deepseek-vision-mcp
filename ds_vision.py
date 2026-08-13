# -*- coding: utf-8 -*-
"""DeepSeek 网页版视觉问答 CLI 与可复用协议客户端。"""
import argparse
import base64
import ctypes
import json
import mimetypes
import os
import struct
import time
from pathlib import Path

import httpx
from wasmtime import Linker, Module, Store

HOST = "https://chat.deepseek.com"
TOKEN_FILE = os.path.join(os.path.expanduser("~"), ".ds_web_token.json")
WASM_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sha3_wasm_bg.wasm")
SUPPORTED_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"


class DeepSeekVisionError(RuntimeError):
    """DeepSeek 网页视觉协议调用失败。"""

def base_headers(token, pow_resp=None):
    h = {
        "authorization": f"Bearer {token}",
        "x-client-bundle-id": "com.deepseek.chat",
        "x-client-version": "2.3.0",
        "x-client-platform": "web",
        "x-client-locale": "zh_CN",
        "x-client-timezone-offset": "-14400",
        "origin": "https://chat.deepseek.com",
        "referer": "https://chat.deepseek.com/",
        "user-agent": UA,
    }
    if pow_resp:
        h["x-ds-pow-response"] = pow_resp
    return h


def load_token():
    try:
        with open(TOKEN_FILE, encoding="utf-8") as f:
            token = json.load(f)["token"]
    except (OSError, KeyError, json.JSONDecodeError) as exc:
        raise DeepSeekVisionError(
            f"无法读取 DeepSeek 登录令牌，请检查 {TOKEN_FILE}"
        ) from exc
    if not isinstance(token, str) or not token.strip():
        raise DeepSeekVisionError("DeepSeek 登录令牌为空")
    return token.strip()


def resolve_image_path(image_path):
    path = Path(image_path).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    try:
        path = path.resolve(strict=True)
    except OSError as exc:
        raise DeepSeekVisionError(f"图片不存在: {image_path}") from exc
    if not path.is_file():
        raise DeepSeekVisionError(f"图片路径不是文件: {path}")
    if path.suffix.lower() not in SUPPORTED_IMAGE_SUFFIXES:
        supported = ", ".join(sorted(SUPPORTED_IMAGE_SUFFIXES))
        raise DeepSeekVisionError(f"不支持的图片格式 {path.suffix!r}，支持: {supported}")
    if path.stat().st_size == 0:
        raise DeepSeekVisionError(f"图片文件为空: {path}")
    return path


def solve_pow(challenge):
    """调用 wasm 求解 DeepSeekHashV1，返回 answer"""
    store = Store()
    linker = Linker(store.engine)
    with open(WASM_PATH, "rb") as f:
        wasm_bytes = f.read()
    module = Module(store.engine, wasm_bytes)
    instance = linker.instantiate(store, module)
    exports = instance.exports(store)
    memory = exports["memory"]
    add_to_stack = exports["__wbindgen_add_to_stack_pointer"]
    alloc = exports["__wbindgen_export_0"]
    wasm_solve = exports["wasm_solve"]

    def write_memory(offset, data):
        base_addr = ctypes.cast(memory.data_ptr(store), ctypes.c_void_p).value
        ctypes.memmove(base_addr + offset, data, len(data))

    def read_memory(offset, size):
        base_addr = ctypes.cast(memory.data_ptr(store), ctypes.c_void_p).value
        return ctypes.string_at(base_addr + offset, size)

    def encode_string(text):
        data = text.encode("utf-8")
        ptr_val = alloc(store, len(data), 1)
        ptr = int(ptr_val.value) if hasattr(ptr_val, "value") else int(ptr_val)
        write_memory(ptr, data)
        return ptr, len(data)

    prefix = f"{challenge['salt']}_{challenge['expire_at']}_"
    retptr = add_to_stack(store, -16)
    ptr_c, len_c = encode_string(challenge["challenge"])
    ptr_p, len_p = encode_string(prefix)
    wasm_solve(store, retptr, ptr_c, len_c, ptr_p, len_p, float(challenge["difficulty"]))
    status = struct.unpack("<i", read_memory(retptr, 4))[0]
    value = struct.unpack("<d", read_memory(retptr + 8, 8))[0]
    add_to_stack(store, 16)
    if status != 0:
        return int(value)
    return None


def get_pow_response(client, token, target_path):
    """获取 PoW challenge 并求解，返回 x-ds-pow-response 值"""
    r = client.post(f"{HOST}/api/v0/chat/create_pow_challenge",
                    headers=base_headers(token),
                    json={"target_path": target_path})
    r.raise_for_status()
    data = r.json()
    if data.get("code") != 0:
        raise DeepSeekVisionError(f"create_pow_challenge failed: {data}")
    ch = data["data"]["biz_data"]["challenge"]
    answer = solve_pow(ch)
    if answer is None:
        raise DeepSeekVisionError("pow solve failed")
    payload = {
        "algorithm": ch["algorithm"],
        "challenge": ch["challenge"],
        "salt": ch["salt"],
        "answer": answer,
        "signature": ch["signature"],
        "target_path": target_path,
    }
    return base64.b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode()


def create_session(client, token):
    r = client.post(f"{HOST}/api/v0/chat_session/create",
                    headers=base_headers(token),
                    json={})
    r.raise_for_status()
    data = r.json()
    if data.get("code") != 0:
        raise DeepSeekVisionError(f"create session failed: {data}")
    return data["data"]["biz_data"]["chat_session"]["id"]


def upload_file(client, token, image_path):
    image_path = resolve_image_path(image_path)
    pow_resp = get_pow_response(client, token, "/api/v0/file/upload_file")
    content_type = mimetypes.guess_type(image_path.name)[0] or "application/octet-stream"
    with open(image_path, "rb") as f:
        files = {"file": (image_path.name, f, content_type)}
        r = client.post(f"{HOST}/api/v0/file/upload_file",
                        headers=base_headers(token, pow_resp),
                        files=files)
    r.raise_for_status()
    data = r.json()
    if data.get("code") != 0:
        raise DeepSeekVisionError(f"upload failed: {data}")
    return data["data"]["biz_data"]["id"]


def wait_for_files_ready(
    client,
    token,
    file_ids,
    timeout=60.0,
    poll_interval=0.5,
):
    """Wait until DeepSeek finishes parsing uploaded files before completion."""
    pending = {str(file_id) for file_id in file_ids if file_id}
    if not pending:
        return
    deadline = time.monotonic() + timeout
    last_statuses = {}

    while pending:
        completed = []
        for file_id in sorted(pending):
            r = client.get(
                f"{HOST}/api/v0/file/fetch_files",
                headers=base_headers(token),
                params={"file_ids": file_id},
            )
            r.raise_for_status()
            data = r.json()
            response_data = data.get("data", {})
            if data.get("code", 0) != 0 or response_data.get("biz_code", 0) != 0:
                raise DeepSeekVisionError(f"fetch_files failed: {data}")

            files = response_data.get("biz_data", {}).get("files", [])
            info = next(
                (item for item in files if str(item.get("id", "")) == file_id),
                None,
            )
            if info is None:
                last_statuses[file_id] = "MISSING"
                continue
            status = str(info.get("status", "")).upper()
            last_statuses[file_id] = status or "UNKNOWN"
            # Images without OCR-readable text are reported as CONTENT_EMPTY,
            # but their visual pixels are already available to completion.
            if status in {"SUCCESS", "CONTENT_EMPTY"}:
                completed.append(file_id)
            elif "FAIL" in status or info.get("error_code"):
                reason = info.get("error_code") or status
                raise DeepSeekVisionError(
                    f"DeepSeek 图片解析失败: file_id={file_id}, reason={reason}"
                )

        pending.difference_update(completed)

        if not pending:
            return
        if time.monotonic() >= deadline:
            statuses = {file_id: last_statuses.get(file_id, "UNKNOWN") for file_id in pending}
            raise DeepSeekVisionError(
                f"等待 DeepSeek 图片解析超时: {statuses}"
            )
        time.sleep(poll_interval)


def fork_files_for_model(client, token, file_ids, model_type="vision"):
    """Convert uploaded NORMAL files into model-specific VISION files."""
    forked_ids = []
    for file_id in file_ids:
        r = client.post(
            f"{HOST}/api/v0/file/fork_file_task",
            headers=base_headers(token),
            json={"file_id": file_id, "to_model_type": model_type},
        )
        r.raise_for_status()
        data = r.json()
        response_data = data.get("data", {})
        if data.get("code", 0) != 0 or response_data.get("biz_code", 0) != 0:
            raise DeepSeekVisionError(f"fork_file_task failed: {data}")
        forked_id = response_data.get("biz_data", {}).get("id")
        if not forked_id:
            raise DeepSeekVisionError("fork_file_task 未返回视觉文件 ID")
        forked_ids.append(forked_id)
    return forked_ids


def parse_completion_stream(lines):
    """解析 DeepSeek SSE 的首块快照与后续增量 patch。"""
    text_parts = []
    thinking_parts = []
    active_channel = "text"
    message_id = None

    def append_content(content, channel):
        if not isinstance(content, str):
            return
        if channel == "thinking":
            thinking_parts.append(content)
        else:
            text_parts.append(content)

    def append_fragment(fragment):
        nonlocal active_channel
        if not isinstance(fragment, dict):
            return
        fragment_type = str(fragment.get("type", "RESPONSE")).upper()
        active_channel = "thinking" if "THINK" in fragment_type else "text"
        append_content(fragment.get("content"), active_channel)

    for line in lines:
        if not line or not line.startswith("data:"):
            continue
        raw = line[5:].strip()
        if raw == "[DONE]":
            break
        try:
            chunk = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if "v" not in chunk:
            continue

        value = chunk["v"]
        path = chunk.get("p", "")
        if path == "response/status" and value == "FINISHED":
            break
        if path == "response/message_id":
            message_id = value
            continue

        if isinstance(value, dict):
            response = value.get("response")
            if isinstance(response, dict):
                if response.get("message_id") is not None:
                    message_id = response["message_id"]
                for fragment in response.get("fragments", []):
                    append_fragment(fragment)
            else:
                append_fragment(value)
            continue

        if not isinstance(value, str):
            continue
        if path == "response/thinking_content":
            active_channel = "thinking"
            append_content(value, active_channel)
        elif path == "response/content":
            active_channel = "text"
            append_content(value, active_channel)
        elif path.startswith("response/fragments/") and path.endswith("/content"):
            append_content(value, active_channel)
        elif not path:
            append_content(value, active_channel)

    return {
        "text": "".join(text_parts),
        "thinking": "".join(thinking_parts),
        "message_id": message_id,
    }


def completion(
    client,
    token,
    session_id,
    prompt,
    file_ids,
    thinking=True,
    search=True,
    parent_message_id=None,
    model_type="vision",
):
    pow_resp = get_pow_response(client, token, "/api/v0/chat/completion")
    payload = {
        "chat_session_id": session_id,
        "parent_message_id": parent_message_id,
        "model_type": model_type,
        "prompt": prompt,
        "ref_file_ids": file_ids,
        "thinking_enabled": thinking,
        "search_enabled": search,
        "action": None,
        "preempt": False,
    }
    with client.stream(
        "POST",
        f"{HOST}/api/v0/chat/completion",
        headers=base_headers(token, pow_resp),
        json=payload,
    ) as r:
        r.raise_for_status()
        return parse_completion_stream(r.iter_lines())


def analyze_images(image_paths, prompt, thinking=True, search=False, token=None):
    """创建独立会话，上传一张或多张图片并返回首轮结果及会话元数据。"""
    if isinstance(image_paths, (str, os.PathLike)):
        image_paths = [image_paths]
    paths = [resolve_image_path(path) for path in image_paths]
    if not paths:
        raise DeepSeekVisionError("至少需要一张图片")
    if not isinstance(prompt, str) or not prompt.strip():
        raise DeepSeekVisionError("问题不能为空")

    auth_token = token or load_token()
    timeout = httpx.Timeout(120.0, connect=20.0)
    try:
        with httpx.Client(timeout=timeout, follow_redirects=False) as client:
            session_id = create_session(client, auth_token)
            file_ids = [upload_file(client, auth_token, path) for path in paths]
            wait_for_files_ready(client, auth_token, file_ids)
            file_ids = fork_files_for_model(client, auth_token, file_ids, "vision")
            wait_for_files_ready(client, auth_token, file_ids)
            result = completion(
                client,
                auth_token,
                session_id,
                prompt.strip(),
                file_ids,
                thinking,
                search,
                model_type="vision",
            )
            result["chat_session_id"] = session_id
            return result
    except httpx.HTTPError as exc:
        raise DeepSeekVisionError(f"DeepSeek HTTP 请求失败: {exc}") from exc


def continue_vision_session(
    chat_session_id,
    parent_message_id,
    prompt,
    image_paths=None,
    thinking=True,
    search=False,
    token=None,
):
    """在既有 DeepSeek 会话中继续追问，可选追加图片。"""
    if not isinstance(chat_session_id, str) or not chat_session_id.strip():
        raise DeepSeekVisionError("chat_session_id 不能为空")
    if parent_message_id is None:
        raise DeepSeekVisionError("parent_message_id 不能为空")
    if not isinstance(prompt, str) or not prompt.strip():
        raise DeepSeekVisionError("问题不能为空")

    if image_paths is None:
        image_paths = []
    elif isinstance(image_paths, (str, os.PathLike)):
        image_paths = [image_paths]
    paths = [resolve_image_path(path) for path in image_paths]

    auth_token = token or load_token()
    timeout = httpx.Timeout(120.0, connect=20.0)
    try:
        with httpx.Client(timeout=timeout, follow_redirects=False) as client:
            file_ids = [upload_file(client, auth_token, path) for path in paths]
            wait_for_files_ready(client, auth_token, file_ids)
            file_ids = fork_files_for_model(client, auth_token, file_ids, "vision")
            wait_for_files_ready(client, auth_token, file_ids)
            result = completion(
                client,
                auth_token,
                chat_session_id.strip(),
                prompt.strip(),
                file_ids,
                thinking=thinking,
                search=search,
                parent_message_id=parent_message_id,
                model_type="vision",
            )
            result["chat_session_id"] = chat_session_id.strip()
            return result
    except httpx.HTTPError as exc:
        raise DeepSeekVisionError(f"DeepSeek HTTP 请求失败: {exc}") from exc


def main():
    parser = argparse.ArgumentParser(description="DeepSeek 网页版视觉问答")
    parser.add_argument("image_path", help="本地图片路径")
    parser.add_argument("prompt", help="针对图片的问题")
    parser.add_argument("--no-thinking", action="store_true", help="关闭思考模式")
    parser.add_argument("--no-search", action="store_true", help="关闭联网搜索")
    args = parser.parse_args()

    token = load_token()
    image_path = resolve_image_path(args.image_path)
    timeout = httpx.Timeout(120.0, connect=20.0)
    with httpx.Client(timeout=timeout, follow_redirects=False) as client:
        print("[1/4] 创建会话...")
        session_id = create_session(client, token)
        print(f"      session: {session_id}")
        print("[2/4] 上传图片...")
        file_id = upload_file(client, token, image_path)
        print(f"      file_id: {file_id}")
        wait_for_files_ready(client, token, [file_id])
        file_id = fork_files_for_model(client, token, [file_id], "vision")[0]
        wait_for_files_ready(client, token, [file_id])
        print("[3/4] 求解 PoW...")
        print("[4/4] 请求视觉问答...")
        t0 = time.time()
        result = completion(
            client,
            token,
            session_id,
            args.prompt,
            [file_id],
            not args.no_thinking,
            not args.no_search,
            model_type="vision",
        )
        dt = time.time() - t0
        print(f"耗时: {dt:.1f}s")
        if result["thinking"]:
            print("==== 思考 ====")
            print(result["thinking"])
        print("==== 回答 ====")
        print(result["text"] or "(空)")


if __name__ == "__main__":
    main()
