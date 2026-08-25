# RTO/RPO Evidence — Lab 23

Quy tắc duy nhất: mỗi con số ở đây phải trỏ được về **một dòng log thật**
(`đường/dẫn.jsonl:số_dòng`). `pytest tests/test_rto_evidence.py` sẽ mở từng file ra kiểm tra.
Con số không có evidence = trượt, bất kể các phần khác.

## 1. Drill 1 — không có DR (baseline)

| Chỉ số | Giá trị | Cách đo | Evidence |
|---|---|---|---|
| t_outage | `2026-08-25T19:30:51` | chaos kill | `chaos/chaos-events.jsonl:1` |
| Request fail đầu tiên | `+0.4s` | dòng `ok:false` đầu tiên sau t_outage | `reports/drill-1-nodr.jsonl:18` |
| Request thành công sau đó | không có | không có dòng `ok:true` nào sau t_outage | `reports/drill-1-nodr.jsonl` |
| RTO | `NO_RECOVERY` | `tools/measure_rto.py` | 16/33 requests failed |

## 2. Drill 2 — có DR

| Mốc | +giây từ t_outage | Cách đo | Evidence |
|---|---|---|---|
| t_outage (mốc 0) | 0 | `action:kill` | `chaos/chaos-events.jsonl:3` |
| User thấy lỗi đầu tiên | +0.0s | dòng `ok:false` đầu | `reports/drill-2-withdr.jsonl:31` |
| Health check phát hiện | +16.5s | `to:UNHEALTHY, region:a` | `reports/health-events.jsonl:2` |
| Snapshot restore xong | +29.3s | `step:2_restore_snapshot` | `reports/failover-events.jsonl:2` |
| Region phụ ready | +35.7s | `step:4_wait_ready` | `reports/failover-events.jsonl:4` |
| DNS cutover | +35.7s | `step:5_dns_cutover` | `reports/failover-events.jsonl:5` |
| **RTO đo được** | **+38.8s** | dòng `ok:true` đầu sau lỗi, `served_by:b` | `reports/drill-2-withdr.jsonl:50` |

| Chỉ số | Đo được | Mục tiêu (slide §1) | Verdict |
|---|---|---|---|
| RTO — Inference API | `38.8s` | 300s (5 phút) | PASS |
| RPO — Vector DB | `20.0s` / `10` doc | 300s (5 phút) | PASS |

## 3. RTO của tôi gồm những gì (bắt buộc — đây là phần chấm điểm hiểu bài)

| Thành phần | Giây | Nó đến từ đâu | Giảm được bằng cách nào |
|---|---|---|---|
| Health-check detect floor | 16.5s | `interval_s × threshold` = 5 × 3 = 15s lý thuyết, thực tế 16.5s do phase offset. `reports/health-events.jsonl:2` | Giảm interval xuống 2s (floor = 6s) nhưng tăng risk flapping; hoặc giảm threshold xuống 2 nhưng tăng false positive |
| Snapshot restore + runbook startup | 12.8s | Từ health detect đến 3_scale_pool: gồm runbook confirm (3 probe × 2s) + snapshot.get() copy DB + weights. `reports/failover-events.jsonl:2` → `reports/failover-events.jsonl:3` | Tự động trigger failover ngay khi detect (bỏ confirm) — nhưng slide §4 cảnh báo full-auto gây flapping |
| GPU pool warm-up | 6.4s | Từ `3_scale_pool` đến `4_wait_ready`: WARMUP_SECONDS mô phỏng GPU pool initialization. `reports/failover-events.jsonl:3` → `reports/failover-events.jsonl:4` | Pre-warm Region B (giữ pool ở warm thay vì cold) — tốn tài nguyên nhưng giảm RTO |
| DNS/LB TTL cache | 3.1s | Từ `5_dns_cutover` đến request thành công đầu: edge cache active_region trong EDGE_TTL_SECONDS. `reports/failover-events.jsonl:5` → `reports/drill-2-withdr.jsonl:50` | Giảm EDGE_TTL_SECONDS — nhưng tăng tải đọc file mỗi request |
| **Tổng** | **38.8s** | | |
