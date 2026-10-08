"""Noninteractive board readers must not inherit a busy Windows input pipe."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest


_CHILD = r'''
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

os.environ['QT_QPA_PLATFORM'] = 'offscreen'
root, mode = Path(sys.argv[1]), sys.argv[2]
original_run = subprocess.run

def bounded_run(*args, **kwargs):
    kwargs['timeout'] = 3
    return original_run(*args, **kwargs)

subprocess.run = bounded_run
reader = threading.Thread(target=sys.stdin.readline, daemon=True)
reader.start()
time.sleep(0.1)
if mode == 'panel':
    from PySide6.QtWidgets import QApplication
    from workflow_editor.core import odb_inspect
    from workflow_editor.widgets.netlist_panel import NetlistPanel, _LIVE_WORKERS
    odb_inspect.odb_cli_path = lambda: root / 'cli.py'
    app = QApplication([])
    panel = NetlistPanel()
    panel.load(root)
    while _LIVE_WORKERS:
        app.processEvents()
        time.sleep(0.005)
    result = {'components': panel._components, 'error': panel._status.text()}
    panel.close()
else:
    from workflow_editor.authoring import _project_tools_mcp as server
    server._CLI_PATH = str(root / 'cli.py')
    result = {}
    def work():
        try:
            result.update(server._run_cli(str(root / 'board.tgz'), ['--list-nets']))
        except Exception as exc:
            result['error'] = str(exc)
    worker = threading.Thread(target=work)
    worker.start()
    worker.join()
(root / 'result.json').write_text(json.dumps(result), encoding='utf-8')
reader.join(10)
'''


@pytest.mark.parametrize('mode', ['panel', 'mcp'])
def test_board_reader_finishes_while_inherited_stdin_is_being_read(tmp_path, mode):
    board = {'components': [{'refdes': 'U1', 'pins': [{'name': '1', 'net': 'RESET'}],
                             'properties': {'MPN': 'MCU'}}],
             'nets': [{'net': 'RESET', 'nodes': [{'refdes': 'U1', 'pin': '1'}]}]}
    (tmp_path / 'board.tgz').write_bytes(b'synthetic archive')
    (tmp_path / 'cli.py').write_text('import json\nprint(json.dumps(' + repr(board) + '))\n',
                                    encoding='utf-8')
    child = tmp_path / 'reader.py'
    child.write_text(_CHILD, encoding='utf-8')
    env = os.environ.copy()
    # The child must import this checkout even when CI has not installed it.
    editor = Path(__file__).resolve().parents[1]
    env['PYTHONPATH'] = os.pathsep.join([str(editor), str(editor / 'external/opencode_client'),
                                       env.get('PYTHONPATH', '')])
    proc = subprocess.Popen([sys.executable, str(child), str(tmp_path), mode], env=env,
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    output = tmp_path / 'result.json'
    try:
        deadline = time.monotonic() + 20
        while not output.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert output.exists(), 'reader did not finish while stdin remained open'
        result = json.loads(output.read_text(encoding='utf-8'))
        assert result.get('components') == board['components'], result.get('error')
        if mode == 'mcp':
            assert result['nets'] == board['nets']
    finally:
        proc.stdin.close()
        proc.stdin = None
        try:
            _, stderr = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            _, stderr = proc.communicate(timeout=5)
        assert proc.returncode == 0, stderr.decode('utf-8', 'replace')
