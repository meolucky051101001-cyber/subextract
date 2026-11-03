"""Kiểm tra luồng giao phụ đề SubExtract → Tool V1 (server cô lập, không GPU, không Gemini).

- Stub Tool V1 trên cổng 8791 ghi lại payload /api/handoff/import và giả lập tiến độ render.
- SubExtract thật chạy trên cổng 8790 với data/settings tạm; job mồi được ghi sẵn job.json
  (cue có trường region) để không phải chạy OCR.
- Kiểm tra: region được giữ qua post.build_cues và PUT /cues; hộp che theo đúng ROI đã chọn;
  guard 409 khi đang render; tiến độ được lưu vào job.json; hủy render; cài đặt tool_v1_url.

Chạy: .venv/Scripts/python.exe data/test/handoff_subextract_test.py
"""
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

TMP = ROOT / "data" / "test" / "tmp_handoff"
JOBS = TMP / "jobs"
PORT = "8790"
STUB_PORT = 8791
BASE = f"http://127.0.0.1:{PORT}"
STUB = f"http://127.0.0.1:{STUB_PORT}"
VENV = ROOT / ".venv" / "Scripts" / "python.exe"
SEED_VIDEO = ROOT / "data" / "test" / "hardsub.mp4"
JOB_ID = "handoffseed01"
ROIS = [[0.0, 0.68, 1.0, 0.32], [0.02, 0.05, 0.6, 0.12]]

stub_state = {
    "imports": [],
    "renders": [],
    "cancels": [],
    "polls": 0,
    "job_id": "stubjob00001",
}


