# Kế hoạch bàn giao — SubExtract

Cập nhật: 2026-10-30 (bản 4 — thêm sửa/thêm/tách/gộp dòng phụ đề trong UI, bên cạnh AI lồng tiếng và bản vá kẹt 57%)

## 1. Trạng thái hiện tại

| Hạng mục | Trạng thái |
| --- | --- |
| SubExtract 8787 | Đang chạy qua scheduled task `SubExtractRun` (uvicorn ẩn cửa sổ), đã triển khai P1 |
| Dashboard Tool V1 8088 | Không thay đổi (status 200) |
| GPU | RTX 4050 Laptop, torch 2.11.0+cu128 |
| ffmpeg / ffprobe | OK |
| Gemini API key | Đã cấu hình trong `data/settings.json`, nguồn `C:\tool v1\.env` (`GEMINI_API_KEY`) — không in/log key |
| Key dự phòng | Hỗ trợ `gemini_api_keys` (list trong settings.json) và env `GEMINI_API_KEYS` (phân tách phẩy); xoay tự động khi key hết quota/bị từ chối |
| Model dịch mặc định | `gemini-3.5-flash-lite` |
| Model tùy chọn | `gemini-3.8-flash`, `gemini-4-argon` (đã có giới hạn timeout/batch riêng) |
| AI lồng tiếng | Bật sẵn trong UI (checkbox "AI lồng tiếng · Chí Mai"); giọng CapCut `BV562_streaming`, xuất `D:\banve\Dubbed_<tên video>.mp4` |
| Chống kẹt khi nạp model | Đã vá: khôi phục job treo sau restart, khóa 1 pipeline/job, khóa suy luận OCR, heartbeat tiến độ khi nạp |
| Job còn dữ liệu | `814d698045db` (video dài 150s, 55 cue, done) · `98e9071e0d75` (hardsub.mp4, 4 cue) · `38422d504485` (gián đoạn do restart, đã chuyển `error` có thông báo) |

Lưu ý: 8787 chạy nền qua task `SubExtractRun` để không bị dọn khi phiên agent kết thúc. Nếu máy/session reboot, mở lại bằng shortcut SubExtract hoặc chạy task `SubExtractRun`.

## 2. Phạm vi đã hoàn thành

- Dịch sau OCR bằng Gemini, chạy trong pipeline job ([app/pipeline/runner.py](app/pipeline/runner.py)).
- Client Gemini dùng `generateContent` + `responseSchema`, bỏ `temperature` (2026 deprecated) ([app/pipeline/translate.py](app/pipeline/translate.py)).
- Phát hiện ngôn ngữ từng cue: Trung (Han), Việt (dấu), Anh (Latin); bỏ qua cue đã đúng ngôn ngữ đích.
- Dịch theo lô có ngữ cảnh cue trước (batch theo model: Lite 24 / 4-argon 12 / 3.8-flash 8), retry lỗi tạm thời, tôn trọng hủy job.
- Kiểm tra hậu dịch: cue bị giữ nguyên ngôn ngữ gốc sẽ được dịch lại riêng với prompt nghiêm ngặt.
- Lưu key cục bộ, không trả key qua API; chỉ gửi bằng header `x-goog-api-key`.
- Xoay key tự động khi 429/quota/key bị từ chối (`gemini_api_keys` list hoặc env `GEMINI_API_KEYS`).
- Hủy job tức thì cả khi đang chờ Gemini (request chạy luồng worker, cancel poll 0.2s + đóng connection).
- Giới hạn timeout/batch riêng theo model (`MODEL_LIMITS`) để model chậm không treo job.
- Lưu `job.json` ngay khi upload — job upload-only không mất sau restart.
- **Đa vùng ROI (30-10)**: nhận bản được vùng chọn — UI có "Nhân bản vùng" (vùng đáy → nhân bản lên đỉnh và ngược lại), "Xóa vùng", bấm chọn vùng active, vẽ thêm vùng, tối đa 4 vùng; pipeline crop theo HỢP các vùng và quét từng vùng trong MỘT lần decode; cue khác vùng được phép trùng giờ (đa vùng trên+dưới song song). API nhận `roi` dạng `[x,y,w,h]` cũ hoặc `[[x,y,w,h],…]`, video 1 vùng hành vi y hệt trước đây.
- API: `GET/PUT /api/settings`, `translate_to` khi start, `trans` trong cue, xuất `original` / `translated` / `bilingual`, hậu tố `.vi` / `.song-ngu`.
- UI: chọn ngôn ngữ dịch, chọn model, nhập/kiểm tra key, glossary thuật ngữ, sửa bản dịch từng cue, nút ⟳ dịch lại từng cue + dịch lại chỗ thiếu, badge cảnh báo cue chưa dịch, chọn biến thể xuất.
- Fix ROI regression trước đó vẫn giữ nguyên.

