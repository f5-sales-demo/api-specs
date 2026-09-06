import subprocess
import json
import hashlib
import sys
from pathlib import Path

def main():
    print("Running equivalence verification...")
    specs = sorted(list(Path("release/specs").glob("*.json")))
    if not specs:
        print("Error: No specs found in release/specs")
        return 1

    print(f"Found {len(specs)} spec files.")

    # Run legacy CLI
    print("Running legacy Spectral CLI via npx...")
    legacy_cmd = [
        "npx",
        "@stoplight/spectral-cli@6.16.2",
        "lint",
        "--ruleset",
        "spectral-pipeline.mjs",
        "-f",
        "json",
        *map(str, specs)
    ]

    legacy_res = subprocess.run(legacy_cmd, capture_output=True, text=True, check=False)
    if not legacy_res.stdout.strip():
        print(f"Error: Legacy CLI stdout was empty! Stderr: {legacy_res.stderr}")
        return 1

    try:
        legacy_data = json.loads(legacy_res.stdout)
    except json.JSONDecodeError as e:
        print(f"Error: Failed to parse legacy stdout as JSON: {e}")
        return 1

    # Run direct-core runner
    print("Running direct-core ESM runner...")
    runner_cmd = [
        "node",
        "scripts/spectral_runner.mjs",
        "--ruleset",
        "spectral-pipeline.mjs",
        *map(str, specs)
    ]
    runner_res = subprocess.run(runner_cmd, capture_output=True, text=True, check=False)
    if runner_res.returncode != 0:
        print(f"Error: Runner failed with exit code {runner_res.returncode}: {runner_res.stderr}")
        return 1

    try:
        runner_data = json.loads(runner_res.stdout)
    except json.JSONDecodeError as e:
        print(f"Error: Failed to parse runner stdout as JSON: {e}")
        return 1

    print(f"Legacy findings count: {len(legacy_data)}")
    print(f"Runner findings count: {len(runner_data)}")

    def normalize_finding(f):
        code = f.get("code")
        message = f.get("message", "")
        path = f.get("path", [])
        
        rng = f.get("range", {})
        start_line = rng.get("start", {}).get("line", 0)
        start_char = rng.get("start", {}).get("character", 0)
        end_line = rng.get("end", {}).get("line", 0)
        end_char = rng.get("end", {}).get("character", 0)
        
        severity = f.get("severity")
        if isinstance(severity, str):
            severity_map = {"error": 0, "warn": 1, "info": 2, "hint": 3}
            severity = severity_map.get(severity.lower(), severity)
        
        source = f.get("source", "")
        if source:
            source = str(Path(source).resolve())

        return {
            "code": code,
            "message": message,
            "path": path,
            "range": {
                "start": {"line": start_line, "character": start_char},
                "end": {"line": end_line, "character": end_char}
            },
            "severity": severity,
            "source": source
        }

    legacy_normalized = [normalize_finding(f) for f in legacy_data]
    runner_normalized = [normalize_finding(f) for f in runner_data]

    def sort_key(f):
        return (
            f["source"],
            ".".join(str(p) for p in f["path"]),
            f["code"],
            f["range"]["start"]["line"],
            f["range"]["start"]["character"]
        )

    legacy_sorted = sorted(legacy_normalized, key=sort_key)
    runner_sorted = sorted(runner_normalized, key=sort_key)

    mismatches = []
    max_compare = max(len(legacy_sorted), len(runner_sorted))
    for i in range(max_compare):
        if i >= len(legacy_sorted):
            mismatches.append(f"Extra runner finding at index {i}: {runner_sorted[i]}")
            continue
        if i >= len(runner_sorted):
            mismatches.append(f"Extra legacy finding at index {i}: {legacy_sorted[i]}")
            continue
        
        l_item = legacy_sorted[i]
        r_item = runner_sorted[i]
        
        if l_item != r_item:
            mismatches.append(
                f"Mismatch at index {i}:\n"
                f"Legacy: {json.dumps(l_item, indent=2)}\n"
                f"Runner: {json.dumps(r_item, indent=2)}"
            )

    if mismatches:
        print(f"FAILED: Found {len(mismatches)} mismatches between legacy CLI and direct runner!")
        for m in mismatches[:10]:
            print(m)
        if len(mismatches) > 10:
            print(f"... and {len(mismatches) - 10} more.")
        return 1

    print("SUCCESS: Full result equivalence restored perfectly! Both engines emit exactly identical findings.")
    
    legacy_json_str = json.dumps(legacy_sorted, indent=2, sort_keys=True)
    runner_json_str = json.dumps(runner_sorted, indent=2, sort_keys=True)
    
    legacy_sha = hashlib.sha256(legacy_json_str.encode()).hexdigest()
    runner_sha = hashlib.sha256(runner_json_str.encode()).hexdigest()
    
    print(f"Normalized Legacy JSON SHA-256: {legacy_sha}")
    print(f"Normalized Runner JSON SHA-256: {runner_sha}")
    
    reports_dir = Path("reports")
    reports_dir.mkdir(parents=True, exist_ok=True)
    reports_dir.joinpath("legacy_normalized.json").write_text(legacy_json_str)
    reports_dir.joinpath("runner_normalized.json").write_text(runner_json_str)
    print("Saved normalized reports to reports/")
    return 0

if __name__ == "__main__":
    sys.exit(main())
