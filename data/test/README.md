# Kiểm thử hồi quy offline

Chạy từ thư mục gốc repo với Python 3.10 trở lên và Node.js 18.8 trở lên. Dùng môi trường Python đã cài dependencies của ứng dụng; test API cần thêm httpx. Danh sách dependencies kiểm thử ở ../../requirements-dev.txt. Các test dưới đây không chạy OCR, gọi Gemini, kết nối Tool V1 hoặc mở server.


Lệnh chạy riêng từng suite:

    python -B data/test/post_timeline_test.py
    python -B data/test/cue_api_test.py
    python -B data/test/job_persistence_test.py
    python -B data/test/job_recovery_test.py
    python -B data/test/settings_persistence_test.py
    python -B data/test/audit_api_test.py
    python -B data/test/pipeline_reliability_test.py
    python -B data/test/ffmpeg_stream_test.py
    python -B data/test/handoff_boundary_test.py
    python -B data/test/handoff_connection_test.py
    python -B data/test/chinese_source_test.py
    node --test data/test/cue_editor_test.cjs
    node --check app/static/app.js
    git diff --check

Trong PowerShell, nếu python không có trong PATH, dùng & 'đường-dẫn/python.exe' -B ... . Không chạy unittest discover trên toàn data/test: các script cũ có thể khởi chạy server, OCR, hoặc dùng dữ liệu/video thật.

## Hợp đồng được bảo vệ

- Timeline: sắp xếp ổn định theo thời gian trên toàn bộ ROI; chỉ merge text liên tiếp cùng ROI, end lấy max; clip overlap ở mốc bắt đầu kế tiếp trong cùng ROI. Hai ROI được chạy song song. Hai text khác nhau cùng start_ms được giữ cả hai để không mất nội dung; đây là overlap có chủ đích cần review nếu xuất hiện. Timestamp hợp lệ nhỏ hơn 1 ms được biểu diễn bằng khoảng 1 ms trong độ phân giải SRT/VTT. Build không mutate nhóm đầu vào.
- Editor: PUT tuần tự theo phiên, lưu thêm snapshot mới nếu có edit trong lúc request chờ, chỉ báo đã lưu khi snapshot mới nhất được xác nhận. Export/handoff/retranslate/đổi video/chạy lại phải chờ save thành công. Response cũ không tác động editor mới. Handoff double click chỉ gửi một lần trong lúc đang chờ.
- Dịch lại: cue ID ổn định, không lấy index đã thay đổi bởi server sort/filter. Editor dùng persist:false để nhận preview, kiểm tra đúng cue/nội dung/phiên/ngôn ngữ rồi PUT bản dịch đã chấp nhận. API vẫn giữ legacy index và persist:true mặc định; commit legacy bị từ chối 409 nếu cue/collection đổi trong lúc Gemini chờ. ID được ghi bổ sung khi PUT; job cũ không cần migrate toàn bộ.
- Start/result: khóa run giữ trước khi tạo worker. Cues/stats/params tốt gần nhất tồn tại trong suốt rerun; kết quả mới chỉ publish dưới lock khi save thành công. Fail/cancel/restart vẫn có cue cũ và diagnostic; GET/export không thấy kết quả publish chưa lưu. Dubbing dùng thư mục attempt riêng và tên export riêng, không ghi đè file tốt cũ. Editor reconcile GET cả khi error/cancel trước khi cho lưu. Mất response start hoặc result GET lỗi vẫn chặn PUT cho đến khi biết trạng thái server.
- Persistence: save lock theo Job, ghi file tạm cùng thư mục, flush/fsync, rồi os.replace; lỗi serialize/ghi/thay thế giữ job.json cũ. Test dùng TemporaryDirectory, không đụng job thật. Test API thay JobStore trước import app.main để tránh recovery làm ghi dữ liệu thật.

## Nguồn tiếng Trung và số đi kèm