### 2.1 AI lồng tiếng sau dịch (30-10)

- Tái dùng đúng giọng "chí mai" của Tool V1: CapCut TTS `BV562_streaming` (như `C:\tool v1\backend\rerender_with_chimai_tts.py`), không cài thêm gì vào venv SubExtract.
- Chạy client CapCut qua subprocess bằng python của Tool V1 (`C:\tool v1\backend\venv\Scripts\python.exe`), protocol `dub_worker.py request.json result.json`; có thể đổi bằng settings `dub_python` hoặc env `SUBEXTRACT_TTS_PYTHON`.
- Chỉ lồng tiếng cho cue có bản dịch (`trans`), bỏ cue không có chữ cái hoặc phần lớn là chữ Hán.
- Trộn audio: giọng gốc hạ còn 0.18 (ducking nhẹ, vẫn nghe được tiếng nền), giọng AI 1.0, mỗi câu co giãn `atempo` (chuỗi tối đa 8×) và `atrim` để không lấn cue kế, `adelay` đặt đúng mốc thời gian, `amix normalize=0` + `alimiter=0.95`, xuất AAC 192k, `-c:v copy` (fallback libx264 nếu copy lỗi).
- Xuất `D:\banve\Dubbed_<tên video>.mp4` (tên trùng thì thêm `_2`, `_3`…); tên file được làm sạch ký tự cấm/emoji.
- UI: checkbox "AI lồng tiếng · Chí Mai" (mặc định bật), gợi ý đường dẫn xuất, sau khi xong hiện đường dẫn + nút "Tải video lồng tiếng" (`GET /api/jobs/{id}/dubbed`).
- Settings mới: `dub_voice`, `dub_voice_label`, `dub_output_dir` (mặc định `D:\banve`), `dub_python` (tùy chọn).
- Lỗi lồng tiếng **không** làm hỏng kết quả OCR/dịch: job vẫn `done`, stats ghi `dub_error`, message nêu lỗi, file SRT/VTT vẫn tải bình thường.

### 2.2 Vá lỗi kẹt "Nạp model OCR 57%"

Nguyên nhân (theo log + `job.json` + Windows event log, không phải EasyOCR treo):

1. Tiến trình server bị dừng/khởi động lại giữa lúc job đang chạy (không có crash event của Python) → `job.json` còn `running`, còn bộ nhớ đã reset, API trả `uploaded`; UI đứng ở 57% vĩnh viễn.
2. Bấm Hủy rồi Bắt đầu ngay có thể tạo 2 luồng pipeline chạy song song trên cùng job.
3. Nhiều job dùng chung reader EasyOCR — suy luận song song trên cùng reader không an toàn, dễ treo ở bước nạp/chạy model.

Đã sửa:

