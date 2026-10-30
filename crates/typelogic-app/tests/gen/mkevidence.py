"""evidence._check of TypeLogic 1.x (needle.evidence) on value/type/text combinations.
    python mkevidence.py <TypeLogic-AI> > ../evidence_fixture.json"""
import json, sys
sys.path.insert(0, sys.argv[1])
from typelogic import evidence
texts = ['My payout has failed three days in a row; ticket 4,521.50 USD.', 'Order 1.234.567,89 arrived on 05/03/2024.',
         'Paid 1 234,5 EUR on March 5th, 2024 and again on 2024-03-05.', 'Shipped zero units, duas vezes, three-day delay.',
         'Invoice INV-2291 for $12,000 due 3/5/24.', 'Temperature -4.5C and +3 points; v2.0 build.', 'No numbers here at all.',
         'Customer: ACME Ltd. Contact: João  SILVA. Straße 12.', '١٢٣ units arrived on 12.3.2024', 'Due 5 March 2024, total 1,5 kg.',
         'one hundred and twelve apples', 'Ten apples, Two pears and ONE melon.', 'ref 0099 and 99 and 9.0',
         'The meeting is on 2024-12-01 at 10:30.', 'Price 1.000 (one thousand) or 1,000.00']
values = [('number', 3), ('number', 4521.5), ('number', 4521.50), ('integer', 1234567), ('number', 1234567.89), ('number', 1234.5),
          ('number', 12000), ('number', 0), ('number', 2), ('number', -4.5), ('number', 3), ('integer', 123), ('number', 1.5),
          ('number', 15), ('integer', 10), ('integer', 1), ('integer', 12), ('number', 99), ('number', 9), ('number', 1000), ('number', 1),
          ('date', '2024-03-05'), ('date', '2024-05-03'), ('date', '2024-03-12'), ('date', '2024-12-01'), ('date', '2024-02-30'),
          ('text', 'ACME Ltd'), ('text', 'joão silva'), ('text', 'strasse 12'), ('text', 'INV-2291'), ('text', '  '), ('text', 'missing'),
          ('enum', 'high'), ('enum', 'apples'), ('boolean', True), ('number', True), ('number', 'x')]
rows = []
for t in texts:
    for kind, v in values:
        rows.append({'value': v, 'kind': kind, 'text': t, 'check': evidence._check(v, {'type': kind}, t)})
json.dump(rows, sys.stdout, ensure_ascii=False)
