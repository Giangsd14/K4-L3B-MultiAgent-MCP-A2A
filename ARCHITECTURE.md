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
  ├── [Order Agent]    ──(MCP: get_order_items, get_product_context, get_sellers)
  ├── [Shipment Agent] ──(MCP: get_shipment_summary)
  └── [Payment Agent]  ──(MCP: get_payment_timeline, get_refund_timeline)
       │
   (handoff)
       ▼
[Policy & Conflict Agent] ──(MCP: get_policy, LLM: gpt-4o-mini reasoning)
       │
   (handoff)
       ▼
[Verifier Agent] ──(Schema & Invariants Validation)
       │
   (case_finalized)
       ▼
Output JSON (outputs/<case_id>.json) & Trace (traces/trace.jsonl)
```

## 2. Agent ownership

| Actor | Input | Trách nhiệm | Tool permission | Output/handoff |
| --- | --- | --- | --- | --- |
| `coordinator` | `case.json` | Khởi tạo phiên điều tra, phân bổ task, điều phối vòng đời workflow và finalize case | Không gọi trực tiếp MCP data tool | Giao việc cho `entity_agent`, quản lý trace lifecycle |
| `entity_agent` | `candidate_order_ids`, `customer_unique_id_hint` | Xác thực candidate orders, trích xuất lịch sử khách hàng, loại bỏ candidate giả lập/sai | `get_customer_history`, `get_order` | `resolved_order_ids`, `rejected_candidates`, handoff sang `order_agent` |
| `order_agent` | `resolved_order_id`, `investigation_scope` | Trích xuất items, sellers, và bối cảnh phân loại sản phẩm | `get_order_items`, `get_product_context`, `get_sellers` | `item_ids`, `seller_ids`, category context, handoff sang `shipment_agent` |
| `shipment_agent` | `resolved_order_id` | Phân tích mốc thời gian giao hàng, trễ hạn seller vs logistics, xác định trách nhiệm chậm trễ | `get_shipment_summary` | `shipment_analysis` (verdict, late_seller_ids, timeline_complete), handoff sang `payment_agent` |
| `payment_agent` | `resolved_order_id` | Phân tích dòng tiền, sự kiện capture, đối soát thanh toán, và vòng đời hoàn tiền | `get_payment_timeline`, `get_refund_timeline` | `payment_analysis` (verdict, captured_total_brl, refunded_total_brl), handoff sang `policy_agent` |
| `policy_agent` | `policy_version`, toàn bộ evidence đã thu thập | Đối chiếu quy định chính sách (`EC_POLICY_V2`), xác định primary issue, case status, phân bổ trách nhiệm và hạn mức bồi hoàn | `get_policy` | Quyết định chính sách (`policy_decided`), root cause, financial resolution, handoff sang `verifier` |
| `verifier` | Toàn bộ payload output & trace | Kiểm định độc lập: JSON schema compliance, toàn vẹn evidence refs, tính nhất quán tài chính và quan hệ thực thể | Không gọi tool | `verification_completed` (status: passed) |

Áp dụng nguyên tắc Least Privilege: mỗi agent chỉ có quyền gọi các MCP tool thuộc phạm vi trách nhiệm của mình.

## 3. Entity resolution và A2A protocol

1. **Candidate Validation & Filtering**:
   - Đối chiếu danh sách `candidate_order_ids` qua MCP tool `get_order`. Candidate không tồn tại trong hệ thống (lỗi MCP 404/not found) được phân loại vào `rejected_candidates`.
   - Kết hợp tra cứu `get_customer_history` bằng `customer_unique_id_hint` để đối chiếu các order thực sự thuộc về khách hàng khiếu nại.
2. **Confidence Scoring**:
   - Gán `confidence = 1.0` khi tìm thấy chính xác duy nhất 1 order hợp lệ khớp với `claimed_order_id` và customer history.
   - Gán `confidence = 0.9` nếu không tìm thấy order hợp lệ (`status = "not_found"`).
3. **Correlation & Message Envelope**:
   - Mọi message và trace event đều gắn `case_id` và `event_id` theo chuẩn `evt_[A-Za-z0-9_-]{12,96}`.
   - Luồng handoff tuần tự, xác định rõ actor nguồn và actor đích, không tạo vòng lặp.

## 4. Evidence và conflict lifecycle

1. **Validation & Registration**:
   - Mọi phản hồi từ MCP tool đều được kiểm tra theo schema `day09-mcp-evidence-v1`.
   - `evidence_ref` hợp lệ dạng `ev_[A-Za-z0-9_-]{20,96}` được lưu vào `InvestigationState` và chỉ sử dụng trong phạm vi case hiện tại.
2. **Consumption Tracking**:
   - Mỗi lần agent sử dụng dữ liệu từ tool, một event `tool_result_consumed` được phát ra trong trace gắn kèm `evidence_refs` tương ứng.
3. **Data Conflict Resolution**:
   - Khi có sự sai lệch giữa thông tin ứng viên khai báo và thực tế tra cứu từ cơ sở dữ liệu MCP, một record trong `data_conflicts` được ghi nhận với `sources=["candidate_list", "mcp_order_registry"]` và `selected_source="mcp_order_registry"`.

## 5. Failure and efficiency policy

| Failure | Retry budget | Fallback | Trace event/code |
| --- | ---: | --- | --- |
| MCP timeout / network error | 2 retries | Ghi nhận thiếu bằng chứng, chuyển sang đánh giá fallback an toàn | `mcp_error` |
| Entity not found / candidate invalid | 0 retries | Đưa vào `rejected_candidates`, set status `not_found` | `unresolvable_candidate_rejected` |
| Refund timeline unavailable (404) | 0 retries | Coi như chưa có hoàn tiền (`refunded_total = 0.0`) | `refund_not_found` |
| Invalid specialist result | 1 retry | Phân tích theo thông tin tối thiểu thu thập được từ order summary | `specialist_fallback` |

* **Query Budget & Efficiency**:
  - Không gọi lại tool đã gọi với cùng tham số trong 1 case (in-memory per-case caching).
  - Không quét ngẫu nhiên các order_id không nằm trong candidate list.
  - Số lượng tool call trung bình mỗi case được tối ưu ở mức 5–8 calls, đảm bảo đạt điểm tối đa ở tiêu chí `efficiency`.

## 6. Verification invariants

Trước khi finalize case output, Verifier Agent kiểm tra các bất biến sau:
1. **Schema Invariant**: Output tuân thủ 100% `day09-l3b-output-v2.schema.json`.
2. **Entity Scope**: `affected_entities` chứa đầy đủ order_ids, item_ids, seller_ids, payment_references, shipment_ids không trùng lặp.
3. **Rejected Candidates**: Hợp của `resolved_order_ids` và `rejected_candidates` bao phủ toàn bộ `candidate_order_ids`.
4. **Evidence Ownership**: Toàn bộ `evidence_refs` trong output và trace đều xuất phát từ MCP tool calls của chính case đó trong cùng phiên chạy.
5. **Financial Balance**: Nếu `recommended_refund_brl > 0`, tổng số tiền các dòng trong `refund_lines` phải bằng chính xác `recommended_refund_brl`. Nếu `recommended_refund_brl == 0`, `refund_lines` phải rỗng.
6. **Policy Precedence**: `primary_issue`, `case_status`, và `resolution_actions` phải nhất quán với quy định trong `EC_POLICY_V2`.
7. **Calibration Bounds**: `confidence` phải nằm trong khoảng $[0.0, 1.0]$.

## 7. Reproducibility

- **Model**: `gpt-4o-mini` (qua OpenAI API) với `temperature = 0.0`.
- **Python Version**: Python 3.11+.
- **Dependencies**: `openai>=1.0`, `httpx2>=2,<3`, `mcp>=2,<3`, `jsonschema>=4.25`, `python-dotenv>=1.1`.
- **Lệnh chạy toàn bộ**:
  ```bash
  day09 run
  day09 validate
  day09 package --output dist/submission.zip
  ```