- `app/jobs.py`: khi khởi động, job còn `running` được chuyển thành `error` kèm thông báo "Server đã dừng khi job đang chạy nên tiến trình bị gián đoạn. Bấm Bắt đầu để chạy lại." và lưu lại; thêm `run_lock` chống 2 pipeline cùng job; lưu `message` vào `job.json`.
- `app/main.py`: `start_job` giành `run_lock` không chờ, đang chạy thì trả 409 "Job đang chạy"; luôn nhả khóa trong `finally`; lỗi phát sinh sau khi đã bấm Hủy được ghi nhận là `cancelled` (trước đây hiện nhầm "ffmpeg thoát với mã 1").
- `app/pipeline/ocr.py`: `read_line` đi qua `threading.Lock` — không bao giờ có 2 job suy luận đồng thời trên một reader.
- `app/pipeline/runner.py`: nạp model chạy ở luồng riêng, vòng chính báo tiến độ mỗi 0.5s ("Đang nạp model OCR (GPU)… Ns"), vẫn hủy được ngay khi nạp; tiến độ dịch 0.93→0.96, lồng tiếng 0.96→0.99.

### 2.3 Sửa dòng phụ đề ngay trong danh sách cue (30-10)

Trước đây ô nội dung gốc/bản dịch đã sửa được nhưng hiển thị như chữ tĩnh (nền và viền trong suốt) nên khó nhận ra. Nay:

- Ô nội dung gốc và bản dịch có nền/viền nhẹ, đổi màu khi hover/focus, tooltip "Nhấp để sửa…"; gõ là tự lưu (debounce 0.7s), trạng thái "Đang lưu…/Đã lưu" ở đầu khối.
- Nút **+ Thêm dòng**: chèn cue mới ngay sau dòng đang phát (hoặc cuối danh sách), đặt con trỏ sẵn; thời gian mặc định 1.5s trong phạm vi video.
- Nút **tách dòng** trên từng cue: đặt con trỏ giữa dòng rồi bấm, cue được chia đôi; thời gian chia theo tỷ lệ ký tự (mỗi bên ≥ 0.2s), bản dịch chia theo ranh giới từ gần nhất. Dùng để xử lý các cue bị gộp 2 câu.
- Nút **gộp dòng dưới**: nối nội dung + bản dịch với dòng kế, mở rộng thời gian tới cuối dòng dưới; nút bị vô hiệu ở dòng cuối.
- **Hoàn tác nhiều bước** (nút đầu khối hoặc Ctrl+Z khi không ở trong ô nhập): lưu tối đa 40 mốc cho thêm/tách/gộp/xóa dòng, khôi phục cả danh sách và trạng thái "chưa dịch".
- Xóa dòng (nút thùng rác hoặc xóa trắng nội dung) cũng vào lịch sử hoàn tác.
- Sửa thời gian start/end bằng cách gõ trực tiếp vào ô thời gian như trước.
- Mọi thay đổi lưu qua `PUT /api/jobs/{id}/cues` vào `job.json` và được dùng ngay khi xuất SRT/VTT hoặc dịch lại cue.

### Bằng chứng đã chạy

