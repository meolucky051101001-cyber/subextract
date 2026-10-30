from __future__ import annotations

import http.client
import json
import re
import socket
import threading
import time
from urllib import parse

from .cancellation import Cancelled
from .post import clean_text

API_HOST = "generativelanguage.googleapis.com"
API_ROOT = f"https://{API_HOST}/v1beta/models"
DEFAULT_MODEL = "gemini-3.5-flash-lite"
ALLOWED_MODELS = ("gemini-3.5-flash-lite", "gemini-3.8-flash", "gemini-4-argon")
# Timeout/batch riêng theo model: model chậm không được treo request quá lâu
# và không gom quá nhiều cue vào một lần gọi.
MODEL_LIMITS = {
    "gemini-3.5-flash-lite": {"timeout": 120.0, "batch_size": 24},
    "gemini-3.8-flash": {"timeout": 480.0, "batch_size": 8},
    "gemini-4-argon": {"timeout": 300.0, "batch_size": 12},
}
TARGET_LABELS = {"vi": "tiếng Việt", "en": "English", "zh": "中文（简体）"}

_HAN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
_VI_CHARS = set("ăâđêôơưàáảãạằắẳẵặầấẩẫậèéẻẽẹềếểễệìíỉĩịòóỏõọồốổỗộờớởỡợùúủũụừứửữựỳýỷỹỵ")
_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "text": {"type": "string"},
                },
                "required": ["id", "text"],
            },
        }
    },
    "required": ["items"],
}


class TranslateError(RuntimeError):
    pass


class TranslateAuthError(TranslateError):
    pass


class TranslateTransientError(TranslateError):
    pass


class TranslateQuotaError(TranslateTransientError):
    pass


def normalize_model(model: str | None) -> str:
    return model if model in ALLOWED_MODELS else DEFAULT_MODEL


def detect_lang(text: str) -> str:
    letters = [c for c in text.lower() if c.isalpha()]
    if not letters:
        return ""
    han = len(_HAN.findall(text))
    if han / len(letters) >= 0.25:
        return "zh"
    lowered = text.lower()
    if any(c in _VI_CHARS for c in lowered):
        return "vi"
    latin = sum(1 for c in letters if "a" <= c <= "z")
    if latin / len(letters) >= 0.6:
        return "en"
    return ""


def _norm_glossary(glossary) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for item in glossary or ():
        if isinstance(item, dict):
            src = str(item.get("src") or "").strip()
            dst = str(item.get("dst") or "").strip()
        elif isinstance(item, (list, tuple)) and len(item) >= 2:
            src = str(item[0]).strip()
            dst = str(item[1]).strip()
        else:
            continue
        if src and dst:
            pairs.append((src, dst))
    return pairs[:100]


def _glossary_text(pairs: list[tuple[str, str]]) -> str:
    if not pairs:
        return ""
    lines = "\n".join(f"- {src} → {dst}" for src, dst in pairs)
    return f"Bảng thuật ngữ bắt buộc (dịch nhất quán, đúng nghĩa):\n{lines}\n\n"


def _prompt(items: list[tuple[int, str]], target: str, previous: str, glossary_text: str = "") -> str:
    payload = [{"id": idx, "text": text} for idx, text in items]
    label = TARGET_LABELS[target]
    context = f"Ngữ cảnh cue ngay trước (chỉ tham khảo, không dịch lại):\n{previous}\n\n" if previous else ""
    return (
        f"Bạn là biên dịch phụ đề chuyên nghiệp. Dịch các cue sau sang {label}.\n"
        "Yêu cầu:\n"
        "- Dịch tự nhiên như phụ đề phim, đúng nghĩa và sắc thái, xưng hô phù hợp ngữ cảnh.\n"
        "- Giữ tên riêng, thương hiệu, số liệu; không thêm chú thích hay nội dung mới.\n"
        "- Giữ xuống dòng nếu cue gốc có xuống dòng.\n"
        f"- Cue nào đã ở {label} thì giữ nguyên.\n"
        "- Trả về đúng JSON schema, đủ mọi id, không thêm chữ ngoài JSON.\n\n"
        f"{glossary_text}"
        f"{context}"
        "Các cue cần dịch:\n"
        + json.dumps(payload, ensure_ascii=False)
    )


def _retry_prompt(index: int, text: str, target: str, glossary_text: str = "") -> str:
    label = TARGET_LABELS[target]
    payload = json.dumps([{"id": index, "text": text}], ensure_ascii=False)
    return (
        f"Dịch cue sau sang {label}. Câu gốc có thể là tiếng Anh hoặc tiếng Trung.\n"
        f"Bắt buộc dịch sang {label}; tuyệt đối không giữ nguyên câu gốc.\n"
        "Trả về đúng JSON schema, không thêm chữ ngoài JSON.\n\n"
        f"{glossary_text}"
        f"{payload}"
    )


