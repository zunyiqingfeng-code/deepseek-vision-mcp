# -*- coding: utf-8 -*-
"""MCP server exposing one persistent DeepSeek web vision conversation."""
import json
import errno
import glob
import os
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from threading import BoundedSemaphore, Lock
from uuid import uuid4

from mcp.server.fastmcp import FastMCP

from ds_vision import (
    TOKEN_FILE,
    WASM_PATH,
    SUPPORTED_IMAGE_SUFFIXES,
    DeepSeekVisionError,
    analyze_images,
    continue_vision_session,
    load_token,
    resolve_image_path,
)


mcp = FastMCP(
    "deepseek-vision",
    instructions=(
        "当用户要求查看、识别、OCR、解释或比较本地图片时，调用本服务器的工具。"
        "把用户真正想从图片中得知的问题原样传给 question。所有视觉请求默认复用同一个 "
        "DeepSeek 网页对话；analyze_image/analyze_images 也会追加到这个共享对话，不要因为图片 "
        "不同就创建新的网页会话。每次首轮工具返回独立逻辑分支 session_id，多个分支可并发；"
        "web_chat_session_id 始终是同一个网页会话。后续追问必须用对应分支 session_id 调用 "
        "continue_analysis。用户要求查看当前屏幕、窗口或界面时，"
        "直接调用 capture_screen 自主截图并分析；界面变化后可再次调用该工具复查。"
        "前端、网页、游戏或 GUI 验收产生多张截图时，调用 analyze_image_set 按目录或 glob "
        "批量复核，不得把 DOM 断言或像素差分当作视觉分析。"
    ),
    log_level="ERROR",
)


@dataclass
class VisionBranchState:
    parent_message_id: object
    turn: int = 1
    updated_at: float = 0.0


@dataclass
class SharedVisionSessionState:
    chat_session_id: str
    anchor_message_id: object
    anchor_turn: int = 0
    branches: dict[str, VisionBranchState] = field(default_factory=dict)


SESSION_STATE_FILE = Path(
    os.environ.get(
        "DS_VISION_SESSION_FILE",
        str(Path.home() / "ds_vision" / ".shared_session.json"),
    )
)
SESSION_LOCK_FILE = SESSION_STATE_FILE.with_name(SESSION_STATE_FILE.name + ".lock")
SESSION_LOCK_TIMEOUT = float(os.environ.get("DS_VISION_LOCK_TIMEOUT", "180"))
MAX_PARALLEL_REQUESTS = max(1, int(os.environ.get("DS_VISION_MAX_PARALLEL", "2")))
_state_thread_lock = Lock()
_request_thread_slots = BoundedSemaphore(MAX_PARALLEL_REQUESTS)
_branch_thread_locks: dict[str, Lock] = {}
_branch_thread_locks_guard = Lock()


def _state_from_json(data):
    if not isinstance(data, dict):
        raise DeepSeekVisionError("共享视觉会话状态格式无效")
    chat_session_id = data.get("chat_session_id")
    if not isinstance(chat_session_id, str) or not chat_session_id.strip():
        raise DeepSeekVisionError("共享视觉会话缺少 chat_session_id")

    # Migrate the original single-parent format without creating a new web chat.
    anchor_message_id = data.get("anchor_message_id", data.get("parent_message_id"))
    anchor_turn = data.get("anchor_turn", data.get("turn", 0))
    if anchor_message_id is None:
        raise DeepSeekVisionError("共享视觉会话缺少 anchor_message_id")
    if not isinstance(anchor_turn, int) or anchor_turn < 0:
        raise DeepSeekVisionError("共享视觉会话 anchor_turn 无效")

    branches = {}
    raw_branches = data.get("branches", {})
    if not isinstance(raw_branches, dict):
        raise DeepSeekVisionError("共享视觉会话 branches 格式无效")
    for branch_id, raw_branch in raw_branches.items():
        if not isinstance(branch_id, str) or not isinstance(raw_branch, dict):
            continue
        parent_message_id = raw_branch.get("parent_message_id")
        turn = raw_branch.get("turn", 1)
        updated_at = raw_branch.get("updated_at", 0.0)
        if parent_message_id is None or not isinstance(turn, int) or turn < 1:
            continue
        branches[branch_id] = VisionBranchState(
            parent_message_id=parent_message_id,
            turn=turn,
            updated_at=float(updated_at or 0.0),
        )
    return SharedVisionSessionState(
        chat_session_id=chat_session_id.strip(),
        anchor_message_id=anchor_message_id,
        anchor_turn=anchor_turn,
        branches=branches,
    )


