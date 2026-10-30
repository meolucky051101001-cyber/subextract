import json
import sys
import time
from pathlib import Path

from capcut_tts_api import CapCutClient

OUT = Path(r"C:\Users\admin\Documents\Qoder\2026-10-29\6b462a65\data\test\tts_out")
OUT.mkdir(parents=True, exist_ok=True)
texts = ["Xin chào, đây là giọng Chí Mai.", "Hôm nay chúng ta cùng mở hộp sản phẩm mới."]
client = CapCutClient()
for i, text in enumerate(texts):
    t0 = time.time()
    res = client.create_tts_task(texts=text, voice="BV562_streaming")
    task = res["data"]["tasks"][0]
    print("create", i, f"{time.time()-t0:.1f}s", task.get("id"), flush=True)
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        time.sleep(1.0)
        q = client.query_tts_task(task["id"], task["token"])
        st = q["data"]["tasks"][0]["status"]
        if st in ("success", "succeed"):
            payload = json.loads(q["data"]["tasks"][0]["payload"])
            url = payload["audio_subtitles"][0]["speech_url"]
            r = client.session.get(url, timeout=30)
            r.raise_for_status()
            path = OUT / f"probe_{i}.mp3"
            path.write_bytes(r.content)
            print("saved", path, len(r.content), "bytes", f"{time.time()-t0:.1f}s", flush=True)
            break
        if st == "failed":
            raise RuntimeError("failed")
    else:
        raise TimeoutError("timeout")
print("DONE")
