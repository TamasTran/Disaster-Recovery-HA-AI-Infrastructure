"""BƯỚC 3a — SINH VIÊN VIẾT. Health checker cho 2 region.

Yêu cầu (đọc §4 "Kiến Trúc Health-Check-Based Failover" + §2 "DNS Failover"):
  1. Poll /readyz của CẢ HAI region mỗi `interval` giây (mặc định 5s).
     Dùng /readyz, KHÔNG dùng /healthz. /healthz chỉ nói "process còn sống" —
     region có process sống nhưng vector DB rỗng thì vẫn không serve được.
  2. Chỉ đổi trạng thái sau `threshold` lần fail LIÊN TIẾP (mặc định 3).
     Một lần fail không phải outage. Đây là chống flapping (§4 Anti-Patterns).
  3. Ghi 1 dòng JSONL MỖI LẦN ĐỔI TRẠNG THÁI (không ghi mỗi lần poll — log sẽ ngập).
     Dòng bắt buộc có: ts, region, to (HEALTHY|UNHEALTHY), reason,
     interval_s, threshold. Thiếu interval_s/threshold thì tools/measure_rto.py
     không tính được detect floor -> mất điểm.

Chạy:  python dr/health_checker.py --interval 5 --threshold 3 --duration 300 \
              --out reports/health-events.jsonl

CÂU HỎI PHẢI TRẢ LỜI TRƯỚC KHI VIẾT (ghi câu trả lời vào reports/postmortem.md):
  interval=5s, threshold=3 -> sớm nhất bạn có thể phát hiện outage là bao nhiêu giây?
  Con số đó nằm TRONG RTO của bạn. Muốn RTO 5 phút thì được phép chọn interval bao nhiêu?
"""
import argparse
import json
import pathlib
import time

import httpx

URL = {"a": "http://127.0.0.1:8001", "b": "http://127.0.0.1:8002"}


def probe(region: str, timeout: float) -> tuple[bool, str]:
    """Trả về (ready, reason). Timeout PHẢI có — netblock làm request treo mãi."""
    try:
        resp = httpx.get(
            f"{URL[region]}/readyz",
            timeout=timeout,
        )
        if resp.status_code == 200:
            return True, "OK"
        else:
            return False, f"Status {resp.status_code}"
    except Exception as e:
        return False, str(e)


def run(interval: float, timeout: float, threshold: int, duration: float, out: pathlib.Path):
    """Vòng lặp poll + phát hiện transition + ghi JSONL."""
    state = {"a": "HEALTHY", "b": "HEALTHY"}
    # consecutive_failures chỉ đếm khi ĐANG HEALTHY → detect ngưỡng UNHEALTHY
    consecutive_failures = {"a": 0, "b": 0}

    start_time = time.time()
    out.parent.mkdir(parents=True, exist_ok=True)

    with open(out, "w") as f:

        def emit_state_change(region, to, reason, consec=0):
            row = {
                "ts": time.time(),
                "event": "state_change",
                "region": region,
                "to": to,
                "reason": reason,
                "interval_s": interval,
                "threshold": threshold,
                "consecutive_fails": consec,
            }
            f.write(json.dumps(row) + "\n")
            f.flush()

        while time.time() - start_time < duration:
            for region in ["a", "b"]:
                ready, reason = probe(region, timeout)

                if state[region] == "HEALTHY":
                    if not ready:
                        # Tích lũy consecutive failures
                        consecutive_failures[region] += 1
                        if consecutive_failures[region] >= threshold:
                            # Đủ threshold → flip sang UNHEALTHY
                            consec = consecutive_failures[region]
                            state[region] = "UNHEALTHY"
                            consecutive_failures[region] = 0
                            emit_state_change(region, "UNHEALTHY", reason, consec=consec)
                    else:
                        # Healthy → reset counter
                        consecutive_failures[region] = 0

                else:  # state == "UNHEALTHY"
                    if ready:
                        # Đã recover → flip sang HEALTHY
                        state[region] = "HEALTHY"
                        consecutive_failures[region] = 0
                        emit_state_change(region, "HEALTHY", reason)
                    # Nếu vẫn fail khi đang UNHEALTHY → không làm gì (đã ghi rồi)

            time.sleep(interval)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--interval", type=float, default=5.0)
    p.add_argument("--timeout", type=float, default=2.0)
    p.add_argument("--threshold", type=int, default=3)
    p.add_argument("--duration", type=float, default=300)
    p.add_argument("--out", default="reports/health-events.jsonl")
    a = p.parse_args()
    run(a.interval, a.timeout, a.threshold, a.duration, pathlib.Path(a.out))