| Bài test | Kết quả |
| --- | --- |
| Key thật + danh sách model | 60 model, đủ 3 model dùng được |
| Anh → Việt, 8 cue | Lite 3.0s, 3.8-flash 213.5s, bản dịch tự nhiên |
| Trung → Việt, 4 cue | Lite 6.9s, 3.8-flash 179.1s |
| Video thật `hardsub_logo.mp4` (Việt + Anh + Trung) | 3 cue, dịch 2 cue Anh/Trung, 3.5s, xuất 3 biến thể OK |
| UI test cô lập cổng 8790 | Pass, không lỗi console |
| Key sai (graceful fallback) | Job done, giữ 4 cue OCR, báo lỗi dịch rõ ràng |
| `hardsub_logo` sau guard chống giữ nguyên | 4/4 lần đều dịch đúng cue Trung |
| **P0: video dài 150s, 55 cue Việt/Anh xen kẽ** (`814d698045db`) | Job done 73.6s (OCR GPU 21.8s + dịch Lite 45.7s), dịch đúng 25/25 cue Anh, không lỗi; xuất 6 file SRT/VTT đúng cấu trúc, song ngữ 2 dòng ở cue có dịch |
| **Xoay key: 2 key giả + key thật** | Batch lỗi trên key giả → xoay sang key thật → dịch 2/2, không lỗi |
| **Xoay key: chỉ key giả** | Job done, giữ 3 cue OCR, báo lỗi rõ ràng |
| **Hủy job khi đang chờ Gemini (3.8-flash)** | `cancelled` sau 0.11s (tiêu chí ≤ 2s) |
| **Persist job upload-only qua restart** | Upload không chạy → restart 8787 → job vẫn hiện `uploaded` |
| **Đích English** (`hardsub.mp4`, nguồn Trung) | Dịch 3/3, 33.6s, không lỗi |
| **Đích 中文** (`hardsub_logo.mp4`) | Dịch 2/2, 37.2s, không lỗi |
| **Hủy khi request treo (unit, IP không định tuyến)** | `Cancelled` sau 0.62s |
| **P2: retranslate + glossary (8790 cô lập)** | 11/11 PASS: lưu/đọc glossary, dịch lại cue → trans cập nhật + persist, lỗi 400 rõ ràng, prompt có glossary |
| **P2: UI test bằng trình duyệt thật (IAB, job 55 cue)** | Layout ✓, nút ⟳ dịch lại cue → toast + bản dịch mới (31.5s Lite) ✓, glossary lưu → toast ✓, dòng sai định dạng → toast lỗi "bỏ qua 1 dòng" ✓ |
| **30-10: vá OCR trên video portrait hộp-trắng-chữ-đen** | Cue thu được 8 → 10 (thu hồi 2 khoảng 9.0–12.2 và 13.8–19.8 trước đây bị rơi); 11/11 group OCR đạt ngưỡng (cũ 9/11); thông báo mới "phụ đề gốc đã là tiếng Việt nên không có gì để dịch" khi target trùng nguồn |
| **30-10: đa vùng ROI (video tổng hợp 2 vùng trên+dưới)** | 16/16 cue bóc đúng đủ thời lượng song song ở cả hai vùng; test UI trình duyệt: Nhân bản → hộp đối diện + nhãn lật chiều, bấm chọn vùng, Xóa vùng — tất cả PASS |
| **30-10: lồng tiếng đầy đủ trên video thật** (`开箱Black_Muji卡片盒.mp4`, 32.27s, 720×1280) | Xuất `D:\banve\Dubbed_开箱Black_Muji卡片盒.mp4` (4,782,445 bytes, h264 + aac 48k stereo, đúng 32.27s); đoạn có tiếng AI mean −22.0 dB so với đoạn lặng −39.2 dB |
| **30-10: E2E lồng tiếng qua API (8790 cô lập)** | 3 cue, TTS ~3s/câu, mux xong 2.6s, file tải được qua `/api/jobs/{id}/dubbed` |
| **30-10: chặn chạy trùng job** | Bắt đầu lần 2 khi job đang chạy → HTTP 409 "Job đang chạy", không sinh luồng thứ 2 |
| **30-10: khôi phục job treo sau restart** | Job `38422d504485`: `error` + thông báo gián đoạn, đã ghi vào `job.json` (thay vì đứng 57%) |
| **30-10: UI live 8787 (Playwright)** | Checkbox lồng tiếng hiện + mặc định bật, hint `D:\banve`, không lỗi console; 8088 vẫn 200 |
| **30-10: sửa cue trên UI (8790 cô lập, Chromium thật)** | 25/25 PASS: sửa nội dung gốc/bản dịch/thời gian → lưu vào `job.json` và đĩa; thêm dòng; tách dòng tại con trỏ (8.875s, đúng 2 nửa); gộp dòng dưới; hoàn tác 2 bước; xóa dòng; SRT xuất ra chứa nội dung đã sửa; 0 lỗi trang |
| **30-10: smoke UI thật 8787 (`6ca01c10b3db`)** | 6 dòng hiển thị, ô sửa được + tooltip, nút thêm/tách/gộp đúng trạng thái, hoàn tác ẩn khi chưa có thao tác, dữ liệu job không đổi sau khi xem |

