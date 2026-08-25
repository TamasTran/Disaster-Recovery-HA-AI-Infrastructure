# Postmortem — DR Drill Lab 23

Theo đúng template §4 "Sau Failover: Blameless Postmortem". Blameless: câu hỏi là
"hệ thống/process nào cho phép chuyện này", không phải "ai làm sai".

## 1. Timeline (mọi dòng phải có evidence path:line)

| ISO time | Sự kiện | Evidence |
|---|---|---|
| 2026-08-25T19:32:24 | outage bắt đầu (SIGSTOP Region A) | `chaos/chaos-events.jsonl:3` |
| 2026-08-25T19:32:24 | user đầu tiên bị ảnh hưởng (+0.0s) | `reports/drill-2-withdr.jsonl:31` |
| 2026-08-25T19:32:40 | health check alert Region A UNHEALTHY (+16.5s) | `reports/health-events.jsonl:2` |
| 2026-08-25T19:32:53 | operator confirm cutover (runbook --auto) (+29.1s) | `reports/runbook-run.jsonl` |
| 2026-08-25T19:32:53 | snapshot restore hoàn tất, RPO=20.0s, 10 docs lost | `reports/failover-events.jsonl:2` |
| 2026-08-25T19:32:59 | Region B ready, DNS cutover (+35.7s) | `reports/failover-events.jsonl:5` |
| 2026-08-25T19:33:02 | resolved — request đầu tiên OK từ Region B (+38.8s) | `reports/drill-2-withdr.jsonl:50` |

## 2. RTO/RPO đo được vs mục tiêu — gap ở bước nào?

- RTO mục tiêu: 300s · đo được: `38.8s` · gap: `261.2s` dưới mục tiêu → PASS
- RPO mục tiêu: 300s · đo được: `20.0s` (`10` doc bị mất) · gap: `280.0s` dưới mục tiêu → PASS
- **Bước tốn nhiều giây nhất:** Health-check detect floor (16.5s = 42.5% tổng RTO) — vì interval=5s × threshold=3 tạo detection floor tối thiểu 15s. Phase offset giữa outage và poll cycle tiếp theo thêm 1.5s.

## 3. Root cause (5 whys)

1. **Tại sao user thấy lỗi?** Vì Region A bị SIGSTOP (network partition), request timeout.
2. **Tại sao không tự phục hồi?** Vì trước khi implement DR, không có health checker nào monitor `/readyz`, không có failover automation, và Region B hoàn toàn trống (count=0, weights=false).
3. **Tại sao Region B trống?** Vì không có replication nào chạy — data chỉ tồn tại ở Region A duy nhất (single point of failure).
4. **Tại sao không ai phát hiện?** Vì `/healthz` chỉ kiểm tra "process còn sống" — SIGSTOP giữ process sống nhưng không phản hồi. Cần `/readyz` (kiểm tra pool + weights + vectors) thay vì `/healthz`.
5. **Tại sao flip DNS không đủ?** Vì DNS chỉ là routing layer — nếu target region không có data/weights/pool ready, user nhận `region_not_ready` (503). Cần restore state TRƯỚC khi cutover.

Nếu đây là outage thật: bước `2_restore_snapshot` sẽ thất bại nếu chưa từng có `replicate.py` chạy (không có MANIFEST.json). Replication phải là background job liên tục, không phải thứ chạy tay khi outage đã xảy ra.

## 4. Action items (có owner + deadline)

| # | Action | Owner | Deadline | Giảm RTO/RPO bao nhiêu giây |
|---|---|---|---|---|
| 1 | Chạy `state/replicate.py` như cron job mỗi 30s liên tục (không chỉ khi drill) | SRE team | Sprint 1 | RPO giảm từ 20s xuống ≤30s (bounded) |
| 2 | Giảm health check interval xuống 2s (detect floor = 6s thay vì 15s) | Platform team | Sprint 1 | RTO giảm ~10s (16.5→6.5) |
| 3 | Pre-warm Region B (pool_state=warm + model weights sẵn sàng) | Infra team | Sprint 2 | RTO giảm ~6.4s (bỏ GPU warm-up) |
| 4 | Giảm EDGE_TTL_SECONDS từ 5s xuống 1s | Network team | Sprint 1 | RTO giảm ~2s (DNS TTL) |

## 5. Ba câu hỏi bắt buộc trả lời

1. `interval × threshold` = 5 × 3 = **15s** (detection floor lý thuyết). Thực tế detect ở 16.5s. Chiếm **16.5/38.8 = 42.5%** tổng RTO — thành phần lớn nhất.

2. Nếu hạ interval xuống 1s: detect floor = 1 × 3 = **3s**, RTO giảm ~13s (từ 38.8 xuống ~25.8). **Cái giá**: tăng 5× số lượng HTTP request vào `/readyz` mỗi giây (từ 0.4 req/s lên 2 req/s mỗi region). Nếu network chập chờn, dễ trigger false alarm → failover không cần thiết → flapping giữa 2 region (§4 Anti-Pattern: "mỗi lần flip tạo thêm downtime cho user").

3. Nếu outage 6 giờ và Region A mất dữ liệu vĩnh viễn: `docs_lost = 10` nghĩa là **10 document** mà khách hàng đã upload nhưng chưa được replicate sang Region B sẽ biến mất hoàn toàn. Khách hàng sẽ tìm kiếm/query các document đó và không thấy kết quả — đây là data loss thật, không phải downtime tạm. Cần thông báo khách hàng bị ảnh hưởng và yêu cầu re-upload nếu có thể.