def _load_shared_state():
    try:
        with SESSION_STATE_FILE.open("r", encoding="utf-8") as f:
            return _state_from_json(json.load(f))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as exc:
        raise DeepSeekVisionError(
            f"无法读取共享视觉会话状态: {SESSION_STATE_FILE}"
        ) from exc


def _save_shared_state(state):
    SESSION_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{SESSION_STATE_FILE.name}.",
        suffix=".tmp",
        dir=str(SESSION_STATE_FILE.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            json.dump(
                {
                    "version": 2,
                    "chat_session_id": state.chat_session_id,
                    "anchor_message_id": state.anchor_message_id,
                    "anchor_turn": state.anchor_turn,
                    "branches": {
                        branch_id: {
                            "parent_message_id": branch.parent_message_id,
                            "turn": branch.turn,
                            "updated_at": branch.updated_at,
                        }
                        for branch_id, branch in state.branches.items()
                    },
                },
                f,
                ensure_ascii=True,
                indent=2,
            )
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_name, SESSION_STATE_FILE)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def _thread_lock_for(lock_path):
    key = str(lock_path)
    with _branch_thread_locks_guard:
        return _branch_thread_locks.setdefault(key, Lock())


@contextmanager
def _file_lock(lock_path, timeout=SESSION_LOCK_TIMEOUT):
    """Cross-process lock used only for metadata or one logical branch."""
    thread_lock = _state_thread_lock if lock_path == SESSION_LOCK_FILE else _thread_lock_for(lock_path)
    with thread_lock:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+b") as lock_file:
            lock_file.seek(0, os.SEEK_END)
            if lock_file.tell() == 0:
                lock_file.write(b"0")
                lock_file.flush()
            lock_file.seek(0)
            if os.name == "nt":
                import msvcrt

                deadline = time.monotonic() + timeout
                while True:
                    lock_file.seek(0)
                    try:
                        msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
                        break
                    except OSError as exc:
                        if exc.errno not in {
                            errno.EACCES,
                            errno.EAGAIN,
                            errno.EDEADLK,
                        }:
                            raise
                        if time.monotonic() >= deadline:
                            raise DeepSeekVisionError(
                                "共享视觉会话正被另一个请求占用，"
                                f"等待 {timeout:.0f} 秒后仍未释放"
                            ) from exc
                        time.sleep(0.1)
                try:
                    yield
                finally:
                    lock_file.seek(0)
                    msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


@contextmanager
def _shared_state_lock():
    """Short metadata lock; never wraps uploads, PoW, or model generation."""
    with _file_lock(SESSION_LOCK_FILE):
        yield


@contextmanager
def _branch_lock(branch_id):
    safe_id = "".join(ch for ch in branch_id if ch.isalnum() or ch in "-_")[:96]
    if not safe_id:
        raise DeepSeekVisionError("分支 session_id 无效")
    lock_path = SESSION_LOCK_FILE.with_name(f"{SESSION_LOCK_FILE.name}.{safe_id}")
    with _file_lock(lock_path):
        yield


def _try_lock_request_slot(lock_file):
    lock_file.seek(0, os.SEEK_END)
    if lock_file.tell() == 0:
        lock_file.write(b"0")
        lock_file.flush()
    lock_file.seek(0)
    if os.name == "nt":
        import msvcrt

        try:
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
            return True
        except OSError as exc:
            if exc.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                return False
            raise
    import fcntl

    try:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except BlockingIOError:
        return False


