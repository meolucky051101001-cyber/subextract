import subprocess, sys
from pathlib import Path
import numpy as np
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "data" / "test" / "hardsub_logo.mp4"
W, H, FPS, DUR = 1280, 720, 25, 10.0
FONT_LATIN = str(ROOT / "data" / "test" / "arialbd.ttf")
FONT_CJK = str(ROOT / "data" / "test" / "msyhbd.ttc")
font = ImageFont.truetype(FONT_LATIN, 36)
font_cjk = ImageFont.truetype(FONT_CJK, 36)
font_logo = ImageFont.truetype(FONT_LATIN, 34)

def draw_text(draw, xy, text, f, fill=(255, 255, 255)):
    draw.text(xy, text, font=f, fill=fill, stroke_width=3, stroke_fill=(0, 0, 0))

events = [
    (1.0, 3.0, [("Đây là dòng thứ nhất", font), ("và dòng thứ hai", font)]),
    (4.0, 6.0, [("This line has watermark", font)]),
    (7.0, 9.0, [("字幕测试：你好世界", font_cjk)]),
]

cmd = ["ffmpeg", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}",
       "-r", str(FPS), "-i", "-", "-c:v", "libx264", "-preset", "veryfast",
       "-pix_fmt", "yuv420p", str(OUT)]
proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

total = int(FPS * DUR)
xs = np.arange(W, dtype=np.float32)
for i in range(total):
    t = i / FPS
    shift = int(80 * np.sin(t * 1.7))
    row = ((xs + shift) * 0.22)
    frame = np.zeros((H, W, 3), np.uint8)
    for c, (base, amp, phase) in enumerate([(60, 90, 0), (110, 80, 2.1), (150, 70, 4.2)]):
        frame[:, :, c] = np.clip(base + amp * np.sin(row * 0.05 + phase + t), 0, 255).astype(np.uint8)
    img = Image.fromarray(frame)
    draw = ImageDraw.Draw(img)
    draw_text(draw, (1000, 640), "CHANNEL HD", font_logo)
    for start, end, lines in events:
        if start <= t < end:
            y = 500
            for text, f in lines:
                bbox = draw.textbbox((0, 0), text, font=f)
                tw = bbox[2] - bbox[0]
                draw_text(draw, ((W - tw) // 2, y), text, f)
                y += 48
    proc.stdin.write(np.asarray(img).tobytes())
proc.stdin.close()
proc.wait()
print("wrote", OUT, OUT.stat().st_size, "bytes")
