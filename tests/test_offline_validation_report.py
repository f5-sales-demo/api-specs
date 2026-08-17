from __future__ import annotations

import json

from scripts.offline_validation_report import main


def test_offline_report_records_spec_inventory_without_live_results(tmp_path):
    specs = tmp_path / "specs"
    specs.mkdir()
    (specs / "one.json").write_text('{"paths": {"/one": {"get": {}}}}')
    (specs / "two.json").write_text('{"paths": {"/two": {"post": {}, "delete": {}}}}')
    output = tmp_path / "reports" / "validation_report.json"

    assert main(["--spec-dir", str(specs), "--output", str(output)]) == 0

    report = json.loads(output.read_text())
    assert report["summary"]["mode"] == "offline"
    assert report["summary"]["total_endpoints"] == 3
    assert report["summary"]["total_tests"] == 0
    assert report["summary"]["total_discrepancies"] == 0
    assert report["results"] == []
    assert report["discrepancies"] == []