Lệnh chạy lại:

```bash
python data/test/translate_ui_test.py
.venv/Scripts/python.exe data/test/translate_e2e_test.py
.venv/Scripts/python.exe data/test/translate_real_test.py
.venv/Scripts/python.exe data/test/translate_key_rotation_test.py   # xoay key (8790 cô lập)
.venv/Scripts/python.exe data/test/retranslate_glossary_test.py     # P2: dịch lại cue + glossary (8790)
.venv/Scripts/python.exe data/test/multi_roi_test.py                # đa vùng ROI trên+dưới (8790)
.venv/Scripts/python.exe data/test/make_video_dual.py               # tạo video phụ đề trên+dưới
.venv/Scripts/python.exe data/test/make_video_long.py               # tạo video 150s/55 cue
.venv/Scripts/python.exe data/test/acceptance_p0_test.py            # P0 trên 8787 thật
.venv/Scripts/python.exe data/test/cancel_speed_test.py             # hủy ≤2s (tự khôi phục model)
.venv/Scripts/python.exe data/test/target_langs_test.py             # đích English + 中文
.venv/Scripts/python.exe data/test/bench_engine.py                  # đo thời gian nạp model (mốc ~8.8s GPU)
.venv/Scripts/python.exe data/test/start_guard_test.py              # chặn 2 pipeline cùng job (409)
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe data/test/dub_e2e_test.py   # lồng tiếng E2E qua API (8790)
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe data/test/dub_full.py       # lồng tiếng trên video thật → D:\banve
python data/test/cue_edit_browser.py --shot cue_edit_after          # sửa/thêm/tách/gộp/xóa cue trên 8790 cô lập
python data/test/cue_edit_live_smoke.py                             # smoke UI sửa cue trên 8787 (chỉ đọc)
```

Các test 8790 tự dọn thư mục tạm; các test trên 8787 chỉ đọc/để lại job, không ảnh hưởng 8088. File xuất nghiệm thu nằm ở `data/test/out_acceptance/`.

## 3. Việc còn lại — theo ưu tiên

### P0 — Nghiệm thu trên dịch vụ thật (S) — ✅ HOÀN THÀNH
- Đã chạy video 150.2s, 55 cue Việt/Anh xen kẽ (`data/test/hardsub_long.mp4`, tạo bằng `make_video_long.py`) trên 8787 thật.
- Kết quả: job `done` 73.6s, dịch 25/25 cue Anh, không lỗi dịch; xuất đủ 6 biến thể SRT/VTT, cấu trúc đúng, song ngữ 2 dòng đúng chỗ cần.

### P1 — Ổn định & không mất dữ liệu (M) — ✅ HOÀN THÀNH
- ✅ Lưu `job.json` ngay khi upload (`app/main.py` `create_job`) — job upload-only sống qua restart.
- ✅ Hủy tức thì khi chờ Gemini: request chuyển sang luồng worker, luồng chính poll cancel 0.2s và đóng connection (`app/pipeline/translate.py` `_generate`). Đo thực tế 0.11s.
- ✅ Xoay key: `gemini_api_keys` (list trong settings.json) + env `GEMINI_API_KEYS` (phân tách phẩy); lỗi 429/quota hoặc key bị từ chối → tự chuyển key kế tiếp; hết key mới báo lỗi. `app/settings.py` `get_gemini_keys()`.
- ✅ Giới hạn theo model `MODEL_LIMITS`: Lite timeout 120s/batch 24; 3.8-flash 480s/batch 8; 4-argon 300s/batch 12.
- Tiêu chí đạt: restart 8787 vẫn thấy job vừa upload ✅; hủy job dừng trong ≤ 2s ✅ (0.11s).

