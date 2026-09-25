# L3B Architecture Record

Hệ thống Multi-Agent điều tra khiếu nại thương mại điện tử V2 (Day09 L3B).

## 1. System overview

Kiến trúc hệ thống bao gồm Coordinator trung tâm điều phối các Specialist Agents, Conflict Resolver và Verifier:

```text
Input (Case Data) ───► Coordinator ───► Entity Resolver ───► Specialist Agents (Shipment, Payment)
                           │                  │                               │
                           │                  ▼                               ▼
                           │             MCP Gateway ◄─────────────────── MCP Gateway
                           │                  │                               │
                           ▼                  ▼                               ▼
                      Conflict Resolver ──► Verifier ──────────────────► Final Output
                           │                  │
                           └────────────── Trace Writer ◄─────────────────────┘
```

Luồng thực thi:
1. **Case Received**: Coordinator nhận case, khởi tạo blackboard context.
2. **Entity Resolution**: Entity Resolver nhận dạng order candidates, xác định `customer_unique_id`, loại bỏ candidates sai lệch.
3. **Specialist Investigation**:
   - **Shipment Specialist**: Điều tra timeline vận chuyển, phát hiện seller giao trễ (`late_seller_ids`).
   - **Payment Specialist**: Đối soát thanh toán, xác định `captured_total_brl`, `refundable_total_brl`, đề xuất hoàn tiền.
4. **Conflict Resolution**: Phát hiện mâu thuẫn giữa thông tin khiếu nại của khách hàng và dữ liệu hệ thống/MCP, chọn nguồn có độ ưu tiên cao nhất theo policy.
5. **Verification & Invariants**: Verifier Agent kiểm tra tính nhất quán liên trường, cân chỉnh confidence, kiểm tra tính hợp lệ của evidence refs và xuất JSON output chuẩn schema.

---

## 2. Agent ownership

| Actor | Input | Trách nhiệm | Tool permission | Output/handoff |
| :--- | :--- | :--- | :--- | :--- |
| **Coordinator** | Case input payload | Điều phối vòng đời, phân chia task cho các agents, tổng hợp evidence và quản lý handoff | None (No direct MCP tool calls) | Handoff sang Entity Resolver & Specialists |
| **Entity Resolver** | `candidate_order_ids`, `claimed_order_id`, customer hints | Phân giải đơn hàng chính xác, loại bỏ candidate sai, tìm `customer_unique_id` | `get_customer_history`, `search_orders` | `entity_resolution`, `customer_context` |
| **Shipment Specialist** | `resolved_order_ids` | Phân tích tracking vận chuyển, tính toán trễ hạn, xác định `late_seller_ids` | `get_order_tracking`, `get_shipment_details` | `shipment_analysis`, `affected_entities` |
| **Payment Specialist** | `resolved_order_ids`, `claims` | Đối soát số tiền thanh toán, hoàn tiền, tính `recommended_refund_brl` | `get_payment_details`, `get_refund_history` | `payment_analysis`, `financial_resolution` |
| **Conflict Resolver** | Customer claims, Specialist findings | Phát hiện bất đồng dữ liệu, xếp hạng nguyên nhân gốc rễ (`ranked_causes`) | `get_platform_policy` | `data_conflicts`, `root_cause_analysis` |
| **Verifier** | Intermediate aggregated output | Kiểm tra schema, tính toán toán học hoàn tiền, đảm bảo cross-field consistency, calibrate confidence | None (Internal validation only) | Finalized `day09-l3b-output-v2` |

---

## 3. Entity resolution và A2A protocol

