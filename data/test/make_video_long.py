"""Tạo video hardsub dài để nghiệm thu: ~2,5 phút, 55 cue Việt/Anh xen kẽ.

Chạy: .venv/Scripts/python.exe data/test/make_video_long.py
Kết quả: data/test/hardsub_long.mp4
"""
import subprocess
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "data" / "test" / "hardsub_long.mp4"
W, H, FPS = 1280, 720, 25
CUE_LEN, GAP = 2.4, 0.3

VI = [
    "Chào mừng các bạn đã quay trở lại với kênh của chúng tôi",
    "Hôm nay chúng ta sẽ cùng nhau khám phá một địa điểm mới",
    "Nơi này nổi tiếng với những ngọn núi xanh mát quanh năm",
    "Thời tiết buổi sáng ở đây khá dễ chịu và trong lành",
    "Chúng tôi đã chuẩn bị rất kỹ lưỡng cho chuyến đi lần này",
    "Đầu tiên là việc kiểm tra lại toàn bộ thiết bị quay phim",
    "Pin đã được sạc đầy và thẻ nhớ còn trống gần như hoàn toàn",
    "Bây giờ chúng ta sẽ bắt đầu hành trình ngay lập tức",
    "Đường đi phía trước khá quanh co nên mọi người cần cẩn thận",
    "Cảnh vật hai bên đường thực sự rất đẹp và hùng vĩ",
    "Tôi đã từng đến đây vào mùa đông năm ngoái",
    "Lúc đó toàn bộ khu vực này được phủ một lớp sương mỏng",
    "Nhưng mùa hè lại mang một vẻ đẹp hoàn toàn khác biệt",
    "Những cánh đồng lúa chín vàng trải dài tận chân trời",
    "Người dân địa phương ở đây rất thân thiện và mến khách",
    "Họ sống chủ yếu bằng nghề nông và đánh bắt cá",
    "Bữa trưa hôm nay sẽ là món đặc sản của vùng này",
    "Mùi hương thơm lừng lan tỏa khắp cả không gian bếp",
    "Sau bữa trưa chúng ta sẽ tiếp tục khám phá ngôi đền cổ",
    "Ngôi đền được xây dựng từ hơn ba trăm năm trước đây",
    "Kiến trúc nơi đây thể hiện trình độ kỹ thuật rất tinh xảo",
    "Mỗi họa tiết chạm khắc đều mang một ý nghĩa riêng biệt",
    "Người quản lý đền kể cho chúng tôi nghe lịch sử nơi này",
    "Câu chuyện kể về vị tăng lữ đã từng tu hành tại đây",
    "Chiều muộn ánh nắng dịu hắt qua những tán cây cổ thụ",
    "Chúng tôi quyết định leo lên đỉnh đồi ngắm cảnh hoàng hôn",
    "Con đường mòn nhỏ dẫn lên đỉnh khá dốc và trơn trượt",
    "Mặt trời từ từ lặn dần sau dãy núi xa xa phía chân trời",
    "Bầu trời chuyển sang sắc cam và tím rực rỡ vô cùng",
    "Đây chính là khoảnh khắc đáng nhớ nhất của chuyến đi",
]
EN = [
    "Welcome back to the channel everyone, great to see you",
    "Today we are exploring one of the most beautiful valleys",
    "The river runs right through the middle of the forest",
    "The water here is incredibly clear and surprisingly cold",
    "We woke up before sunrise to catch the morning mist",
    "It was totally worth losing a couple hours of sleep",
    "The trailhead is just a short walk from the village",
    "Local guides recommend starting early to avoid the heat",
    "Listen to that sound, the birds are singing everywhere",
    "This place feels completely untouched by modern life",
    "We met a farmer who showed us his coffee plantation",
    "He explained how the beans are harvested and dried",
    "His family has worked this land for three generations",
    "The coffee smelled amazing even before it was roasted",
    "Lunch is a simple bowl of noodles by the riverside",
    "Honestly one of the best meals we have had on this trip",
    "In the afternoon we visited the old stone bridge",
    "It was built more than two hundred years ago",
    "The stones are still standing strong after all this time",
    "Our guide told us stories about the merchants here",
    "Tomorrow we head further north toward the highest peak",
    "The forecast says clear skies for the next three days",
    "We need to rest early because the hike starts at five",
    "Thank you all so much for watching until the end",
    "See you in the next adventure, goodbye for now",
]

# Xen kẽ Việt/Anh; hết một loại thì lấy nốt phần còn lại của loại kia.
texts: list[str] = []
vi_i = en_i = 0
while vi_i < len(VI) and en_i < len(EN):
    if vi_i <= en_i:
        texts.append(VI[vi_i]); vi_i += 1
    else:
        texts.append(EN[en_i]); en_i += 1
texts.extend(VI[vi_i:])
texts.extend(EN[en_i:])

events = []
t = 1.0
for text in texts:
    events.append((t, t + CUE_LEN, text))
    t += CUE_LEN + GAP
DUR = events[-1][1] + 1.0

font = ImageFont.truetype(str(ROOT / "data" / "test" / "arialbd.ttf"), 36)

yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)
base = np.dstack([
    np.clip(38 + 14 * np.sin(xx * 0.003 + 1.0), 0, 255),
    np.clip(36 + 12 * np.sin(yy * 0.004), 0, 255),
    np.clip(42 + 10 * np.sin((xx + yy) * 0.002), 0, 255),
]).astype(np.uint8)

cmd = ["ffmpeg", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", str(FPS),
       "-i", "-", "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p", str(OUT)]
proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
total = int(FPS * DUR)
for i in range(total):
    t = i / FPS
    img = Image.fromarray(base)
    draw = ImageDraw.Draw(img)
    for start, end, text in events:
        if start <= t < end:
            bbox = draw.textbbox((0, 0), text, font=font)
            tw = bbox[2] - bbox[0]
            draw.text(((W - tw) // 2, 552), text, font=font, fill=(255, 255, 255),
                      stroke_width=2, stroke_fill=(0, 0, 0))
    proc.stdin.write(np.asarray(img).tobytes())
proc.stdin.close()
proc.wait()
print("wrote", OUT, f"dur={DUR:.1f}s cues={len(events)} size={OUT.stat().st_size}")
