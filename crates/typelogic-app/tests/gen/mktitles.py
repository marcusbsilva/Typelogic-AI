"""titles.from_text of TypeLogic 1.x on many texts.   python mktitles.py <TypeLogic-AI> > ../titles_fixture.json"""
import json, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1]); sys.path.insert(0, str(Path(__file__).resolve().parents[4] / 'spike'))
from typelogic import titles, templates
texts = ['', '   ', 'Hi', 'Hello there', 'Hi team, my payout has failed three days in a row and I need help today.',
         'Good morning! The invoice 1182 was paid twice.', 'HEY,   what about this', 'Dear support team. Order arrived broken.',
         'High priority: the server is down.', 'Hint. This is short', 'one two', 'a.b. c d e f', 'Line one\nLine two has more words here',
         'Wait!!!   Then something happened in the warehouse yesterday morning at nine', 'x' * 200,
         'Supercalifragilisticexpialidocious ' * 6, '“Quoted” *title* -- with: marks...', 'ßtraße is a street name in town',
         'The payment of the customer for the order in a', 'Item?Next sentence here with words', 'Tabs\tand\x1cseparators here now',
         'Olá equipe, meu pagamento falhou três dias seguidos.', {'case': 'structured', 'n': 1.5}, ['a', 'b']]
for row in templates.load_all(sys.argv[1])['templates']:
    texts.append(row['template'].get('case') or '')
try:
    import suites
    for name in ('jevbench', 'test2'):
        for item in suites.load(sys.argv[1], name, None)[:120]:
            texts.append(item['state'])
except Exception as e:
    print(e, file=sys.stderr)
json.dump([{'text': t, 'title': titles.from_text(t)} for t in texts], sys.stdout, ensure_ascii=False)
