"""BƯỚC 3c — SINH VIÊN VIẾT. Tự động hoá runbook §4 "Runbook: Region Chính Down".

7 bước trên slide, mỗi bước 1 dòng log có ts. Log này CHÍNH LÀ timeline của postmortem.
  1 xac_nhan_outage          — probe cả 2 region, đừng tin 1 lần fail (dùng nhiều lần
                              hoặc gọi health_checker.probe nếu đã viết xong 3a)
  2 thong_bao_incident       — ts của dòng này là mốc "operator biết tin", LUÔN LUÔN
                              SAU t_outage trong chaos-events (không thể trùng — operator
                              không thể biết ngay giây outage xảy ra). Ghi cả 2 ts vào
                              log để postmortem tính được "độ trễ thông báo".
  3 scale_gpu_pool           — gọi HÀM `failover.failover(...)` MỘT LẦN DUY NHẤT. Hàm
                              đó tự làm đủ 5 bước con (verify/restore/scale/wait/cutover)
                              và tự ghi log riêng vào reports/failover-events.jsonl.
  4 verify_state_replica     — KHÔNG gọi lại failover — chỉ ĐỌC kết quả (vector count +
                              weights ở region phụ) từ dict mà bước 3 trả về, để log vào
                              runbook-run.jsonl cho postmortem đọc 1 chỗ duy nhất.
  5 dns_cutover              — cũng chỉ đọc lại: kết quả cutover có ok hay không.
  6 verify_golden_signals    — 10 request thật vào region phụ: p95 latency + error rate
  7 post_incident            — elapsed_s + lệnh đo RTO

BÁN TỰ ĐỘNG, KHÔNG FULL-AUTO (§4: "failover đầu tiên nên là bán tự động — alert +
1-click confirm — tránh flapping gây failover 2 chiều liên tục"). Mặc định phải hỏi
người vận hành confirm; --auto chỉ dùng trong CI/khi chấm điểm.

Chạy:  python dr/runbook.py --primary a --target b --backend fs
"""
import argparse
import json
import pathlib
import sys
import time

import httpx

sys.path.insert(0, ".")
from dr import failover as fo        # noqa: E402
from dr.health_checker import probe  # noqa: E402

LOG = pathlib.Path("reports/runbook-run.jsonl")
URL = {"a": "http://127.0.0.1:8001", "b": "http://127.0.0.1:8002"}


def step(n, name, **kw):
    """Ghi 1 dòng {ts, iso, step, name, ...} vào LOG và in ra stdout."""
    import datetime
    now = datetime.datetime.now(datetime.timezone.utc)
    row = {
        "ts": now.timestamp(),
        "iso": now.isoformat().replace("+00:00", "Z"),
        "step": f"{n}_{name}",
        **kw,
    }
    print(json.dumps(row))
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG, "a") as f:
        f.write(json.dumps(row) + "\n")
    return row


def confirm(auto: bool, msg: str) -> bool:
    """auto=True → True ngay; ngược lại hỏi y/N người vận hành."""
    if auto:
        return True
    ans = input(f"\n{msg} [y/N]: ").strip().lower()
    return ans == "y"


