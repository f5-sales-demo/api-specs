import json
import pytest
import shutil
import sys
import subprocess
from pathlib import Path
from scripts.spectral_lint import main, SpectralOperationalError, SpectralAdapter

class MockCompletedProcess:
    def __init__(self, stdout="", stderr="", returncode=0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode

@pytest.fixture
def setup_main(monkeypatch, tmp_path):
    spec_dir = tmp_path / "specs"
    spec_dir.mkdir()
    (spec_dir / "spec.json").touch()
    
    config_file = tmp_path / "config.yaml"
    config_file.write_text("spectral:\n  enabled: true\n  ruleset: '.spectral.mjs'\n")
    
    monkeypatch.setattr(sys, "argv", ["spectral_lint.py", "--mode", "discover", "--spec-dir", str(spec_dir), "--config", str(config_file)])
    
    # Mock node presence
    monkeypatch.setattr(shutil, "which", lambda x: "/usr/bin/node" if x == "node" else None)
    
    # Mock file existence for runner and ruleset
    original_exists = Path.exists
    def mock_exists(self):
        if self.name in ["spectral_runner.mjs", ".spectral.mjs"]:
            return True
        return original_exists(self)
    monkeypatch.setattr(Path, "exists", mock_exists)

@pytest.mark.parametrize("stdout,stderr,returncode,desc", [
    ("", "", 0, "empty stdout"),
    ("{}", "", 0, "empty object"),
    ("null", "", 0, "null"),
    ('["string", 123]', "", 0, "non-object array entries"),
    ('[{"code": "error"}]', "", 0, "missing required fields"),
    ('[{"code": "error", "message": "msg", "path": [], "range": {"start": {"line": "invalid", "character": 0}, "end": {"line": 0, "character": 0}}, "severity": 0, "source": "src"}]', "", 0, "invalid field types"),
    ("invalid json", "", 0, "malformed output"),
    ("", "Some engine error", 1, "nonzero exit code"),
])
def test_main_exit_code_2_on_operational_failures(setup_main, monkeypatch, stdout, stderr, returncode, desc):
    def mock_run(*args, **kwargs):
        return MockCompletedProcess(stdout=stdout, stderr=stderr, returncode=returncode)
    monkeypatch.setattr(subprocess, "run", mock_run)
    monkeypatch.setattr("subprocess.run", mock_run)
    monkeypatch.setattr("scripts.spectral_lint.subprocess.run", mock_run)
    
    assert main() == 2, f"Failed for {desc}"

def test_main_exit_code_2_on_missing_node(setup_main, monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda x: None)
    assert main() == 2

def test_main_exit_code_2_on_missing_specs(setup_main, monkeypatch, tmp_path):
    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()
    monkeypatch.setattr(sys, "argv", ["spectral_lint.py", "--mode", "discover", "--spec-dir", str(empty_dir)])
    assert main() == 2