class StubHandler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def _send(self, payload, code=200):
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if self.path.startswith("/api/handoff/status"):
            stub_state["polls"] += 1
            step = stub_state["polls"]
            if step == 1:
                body = {"ok": True, "status": "queued", "progress": 4, "stage": "Trong hàng đợi..."}
            elif step == 2:
                body = {"ok": True, "status": "running", "progress": 55,
                        "stage": "Đang tạo giọng đọc Việt..."}
            elif step >= 3 and stub_state.get("final", True):
                body = {"ok": True, "status": "done", "progress": 100, "stage": "Hoàn tất",
                        "result": {"stem": "seed_video", "delivered": "D:/banve/Dubbed_seed_video.mp4",
                                   "voice": "edge/vi-VN-HoaiMyNeural", "cue_count": 3,
                                   "voiced_count": 2, "failed_cues": [], "seconds": 12.3}}
            else:
                body = {"ok": True, "status": "cancelled", "progress": 0, "stage": "Đã hủy"}
            self._send(body)
        else:
            self._send({"detail": "not found"}, 404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8"))
        except ValueError:
            payload = {}
        if self.path == "/api/handoff/import":
            stub_state["imports"].append(payload)
            cues = payload.get("cues") or []
            self._send({
                "ok": True, "stem": payload.get("stem"), "job_dir": "T:/stub/jobs/seed_video",
                "video": "T:/stub/jobs/seed_video/seed_video.mp4", "video_link": "hardlink",
                "width": payload.get("video_width"), "height": payload.get("video_height"),
                "duration": payload.get("duration"), "main_y_pct": payload.get("main_y_pct"),
                "cue_count": len(cues),
                "translated_count": sum(1 for c in cues if str(c.get("trans") or "").strip()),
            })
        elif self.path == "/api/handoff/render":
            stub_state["renders"].append(payload)
            stub_state["polls"] = 0
            stub_state["final"] = True
            self._send({"status": "ok", "job_id": stub_state["job_id"], "stem": payload.get("stem")})
        elif self.path == "/api/handoff/cancel":
            stub_state["cancels"].append(payload)
            stub_state["final"] = False
            self._send({"status": "cancelling", "job_id": payload.get("job_id")})
        else:
            self._send({"detail": "not found"}, 404)


def api(path, method="GET", body=None, timeout=30, base=BASE):
    req = urllib.request.Request(
        base + path,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None,
        method=method, headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def expect_error(path, method="POST", body=None):
    try:
        api(path, method=method, body=body)
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = str(json.loads(exc.read().decode("utf-8")).get("detail") or "")
        except ValueError:
            pass
        return exc.code, detail
    raise AssertionError(f"{path} phải trả lỗi nhưng lại thành công")


def seed_job() -> dict:
    work_dir = JOBS / JOB_ID
    work_dir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(SEED_VIDEO, work_dir / "source.mp4")
    cues = [
        {"start": 0.5, "end": 2.0, "text": "第一句", "trans": "câu một", "region": 0},
        {"start": 2.2, "end": 3.5, "text": "第二句在上方", "trans": "câu hai", "region": 1},
        {"start": 3.6, "end": 5.0, "text": "第三句 không dịch", "trans": "", "region": 0},
    ]
    job = {
        "id": JOB_ID,
        "filename": "seed_video.mp4",
        "status": "done",
        "cues": cues,
        "stats": {"duration": 6.0, "video_width": 720, "video_height": 1280, "rois": ROIS,
                  "cues": len(cues), "translated_cues": 2},
        "info": {"duration": 6.0, "width": 720, "height": 1280, "fps": 30.0, "codec": "h264"},
        "params": {"translate_to": "vi", "roi": ROIS, "langs": ["zh"]},
        "error": "",
        "message": "Hoàn tất: 3 cue phụ đề.",
        "created_at": time.time(),
        "preview_status": "none",
    }
    (work_dir / "job.json").write_text(json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8")
    return job


def start_stub():
    server = ThreadingHTTPServer(("127.0.0.1", STUB_PORT), StubHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def wait_health(timeout=90):
    end = time.time() + timeout
    while time.time() < end:
        try:
            with urllib.request.urlopen(BASE + "/api/health", timeout=2) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            time.sleep(0.4)
    return False


def unit_checks():
    from app.pipeline.post import build_cues
    from app import handoff

    groups = [
        {"start": 0.5, "end": 2.0, "region": 0, "conf": 0.9,
         "lines": [{"text": "第一句", "conf": 0.9}]},
        {"start": 2.2, "end": 3.5, "region": 1, "conf": 0.9,
         "lines": [{"text": "第二句", "conf": 0.9}]},
    ]
    cues = build_cues(groups, 6.0, min_conf=0.2)
    regions = [cue.get("region") for cue in cues]

    class FakeJob:
        id = "x"
        filename = "clip.mp4"
        video_path = Path("T:/videos/clip.mp4")
        info = {"duration": 6.0, "width": 720, "height": 1280}
        stats = {"rois": ROIS, "duration": 6.0, "video_width": 720, "video_height": 1280}
        params = {"translate_to": "vi"}
        cues = [{"start": 0.5, "end": 2.0, "text": "a", "trans": "b", "region": 1}]

    payload = handoff.build_payload(FakeJob())
    fake_empty = type("E", (FakeJob,), {"cues": []})
    try:
        handoff.build_payload(fake_empty())
        no_cue_error = ""
    except handoff.HandoffError as exc:
        no_cue_error = str(exc)
    try:
        handoff.resolve_url("http://192.168.1.20:8088")
        remote_host_error = ""
    except handoff.HandoffError as exc:
        remote_host_error = str(exc)

    return {
        "build_cues_regions": regions,
        "build_cues_kept_region": regions == [0, 1],
        "box_region_1": handoff._box_for(1, ROIS),
        "box_region_none": handoff._box_for(None, ROIS),
        "box_region_overflow": handoff._box_for(9, ROIS),
        "payload_box": payload["cues"][0]["box"],
        "payload_main_y": payload["main_y_pct"],
        "payload_lang": payload["target_lang"],
        "empty_job_message": no_cue_error,
        "remote_host_message": remote_host_error,
    }


def main() -> int:
    checks = {}
    details = {}
    shutil.rmtree(TMP, ignore_errors=True)
    JOBS.mkdir(parents=True)
    (TMP / "settings.json").write_text(json.dumps({"translate_model": "gemini-3.5-flash-lite"}),
                                       encoding="utf-8")
    seed = seed_job()
    units = unit_checks()
    details["units"] = units

    stub = start_stub()
    env = os.environ.copy()
    env.pop("GEMINI_API_KEY", None)
    env.pop("GEMINI_API_KEYS", None)
    env["SUBEXTRACT_DATA_DIR"] = str(JOBS)
    env["SUBEXTRACT_SETTINGS"] = str(TMP / "settings.json")
    log = open(TMP / "server.log", "wb")
    server = subprocess.Popen(
        [str(VENV), "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", PORT],
        cwd=str(ROOT), env=env, stdout=log, stderr=log,
    )
    try:
        checks["server_up"] = wait_health()
        if not checks["server_up"]:
            print("FAIL: SubExtract không khởi động được"); return 1

        # 1) Job mồi nạp từ đĩa, region còn nguyên
        loaded = api(f"/api/jobs/{JOB_ID}")
        checks["job_loaded"] = loaded["status"] == "done" and len(loaded["cues"]) == 3
        checks["region_survives_reload"] = [c.get("region") for c in loaded["cues"]] == [0, 1, 0]

        # 2) Cài đặt tool_v1_url: mặc định, lưu, chặn host ngoài vòng lặp
        settings = api("/api/settings")
        checks["url_default"] = settings.get("tool_v1_url") == "http://127.0.0.1:8088"
        checks["url_accepts_explicit"] = STUB in str(
            api("/api/settings", "PUT", {"tool_v1_url": STUB}).get("tool_v1_url"))
        saved = api("/api/settings")
        checks["url_roundtrip"] = saved.get("tool_v1_url") == STUB
        code, detail = expect_error("/api/settings", "PUT", {"tool_v1_url": "http://example.com:8088"})
        checks["url_blocks_remote"] = code == 400 and "vòng lặp" in detail

        # 3) Gửi handoff → payload có hộp theo ROI
        before = len(stub_state["imports"])
        sent = api(f"/api/jobs/{JOB_ID}/handoff", "POST", {"target_lang": "vi"})
        checks["send_ok"] = sent["ok"] and sent["handoff"]["remote_job_id"] == stub_state["job_id"]
        checks["send_reports_counts"] = (sent["handoff"]["cue_count"] == 3
                                        and sent["handoff"]["translated_count"] == 2)
        sent_box = [c["box"] for c in stub_state["imports"][-1]["cues"]]
        checks["import_region_boxes"] = (
            sent_box[0] == [0.0, 0.68, 1.0, 1.0] and sent_box[1] == [0.02, 0.05, 0.62, 0.17]
            and sent_box[2] == [0.0, 0.68, 1.0, 1.0]
        )
        imp = stub_state["imports"][-1]
        checks["import_metadata"] = (imp["stem"] == "seed_video" and imp["video_width"] == 720
                                     and imp["source_job_id"] == JOB_ID
                                     and imp["source_video"].endswith("source.mp4"))
        checks["import_main_y"] = abs(imp["main_y_pct"] - 0.84) < 1e-6
        checks["import_only_one_call"] = len(stub_state["imports"]) == before + 1
        checks["render_body_sent"] = stub_state["renders"][-1]["stem"] == "seed_video"

        # 4) Đang render thì không được gửi đè
        code, detail = expect_error(f"/api/jobs/{JOB_ID}/handoff", "POST", {"target_lang": "vi"})
        checks["guard_409_while_running"] = code == 409

        # 5) Tiến độ được cập nhật và lưu xuống job.json
        first = api(f"/api/jobs/{JOB_ID}/handoff")
        checks["poll_queued"] = first["handoff"]["status"] in {"queued", "running"}
        deadline = time.time() + 20
        last = first["handoff"]
        while time.time() < deadline:
            last = api(f"/api/jobs/{JOB_ID}/handoff")["handoff"]
            if last["status"] in {"done", "error", "cancelled"}:
                break
            time.sleep(1.0)
        checks["poll_reaches_done"] = last["status"] == "done"
        checks["poll_progress_100"] = int(last["progress"]) == 100
        checks["result_delivered"] = str((last.get("result") or {}).get("delivered") or "").endswith(
            "Dubbed_seed_video.mp4")
        on_disk = json.loads((JOBS / JOB_ID / "job.json").read_text(encoding="utf-8"))
        checks["status_persisted"] = (on_disk["stats"]["handoff"]["status"] == "done"
                                     and on_disk["stats"]["handoff"]["stem"] == "seed_video")

        # 6) Hủy render
        api(f"/api/jobs/{JOB_ID}/handoff", "POST", {"target_lang": "vi"})
        canceled = api(f"/api/jobs/{JOB_ID}/handoff/cancel", "POST", {})
        checks["cancel_called"] = stub_state["cancels"][-1]["job_id"] == stub_state["job_id"]
        checks["cancel_marks_cancelling"] = canceled["handoff"]["status"] == "cancelling"
        deadline = time.time() + 20
        final = canceled["handoff"]
        while time.time() < deadline:
            final = api(f"/api/jobs/{JOB_ID}/handoff")["handoff"]
            if final["status"] in {"cancelled", "error", "done"}:
                break
            time.sleep(1.0)
        checks["cancel_final_status"] = final["status"] == "cancelled"

        # 7) Job chưa chạy xong / chưa có cue
        code, detail = expect_error(f"/api/jobs/{JOB_ID}/handoff", "POST", {"target_lang": "xx"})
        checks["bad_lang_rejected"] = code == 400
        code, detail = expect_error("/api/jobs/khongtonotnai/handoff", "GET")
        checks["missing_job_404"] = code == 404

        # 7b) Cổng Tool V1 không ai nghe → 502 thân thiện, không ghi đè bản gửi đã lưu
        api("/api/settings", "PUT", {"tool_v1_url": "http://127.0.0.1:8799"})
        before_record = api(f"/api/jobs/{JOB_ID}/handoff")["handoff"]
        code, detail = expect_error(f"/api/jobs/{JOB_ID}/handoff", "POST", {"target_lang": "vi"})
        checks["unreachable_502"] = code == 502
        checks["unreachable_message"] = (str(detail).startswith("Không gọi được Tool V1 ở ")
                                        and "Dashboard Tool V1 chưa chạy?" in str(detail))
        after_record = api(f"/api/jobs/{JOB_ID}/handoff")["handoff"]
        checks["unreachable_keeps_record"] = (after_record["status"] == before_record["status"]
                                             and after_record["sent_at"] == before_record["sent_at"])
        checks["url_saved_back"] = api("/api/settings", "PUT", {"tool_v1_url": STUB})["tool_v1_url"] == STUB
        # 8) PUT /cues giữ lại region
        edited = [dict(c) for c in loaded["cues"]]
        edited[0]["trans"] = "câu một đã sửa"
        api(f"/api/jobs/{JOB_ID}/cues", "PUT", {"cues": edited})
        after = api(f"/api/jobs/{JOB_ID}")
        checks["put_cues_keeps_region"] = [c.get("region") for c in after["cues"]] == [0, 1, 0]
        checks["put_cues_keeps_text"] = after["cues"][0]["trans"] == "câu một đã sửa"

        checks["unit_build_cues_region"] = units["build_cues_kept_region"]
        checks["unit_box_out_of_range_falls_back"] = (units["box_region_overflow"]
                                                      == units["box_region_none"])
        checks["unit_payload_box_uses_region"] = units["payload_box"] == units["box_region_1"]
    finally:
        server.terminate()
        try:
            server.wait(timeout=15)
        except subprocess.TimeoutExpired:
            server.kill()
        stub.shutdown()
        stub.server_close()
        log.close()

    failed = [name for name, ok in checks.items() if not ok]
    print(json.dumps({"checks": checks, "details": details}, ensure_ascii=False, indent=2))
    if failed:
        print("FAILED:", ", ".join(failed))
        print((TMP / "server.log").read_text(encoding="utf-8", errors="replace")[-3000:])
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
