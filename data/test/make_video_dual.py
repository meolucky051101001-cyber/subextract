"""Tạo video tổng hợp có phụ đề CẢ TRÊN CẢ DƯỜNG để test đa vùng ROI.

Chạy: .venv/Scripts/python.exe data/test/make_video_dual.py
Kết quả: data/test/hardsub_dual.mp4 (~21s, 8 cặp câu trên+dưới)
"""
import subprocess
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "data" / "test" / "hardsub_dual.mp4"
W, H, FPS = 1280, 720, 25

TOP = [
    "Welcome back to the painting channel",
    "Today we paint a night city scene",
    "First we sketch the basic shapes",
    "Dark blue works best for the sky",
    "Add small windows one by one",
    "The brush must stay almost dry",
    "Highlights make the city glow",
    "Thanks for watching this tutorial",
]
BOTTOM = [
    "Chào mừng các bạn quay lại kênh vẽ",
    "Hôm nay chúng ta vẽ phố đêm bằng súng phun",
    "Đầu tiên là phác thảo các khối chính",
    "Xanh đậm là màu hợp nhất cho bầu trời",
    "Thêm từng ô cửa sổ nhỏ một cách kiện toàn",
    "Cọ phải gần như khô để màu không loang",
    "Điểm sáng làm cả thành phố phát sáng",
    "Cảm ơn các bạn đã xem hết bài hướng dẫn",
]

font = ImageFont.truetype(str(ROOT / "data" / "test" / "arialbd.ttf"), 30)
CUE, GAP = 2.2, 0.4

events = []
t = 0.6
for i in range(8):
    events.append((t, t + CUE, "top", TOP[i]))
    events.append((t, t + CUE, "bottom", BOTTOM[i]))
    t += CUE + GAP
DUR = events[-1][1] + 0.6

yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)
base = np.dstack([
    np.clip(52 + 16 * np.sin(xx * 0.004), 0, 255),
    np.clip(48 + 14 * np.sin(yy * 0.005), 0, 255),
    np.clip(60 + 12 * np.sin((xx + yy) * 0.003), 0, 255),
]).astype(np.uint8)

cmd = ["ffmpeg", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", str(FPS),
       "-i", "-", "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p", str(OUT)]
proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
for i in range(int(FPS * DUR)):
    t = i / FPS
    img = Image.fromarray(base)
    draw = ImageDraw.Draw(img)
    for start, end, pos, text in events:
        if start <= t < end:
            bbox = draw.textbbox((0, 0), text, font=font)
            tw = bbox[2] - bbox[0]
            y = 46 if pos == "top" else 600
            draw.text(((W - tw) // 2, y), text, font=font, fill=(255, 255, 255),
                      stroke_width=2, stroke_fill=(0, 0, 0))
    proc.stdin.write(np.asarray(img).tobytes())
proc.stdin.close()
proc.wait()
print("wrote", OUT, f"dur={DUR:.1f}s cues={len(events)}")