Giao diện mặc định chỉ chọn 中文. Khi nguồn chỉ có zh, mỗi dòng OCR phải chứa ít nhất một chữ Hán: giữ 第2集, 价格20元, bỏ 2, 7, 2026 hoặc Latin đứng riêng. Dòng số riêng không được giữ chỉ vì dòng khác hoặc ROI khác có chữ Hán. Fallback nhóm token theo hàng ảnh trước khi lọc; candidate chỉ có số không được thắng candidate tiếng Trung nhờ confidence cao hơn. Chọn thêm Việt/Anh sẽ dùng lại chế độ nhiều nguồn. Không áp bộ lọc lên bản dịch, cue người dùng sửa thủ công hoặc tự xóa dữ liệu đã lưu; cần chạy lại OCR để tạo kết quả mới sau khi backend đã nạp code mới.

## Các regression sau audit dc51ac1

- F01/F07: missing translation dùng stable cue ID ở frontend và metadata backend, giữ exemption nguồn đã ở ngôn ngữ đích; thêm/xóa/reorder/reload không nhắm nhầm bản dịch thủ công. Preview/timer/bulk/cancel handoff không xuyên phiên editor.
- F02/F03/F06: detector group ổn định chia thành window 12 giây; giữ đầu/cuối và short continuation ở ranh giới rồi merge text giống nhau. OCR theo thời gian thay vì 16 mẫu đầu clip. Watermark cần evidence cùng ROI, liên tục và có dòng khác cùng xuất hiện thay đổi, không chỉ đếm câu lặp. FFmpeg báo mọi nonzero bất thường/truncated frame, phân biệt cancel và dọn process/pipe khi đọc bị chặn.
- F04/F05/F11: giữ last-good, từ chối số không hữu hạn trước mutation, lỗi validation tự serialize an toàn; export SRT/VTT sort ổn định theo start mà không migrate/sửa job cũ.
- F08/F09/F10: preview encode file tạm, validate, replace rồi tạo marker .preview-ready; chỉ final file có marker mới được phục vụ. Preview cũ chưa có marker sẽ được tạo lại khi yêu cầu. Handoff có durable intent trước network, giữ reservation qua commit, CAS status/cancel theo attempt và record; lỗi save rollback. URL chỉ là HTTP(S) origin của exact localhost hoặc literal loopback; không proxy/redirect.
- F12/F13: metadata sai shape/type được log và cách ly từng job, không di chuyển/xóa dữ liệu. Status/error và last-good còn sau restart; lỗi save recovery vẫn giữ job trong RAM. Settings lock toàn read–merge–write và temp riêng, flush/fsync/replace.
- F14: upload giữ reservation trước multipart parsing; giới hạn request và file, kiểm tra đĩa tạm/data, quota kho nguồn, cleanup mọi nhánh lỗi; persist metadata trước register. Request thứ hai trả 429 trước spool. Không tự xóa job người dùng.

### Hạn mức upload

Mặc định file tối đa 2048 MiB, tổng kho job tối đa 20480 MiB tại thời điểm nhận upload, giữ trống ít nhất 512 MiB. Có thể cấu hình bằng SUBEXTRACT_MAX_UPLOAD_MB, SUBEXTRACT_MAX_DATA_MB và SUBEXTRACT_MIN_FREE_MB trước khi khởi động. Middleware cho multipart framing thêm 1 MiB, route vẫn áp byte limit riêng cho file. Quota này chặn upload mới, không phải hard cap cho crops/preview/dub phát sinh từ processing; chưa có tự retention hay UI xóa job. Các lock dành cho một process server.

### Handoff khi kết quả gửi chưa xác định

