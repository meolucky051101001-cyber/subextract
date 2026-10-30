import json
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8")

from app.pipeline.post import to_srt
from app.pipeline.runner import RunParams, run_pipeline

video = ROOT / "data" / "test" / (sys.argv[1] if len(sys.argv) > 1 else "hardsub.mp4")
work = ROOT / "data" / "test" / "work"
work.mkdir(parents=True, exist_ok=True)


def progress(stage, value, message):
    print(f"[{stage:10s}] {value * 100:5.1f}%  {message}", flush=True)


started = time.time()
result = run_pipeline(
    video_path=video,
    work_dir=work,
    params=RunParams(
        roi=(0.0, 0.6, 1.0, 0.4),
        sample_fps=5.0,
        langs=["vi", "en", "zh"],
    ),
    progress=progress,
    cancel_event=threading.Event(),
    gpu=True,
)
print(json.dumps(result["stats"], ensure_ascii=False, indent=2))
print("----- SRT -----")
print(to_srt(result["cues"]))
(work / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
print(f"total {time.time() - started:.1f}s")
