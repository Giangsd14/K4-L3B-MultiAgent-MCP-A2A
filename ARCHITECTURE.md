# L3B Architecture Record

Tài liệu thiết kế kiến trúc hệ thống Multi-Agent điều tra khiếu nại thương mại điện tử (K4 L3B - Multi-Agent MCP + A2A).

## 1. System overview

Luồng xử lý từ input case, candidate resolution, MCP investigation, specialist agents, policy & conflict synthesis, verifier, output và trace:

```text
Input Case (JSON)
       │
       ▼
[Coordinator] ──(task_assigned)──► [Entity Resolver] ──(MCP: get_customer_history, get_order)
       │                                     │
       │                                 (handoff)
       ▼                                     ▼
[Specialist Agents] ◄────────────────────────┘
  ├── [Order Agent]    ──(MCP: get_order_items, get_product_context; get_sellers khi cần)
  ├── [Shipment Agent] ──(MCP: get_shipment_summary)
  └── [Payment Agent]  ──(MCP: get_payment_timeline, get_refund_timeline)
       │
   (handoff)
       ▼
[Temporal Fact Normalizer] ──(purchase/approval/delivery anchored record cohorts)
       │
       ▼
[Decision Engine] ──(MCP: get_policy; deterministic rules over normalized facts)
       │
   (handoff)
       ▼
[Verifier Agent] ──(Schema, source-level semantics & invariants)
       │
   (case_finalized)
       ▼
Output JSON (outputs/<case_id>.json) & Trace (traces/trace.jsonl)
```

## 2. Agent ownership

| Actor | Input | Trách nhiệm | Tool permission | Output/handoff |
| --- | --- | --- | --- | --- |
| `coordinator` | `case.json` | Khởi tạo phiên điều tra, phân bổ task, điều phối vòng đời workflow và finalize case | Không gọi trực tiếp MCP data tool | Giao việc cho `entity_agent`, quản lý trace lifecycle |
| `entity_agent` | `candidate_order_ids`, `customer_unique_id_hint` | Đối chiếu customer history, kiểm chứng order, giữ trạng thái ambiguous khi có nhiều order hợp lệ | `get_customer_history`, `get_order` | `resolved_order_ids`, `rejected_candidates`, handoff sang specialist hoặc policy |
| `order_agent` | `resolved_order_id`, `investigation_scope` | Trích xuất items và product context; chỉ tra seller khi cần xác định trách nhiệm hoặc items thiếu seller ID | `get_order_items`, `get_product_context`, `get_sellers` | `item_ids`, `seller_ids`, handoff sang `shipment_agent` |
| `shipment_agent` | `resolved_order_id` | Phân tích mốc thời gian giao hàng, trễ hạn seller vs logistics, xác định trách nhiệm chậm trễ | `get_shipment_summary` | `shipment_analysis` (verdict, late_seller_ids, timeline_complete), handoff sang `payment_agent` |
| `payment_agent` | `resolved_order_id` | Phân tích dòng tiền, sự kiện capture, đối soát thanh toán, và vòng đời hoàn tiền | `get_payment_timeline`, `get_refund_timeline` | `payment_analysis` (verdict, captured_total_brl, refunded_total_brl), handoff sang `policy_agent` |
| `policy_agent` | `policy_version`, các fact đã chuẩn hoá | Xác định issue từ bằng chứng, áp dụng rule của `EC_POLICY_V2`, tính số tiền dựa trên payment/refund đã xác minh | `get_policy` | `policy_decided`, root cause, financial resolution, handoff sang `verifier` |
| `verifier` | Output và evidence registry của case | Kiểm tra schema, quyền sở hữu evidence refs, quan hệ thực thể và cân bằng refund | Không gọi tool | `verification_completed` (status: passed) |

Áp dụng nguyên tắc Least Privilege: mỗi agent chỉ có quyền gọi các MCP tool thuộc phạm vi trách nhiệm của mình.

## 3. Entity resolution và A2A protocol

1. **Candidate Validation & Filtering**:
   - Lấy `get_customer_history` trước để loại candidate không thuộc khách hàng mà không cần `get_order` riêng cho từng candidate.
   - Gọi `get_order` cho claimed order và các candidate còn có khả năng đúng. Nếu nhiều order hợp lệ, giữ `ambiguous` thay vì chọn order đầu tiên.
2. **Confidence Scoring**:
   - Entity confidence là `1.0` khi claimed order được cả order record và customer history xác nhận; giảm khi chỉ có một nguồn hoặc không thể resolve.
   - Assessment confidence phụ thuộc loại bằng chứng và độ đầy đủ của policy; không cố định ở `0.95`.
3. **Correlation & Message Envelope**:
   - Mọi message và trace event đều gắn `case_id` và `event_id` theo chuẩn `evt_[A-Za-z0-9_-]{12,96}`.
   - Luồng handoff tuần tự, xác định rõ actor nguồn và actor đích, không tạo vòng lặp.

## 4. Evidence và conflict lifecycle

1. **Validation & Registration**:
   - `EvidenceGateway` kiểm tra phản hồi theo schema `day09-mcp-evidence-v1`; `CaseEvidence` lưu kết quả theo `(tool, arguments)` trong phạm vi case, gồm cả lookup thất bại.
   - Chỉ dùng `evidence_ref` do MCP trả về. Claim assessment chọn refs theo domain của claim.
2. **Consumption Tracking**:
   - Mỗi lần agent sử dụng dữ liệu từ tool, một event `tool_result_consumed` được phát ra trong trace gắn kèm `evidence_refs` tương ứng.