Nếu Tool V1 đã nhận nhưng save local lỗi, GET handoff có thể retry commit ID đã biết trong process mà không gửi render lần nữa. Intent queued không có remote ID được persist trước network để sau restart không tự gửi trùng. UI hiển thị hướng dẫn kiểm tra Tool V1; chưa có exactly-once reconciliation/idempotency bên Tool V1. Lỗi chắc chắn chưa import (connection refused, 401/403/404 của Tool V1 hoặc không đọc được khóa ghép nối) trả về trạng thái cũ để cho retry. Lỗi sau import không tự mở retry, ngoại trừ HTTP 429 từ endpoint render xác nhận hàng chờ đã đầy và chưa nhận render. Không tự xóa intent vì timeout hoặc response thất lạc.

## Kết nối Tool V1 và blur

Sau OCR, bước **Blur che phụ đề Trung** hiển thị kết nối, mức làm mờ và liên kết dashboard. Nút xuất gửi video và các cue đã lưu, dùng vùng OCR tương ứng theo cue, với `sub_cover_style=localized_blur`. Tool V1 chèn bản dịch và lồng tiếng khi có bản dịch; video lồng tiếng cục bộ ở bước OCR không có lớp blur này.

Cấu hình máy ghép nối bằng `tool_v1_token_origin` và `tool_v1_token_file` trong settings backend; không đưa nội dung khóa vào settings hay frontend. Đổi `tool_v1_url` không tự chuyển khóa sang origin khác. Kiểm tra kết nối gọi `/api/handoff/info`, sau đó xác minh quyền POST qua một đường dẫn không có handler; không import hay render trong bước này.

## Hàng chờ Tool V1

Tool V1 cấp thư mục và tên đầu ra riêng cho mỗi lần import, nhận render vào FIFO và chờ quyền xử lý chung với batch cùng khóa render Studio. SubExtract hiển thị vị trí chờ, cho rút đúng job đang chờ và bỏ vị trí khi đã chạy hoặc kết thúc. Hàng đầy trả 429 và cho phép thử lại; kết quả gửi không rõ vẫn giữ intent để chặn gửi trùng. FIFO chỉ áp dụng giữa các handoff SubExtract, hàng chờ hiện không tồn tại sau khi Tool V1 restart.

## Kết quả xác minh trên máy

290/290 test đạt trong lượt chạy tích hợp: timeline 16, cue API 24, audit API 48, editor 90, pipeline reliability 28, FFmpeg stream 15, handoff boundary 7, handoff connection 20, Chinese source 13, job persistence 8, job recovery 11, settings persistence 10. Cú pháp Python/JavaScript và git diff --check đạt. Python 3.10 và Node.js 26; API dùng ASGI với network/model được mock, persistence dùng thư mục tạm thực, editor dùng VM/fake DOM nên chưa thay thế nghiệm thu trình duyệt. Smoke riêng với FFmpeg thật trên video synthetic 2 giây nhận 10 frame ở 5 fps, hủy và reap hai decoder thành công; không gọi OCR/Gemini/Tool V1.

## Kích hoạt và phạm vi kiểm chứng

Cần khởi động lại backend SubExtract rồi tải lại trang để frontend/API cùng phiên bản. Refactor này được kiểm chứng bằng test Python, ASGI TestClient và Node VM với mạng giả; chưa phải nghiệm thu browser thật, GPU/OCR hay dịch/render thật. Không thay cài đặt API key, lịch khởi động, GPU stack hay các job có sẵn khi thực hiện refactor.

Chưa giải quyết xung đột hai tab cùng sửa một job (server revision/If-Match), đồng bộ nhiều process, nghiệm thu recall OCR vùng trên, logging/benchmark ground truth, toàn bộ Host/Origin/CSRF policy, dependency lock/CI, full backup/restore hoặc đóng gói Windows. Sampling vẫn xấp xỉ trong từng window và tốn thêm OCR ở group dài; watermark ưu tiên giữ chữ khi evidence chưa chắc. Quota upload không dọn dữ liệu tự động. Atomic replace không bảo đảm tuyệt đối trước mất điện/cloud sync. Các đường multi-process, exactly-once remote và mọi writer ngoài scope vẫn cần thiết kế riêng.
