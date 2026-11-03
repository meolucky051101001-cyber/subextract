"""Test offline logic tự chuyển model khi model đang chọn quá tải / hết quota.

Không gọi Gemini thật: _translate_batch được thay bằng hàm giả để dựng đúng các
kịch bản lỗi. Kiểm tra translate_cues đổi model, dừng đúng lúc khi key bị từ chối
và báo cáo model thực sự dùng.

Chạy: .venv/Scripts/python.exe data/test/translate_fallback_test.py
"""
import sys
from pathlib import Path

for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from app.pipeline import translate as T  # noqa: E402

FIRST = "gemini-3.5-flash-lite"
SECOND = "gemini-3.8-flash"
THIRD = "gemini-4-argon"
KEYS = ["dummy-key"]
CUES = [{"text": "hello world"}, {"text": "good morning everyone"}]


def fake(mapping_for):
    """mapping_for(model, items) -> dict | Exception cần raise."""

    def _batch(api_keys, model, items, target, previous, cancel_event, strict=False, glossary_text=""):
        result = mapping_for(model, items)
        if isinstance(result, Exception):
            raise result
        return result

    return _batch


def translated(cues):
    return [c.get("trans") for c in cues]


def vi(n: int) -> str:
    """Bản dịch giả có dấu tiếng Việt rõ ràng để qua được _looks_untranslated."""
    return f"bản dịch thứ {n}"


def scenario_switch_after_transient():
    """Model đầu quá tải ở mọi lô -> phải chuyển sang model thứ hai và dịch đủ."""
    calls = []

    def mapping_for(model, items):
        calls.append(model)
        if model == FIRST:
            return T.TranslateTransientError("Gemini lỗi 503: high demand")
        return {index: vi(n) for n, (index, _text) in enumerate(items, 1)}

    T._translate_batch = fake(mapping_for)
    cues = [dict(c) for c in CUES]
    result = T.translate_cues(cues, target="vi", api_key=KEYS, model=FIRST, batch_size=24)
    ok = (
        result["translated"] == 2
        and result["model"] == SECOND
        and result["fallback_from"] == [FIRST]
        and not result["error"]
        and all(t for t in translated(cues))
    )
    print(f"[switch-after-transient] model={result['model']} from={result['fallback_from']} "
          f"translated={result['translated']} calls={len(calls)}")
    return ok


def scenario_quota_switch():
    """Hết quota trên model đầu -> chuyển model, không dừng job."""
    def mapping_for(model, items):
        if model == FIRST:
            return T.TranslateQuotaError("Gemini hết hạn mức (429)")
        return {index: vi(n) for n, (index, _text) in enumerate(items, 1)}

    T._translate_batch = fake(mapping_for)
    cues = [dict(c) for c in CUES]
    result = T.translate_cues(cues, target="vi", api_key=KEYS, model=FIRST)
    ok = result["translated"] == 2 and result["model"] == SECOND and result["fallback_from"] == [FIRST]
    print(f"[quota-switch] model={result['model']} translated={result['translated']}")
    return ok


def scenario_auth_stops():
    """Key bị từ chối thì model nào cũng vô nghĩa -> phải dừng ngay, không đổi model."""
    calls = []

    def mapping_for(model, items):
        calls.append(model)
        return T.TranslateAuthError("API key not valid. Please pass a valid API key.")

    T._translate_batch = fake(mapping_for)
    cues = [dict(c) for c in CUES]
    result = T.translate_cues(cues, target="vi", api_key=KEYS, model=FIRST)
    ok = (
        result["translated"] == 0
        and not result["fallback_from"]
        and len(calls) == 1
        and "API key" in result["error"]
    )
    print(f"[auth-stops] calls={calls} error='{result['error'][:48]}'")
    return ok


def scenario_all_models_dead():
    """Cả ba model đều lỗi -> báo lỗi và giữ nguyên cue chưa dịch."""
    def mapping_for(model, items):
        return T.TranslateTransientError(f"Gemini lỗi 503: {model}")

    T._translate_batch = fake(mapping_for)
    cues = [dict(c) for c in CUES]
    result = T.translate_cues(cues, target="vi", api_key=KEYS, model=FIRST)
    ok = (
        result["translated"] == 0
        and bool(result["error"])
        and result["fallback_from"] == [FIRST, SECOND]
        and not any(translated(cues))
    )
    print(f"[all-dead] from={result['fallback_from']} error='{result['error'][:48]}'")
    return ok


def scenario_no_switch_when_healthy():
    """Model đầu chạy tốt -> không đổi model, không ghi fallback."""
    def mapping_for(model, items):
        return {index: vi(n) for n, (index, _text) in enumerate(items, 1)}

    T._translate_batch = fake(mapping_for)
    cues = [dict(c) for c in CUES]
    result = T.translate_cues(cues, target="vi", api_key=KEYS, model=FIRST)
    ok = result["model"] == FIRST and not result["fallback_from"] and result["translated"] == 2
    print(f"[healthy] model={result['model']} from={result['fallback_from']}")
    return ok


def scenario_batch_size_follows_model():
    """Sau khi chuyển sang model có giới hạn lô nhỏ, các lô sau phải tuân theo."""
    sizes = []

    def mapping_for(model, items):
        sizes.append(len(items))
        if model == FIRST:
            return T.TranslateTransientError("Gemini lỗi 503")
        return {index: vi(n) for n, (index, _text) in enumerate(items, 1)}

    T._translate_batch = fake(mapping_for)
    cues = [{"text": f"sentence number {i}"} for i in range(20)]
    result = T.translate_cues(cues, target="vi", api_key=KEYS, model=FIRST, batch_size=24)
    limit = T.MODEL_LIMITS[SECOND]["batch_size"]
    ok = all(size <= limit for size in sizes[1:]) and result["translated"] == 20
    print(f"[batch-size] sizes={sizes} limit={limit} translated={result['translated']}")
    return ok


if __name__ == "__main__":
    original = T._translate_batch
    scenarios = {
        "switch_after_transient": scenario_switch_after_transient,
        "quota_switch": scenario_quota_switch,
        "auth_stops": scenario_auth_stops,
        "all_models_dead": scenario_all_models_dead,
        "no_switch_when_healthy": scenario_no_switch_when_healthy,
        "batch_size_follows_model": scenario_batch_size_follows_model,
    }
    results = {}
    try:
        for name, fn in scenarios.items():
            results[name] = fn()
    finally:
        T._translate_batch = original
    failed = [name for name, ok in results.items() if not ok]
    print("RESULTS:", results)
    if failed:
        print("FAILED:", ", ".join(failed))
    sys.exit(0 if not failed else 1)
