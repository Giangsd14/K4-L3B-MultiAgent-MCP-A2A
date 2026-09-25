import asyncio
from pathlib import Path
from student_agent.config import Settings
from student_agent.contracts import Contracts
from student_agent.mcp_gateway import connect_gateway
from student_agent.cases import load_case_set

async def main():
    root = Path('.').resolve()
    settings = Settings.load(root)
    contracts = Contracts(root / 'contracts' / 'schemas')
    case_set = load_case_set(root)
    async with connect_gateway(settings.mcp_endpoint, settings.team_api_key, contracts) as gateway:
        for cid in list(case_set.case_ids)[:5]:
            case = case_set.cases[cid]
            cands = case.get('candidate_order_ids', [])
            print(f'Case {cid}: candidates={cands}')
            for cand in cands:
                try:
                    res = await gateway.call('get_order', case_id=cid, order_id=cand)
                    print(f'  SUCCESS get_order {cand}: {res.get("evidence_ref")}')
                except Exception as e:
                    print(f'  FAIL get_order {cand}: {e}')
                
                try:
                    res = await gateway.call('get_shipment_summary', case_id=cid, order_id=cand)
                    print(f'  SUCCESS get_shipment_summary {cand}: {res.get("evidence_ref")}')
                except Exception as e:
                    print(f'  FAIL get_shipment_summary {cand}: {e}')

if __name__ == '__main__':
    asyncio.run(main())
