import sys
# JSON and help text are UTF-8 on every platform; Windows pipes otherwise use the ANSI code page (cp1252).
for _stream in (sys.stdout, sys.stderr):
    if _stream is not None and hasattr(_stream, 'reconfigure') and (_stream.encoding or '').lower().replace('-', '') != 'utf8':
        _stream.reconfigure(encoding='utf-8', errors='backslashreplace')
# `--input -` reads UTF-8 JSON (a BOM from PowerShell pipes is accepted).
if sys.stdin is not None and hasattr(sys.stdin, 'reconfigure') and (sys.stdin.encoding or '').lower().replace('-', '') != 'utf8':
    sys.stdin.reconfigure(encoding='utf-8-sig')
from .cli import COMMANDS
if sys.argv[1:] and sys.argv[1] in COMMANDS:
    from .cli import main
    raise SystemExit(main())
from .server import main
main()