def _unlock_request_slot(lock_file):
    lock_file.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


@contextmanager
def _request_slot(timeout=SESSION_LOCK_TIMEOUT):
    """Bound DeepSeek web concurrency across all local MCP processes."""
    deadline = time.monotonic() + timeout
    with _request_thread_slots:
        while True:
            for index in range(MAX_PARALLEL_REQUESTS):
                slot_path = SESSION_LOCK_FILE.with_name(
                    f"{SESSION_LOCK_FILE.name}.request-{index}"
                )
                slot_path.parent.mkdir(parents=True, exist_ok=True)
                slot_file = slot_path.open("a+b")
                try:
                    if not _try_lock_request_slot(slot_file):
                        slot_file.close()
                        continue
                    try:
                        yield index
                    finally:
                        _unlock_request_slot(slot_file)
                        slot_file.close()
                    return
                except Exception:
                    if not slot_file.closed:
                        slot_file.close()
                    raise
            if time.monotonic() >= deadline:
                raise DeepSeekVisionError(
                    f"DeepSeek 并发池已满，等待 {timeout:.0f} 秒后仍无可用槽位"
                )
            time.sleep(0.1)


def _format_result(result, branch_id, turn, chat_session_id):
    answer = result.get("text", "").strip()
    thinking = result.get("thinking", "").strip()
    if not answer:
        raise DeepSeekVisionError("DeepSeek 返回了空回答")
    payload = {
        "answer": answer,
        "session_id": branch_id,
        "turn": turn,
        "web_chat_session_id": chat_session_id,
    }
    if thinking:
        payload["thinking"] = thinking
    if result.get("_thinking_fallback"):
        payload["thinking_fallback"] = True
    return payload


def _state_snapshot():
    with _shared_state_lock():
        return _load_shared_state()


def _run_existing_chat_turn(
    chat_session_id,
    parent_message_id,
    question,
    image_paths,
    thinking,
    search,
):
    """Run one branch turn without holding global metadata locks."""
    result = None
    message_id = None
    for attempt in range(3):
        with _request_slot():
            result = continue_vision_session(
                chat_session_id,
                parent_message_id,
                question,
                image_paths=image_paths,
                thinking=thinking,
                search=search,
            )
        message_id = result.get("message_id")
        if message_id is not None:
            break
        if attempt < 2:
            time.sleep(0.5 * (2 ** attempt))
    if message_id is None:
        raise DeepSeekVisionError("DeepSeek 连续 3 次未返回分支 message_id")

    if thinking and not result.get("text", "").strip():
        thinking_text = result.get("thinking", "").strip()
        fallback, fallback_message_id = _run_existing_chat_turn(
            chat_session_id,
            message_id,
            "请基于上一轮图片和问题，只输出最终答案，不要重复思考过程。\n原问题："
            + question.strip(),
            [],
            False,
            search,
        )
        if thinking_text:
            fallback["thinking"] = thinking_text
        fallback["_thinking_fallback"] = True
        return fallback, fallback_message_id
    return result, message_id


def _commit_branch(chat_session_id, branch_id, parent_message_id, turn):
    with _shared_state_lock():
        state = _load_shared_state()
        if state is None or state.chat_session_id != chat_session_id:
            raise DeepSeekVisionError("共享网页会话在请求期间发生变化")
        state.branches[branch_id] = VisionBranchState(
            parent_message_id=parent_message_id,
            turn=turn,
            updated_at=time.time(),
        )
        state.anchor_message_id = parent_message_id
        state.anchor_turn += 1
        _save_shared_state(state)


