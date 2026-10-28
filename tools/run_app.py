"""Explicit project bootstrap for isolated/embedded Python, independent of cwd."""
from pathlib import Path
import os
import runpy
import sys

ROOT=Path(__file__).resolve().parents[1]
os.environ['ORT_DISABLE_TELEMETRY']='1'
if not (ROOT/'typelogic/__main__.py').is_file():
    raise SystemExit('The typelogic directory is missing. Extract the complete application before starting.')
sys.path.insert(0,str(ROOT))
if __name__=='__main__':
    if hasattr(__import__('signal'),'SIGBREAK'):
        import signal
        def _stop(*_):raise KeyboardInterrupt
        signal.signal(signal.SIGBREAK,_stop)   # the launcher stops the app with CTRL_BREAK (see launcher.wait_child)
    try:runpy.run_module('typelogic',run_name='__main__')
    except KeyboardInterrupt:raise SystemExit(130)   # stopped by the user: no traceback
