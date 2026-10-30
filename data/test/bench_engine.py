import sys, time
sys.path.insert(0, r"C:\Users\admin\Documents\Qoder\2026-10-29\6b462a65")
t0 = time.time()
from app.pipeline.ocr import get_engine
print(f"import: {time.time()-t0:.1f}s", flush=True)
t1 = time.time()
eng = get_engine(["vi", "en", "zh"], True)
print(f"get_engine: {time.time()-t1:.1f}s readers={list(eng.readers)}", flush=True)
import numpy as np
for name, reader in eng.readers.items():
    t2 = time.time()
    img = (np.ones((60, 300, 3), dtype=np.uint8) * 255)
    out = eng.read_line(img)
    print(f"{name} read: {time.time()-t2:.2f}s -> {out[:2]}", flush=True)
print("OK", flush=True)