def _initialize_shared_session(branch_id, image_paths, question, thinking, search):
    with _request_slot():
        result = analyze_images(
            image_paths,
            question,
            thinking=thinking,
            search=search,
        )
    chat_session_id = result.get("chat_session_id")
    message_id = result.get("message_id")
    if not chat_session_id or message_id is None:
        raise DeepSeekVisionError("DeepSeek 未返回共享会话初始化元数据")

    if thinking and not result.get("text", "").strip():
        thinking_text = result.get("thinking", "").strip()
        result, message_id = _run_existing_chat_turn(
            chat_session_id,
            message_id,
            question,
            [],
            False,
            search,
        )
        if thinking_text:
            result["thinking"] = thinking_text
        result["_thinking_fallback"] = True

    state = SharedVisionSessionState(
        chat_session_id=chat_session_id,
        anchor_message_id=message_id,
        anchor_turn=1,
        branches={
            branch_id: VisionBranchState(
                parent_message_id=message_id,
                turn=1,
                updated_at=time.time(),
            )
        },
    )
    with _shared_state_lock():
        if _load_shared_state() is not None:
            raise DeepSeekVisionError("共享网页会话已由另一个并发请求初始化")
        _save_shared_state(state)
    return _format_result(result, branch_id, 1, chat_session_id)


def _start_analysis(image_paths, question, thinking, search):
    branch_id = uuid4().hex
    state = _state_snapshot()
    if state is None:
        with _branch_lock("initialize"):
            state = _state_snapshot()
            if state is None:
                return _initialize_shared_session(
                    branch_id,
                    image_paths,
                    question,
                    thinking,
                    search,
                )

    chat_session_id = state.chat_session_id
    parent_message_id = state.anchor_message_id
    result, message_id = _run_existing_chat_turn(
        chat_session_id,
        parent_message_id,
        question,
        image_paths,
        thinking,
        search,
    )
    _commit_branch(chat_session_id, branch_id, message_id, 1)
    return _format_result(result, branch_id, 1, chat_session_id)


def _resolve_branch(state, session_id):
    if session_id in {state.chat_session_id, "shared"}:
        branch_id = "shared"
        branch = state.branches.get(branch_id)
        if branch is None:
            branch = VisionBranchState(
                parent_message_id=state.anchor_message_id,
                turn=state.anchor_turn or 1,
                updated_at=0.0,
            )
        return branch_id, branch
    branch = state.branches.get(session_id)
    if branch is None:
        raise DeepSeekVisionError(
            "未知的视觉分支 session_id；请使用对应工具调用最新返回的 session_id"
        )
    return session_id, branch


def _continue_analysis(session_id, question, image_paths, thinking, search):
    state = _state_snapshot()
    if state is None:
        raise DeepSeekVisionError("共享视觉会话不存在；请先调用 analyze_image")
    branch_id, _ = _resolve_branch(state, session_id)

    # Only the same logical branch is serialized; other branches remain parallel.
    with _branch_lock(branch_id):
        state = _state_snapshot()
        if state is None:
            raise DeepSeekVisionError("共享视觉会话在续问期间丢失")
        branch_id, branch = _resolve_branch(state, branch_id)
        result, message_id = _run_existing_chat_turn(
            state.chat_session_id,
            branch.parent_message_id,
            question,
            image_paths,
            thinking,
            search,
        )
        next_turn = branch.turn + 1
        _commit_branch(state.chat_session_id, branch_id, message_id, next_turn)
        return _format_result(result, branch_id, next_turn, state.chat_session_id)


def _active_window_region():
    if os.name != "nt":
        raise DeepSeekVisionError("当前窗口截图目前仅支持 Windows")
    import ctypes
    from ctypes import wintypes

    hwnd = ctypes.windll.user32.GetForegroundWindow()
    if not hwnd:
        raise DeepSeekVisionError("无法获取当前前台窗口")
    rect = wintypes.RECT()
    if not ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        raise DeepSeekVisionError("无法读取当前前台窗口边界")
    region = {
        "left": rect.left,
        "top": rect.top,
        "width": rect.right - rect.left,
        "height": rect.bottom - rect.top,
    }
    if region["width"] <= 0 or region["height"] <= 0:
        raise DeepSeekVisionError("当前前台窗口尺寸无效")
    return region