### P2 — Chất lượng & UX (M) — ✅ HOÀN THÀNH (phần tính năng UI)
- ✅ Glossary/thuật ngữ riêng: ô "Thuật ngữ riêng" trong khối Gemini (mỗi dòng `gốc = bản dịch`, chấp nhận `=`/`→`/`->`), lưu vào `glossary` trong settings.json, tự chèn vào prompt dịch cả lô và dịch lại từng cue. Giới hạn 100 mục × 120 ký tự, dòng sai định dạng được bỏ qua và báo rõ.
- ✅ Nút "Dịch lại cue này bằng Gemini" (⟳) trên mỗi cue khi có dịch: gọi `POST /api/jobs/{id}/retranslate`, điền kết quả vào ô bản dịch, lưu job. Nút "Dịch lại chỗ thiếu" xử lý loạt các cue chưa dịch kèm tiến độ.
- ✅ Hiển thị lỗi/cue chưa dịch: stats thêm `untranslated_cues` + `untranslated_indices`; badge vàng "N cue chưa dịch" + nút dịch lại chỗ thiếu hiện khi còn thiếu; dòng thông báo hoàn tất nêu số cue chưa dịch.
- ✅ API mới: `POST /api/jobs/{id}/retranslate` (index/text/target), `GET/PUT /api/settings` thêm `glossary`.
- Còn lại (nghiệm thu mở rộng): cue trộn nhiều ngôn ngữ trong MỘT cue; chạy video 100 cue; đo lại chi phí theo model.
- Tiêu chí đạt: video 100 cue, tỷ lệ cue có bản dịch ≥ 98%, không sai nghĩa nghiêm trọng khi spot-check. (Video 55 cue đạt 100%; chưa chạy 100 cue.)

### P3 — Vận hành (S–M) — còn lại
- Theo dõi usage/cost Gemini (key dùng chung Tool V1).
- Log rotation cho `data/server.log`.
- Backup `data/settings.json` và `data/jobs/` (không chứa key trong backup công khai).
- Khởi tạo git/versioning nếu muốn rollback.
- Tiêu chí đạt: có log xoay vòng, có bản backup định kỳ, biết chi phí tuần.

### P4 — AI lồng tiếng Chí Mai → `D:\banve` (M) — ✅ HOÀN THÀNH
- ✅ Lõi lồng tiếng `app/pipeline/dub.py` + worker CapCut `app/dub_worker.py`, dùng đúng voice `BV562_streaming` của Tool V1, không cài thêm vào venv SubExtract.
- ✅ Nối vào pipeline sau dịch (stage `dub`, 0.96→0.99), stats/message, endpoint `GET /api/jobs/{id}/dubbed`, checkbox + nút tải trong UI, settings `dub_*`.
- ✅ Trộn: gốc 0.18, AI 1.0, co giãn theo khoảng cue, limiter, AAC 192k, video copy.
- Còn lại: tùy chọn tắt hẳn tiếng gốc; chọn giọng trong UI; xử lý câu dịch quá dài tốt hơn (rút gọn thay vì tăng tốc mạnh); đo lại khi chạy video 100 cue.

## 4. Vận hành nhanh

- Khởi động: chạy `launcher.py` (ẩn cửa sổ) hoặc shortcut SubExtract.
- Chạy nền bền vững: scheduled task `SubExtractRun` chạy `pythonw launcher.py --no-browser --keep-alive`; task không có trigger nên không tự chạy khi đăng nhập.
- Kiểm tra: `http://127.0.0.1:8787/api/health` và `/api/settings`.
- Log: `data/server.log`.
- Dừng/khởi động lại: chỉ tác động tiến trình cổng 8787; tuyệt đối không đụng 8088.
- Dừng bản chạy nền: `Stop-ScheduledTask -TaskName SubExtractRun` hoặc dừng tiến trình uvicorn cổng 8787.
- **Cảnh báo orphan**: `Stop-ScheduledTask` có thể chỉ dừng `launcher.py`/`pythonw`, còn tiến trình uvicorn con vẫn giữ cổng 8787 (code cũ vẫn phục vụ). Sau khi stop, kiểm tra `netstat -ano | findstr :8787` và `Get-CimInstance Win32_Process` để dừng nốt uvicorn còn sống, rồi `Start-ScheduledTask -TaskName SubExtractRun`.
- Lồng tiếng: bật/tắt bằng checkbox trong khối xuất; đổi thư mục xuất ở `dub_output_dir` trong `data/settings.json` (mặc định `D:\banve`, thư mục phải ghi được). Cần python Tool V1 có `capcut_tts_api` — nếu Tool V1 đổi venv, đặt `dub_python` hoặc env `SUBEXTRACT_TTS_PYTHON`.
- Job bị gián đoạn giờ hiện lỗi rõ ràng ở 57% cũ; bấm Bắt đầu để chạy lại, không cần restart server.
- Key: đổi trong UI SubExtract hoặc sửa `data/settings.json`; không in key ra chat/log.
- Model: Lite nhanh (khuyến nghị); 3.8-flash chất lượng cao nhưng rất chậm; 4-argon chưa benchmark.