def _looks_untranslated(source: str, value: str, target: str) -> bool:
    src = source.strip()
    val = value.strip()
    if not val or val == src:
        return True
    src_lang = detect_lang(src)
    val_lang = detect_lang(val)
    return bool(src_lang and val_lang and src_lang != target and val_lang == src_lang)


def _hard_close(conn: http.client.HTTPConnection) -> None:
    sock = getattr(conn, "sock", None)
    if sock is not None:
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
    try:
        conn.close()
    except Exception:
        pass


def _generate(api_key: str, model: str, prompt: str, timeout: float | None = None, cancel_event=None) -> dict:
    if timeout is None:
        timeout = float(MODEL_LIMITS.get(model, {}).get("timeout", 300.0))
    body = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {
            "maxOutputTokens": 8192,
            "responseMimeType": "application/json",
            "responseSchema": _SCHEMA,
        },
    }
    path = f"/v1beta/models/{parse.quote(model, safe='')}:generateContent"
    conn = http.client.HTTPSConnection(API_HOST, timeout=timeout)
    finished = threading.Event()
    result: dict = {}

    def worker() -> None:
        # Chạy request trong luồng riêng để hủy job không phụ thuộc vào việc
        # close() từ luồng khác có đánh thức được recv bị chặn hay không
        # (trên Windows thì không đáng tin cậy). Luồng này là daemon và bị
        # chặn tối đa `timeout` giây bởi socket timeout.
        try:
            conn.request(
                "POST",
                path,
                body=json.dumps(body).encode("utf-8"),
                headers={"Content-Type": "application/json", "x-goog-api-key": api_key},
            )
            resp = conn.getresponse()
            result["status"] = resp.status
            result["raw"] = resp.read()
        except Exception as exc:  # noqa: BLE001 - mọi lỗi đều phân loại bên dưới
            result["exc"] = exc
        finally:
            finished.set()

    threading.Thread(target=worker, daemon=True).start()
    while not finished.wait(0.2):
        if cancel_event is not None and cancel_event.is_set():
            _hard_close(conn)
            raise Cancelled() from None

    if "exc" in result:
        exc = result["exc"]
        if cancel_event is not None and cancel_event.is_set():
            raise Cancelled() from None
        if isinstance(exc, (OSError, http.client.HTTPException)):
            raise TranslateTransientError(f"Không kết nối được Gemini: {exc}") from None
        raise TranslateTransientError(f"Lỗi không xác định khi gọi Gemini: {exc}") from None

    try:
        payload = json.loads(result["raw"].decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        if result.get("status") == 200:
            raise TranslateError("Gemini trả về dữ liệu không hợp lệ") from None
        payload = {}
    return _handle_response(result.get("status"), payload, api_key)


def _handle_response(status: int, payload: dict, api_key: str) -> dict:
    if status == 200:
        if payload:
            return payload
        raise TranslateError("Gemini trả về dữ liệu rỗng") from None
    err = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(err, list) and err:
        err = err[0]
    message = str((err or {}).get("message") or f"HTTP {status}")
    message = message.replace(api_key, "***")[:300]
    lowered = message.lower()
    if status in (401, 403) or "api key" in lowered or "api_key" in lowered or "permission" in lowered:
        raise TranslateAuthError(message) from None
    if status == 429 or "quota" in lowered or "resource_exhausted" in lowered:
        raise TranslateQuotaError(f"Gemini hết hạn mức (429): {message}") from None
    if status in (408, 409, 500, 502, 503, 504):
        raise TranslateTransientError(f"Gemini lỗi {status}: {message}") from None
    raise TranslateError(f"Gemini lỗi {status}: {message}") from None


def _extract_text(payload: dict) -> str:
    feedback = payload.get("promptFeedback") or {}
    if feedback.get("blockReason"):
        raise TranslateError(f"Gemini chặn nội dung ({feedback.get('blockReason')})")
    candidates = payload.get("candidates") or []
    if not candidates:
        raise TranslateError("Gemini không trả về kết quả")
    parts = (candidates[0].get("content") or {}).get("parts") or []
    text = "".join(str(part.get("text") or "") for part in parts).strip()
    if not text:
        reason = candidates[0].get("finishReason") or "EMPTY"
        raise TranslateError(f"Gemini trả về rỗng ({reason})")
    return text


def _parse_items(text: str) -> dict[int, str]:
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        match = re.search(r"\{[\s\S]*\}", raw)
        if not match:
            raise TranslateError("Gemini trả về không phải JSON") from None
        try:
            data = json.loads(match.group(0))
        except json.JSONDecodeError:
            raise TranslateError("Không parse được JSON của Gemini") from None
    items = data.get("items") if isinstance(data, dict) else data
    out: dict[int, str] = {}
    for item in items or []:
        if not isinstance(item, dict):
            continue
        try:
            idx = int(item.get("id"))
        except (TypeError, ValueError):
            continue
        out[idx] = clean_text(str(item.get("text") or ""))
    return out


def _sleep(seconds: float, cancel_event) -> None:
    end = time.time() + seconds
    while time.time() < end:
        if cancel_event is not None and cancel_event.is_set():
            raise Cancelled()
        time.sleep(0.25)


def _translate_batch(api_keys, model, items, target, previous, cancel_event, strict=False, glossary_text=""):
    if strict and len(items) == 1:
        prompt = _retry_prompt(items[0][0], items[0][1], target, glossary_text)
    else:
        prompt = _prompt(items, target, previous, glossary_text)
    delay = 2.0
    attempt = 0
    key_index = 0
    while True:
        try:
            payload = _generate(api_keys[key_index], model, prompt, cancel_event=cancel_event)
            return _parse_items(_extract_text(payload))
        except (TranslateQuotaError, TranslateAuthError):
            # Hết quota hoặc key bị từ chối: xoay sang key kế tiếp nếu còn.
            if key_index + 1 >= len(api_keys):
                raise
            key_index += 1
        except TranslateTransientError:
            attempt += 1
            if attempt >= 4:
                raise
            _sleep(delay, cancel_event)
            delay *= 2.5


def translate_cues(
    cues: list[dict],
    target: str,
    api_key,
    model: str = DEFAULT_MODEL,
    cancel_event=None,
    progress=None,
    batch_size: int = 24,
    glossary=None,
) -> dict:
    started = time.time()
    if target not in TARGET_LABELS:
        return {"translated": 0, "total": 0, "chars": 0, "seconds": 0.0, "error": f"Ngôn ngữ đích không hỗ trợ: {target}", "untranslated": []}
    if isinstance(api_key, str):
        keys = [api_key.strip()] if api_key.strip() else []
    else:
        keys = []
        for key in api_key or ():
            key = str(key).strip()
            if key and key not in keys:
                keys.append(key)
    if not keys:
        return {"translated": 0, "total": 0, "chars": 0, "seconds": 0.0, "error": "Chưa cấu hình Gemini API key.", "untranslated": []}

    model = normalize_model(model)
    batch_size = min(int(batch_size), int(MODEL_LIMITS.get(model, {}).get("batch_size", 24)))
    glossary_text = _glossary_text(_norm_glossary(glossary))
    pending: list[tuple[int, str]] = []
    for index, cue in enumerate(cues):
        text = str(cue.get("text") or "").strip()
        if not text:
            continue
        source = detect_lang(text)
        if not source or source == target:
            continue
        pending.append((index, text))

    total = len(pending)
    chars = sum(len(text) for _index, text in pending)
    if not total:
        return {"translated": 0, "total": 0, "chars": 0, "seconds": 0.0, "error": "", "untranslated": []}

    translated = 0
    error_message = ""
    previous = ""
    stop_all = False
    for start in range(0, total, batch_size):
        if cancel_event is not None and cancel_event.is_set():
            raise Cancelled()
        chunk = pending[start:start + batch_size]
        retry: list[tuple[int, str]] = []
        try:
            mapping = _translate_batch(keys, model, chunk, target, previous, cancel_event, glossary_text=glossary_text)
        except Cancelled:
            raise
        except (TranslateQuotaError, TranslateAuthError) as exc:
            # Mọi key đều hết quota / bị từ chối: dừng thay vì thử các lô còn lại.
            error_message = str(exc) or "Gemini API key không dùng được."
            break
        except TranslateError as exc:
            error_message = str(exc) or "Dịch thất bại."
            continue
        for index, text in chunk:
            value = mapping.get(index, "").strip()
            if value and not _looks_untranslated(text, value, target):
                cues[index]["trans"] = value
                translated += 1
            else:
                retry.append((index, text))
        for index, text in retry:
            if cancel_event is not None and cancel_event.is_set():
                raise Cancelled()
            try:
                single = _translate_batch(
                    keys, model, [(index, text)], target, "", cancel_event,
                    strict=True, glossary_text=glossary_text,
                )
            except Cancelled:
                raise
            except (TranslateQuotaError, TranslateAuthError) as exc:
                error_message = str(exc) or "Gemini API key không dùng được."
                stop_all = True
                break
            except TranslateError:
                continue
            value = single.get(index, "").strip()
            if value and not _looks_untranslated(text, value, target):
                cues[index]["trans"] = value
                translated += 1
        if stop_all:
            break
        if chunk:
            last_index = chunk[-1][0]
            last_trans = str(cues[last_index].get("trans") or "")
            if last_trans:
                previous = json.dumps(
                    {"text": chunk[-1][1], "translation": last_trans}, ensure_ascii=False
                )
        if progress:
            progress(min(total, start + len(chunk)), total)

    untranslated = [
        index for index, _text in pending
        if not str(cues[index].get("trans") or "").strip()
    ]
    return {
        "translated": translated,
        "total": total,
        "chars": chars,
        "seconds": round(time.time() - started, 1),
        "error": error_message,
        "untranslated": untranslated,
    }
