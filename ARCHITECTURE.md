# L3B architecture record

The implementation is a fixed Python agent graph. Public JSON output, trace events, MCP envelopes and manifest are validated against the existing files in `contracts/schemas/`; those files are not modified.

## Workflow and handoffs

```text
CLI case_received
  → Coordinator task_assigned / handoff
  → Entity + Customer investigation (verify candidate order)
  → Order/Item → Shipment → Payment/Refund → optional Customer/Product/Seller
  → Policy evidence and conflict resolution
  → Policy decision → Verifier → Coordinator output
CLI case_finalized
```

`solve_case(case, gateway, trace)` owns the per-case state. `investigate()` returns an `InvestigationResult` containing identity status, raw `EvidenceFact` records, warnings and errors. Each record has its originating `case_id`, tool, domain, unchanged server `evidence_ref`, `result_hash` and `data`. The coordinator normalizes only recognized fields, passes them into the policy reducer, assembles the 13 required L3B fields, then sends the draft through `verify_output()`. The verifier's output is the only result returned to the CLI. Handoffs are internal structured function calls; the trace exposes the actor and target without private reasoning or raw tool payloads.

After identity is resolved, the coordinator records separate assignments and handoffs to Order/Item, Shipment and Payment/Refund specialists. Shipment and payment calls run sequentially to preserve MCP session safety; an allowlisted topic extracted from free text may only change which specialist runs first. Optional customer/product work receives its own handoff. The evidence collector returns the records to Policy, which hands the draft to Verifier.

Entity resolution is a sequential gate. The investigator tries the claimed order first, then unique listed candidates (up to eight). It accepts an order only when an authoritative order response contains the queried ID. A definitive missing response rejects the candidate; a failed or inconclusive call remains unknown. Without exactly one verified order it does not query order-dependent domains and returns `ambiguous` or `not_found` as supported by the evidence. Customer history orders remain in `customer_context.related_order_ids`, not `affected_entities.order_ids`.

## Agent ownership and tool permissions

The Gateway catalog supplies tool names and input schemas at runtime. A specialist calls a tool only when the discovered schema accepts the available scoped arguments. Current live discovery exposes the following capabilities:

| Actor | Scope | MCP permissions | Handoff |
| --- | --- | --- | --- |
| Coordinator | one case and its accumulated state | catalog discovery only | task assignment, policy/verifier dispatch, final assembly |
| Entity/Customer | claimed/candidate IDs, verified customer identity | `get_order`, `get_customer_history` | verified/rejected/unknown candidates and customer history |
| Order/Item | verified order | `get_order_items`, `get_sellers`, `get_product_context` | item/seller/product facts |
| Shipment | verified order | `get_shipment_summary` | timeline and delivery evidence |
| Payment/Refund | verified order | `get_payment_timeline`, `get_order_payments` if appropriate, `get_refund_timeline` | payment and refund lifecycle evidence |
| Policy | case policy version plus normalized facts | `get_policy` via investigator; reducer itself has no tool access | issue, responsibility, financial resolution and actions |
| Conflict resolver | collected facts/provenance | none | explicit unresolved conflicts |
| Verifier | draft output, evidence registry, contracts | none | validated output or specific error |

The current investigator calls the useful order-level payment timeline tool when available, rather than calling both payment tools automatically. Optional customer/product/seller queries follow `investigation_scope`. It runs MCP requests sequentially because this client session has not been established as safe for concurrent calls and all audited calls count toward efficiency.

## Evidence and conflict handling

Every MCP call receives the exact `case_id` through the Gateway's explicit argument. The Gateway validates the returned envelope against `mcp-evidence-response-v1`, preserves `evidence_ref` and `result_hash`, and caches by `(case_id, tool_name, canonical arguments)` only within its run. Neither the model nor local input data can create an evidence ref. The specialist emits `tool_result_consumed` with the actual actor, tool and ref when it inspects a response. Public output cites only refs tied to the selected conclusions; all cited refs must belong to the case, appear in the collected records and have a corresponding consumed event in the trace.

Normalized facts retain uncertainty. Contradictory order statuses become `data_conflicts` with source labels and no selected source. Missing entity, shipment, payment or policy evidence leads to conservative `insufficient_evidence`/`needs_investigation` behavior. `contracts/scoring/scoring-policy-v2.json` is the scoring rubric, not the business refund policy. A positive BRL refund needs a policy MCP recommendation and a supported refundable balance; calculations use Decimal cents.

## Failure and efficiency policy

| Condition | Behavior |
| --- | --- |
| Transient MCP transport error, timeout, 408/429/5xx | Retry once with the same tool, args and `case_id`, after a short delay; then mark that fact unavailable. |
| Scope/4xx error or invalid evidence envelope | No retry; record a missing fact or stop on an invalid envelope. |
| Ambiguous or unknown order identity | Do not choose a candidate by formatting or model guess; return an investigation outcome. |
| Model key missing, free quota exhausted, bad response | Continue using structured claims and MCP facts; the model has no authority over tools, refs or final fields. |
| Output invariant failure | Fail verification with a specific error so invalid JSON is not submitted. |

All MCP calls, including retries and exploratory calls, may count toward the server's efficiency metric. The investigator limits candidate orders and optional product/seller expansion; no cross-case cache or unbounded agent loop is used.

## Model boundary

The only configured model is OpenRouter `meta-llama/llama-3.1-8b-instruct:free` (8B parameters). It is an optional helper for extracting allowlisted claim topics from unstructured customer text when the input does not already supply usable topics. These topics only prioritize the order of shipment/payment/refund investigation; they do not remove any required domain, change MCP arguments, establish a verdict or create evidence. The request is stripped of obvious identifiers; no raw MCP data, customer/payment records, API keys or evidence refs are sent. Model text is parsed and validated as untrusted input. Python alone dispatches MCP calls, applies business policy, computes money, validates contracts and writes output. A persisted UTC-day request counter keeps free-model usage below the account's published daily cap; unavailable model access does not stop a case.

## Verification and packaging

`verify_output()` checks the exact L3B JSON Schema plus case/ref ownership, resolved/rejected candidates, seller responsibility, shipment/payment issue consistency, refund line totals and refundable balance. It lowers confidence when evidence is missing, conflicting or warned. The artifact validator then checks all 100 output filenames against `case-set.json`, every JSON/trace schema, case ownership, unique event IDs, lifecycle ordering, at least one consumed MCP result and evidence-to-policy handoff per case, tool-result linkage and Team/OpenRouter key leakage.

CLI emits `case_received` before the solver and `case_finalized` after writing a validated output. The workflow emits `task_assigned`, handoffs, `policy_decided` and `verification_completed`; specialists emit actual `tool_result_consumed` events. The package builder creates only `manifest.json`, `trace.jsonl` and `outputs/<case_id>.json` entries at ZIP root, enforcing size and secret checks.

## Reproduction

Use Python 3.11+ and install `pyproject.toml` dependencies. Configure Competition/MCP values in an ignored `.env` and, optionally, `OPENROUTER_API_KEY`. Run `day09 mcp-tools --json` to inspect current tool contracts; then run `day09 validate-inputs`, `day09 run`, `day09 validate`, and `day09 package --output dist/submission.zip`. The input bundle's `case-set.json` and 100 direct `inputs/<case_id>.json` files are prerequisites. The workflow has no random sampling or model-generated public JSON; tool/session availability and provider/model response may vary at runtime.
