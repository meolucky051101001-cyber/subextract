import json
import sys
import time
from pathlib import Path

ROOT = Path(r"C:\Users\admin\Documents\Qoder\2026-10-29\6b462a65")
sys.path.insert(0, str(ROOT))

from app.pipeline.dub import run_dubbing

job_dir = ROOT / "data" / "jobs" / "6ca01c10b3db"
job = json.loads((job_dir / "job.json").read_text(encoding="utf-8"))
cues = job["cues"]
work = job_dir
t0 = time.time()
result = run_dubbing(
    video_path=job_dir / "source.mp4",
    cues=cues,
    work_dir=work,
    duration=float(job["info"]["duration"]),
    output_dir=r"D:\banve",
    source_name=job["filename"],
    progress=lambda v, m: print(f"  {v*100:.0f}% {m}", flush=True),
)
print(json.dumps(result, ensure_ascii=False, indent=1))
print(f"elapsed={time.time()-t0:.1f}s")