3. **Data Conflict Resolution**:
   - Candidate không tồn tại chỉ được đưa vào `rejected_candidates`. Ghi `data_conflicts` khi hai nguồn đã quan sát bất đồng về chủ sở hữu, claimed order hoặc order status; không coi một candidate bị loại là xung đột nguồn.
   - Đánh giá từng claim từ fact tương ứng, độc lập với thứ tự ưu tiên của `primary_issue`. Các issue có bằng chứng nhưng không được chọn làm issue chính được ghi vào `secondary_issues`.
   - Tách payment rows theo chuỗi `payment_sequential`: chuỗi mới bắt đầu khi sequence quay lại `1`. Ghép cohort đầu với các capture events; giữ raw timeline riêng để xác minh capture lặp.
   - Đối chiếu sự kiện shipment với ngày mua và ngày giao thật. Một `delivered_late` không thể thuộc đơn đã hủy/chưa giao hoặc xảy ra trước khi mua.
   - Gắn refund event với capture cohort tương ứng; sự kiện hoàn tiền gắn với khoản capture chỉ có ở cohort khác không được dùng để kết luận claim hiện tại.
   - Chỉ kết luận reconciliation mismatch khi payment timeline có sự kiện authoritative tương ứng. Duplicate capture được nhận diện từ sự kiện authoritative hoặc các payment rows trùng đầy đủ sequence, method, installments và amount. Giá hàng cộng phí vận chuyển không được xem là mốc đối soát thanh toán nếu contract nguồn chưa xác nhận cùng cơ sở tính.

## 5. Failure and efficiency policy

| Failure | Retry budget | Fallback | Trace event/code |
| --- | ---: | --- | --- |
| MCP timeout / network error | 0 retries trong cùng case | Ghi nhận thiếu bằng chứng; không chuyển thành verdict chắc chắn | Không tạo event giả |
| Entity not found / candidate invalid | 0 retries | Đưa vào `rejected_candidates`; giữ `ambiguous` hoặc `not_found` theo số ứng viên hợp lệ | Handoff có `entity_status` |
| Refund timeline không có bản ghi | 0 retries | Phân biệt `not_found`, suy ra vắng bản ghi từ lỗi ứng dụng + payment timeline, và lỗi kết nối; giảm confidence khi phải suy ra | Không tạo evidence ref giả |

* **Query Budget & Efficiency**:
  - Cache cả kết quả thành công và thất bại trong một case; các request đồng thời cùng key dùng chung một call.
  - Customer history giúp tránh gọi order không liên quan; seller/refund chỉ được tra khi case cần.
  - Số call thực tế cần được đo bằng MCP audit. Không giả định private call budget.

## 6. Verification invariants

Trước khi finalize case output, Verifier Agent kiểm tra các bất biến sau:
1. **Schema Invariant**: Output tuân thủ 100% `day09-l3b-output-v2.schema.json`.
2. **Entity Scope**: Ưu tiên ID có trong dữ liệu MCP. Nếu payment row hoặc shipment summary không có ID riêng, tạo tham chiếu nội bộ ổn định `pay-{n}` hoặc `ship-{order-prefix}` cho bản ghi đã quan sát; không tạo tham chiếu khi thiếu bản ghi.
3. **Rejected Candidates**: Hợp của `resolved_order_ids` và `rejected_candidates` bao phủ toàn bộ `candidate_order_ids`.
4. **Evidence Ownership**: Mọi `evidence_ref` trong output và từng claim phải thuộc registry của case hiện tại.
5. **Financial Balance**: Tổng `refund_lines` bằng `recommended_refund_brl`, và đề xuất không vượt `refundable_total_brl` khi con số này xác định được.
6. **Policy Consistency**: `case_status` và `resolution_actions` khớp rule MCP của `primary_issue` khi rule tồn tại.
7. **Calibration Bounds**: `confidence` phải nằm trong khoảng $[0.0, 1.0]$; cần hiệu chỉnh tiếp bằng nhãn đánh giá nếu có.
8. **Independent Semantic Check**: Verifier đối chiếu các verdict tác động lớn với tín hiệu gốc trong timeline; ví dụ `capture_mismatch` phải có sự kiện `reconciliation_mismatch`, còn `duplicate_capture` phải có sự kiện explicit hoặc các payment rows trùng fingerprint.

## 7. Reproducibility

- **Decision engine**: deterministic rules trên fact từ MCP. `LLMClient` không tham gia luồng hiện tại.
- **Evidence journal**: `run --record-evidence` lưu full MCP envelope và lỗi theo case, ngoài gói nộp. `replay` dùng lại chính bằng chứng đó để kiểm thử thay đổi decision/verifier mà không tạo thêm MCP call hay dùng evidence ref mới. Replay thiếu call sẽ dừng, không âm thầm coi như bằng chứng vắng mặt.
- **Transactional run**: Output và trace được ghi vào thư mục tạm rồi mới công bố sau khi toàn bộ case chạy thành công. Nếu MCP không trả bằng chứng dùng được, run dừng sớm và giữ nguyên artifact trước đó.
- **Python Version**: Python 3.11+.
- **Dependencies**: `httpx2>=2,<3`, `mcp>=2,<3`, `jsonschema>=4.25`, `python-dotenv>=1.1`.
- **Lệnh chạy toàn bộ**:
  ```bash
  day09 run
  day09 validate
  day09 package --output dist/submission.zip
  ```