def run(primary: str, target: str, backend: str, auto: bool) -> dict:
    """7 bước runbook."""
    t_start = time.time()

    # ── Bước 1: xác nhận outage ───────────────────────────────────────────
    # Probe nhiều lần để tránh false alarm
    PROBES = 3
    primary_fails = 0
    for _ in range(PROBES):
        ok, reason = probe(primary, timeout=2.0)
        if not ok:
            primary_fails += 1
        time.sleep(0.5)

    target_ok, target_reason = probe(target, timeout=2.0)

    step(1, "xac_nhan_outage",
         primary=primary,
         primary_fail_count=primary_fails,
         primary_fail_threshold=PROBES,
         primary_down=(primary_fails >= PROBES),
         target=target,
         target_ok=target_ok,
         target_reason=target_reason)

    if primary_fails < PROBES:
        step(1, "xac_nhan_outage_abort",
             reason=f"Region {primary} chỉ fail {primary_fails}/{PROBES} lần — không phải outage thật")
        return {"ok": False, "reason": "false alarm — primary vẫn ổn"}

    # ── Bước 2: thông báo incident ────────────────────────────────────────
    step(2, "thong_bao_incident",
         message=f"INCIDENT: Region {primary} DOWN, bắt đầu failover sang {target}",
         t_detection=time.time(),
         t_start_runbook=t_start)

    if not confirm(auto, f"Xác nhận failover từ Region {primary} sang Region {target}?"):
        step(2, "thong_bao_incident_abort", reason="Operator từ chối failover")
        return {"ok": False, "reason": "operator cancelled"}

    # ── Bước 3: scale GPU pool / gọi failover ────────────────────────────
    step(3, "scale_gpu_pool_start",
         primary=primary,
         target=target,
         backend=backend)

    fo_result = fo.failover(target=target, backend=backend, wait=60.0)

    step(3, "scale_gpu_pool_done",
         failover_ok=fo_result.get("ok"),
         active_region=fo_result.get("active_region"),
         rpo_seconds=fo_result.get("rpo_seconds"),
         docs_lost=fo_result.get("docs_lost"))

    if not fo_result.get("ok"):
        step(3, "scale_gpu_pool_abort",
             reason=fo_result.get("reason", "failover failed"))
        return {"ok": False, "reason": "failover failed", **fo_result}

    # ── Bước 4: verify state replica ─────────────────────────────────────
    # Chỉ ĐỌC kết quả từ fo_result, KHÔNG gọi lại failover
    step(4, "verify_state_replica",
         region=target,
         embed_model_version=fo_result.get("embed_model_version"),
         rpo_seconds=fo_result.get("rpo_seconds"),
         docs_lost=fo_result.get("docs_lost"))

    # ── Bước 5: dns_cutover (đọc kết quả) ────────────────────────────────
    step(5, "dns_cutover",
         active_region=fo_result.get("active_region"),
         cutover_ok=fo_result.get("ok"))

    # ── Bước 6: verify golden signals ────────────────────────────────────
    GOLDEN_N = 10
    latencies = []
    errors = 0
    for _ in range(GOLDEN_N):
        t0 = time.time()
        try:
            r = httpx.get(f"http://127.0.0.1:8080/v1/infer", timeout=5.0)
            latencies.append(time.time() - t0)
            if r.status_code != 200:
                errors += 1
        except Exception:
            errors += 1
            latencies.append(5.0)
        time.sleep(0.2)

    latencies_sorted = sorted(latencies)
    p95 = latencies_sorted[int(len(latencies_sorted) * 0.95)] if latencies_sorted else None
    error_rate = errors / GOLDEN_N

    step(6, "verify_golden_signals",
         requests=GOLDEN_N,
         errors=errors,
         error_rate=round(error_rate, 3),
         p95_latency_s=round(p95, 3) if p95 else None)

    # ── Bước 7: post incident summary ────────────────────────────────────
    elapsed = time.time() - t_start
    step(7, "post_incident",
         elapsed_s=round(elapsed, 2),
         measure_rto_cmd="python3 tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl --target-rto 300",
         active_region=fo_result.get("active_region"),
         rpo_seconds=fo_result.get("rpo_seconds"),
         docs_lost=fo_result.get("docs_lost"))

    return {
        "ok": True,
        "active_region": fo_result.get("active_region"),
        "elapsed_s": round(elapsed, 2),
        "error_rate": round(error_rate, 3),
        "p95_latency_s": round(p95, 3) if p95 else None,
        "rpo_seconds": fo_result.get("rpo_seconds"),
        "docs_lost": fo_result.get("docs_lost"),
    }


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--primary", default="a")
    p.add_argument("--target", default="b")
    p.add_argument("--backend", default="fs", choices=["fs", "minio"])
    p.add_argument("--auto", action="store_true")
    a = p.parse_args()
    print(json.dumps(run(a.primary, a.target, a.backend, a.auto), indent=2))