def _capture_screenshot(target="screen", monitor=0):
    try:
        import mss
        from PIL import Image
    except ImportError as exc:
        raise DeepSeekVisionError("缺少自主截图依赖 mss 或 Pillow") from exc

    normalized_target = target.strip().lower()
    if normalized_target not in {"screen", "active_window"}:
        raise DeepSeekVisionError("target 必须是 screen 或 active_window")

    temp_file = tempfile.NamedTemporaryFile(
        prefix="ds_vision_screen_",
        suffix=".jpg",
        delete=False,
    )
    screenshot_path = Path(temp_file.name)
    temp_file.close()
    try:
        with mss.mss() as capture:
            if normalized_target == "active_window":
                region = _active_window_region()
            else:
                if not isinstance(monitor, int) or not 0 <= monitor < len(capture.monitors):
                    raise DeepSeekVisionError(
                        f"monitor 超出范围，可用编号为 0-{len(capture.monitors) - 1}"
                    )
                region = dict(capture.monitors[monitor])
            shot = capture.grab(region)
            image = Image.frombytes("RGB", shot.size, shot.rgb)
            source_width, source_height = image.size
            image.thumbnail((1600, 1600), Image.Resampling.LANCZOS)
            image.save(screenshot_path, "JPEG", quality=90, optimize=True)
        metadata = {
            "target": normalized_target,
            "monitor": monitor if normalized_target == "screen" else None,
            "left": region["left"],
            "top": region["top"],
            "source_width": source_width,
            "source_height": source_height,
            "width": image.width,
            "height": image.height,
        }
        return screenshot_path, metadata
    except Exception:
        screenshot_path.unlink(missing_ok=True)
        raise


@contextmanager
def _prepared_image_paths(image_paths):
    """Convert formats/sizes that DeepSeek web frequently marks CONTENT_EMPTY."""
    try:
        from PIL import Image
    except ImportError as exc:
        raise DeepSeekVisionError("缺少图片兼容预处理依赖 Pillow") from exc

    prepared = []
    temporary_paths = []
    try:
        for image_path in image_paths:
            source = resolve_image_path(image_path)
            with Image.open(source) as image:
                needs_conversion = (
                    source.suffix.lower() in {".webp", ".gif", ".bmp"}
                    or max(image.size) > 2000
                    or source.stat().st_size > 8 * 1024 * 1024
                )
                if not needs_conversion:
                    prepared.append(str(source))
                    continue

                temp_file = tempfile.NamedTemporaryFile(
                    prefix="ds_vision_upload_",
                    suffix=".jpg",
                    delete=False,
                )
                converted_path = Path(temp_file.name)
                temp_file.close()
                rgba = image.convert("RGBA")
                background = Image.new("RGB", rgba.size, "white")
                background.paste(rgba, mask=rgba.getchannel("A"))
                background.thumbnail((1600, 1600), Image.Resampling.LANCZOS)
                background.save(converted_path, "JPEG", quality=92, optimize=True)
                prepared.append(str(converted_path))
                temporary_paths.append(converted_path)
        yield prepared
    finally:
        for temporary_path in temporary_paths:
            temporary_path.unlink(missing_ok=True)


