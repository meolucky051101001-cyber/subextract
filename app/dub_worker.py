from __future__ import annotations

import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from capcut_tts_api import CapCutClient

_local = threading.local()


def _client() -> CapCutClient:
    client = getattr(_local, "client", None)
    if client is None:
        client = CapCutClient()
        _local.client = client
    return client


def _synthesize(text: str, voice: str, out_path: Path, attempts: int = 2) -> None:
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            client = _client()
            res = client.create_tts_task(texts=text, voice=voice)
            task = res["data"]["tasks"][0]
            deadline = time.monotonic() + 90.0
            while time.monotonic() < deadline:
                time.sleep(0.5)
                query = client.query_tts_task(task["id"], task["token"])
                item = query["data"]["tasks"][0]
                status = item["status"]
                if status in ("success", "succeed"):
                    payload = json.loads(item["payload"])
                    url = payload["audio_subtitles"][0]["speech_url"]
                    resp = client.session.get(url, timeout=30)
                    resp.raise_for_status()
                    if len(resp.content) < 128:
                        raise RuntimeError("CapCut trả audio rỗng")
                    out_path.write_bytes(resp.content)
                    return
                if status == "failed":
                    raise RuntimeError("CapCut báo tác vụ TTS thất bại")
            raise TimeoutError("CapCut TTS quá 90 giây")
        except Exception as exc:  # noqa: BLE001 - worker báo lỗi từng câu
            last_error = exc
            if out_path.exists():
                try:
                    out_path.unlink()
                except OSError:
                    pass
            if attempt + 1 < attempts:
                time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(str(last_error) or "TTS thất bại")


def main() -> int:
    request_path = Path(sys.argv[1])
    result_path = Path(sys.argv[2])
    req = json.loads(request_path.read_text(encoding="utf-8"))
    voice = str(req.get("voice") or "BV562_streaming")
    out_dir = Path(req["out_dir"])
    out_dir.mkdir(parents=True, exist_ok=True)
    items = req.get("items") or []
    workers = max(1, min(int(req.get("workers") or 4), 6))

    results: list[dict] = []
    errors: list[str] = []

    def work(item: dict) -> dict:
        cue_id = int(item["id"])
        text = str(item.get("text") or "").strip()
        if not text:
            return {"id": cue_id, "error": "empty"}
        path = out_dir / f"cue_{cue_id:04d}.mp3"
        _synthesize(text, voice, path)
        return {"id": cue_id, "file": str(path), "bytes": path.stat().st_size}

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(work, item): item for item in items}
        for future in as_completed(futures):
            item = futures[future]
            try:
                results.append(future.result())
            except Exception as exc:  # noqa: BLE001
                results.append({"id": int(item["id"]), "error": str(exc)[:300]})
                errors.append(f"cue {item['id']}: {exc}")

    result_path.write_text(
        json.dumps({"ok": len(errors) < len(items), "results": results, "errors": errors[:20]}, ensure_ascii=False),
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