- **Candidate Evaluation**: So khớp `claimed_order_id` với danh sách `candidate_order_ids` và lịch sử khách hàng từ MCP. Nếu candidate có trạng thái khớp với khiếu nại, candidate đó được xếp vào `resolved_order_ids`; các candidates còn lại đưa vào `rejected_candidates`.
- **A2A Correlation**: Sử dụng `case_id` làm correlation ID duy nhất trong toàn bộ trace envelope và payload trung gian.
- **Loop Prevention**: Workflow thực thi theo dạng Directed Acyclic Graph (DAG) cố định theo pipeline 4 bước, không lặp đệ quy. Timeout mỗi bước tối đa 30s.

---

## 4. Evidence và conflict lifecycle

- **Evidence Collection**: Mọi công cụ MCP trả về structured content chứa `evidence_ref` định dạng `ev_[A-Za-z0-9_-]{20,96}`.
- **Provenance Logging**: Khi evidence được sử dụng, actor lập tức ghi nhận sự kiện `tool_result_consumed` vào trace log kèm `evidence_refs`.
- **Conflict Resolution Precedence**:
  1. MCP Gateway audit records (dữ liệu log thanh toán, tracking bưu cục thực tế).
  2. Platform Policy constants.
  3. Lời khai khiếu nại của khách hàng (Customer Claim).
- **Evidence Boundary**: Tuyệt đối không chia sẻ hoặc sử dụng lại `evidence_ref` giữa các `case_id` khác nhau.

---

## 5. Failure and efficiency policy

| Failure Scenario | Retry budget | Fallback Action | Trace Event / Code |
| :--- | ---: | :--- | :--- |
| **MCP Timeout / Network Error** | 1 retry (backoff 1s) | Ghi nhận `insufficient_evidence`, hạ confidence | `tool_call_failed` |
| **Entity Ambiguous / Not Found** | 0 retries | Đặt `status: ambiguous` hoặc `not_found`, ghi nhận `rejected_candidates` | `entity_unresolved` |
| **Data Conflict Discrepancy** | 0 retries | Ghi nhận vào `data_conflicts`, áp dụng Policy precedence | `conflict_detected` |
| **Invalid Specialist Result** | 0 retries | Verifier áp dụng giá trị mặc định an toàn | `verification_completed (FLAGGED)` |

**Efficiency & Tool Call Budget**:
- Mỗi case có giới hạn ngân sách gọi MCP tool (tối đa 3-5 calls/case).
- Caching evidence trong phạm vi phiên xử lý của từng case để tránh gọi trùng lặp các hàm get_customer_history hoặc get_order_tracking.

---

## 6. Verification invariants

1. **Schema Compliance**: Toàn bộ output phải khớp 100% với JSON Schema `l3b-output-v2.schema.json`.
2. **Disjoint Candidates**: `resolved_order_ids` ∩ `rejected_candidates` = ∅.
3. **Evidence Integrity**: Tất cả `evidence_refs` phải là chuỗi hợp lệ, unique, tối đa 30 items, có trong MCP audit log.
4. **Financial Math Consistency**:
   $$\text{recommended\_refund\_brl} = \sum \text{refund\_lines.amount\_brl}$$
   $\text{recommended\_refund\_brl} \le \text{captured\_total\_brl}$.
5. **Responsibility & Actions Consistency**:
   - Nếu `shipment_analysis.verdict == "seller_delay"`, `late_seller_ids` không được rỗng và `responsible_parties` phải chứa `seller`.
   - Nếu `case_status == "no_action"`, `recommended_refund_brl` = 0.0 và không có hành động phạt/hoàn tiền.
6. **Confidence Calibration**: Phản ánh chính xác mức độ đầy đủ của evidence (hạ confidence nếu thiếu bằng chứng hoặc có xung đột chưa rõ).

---

## 7. Reproducibility

- **Python Runtime**: Python 3.11+.
- **Dependencies**: `jsonschema>=4.20.0`, `referencing>=0.30.0`, `httpx2`, `mcp`.
- **Determinism**: Toàn bộ quy trình mapping, invariant checks và scoring sử dụng quy tắc xác định (deterministic rules), không phụ thuộc vào random seeds ngẫu nhiên.