## 5. Rủi ro & lưu ý

- Interactions API mới của Gemini treo/timeout với key hiện tại; đang dùng `generateContent`. Cần theo dõi khi Google bỏ `generateContent`.
- `gemini-3.8-flash` chậm bất thường (~3 phút cho 8 cue); đã có `MODEL_LIMITS` (batch 8, timeout 480s) nhưng không nên để mặc định cho video dài.
- Key dùng chung với Tool V1, không có giám sát quota riêng.
- Hủy job đang chờ Gemini để lại 1 luồng daemon tồn đến hết socket timeout của model (≤ 480s với 3.8-flash) — vô hại, không chặn job khác.
- Chưa test cue trộn nhiều ngôn ngữ trong một cue; chưa chạy video 100 cue.
- Chưa có git; thay đổi chưa được version hóa.
- OCR EasyOCR vẫn sai một số dấu/từ trên chữ nhỏ (ví dụ 'Đâỵ', 'khôngdùng') — sửa tay trong UI, hoặc dịch sang ngôn ngữ khác để Gemini làm mượt theo ngữ cảnh.
- Hạn chế đã biết: phụ đề dạng hộp (trắng/đen) bán trong suốt chạy dài nhiều câu có thể bị gộp thành một cue dài (detector nhận diện hộp chứ không nhận diện từng câu) — ví dụ video `Dubbed_telegram…用喷枪画夜景` còn cue 21.8–30.8 gộp 2 câu. Cần hướng OCR-trong-khi-detect mới xử lý triệt để.
- Lồng tiếng phụ thuộc dịch vụ CapCut TTS bên thứ ba: cần mạng và có thể đổi API/giới hạn; mỗi lần chạy có thể trễ ~2–4s/câu, video dài sẽ lâu tương ứng. TTS lỗi chỉ mất phần lồng tiếng, không mất SRT/VTT.
- Câu dịch quá dài so với khoảng thời gian cue sẽ bị tăng tốc (atempo, tối đa chuỗi 8×) hoặc cắt bớt ở cuối — nghe nhanh hơn bình thường.
- Giọng gốc bị hạ còn 0.18 chứ không tắt hẳn (giữ tiếng nền); nếu cần tắt hẳn phải thêm tùy chọn volume gốc.
- Voice ID `BV562_streaming` là của tài khoản CapCut dùng chung với Tool V1; nếu CapCut đổi voice/khóa tài khoản thì phải cập nhật `DEFAULT_VOICE`.
- Chưa kiểm thử đồng thời: vừa chạy job 8787 vừa chạy job Tool V1 cùng lúc gọi CapCut (dùng chung hạn mức thiết bị/tài khoản CapCut).

## 6. Checklist nghiệm thu bàn giao