def _expand_image_patterns(image_patterns, base_directory=None, max_images=12):
    """Resolve screenshot globs deterministically for a single visual review."""
    if not image_patterns:
        raise DeepSeekVisionError("至少需要一个图片路径或 glob")
    if not isinstance(max_images, int) or not 1 <= max_images <= 20:
        raise DeepSeekVisionError("max_images 必须是 1-20 的整数")

    base = Path(base_directory or os.getcwd()).expanduser()
    try:
        base = base.resolve(strict=True)
    except OSError as exc:
        raise DeepSeekVisionError(f"基础目录不存在: {base}") from exc
    if not base.is_dir():
        raise DeepSeekVisionError(f"基础路径不是目录: {base}")

    matches = []
    seen = set()
    for raw_pattern in image_patterns:
        if not isinstance(raw_pattern, str) or not raw_pattern.strip():
            raise DeepSeekVisionError("图片 pattern 不能为空")
        expanded = Path(raw_pattern.strip()).expanduser()
        search_pattern = str(expanded if expanded.is_absolute() else base / expanded)
        for candidate_name in sorted(glob.glob(search_pattern, recursive=True)):
            candidate = Path(candidate_name)
            if not candidate.is_file() or candidate.suffix.lower() not in SUPPORTED_IMAGE_SUFFIXES:
                continue
            resolved = candidate.resolve()
            dedupe_key = os.path.normcase(str(resolved))
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            matches.append(str(resolved))

    if not matches:
        raise DeepSeekVisionError("没有找到匹配的图片")
    if len(matches) > max_images:
        raise DeepSeekVisionError(
            f"匹配到 {len(matches)} 张图片，超过 max_images={max_images}；请缩小 glob 范围"
        )
    return matches


@mcp.tool(
    name="analyze_image",
    description=(
        "使用 DeepSeek 网页视觉分析一张本地图片。适用于看图问答、OCR、截图诊断、"
        "图表/界面/代码截图理解。image_path 可为绝对路径或 OpenCode 当前目录下的相对路径。"
    ),
)
def analyze_image(
    image_path: str,
    question: str,
    thinking: bool = True,
    search: bool = False,
) -> dict[str, str | int]:
    with _prepared_image_paths([image_path]) as image_paths:
        return _start_analysis(
            image_paths,
            question,
            thinking,
            search,
        )


@mcp.tool(
    name="analyze_images",
    description=(
        "使用 DeepSeek 网页视觉联合分析多张本地图片。适用于前后对比、多页截图、"
        "多图归纳和跨图信息核对；按希望模型查看的顺序传入 image_paths。"
    ),
)
def analyze_multiple_images(
    image_paths: list[str],
    question: str,
    thinking: bool = True,
    search: bool = False,
) -> dict[str, str | int]:
    with _prepared_image_paths(image_paths) as prepared_paths:
        return _start_analysis(
            prepared_paths,
            question,
            thinking,
            search,
        )


@mcp.tool(
    name="analyze_image_set",
    description=(
        "按目录或 glob 自动收集并联合分析一组本地图片。用于前端/网页/游戏 GUI 验收、"
        "before/after 对比、测试产物复核和多视口截图审查。image_patterns 可传相对 glob，"
        "例如 ['tools/qa-wave48/before/*.png', 'tools/qa-wave48/after/*.png']；"
        "base_directory 默认为 OpenCode 当前目录。返回实际匹配的图片清单。"
    ),
)
def analyze_image_set(
    image_patterns: list[str],
    question: str,
    base_directory: str | None = None,
    max_images: int = 6,
    batch_size: int = 3,
    thinking: bool = False,
    search: bool = False,
) -> dict[str, object]:
    if not isinstance(batch_size, int) or not 1 <= batch_size <= 4:
        raise DeepSeekVisionError("batch_size 必须是 1-4 的整数")
    image_paths = _expand_image_patterns(
        image_patterns,
        base_directory=base_directory,
        max_images=max_images,
    )
    batches = [
        image_paths[index:index + batch_size]
        for index in range(0, len(image_paths), batch_size)
    ]

    def analyze_batch(item):
        index, batch_paths = item
        image_manifest = "\n".join(
            f"{position}. {Path(path).name}"
            for position, path in enumerate(batch_paths, 1)
        )
        batch_question = (
            f"{question.strip()}\n\n"
            f"这是第 {index}/{len(batches)} 批，共 {len(batch_paths)} 张图片。"
            "图片按上传顺序对应以下文件：\n"
            f"{image_manifest}\n"
            "请只根据本批图片给出可合并的视觉结论，并按序号和文件名引用。"
            "不要声称执行了像素级比较，也不要在仅凭视觉观察时使用‘100%完全相同’。"
        )
        with _prepared_image_paths(batch_paths) as prepared_paths:
            batch_result = _start_analysis(
                prepared_paths,
                batch_question,
                thinking,
                search,
            )
        return {
            "batch": index,
            "answer": batch_result["answer"],
            "session_id": batch_result["session_id"],
            "turn": batch_result["turn"],
            "images": batch_paths,
        }

    workers = min(MAX_PARALLEL_REQUESTS, len(batches))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        batch_results = list(pool.map(analyze_batch, enumerate(batches, 1)))

    state = _state_snapshot()
    return {
        "answer": "\n\n".join(
            f"第 {item['batch']} 批：{item['answer']}" for item in batch_results
        ),
        "batch_results": batch_results,
        "batch_count": len(batch_results),
        "matched_images": image_paths,
        "matched_count": len(image_paths),
        "web_chat_session_id": state.chat_session_id if state else "",
    }


