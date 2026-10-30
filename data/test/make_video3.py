import subprocess
from pathlib import Path
import numpy as np
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "data" / "test" / "hardsub_bright.mp4"
W, H, FPS, DUR = 1280, 720, 25, 8.0
latin = str(ROOT / "data" / "test" / "arialbd.ttf")
font_y = ImageFont.truetype(latin, 38)
font_w = ImageFont.truetype(latin, 32)

def draw_text(draw, xy, text, f, fill):
    draw.text(xy, text, font=f, fill=fill, stroke_width=2, stroke_fill=(0, 0, 0))

events = [
    (1.0, 3.0, "Đây là phụ đề màu vàng", font_y, (255, 220, 40)),
    (4.0, 6.0, "Bright background test 123", font_w, (255, 255, 255)),
]
cmd = ["ffmpeg", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", str(FPS),
       "-i", "-", "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p", str(OUT)]
proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
for i in range(int(FPS * DUR)):
    t = i / FPS
    yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)
    base = 150 + 60 * np.sin(xx * 0.004 + t * 0.8) + 25 * np.sin(yy * 0.01 - t * 1.3)
    frame = np.dstack([np.clip(base + 25, 0, 255), np.clip(base + 10, 0, 255), np.clip(base - 30, 0, 255)]).astype(np.uint8)
    img = Image.fromarray(frame)
    draw = ImageDraw.Draw(img)
    for start, end, text, f, fill in events:
        if start <= t < end:
            bbox = draw.textbbox((0, 0), text, font=f)
            tw = bbox[2] - bbox[0]
            draw_text(draw, ((W - tw) // 2, 560), text, f, fill)
    proc.stdin.write(np.asarray(img).tobytes())
proc.stdin.close()
proc.wait()
print("wrote", OUT, OUT.stat().st_size)
