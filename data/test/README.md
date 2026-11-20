# Kiểm thử hồi quy offline

Chạy từ thư mục gốc repo với Python 3.10 trở lên và Node.js 18.8 trở lên. Dùng môi trường Python đã cài dependencies của ứng dụng; test API cần thêm httpx. Danh sách dependencies kiểm thử ở ../../requirements-dev.txt. Các test dưới đây không chạy OCR, gọi Gemini, kết nối Tool V1 hoặc mở server.


Lệnh chạy riêng từng suite:

    python -B data/test/post_timeline_test.py
    python -B data/test/cue_api_test.py
    python -B data/test/job_persistence_test.py
    node --test data/test/cue_editor_test.cjs
    node --check app/static/app.js
    git diff --check

Trong PowerShell, nếu python không có trong PATH, dùng & 'đường-dẫn/python.exe' -B ... . Không chạy unittest discover trên toàn data/test: các script cũ có thể khởi chạy server, OCR, hoặc dùng dữ liệu/video thật.

## Hợp đồng được bảo vệ

- Timeline: sắp xếp ổn định theo thời gian trên toàn bộ ROI; chỉ merge text liên tiếp cùng ROI, end lấy max; clip overlap ở mốc bắt đầu kế tiếp trong cùng ROI. Hai ROI được chạy song song. Hai text khác nhau cùng start_ms được giữ cả hai để không mất nội dung; đây là overlap có chủ đích cần review nếu xuất hiện. Timestamp hợp lệ nhỏ hơn 1 ms được biểu diễn bằng khoảng 1 ms trong độ phân giải SRT/VTT. Build không mutate nhóm đầu vào.
- Editor: PUT tuần tự theo phiên, lưu thêm snapshot mới nếu có edit trong lúc request chờ, chỉ báo đã lưu khi snapshot mới nhất được xác nhận. Export/handoff/retranslate/đổi video/chạy lại phải chờ save thành công. Response cũ không tác động editor mới. Handoff double click chỉ gửi một lần trong lúc đang chờ.
- Dịch lại: cue ID ổn định, không lấy index đã thay đổi bởi server sort/filter. Editor dùng persist:false để nhận preview, kiểm tra đúng cue/nội dung/phiên/ngôn ngữ rồi PUT bản dịch đã chấp nhận. API vẫn giữ legacy index và persist:true mặc định; commit legacy bị từ chối 409 nếu cue/collection đổi trong lúc Gemini chờ. ID được ghi bổ sung khi PUT; job cũ không cần migrate toàn bộ.
- Start: khóa run được giữ trước khi xóa cue; gọi trùng không xóa dữ liệu. Nếu server từ chối start, editor giữ kết quả cũ. Nếu mất response nên chưa biết server đã chạy chưa, editor chặn lưu và hướng dẫn tải lại trang; chọn video khác rời editor mà không ghi cue cũ đè kết quả server. Trong lúc GET kết quả đang chờ hoặc lỗi, không PUT danh sách rỗng lên server.
- Persistence: save lock theo Job, ghi file tạm cùng thư mục, flush/fsync, rồi os.replace; lỗi serialize/ghi/thay thế giữ job.json cũ. Test dùng TemporaryDirectory, không đụng job thật. Test API thay JobStore trước import app.main để tránh recovery làm ghi dữ liệu thật.

## Kết quả xác minh trên máy

84 test đạt: timeline 16, API 24, editor 37, persistence 7. Python 3.10 và Node.js 26; kiểm tra cú pháp Python/JavaScript và git diff --check đều đạt. API dùng ASGI trong process với mạng/OCR/save thật được mock; editor dùng Node VM/fake DOM, không thay thế nghiệm thu trình duyệt. Persistence dùng thư mục tạm thực.

## Kích hoạt và phạm vi kiểm chứng

Cần khởi động lại backend SubExtract rồi tải lại trang để frontend/API cùng phiên bản. Refactor này được kiểm chứng bằng test Python, ASGI TestClient và Node VM với mạng giả; chưa phải nghiệm thu browser thật, GPU/OCR hay dịch/render thật. Không thay cài đặt API key, lịch khởi động, GPU stack hay các job có sẵn khi thực hiện refactor.

Chưa giải quyết xung đột hai tab cùng sửa một job (chưa có server revision/If-Match), đồng bộ nhiều process, recall OCR vùng trên, logging/benchmark ground truth, quota/cleanup, privacy/API hardening hoặc đóng gói Windows. Atomic replace giữ tính hợp lệ của JSON khi save lỗi, không phải bảo đảm tuyệt đối trước mất điện hoặc đồng bộ cloud. Các writer ngoài cue API vẫn cần review nếu muốn transaction toàn bộ trạng thái job.