- [x] 8787 health 200, GPU/ffmpeg OK
- [x] Key Gemini đã cấu hình và không lộ ra ngoài
- [x] Dịch thật Anh + Trung → Việt trên video mẫu
- [x] Xuất SRT/VTT gốc, bản dịch, song ngữ
- [x] UI live không lỗi console
- [x] Key sai không làm mất kết quả OCR
- [x] Video thật ≥ 2 phút, ≥ 50 cue (150.2s, 55 cue, job `814d698045db`)
- [x] Đích English và 中文 (3/3 và 2/2 cue, không lỗi)
- [x] Persist job upload-only sau restart (job `cd0e008ac9b4`)
- [x] Lồng tiếng Chí Mai xuất `D:\banve` trên video thật (32.27s, 4.78 MB, h264+aac)
- [x] Sửa/thêm/tách/gộp/xóa dòng phụ đề trong UI, tự lưu, hoàn tác được, xuất SRT đúng nội dung sửa
- [x] Không còn kẹt im lặng ở bước nạp model (job gián đoạn → báo lỗi rõ, chạy lại được)
- [ ] Theo dõi quota/chi phí (P3)

## 7. Bản đồ file chính

- `app/pipeline/translate.py` — client Gemini (http.client + luồng worker cho hủy tức thì), detect ngôn ngữ, batch, xoay key, `MODEL_LIMITS`, glossary vào prompt, guard/retry, trả `untranslated`.
- `app/pipeline/runner.py` — nối bước dịch sau OCR, truyền danh sách key + glossary, ghi cue chưa dịch vào stats; nạp model ở luồng riêng + heartbeat; gọi lồng tiếng sau dịch.
- `app/pipeline/dub.py` — lõi lồng tiếng: chọn câu, gọi TTS qua worker, co giãn/đặt thời gian, trộn ffmpeg, đặt tên file an toàn, xuất `D:\banve`.
- `app/dub_worker.py` — worker chạy bằng python Tool V1, gọi CapCut TTS song song (tối đa 4 luồng), ghi `cue_<id>.mp3`, trả JSON kết quả.
- `app/pipeline/ocr.py` — khóa suy luận `read_line` (không chạy song song trên cùng reader).
- `app/jobs.py` — lưu `message`, `run_lock` mỗi job, khôi phục job `running` → `error` sau restart.
- `app/settings.py` — đọc/ghi key cục bộ, `get_gemini_keys()` (key đơn + danh sách).
- `app/main.py` — API settings (+glossary), start, cues, retranslate từng cue, export; lưu `job.json` ngay khi upload.
- `app/static/index.html`, `app/static/app.js`, `app/static/style.css` — UI dịch: ô glossary, nút ⟳ dịch lại từng cue, badge "chưa dịch" + nút dịch lại chỗ thiếu; checkbox + đường dẫn + nút tải video lồng tiếng; sửa cue trực tiếp, thêm/tách/gộp dòng, hoàn tác nhiều bước.
- `data/test/cue_edit_browser.py` — test trình duyệt thật cho sửa cue (8790 cô lập); `cue_edit_live_smoke.py` — smoke chỉ đọc trên 8787.
- `data/test/dub_probe.py`, `dub_full.py`, `dub_e2e_test.py`, `tts_probe.py` — thử TTS, lồng tiếng video thật, E2E qua API.
- `data/test/start_guard_test.py`, `bench_engine.py`, `check_proc.ps1`, `check_events.ps1` — kiểm tra chặn chạy trùng, đo nạp model, soi tiến trình/sự kiện Windows.
- `data/settings.json` — cấu hình cục bộ (chứa key + glossary, không chia sẻ; hỗ trợ `gemini_api_keys`).
- `data/test/translate_*_test.py` — bộ test UI/E2E/dịch thật/xoay key.
- `data/test/retranslate_glossary_test.py` — test P2 dịch lại cue + glossary (8790 cô lập).
- `data/test/acceptance_p0_test.py`, `cancel_speed_test.py`, `target_langs_test.py` — nghiệm thu P0, hủy ≤2s, đích en/zh.
- `data/test/make_video_long.py` + `data/test/hardsub_long.mp4` — video nghiệm thu 150s/55 cue.
- `data/test/out_acceptance/` — file xuất SRT/VTT các biến thể từ buổi nghiệm thu.