@mcp.tool(
    name="continue_analysis",
    description=(
        "在 analyze_image/analyze_images 返回的同一个 DeepSeek 视觉会话中继续追问。"
        "只追问原图时省略 image_paths；需要补充新图片时传入新路径。必须复用上一轮返回的 session_id。"
    ),
)
def continue_analysis(
    session_id: str,
    question: str,
    image_paths: list[str] | None = None,
    thinking: bool = True,
    search: bool = False,
) -> dict[str, str | int]:
    with _prepared_image_paths(image_paths or []) as prepared_paths:
        return _continue_analysis(
            session_id,
            question,
            prepared_paths,
            thinking,
            search,
        )


@mcp.tool(
    name="capture_screen",
    description=(
        "自主截取当前 Windows 屏幕并立即交给 DeepSeek 网页视觉分析。用户说‘看看屏幕’、"
        "‘检查当前窗口/界面’或要求基于实时桌面状态判断时使用；无需用户先保存图片。"
        "target=screen 截取 monitor 指定的显示器（0 为整个虚拟桌面），"
        "target=active_window 截取当前前台窗口。临时截图上传后立即删除。"
    ),
)
def capture_screen(
    question: str = "详细分析当前屏幕内容，并回答用户正在处理的问题。",
    target: str = "screen",
    monitor: int = 0,
    thinking: bool = True,
    search: bool = False,
) -> dict[str, object]:
    screenshot_path, metadata = _capture_screenshot(target, monitor)
    try:
        result = _start_analysis(
            [str(screenshot_path)],
            question,
            thinking,
            search,
        )
        result["capture"] = metadata
        return result
    finally:
        screenshot_path.unlink(missing_ok=True)


@mcp.tool(
    name="end_analysis_session",
    description="兼容旧客户端；共享 DeepSeek 网页会话不会被结束或重置。",
)
def end_analysis_session(session_id: str) -> dict[str, str | bool]:
    state = _load_shared_state()
    return {
        "session_id": state.chat_session_id if state else session_id,
        "ended": False,
        "shared": True,
        "message": "共享 DeepSeek 网页会话保持不变。",
    }


@mcp.tool(
    name="vision_status",
    description="仅检查 DeepSeek 视觉 MCP 的本地令牌、WASM 和运行依赖是否就绪，不发起网络请求。",
)
def vision_status() -> dict[str, bool]:
    token_ready = False
    try:
        token_ready = bool(load_token())
    except DeepSeekVisionError:
        pass
    return {
        "ready": token_ready and Path(WASM_PATH).is_file(),
        "token_configured": token_ready,
        "wasm_available": Path(WASM_PATH).is_file(),
        "token_file_available": Path(TOKEN_FILE).is_file(),
    }


if __name__ == "__main__":
    mcp.run(transport="stdio")
