import subprocess
import sys
from pathlib import Path


def test_setup_existing_pinned_checkout_is_non_mutating():
    path = Path('.deps/Check-Account-ChatGPT').resolve()
    before = subprocess.check_output(['git','-C',str(path),'status','--porcelain'])
    result = subprocess.run([sys.executable,'scripts/setup_checklive.py','--path',str(path)],capture_output=True,text=True)
    assert result.returncode == 0
    assert 'dependency ready' in result.stdout
    assert subprocess.check_output(['git','-C',str(path),'status','--porcelain']) == before


def test_setup_does_not_overwrite_an_existing_directory(tmp_path):
    marker = tmp_path / 'user-file.txt'
    marker.write_text('preserve me')
    result = subprocess.run([sys.executable,'scripts/setup_checklive.py','--path',str(tmp_path)],capture_output=True,text=True)
    assert result.returncode != 0
    assert marker.read_text() == 'preserve me'
    assert not (tmp_path / '.git').exists()
