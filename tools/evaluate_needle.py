"""Measure Needle 3 in TypeLogic, offline: field extraction on fictional scenarios.

    python tools/evaluate_needle.py [--output research/results/needle/needle-eval.json]

Extraction: 7 scenarios (5 of the shipped examples, an invoice and a Portuguese refund) with 31 declared fields and
hand-written expected values. 26 fields are stated in the text: correct when the value equals the expected one, wrong when
a different value is returned, omitted when the engine leaves it out. 5 fields are not stated anywhere in the text: the
engine should leave them out, and a value counts as invented. It runs twice: with optional fields (what TypeLogic uses) and
with every field required.
Small samples: these numbers show tendencies, not quality guarantees.
"""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from typelogic.offline import install  # noqa: E402
from typelogic import needle  # noqa: E402
from typelogic.examples import catalogue  # noqa: E402

N, I, T, D, B = 'number', 'integer', 'text', 'date', 'boolean'
CASES = [
    ('request-integrity', None, [('trusted_amount', N, 'Amount in the trusted server record', 49.9), ('received_amount', N, 'Amount in the received request', 0.01),
                                 ('received_action', T, 'Action in the received request', 'refund'), ('added_role', T, 'Role added by the received request', 'admin')]),
    ('purchasing', None, [('first_item_price', N, 'Price of the first item', 40), ('second_item_price', N, 'Price of the second item', 60),
                          ('stated_total', N, 'Total stated in the quote', 100), ('currency', T, 'Currency code', None),
                          ('due_date', D, 'Payment due date', None)]),
    ('rpg-combat', None, [('monster_hp', I, 'Current HP of the monster', 60), ('monster_max_hp', I, 'Maximum HP of the monster', 80),
                          ('monster_mana', I, 'Mana of the monster', 0), ('enemy_distance_m', N, 'Distance to the enemy player in meters', 1),
                          ('claw_reach_m', N, 'Reach of Claw in meters', 2), ('heal_mana_cost', I, 'Mana cost of Heal', 20)]),
    ('inventory', None, [('item_code', T, 'Item code', 'P-10'), ('units_in_stock', I, 'Units in stock', 0), ('units_needed', I, 'Units the customer needs', 1),
                         ('warehouse', T, 'Warehouse name or location', None)]),
    ('expenses', None, [('expense', T, 'What is being reimbursed', 'taxi trip'), ('receipt_attached', B, 'Whether a receipt was attached', False),
                        ('amount', N, 'Amount to reimburse', None), ('trip_date', D, 'Date of the trip', None)]),
    ('invoice', 'Acme Corp invoice INV-2231 for EUR 4,800.00 is due on 2026-11-15. The vendor asks us to change the bank account before payment.',
     [('vendor', T, 'Vendor name', 'Acme Corp'), ('invoice_number', T, 'Invoice number', 'INV-2231'), ('amount', N, 'Invoice amount', 4800),
      ('currency', T, 'Currency code', 'EUR'), ('due_date', D, 'Due date', '2026-11-15')]),
    ('refund-pt', 'O cliente pagou duas vezes R$ 89,90 no dia 3 de outubro de 2026 e pede o reembolso.',
     [('amount', N, 'Valor pago', 89.9), ('payment_date', D, 'Data do pagamento', '2026-10-03'), ('requested_action', T, 'O que o cliente pede', 'reembolso')]),
]


def same(value, expected):
    if isinstance(expected, bool) or isinstance(value, bool):
        return value is expected
    if isinstance(expected, (int, float)):
        return isinstance(value, (int, float)) and abs(value - expected) < 1e-6
    return isinstance(value, str) and value.strip().lower() == str(expected).lower()


def extraction(runtime, required):
    states = {e['id']: e['payload']['state'] for e in catalogue()['examples']}
    totals = {'correct': 0, 'wrong': 0, 'omitted': 0, 'wrong_marked_not_in_text': 0, 'absent_left_out': 0, 'invented': 0, 'invented_marked_not_in_text': 0}
    rows = []
    original = needle.tool_schema
    if required:   # same tool with every field required (not used by TypeLogic)
        needle.tool_schema = lambda fields: {**original(fields), 'parameters': {**original(fields)['parameters'], 'required': [f['name'] for f in fields]}}
    try:
        for name, text, fields in CASES:
            text = text or states[name]
            result = runtime.extract(text, [{'name': f, 'type': t, 'description': d} for f, t, d, _ in fields])
            got = {f['name']: f for f in result['fields']}
            for field, _, _, expected in fields:
                item = got[field]
                if expected is None:
                    kind = 'absent_left_out' if item['value'] is None else 'invented'
                else:
                    kind = 'omitted' if item['value'] is None else 'correct' if same(item['value'], expected) else 'wrong'
                totals[kind] += 1
                if kind in ('wrong', 'invented') and item['check'] in ('not_in_text', 'inferred'):
                    totals[kind + '_marked_not_in_text'] += 1
                rows.append({'case': name, 'field': field, 'expected': expected, 'value': item['value'], 'check': item['check'], 'result': kind})
    finally:
        needle.tool_schema = original
    return {'required_fields': required, 'stated_fields': sum(totals[k] for k in ('correct', 'wrong', 'omitted')),
            'absent_fields': totals['absent_left_out'] + totals['invented'], **totals, 'rows': rows}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    install()
    runtime = needle.Runtime()
    status = runtime.status()
    if not status['available']:
        raise SystemExit(status['reason'])
    report = {'engine': 'needle3', 'optional_fields': extraction(runtime, False), 'required_fields': extraction(runtime, True)}
    for key in ('optional_fields', 'required_fields'):  # flagged = marked not_in_text or inferred by the evidence check
        r = report[key]
        print(f"{key}: stated {r['correct']}/{r['stated_fields']} correct, {r['wrong']} wrong ({r['wrong_marked_not_in_text']} flagged), "
              f"{r['omitted']} omitted; absent {r['absent_left_out']}/{r['absent_fields']} left out, {r['invented']} invented ({r['invented_marked_not_in_text']} flagged)", file=sys.stderr)
    text = json.dumps(report, indent=2, ensure_ascii=False)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text + '\n', encoding='utf-8')
    else:
        print(text)
    return 0


if __name__ == '__main__':
    sys.exit(main())
