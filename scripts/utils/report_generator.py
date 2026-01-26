"""Report generation for validation results."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from jinja2 import BaseLoader, Environment
from rich.console import Console
from rich.table import Table

from .constraint_validator import Discrepancy, DiscrepancyType
from .schemathesis_runner import SchemathesisResult, TestStatus

console = Console()


@dataclass
class ValidationSummary:
    """Summary of validation run."""

    timestamp: str
    total_endpoints: int
    total_tests: int
    passed: int
    failed: int
    errors: int
    total_discrepancies: int
    discrepancies_by_type: dict[str, int]
    modified_files: list[str]
    unmodified_files: list[str]


@dataclass
class ReportConfig:
    """Report generation configuration."""

    output_dir: Path
    formats: list[str]
    include_examples: bool = True
    max_examples_per_issue: int = 5


class ReportGenerator:
    """Generate validation reports in multiple formats."""

    def __init__(self, config: ReportConfig):
        self.config = config
        self.config.output_dir.mkdir(parents=True, exist_ok=True)

    def generate_all(
        self,
        results: list[SchemathesisResult],
        discrepancies: list[Discrepancy],
        modified_files: list[str],
        unmodified_files: list[str],
    ) -> dict[str, Path]:
        """Generate reports in all configured formats."""
        # Create summary
        summary = self._create_summary(results, discrepancies, modified_files, unmodified_files)

        output_files = {}

        for fmt in self.config.formats:
            if fmt == "json":
                output_files["json"] = self._generate_json(summary, results, discrepancies)
            elif fmt == "html":
                output_files["html"] = self._generate_html(summary, results, discrepancies)
            elif fmt == "markdown":
                output_files["markdown"] = self._generate_markdown(summary, results, discrepancies)

        return output_files

    def _create_summary(
        self,
        results: list[SchemathesisResult],
        discrepancies: list[Discrepancy],
        modified_files: list[str],
        unmodified_files: list[str],
    ) -> ValidationSummary:
        """Create validation summary."""
        # Count discrepancies by type
        discrepancies_by_type: dict[str, int] = {}
        for d in discrepancies:
            dtype = d.discrepancy_type.value
            discrepancies_by_type[dtype] = discrepancies_by_type.get(dtype, 0) + 1

        return ValidationSummary(
            timestamp=datetime.utcnow().isoformat(),
            total_endpoints=len(results),
            total_tests=sum(r.examples_tested for r in results),
            passed=sum(1 for r in results if r.status == TestStatus.PASSED),
            failed=sum(1 for r in results if r.status == TestStatus.FAILED),
            errors=sum(1 for r in results if r.status == TestStatus.ERROR),
            total_discrepancies=len(discrepancies),
            discrepancies_by_type=discrepancies_by_type,
            modified_files=modified_files,
            unmodified_files=unmodified_files,
        )

    def _generate_json(
        self,
        summary: ValidationSummary,
        results: list[SchemathesisResult],
        discrepancies: list[Discrepancy],
    ) -> Path:
        """Generate JSON report."""
        output_path = self.config.output_dir / "validation_report.json"

        report = {
            "summary": asdict(summary),
            "results": [self._result_to_dict(r) for r in results],
            "discrepancies": [self._discrepancy_to_dict(d) for d in discrepancies],
        }

        with open(output_path, "w") as f:
            json.dump(report, f, indent=2, default=str)

        console.print(f"[green]JSON report: {output_path}[/green]")
        return output_path

    def _generate_html(
        self,
        summary: ValidationSummary,
        results: list[SchemathesisResult],
        discrepancies: list[Discrepancy],
    ) -> Path:
        """Generate HTML report."""
        output_path = self.config.output_dir / "validation_report.html"

        template = Environment(loader=BaseLoader()).from_string(HTML_TEMPLATE)

        html = template.render(
            summary=summary,
            results=results,
            discrepancies=discrepancies,
            TestStatus=TestStatus,
            DiscrepancyType=DiscrepancyType,
        )

        with open(output_path, "w") as f:
            f.write(html)

        console.print(f"[green]HTML report: {output_path}[/green]")
        return output_path

    def _generate_markdown(
        self,
        summary: ValidationSummary,
        results: list[SchemathesisResult],
        discrepancies: list[Discrepancy],
    ) -> Path:
        """Generate Markdown report."""
        output_path = self.config.output_dir / "validation_report.md"

        lines = [
            "# F5 XC API Validation Report",
            "",
            f"**Generated:** {summary.timestamp}",
            "",
            "## Summary",
            "",
            f"- **Total Endpoints:** {summary.total_endpoints}",
            f"- **Total Tests:** {summary.total_tests}",
            f"- **Passed:** {summary.passed}",
            f"- **Failed:** {summary.failed}",
            f"- **Errors:** {summary.errors}",
            f"- **Discrepancies Found:** {summary.total_discrepancies}",
            "",
            "### Discrepancies by Type",
            "",
        ]

        for dtype, count in summary.discrepancies_by_type.items():
            lines.append(f"- {dtype}: {count}")

        lines.extend(
            [
                "",
                "## Modified Files",
                "",
            ]
        )

        if summary.modified_files:
            for f in summary.modified_files:
                lines.append(f"- `{f}` (fixed)")
        else:
            lines.append("*No files required modification*")

        lines.extend(
            [
                "",
                "## Unmodified Files (Pass-through)",
                "",
            ]
        )

        if summary.unmodified_files:
            for f in summary.unmodified_files:
                lines.append(f"- `{f}`")
        else:
            lines.append("*All files required modification*")

        lines.extend(
            [
                "",
                "## Discrepancy Details",
                "",
            ]
        )

        for i, d in enumerate(discrepancies[: self.config.max_examples_per_issue * 10]):
            lines.extend(
                [
                    f"### {i + 1}. {d.path} - {d.property_name}",
                    "",
                    f"- **Type:** {d.discrepancy_type.value}",
                    f"- **Constraint:** {d.constraint_type}",
                    f"- **Spec Value:** `{d.spec_value}`",
                    f"- **API Behavior:** `{d.api_behavior}`",
                    "",
                ]
            )

            if d.recommendation:
                lines.append(f"**Recommendation:** {d.recommendation}")
                lines.append("")

        lines.extend(
            [
                "",
                "## Test Results by Endpoint",
                "",
                "| Endpoint | Method | Status | Tests | Discrepancies |",
                "|----------|--------|--------|-------|---------------|",
            ]
        )

        for r in results:
            status_icon = {
                TestStatus.PASSED: "✅",
                TestStatus.FAILED: "❌",
                TestStatus.ERROR: "⚠️",
                TestStatus.SKIPPED: "⏭️",
            }.get(r.status, "?")

            lines.append(
                f"| `{r.endpoint}` | {r.method} | {status_icon} {r.status.value} | "
                f"{r.examples_tested} | {len(r.discrepancies)} |"
            )

        with open(output_path, "w") as f:
            f.write("\n".join(lines))

        console.print(f"[green]Markdown report: {output_path}[/green]")
        return output_path

    def _result_to_dict(self, result: SchemathesisResult) -> dict:
        """Convert SchemathesisResult to dictionary."""
        return {
            "endpoint": result.endpoint,
            "method": result.method,
            "status": result.status.value,
            "examples_tested": result.examples_tested,
            "failures": result.failures,
            "errors": result.errors,
            "discrepancies": [self._discrepancy_to_dict(d) for d in result.discrepancies],
        }

    def _discrepancy_to_dict(self, discrepancy: Discrepancy) -> dict:
        """Convert Discrepancy to dictionary."""
        return {
            "path": discrepancy.path,
            "property_name": discrepancy.property_name,
            "constraint_type": discrepancy.constraint_type,
            "discrepancy_type": discrepancy.discrepancy_type.value,
            "spec_value": discrepancy.spec_value,
            "api_behavior": discrepancy.api_behavior,
            "test_values": discrepancy.test_values[: self.config.max_examples_per_issue],
            "recommendation": discrepancy.recommendation,
        }

    def print_summary(self, summary: ValidationSummary) -> None:
        """Print summary to console."""
        table = Table(title="Validation Summary")

        table.add_column("Metric", style="cyan")
        table.add_column("Value", style="green")

        table.add_row("Timestamp", summary.timestamp)
        table.add_row("Total Endpoints", str(summary.total_endpoints))
        table.add_row("Total Tests", str(summary.total_tests))
        table.add_row("Passed", str(summary.passed))
        table.add_row("Failed", str(summary.failed))
        table.add_row("Errors", str(summary.errors))
        table.add_row("Discrepancies", str(summary.total_discrepancies))
        table.add_row("Modified Files", str(len(summary.modified_files)))
        table.add_row("Unmodified Files", str(len(summary.unmodified_files)))

        console.print(table)


HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>F5 XC API Validation Report</title>
    <style>
        :root {
            --bg: #1a1a2e;
            --card: #16213e;
            --primary: #0f3460;
            --accent: #e94560;
            --text: #eee;
            --success: #00bf63;
            --warning: #ffc107;
            --error: #dc3545;
        }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: var(--bg);
            color: var(--text);
            margin: 0;
            padding: 20px;
            line-height: 1.6;
        }
        .container {
            max-width: 1200px;
            margin: 0 auto;
        }
        h1, h2, h3 {
            color: var(--accent);
        }
        .card {
            background: var(--card);
            border-radius: 8px;
            padding: 20px;
            margin: 20px 0;
            box-shadow: 0 4px 6px rgba(0, 0, 0, 0.3);
        }
        .stats {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
            gap: 15px;
        }
        .stat {
            background: var(--primary);
            padding: 15px;
            border-radius: 8px;
            text-align: center;
        }
        .stat-value {
            font-size: 2em;
            font-weight: bold;
        }
        .stat-label {
            color: #aaa;
            font-size: 0.9em;
        }
        .passed { color: var(--success); }
        .failed { color: var(--error); }
        .error { color: var(--warning); }
        table {
            width: 100%;
            border-collapse: collapse;
            margin-top: 15px;
        }
        th, td {
            padding: 12px;
            text-align: left;
            border-bottom: 1px solid #333;
        }
        th {
            background: var(--primary);
        }
        tr:hover {
            background: rgba(255,255,255,0.05);
        }
        .badge {
            padding: 4px 8px;
            border-radius: 4px;
            font-size: 0.85em;
        }
        .badge-passed { background: var(--success); color: #000; }
        .badge-failed { background: var(--error); }
        .badge-error { background: var(--warning); color: #000; }
        code {
            background: #333;
            padding: 2px 6px;
            border-radius: 4px;
            font-family: 'Monaco', 'Consolas', monospace;
        }
    </style>
</head>
<body>
    <div class="container">
        <h1>F5 XC API Validation Report</h1>
        <p>Generated: {{ summary.timestamp }}</p>

        <div class="card">
            <h2>Summary</h2>
            <div class="stats">
                <div class="stat">
                    <div class="stat-value">{{ summary.total_endpoints }}</div>
                    <div class="stat-label">Endpoints</div>
                </div>
                <div class="stat">
                    <div class="stat-value">{{ summary.total_tests }}</div>
                    <div class="stat-label">Tests Run</div>
                </div>
                <div class="stat">
                    <div class="stat-value passed">{{ summary.passed }}</div>
                    <div class="stat-label">Passed</div>
                </div>
                <div class="stat">
                    <div class="stat-value failed">{{ summary.failed }}</div>
                    <div class="stat-label">Failed</div>
                </div>
                <div class="stat">
                    <div class="stat-value error">{{ summary.errors }}</div>
                    <div class="stat-label">Errors</div>
                </div>
                <div class="stat">
                    <div class="stat-value">{{ summary.total_discrepancies }}</div>
                    <div class="stat-label">Discrepancies</div>
                </div>
            </div>
        </div>

        <div class="card">
            <h2>File Status</h2>
            <h3>Modified Files ({{ summary.modified_files|length }})</h3>
            <ul>
            {% for file in summary.modified_files %}
                <li><code>{{ file }}</code> - Fixed</li>
            {% else %}
                <li>No files required modification</li>
            {% endfor %}
            </ul>

            <h3>Unmodified Files ({{ summary.unmodified_files|length }})</h3>
            <ul>
            {% for file in summary.unmodified_files %}
                <li><code>{{ file }}</code> - Pass-through</li>
            {% else %}
                <li>All files required modification</li>
            {% endfor %}
            </ul>
        </div>

        <div class="card">
            <h2>Test Results</h2>
            <table>
                <thead>
                    <tr>
                        <th>Endpoint</th>
                        <th>Method</th>
                        <th>Status</th>
                        <th>Tests</th>
                        <th>Discrepancies</th>
                    </tr>
                </thead>
                <tbody>
                {% for result in results %}
                    <tr>
                        <td><code>{{ result.endpoint }}</code></td>
                        <td>{{ result.method }}</td>
                        <td>
                            <span class="badge badge-{{ result.status.value }}">
                                {{ result.status.value }}
                            </span>
                        </td>
                        <td>{{ result.examples_tested }}</td>
                        <td>{{ result.discrepancies|length }}</td>
                    </tr>
                {% endfor %}
                </tbody>
            </table>
        </div>

        {% if discrepancies %}
        <div class="card">
            <h2>Discrepancies</h2>
            {% for d in discrepancies[:50] %}
            <div style="margin: 15px 0; padding: 15px; background: var(--primary); border-radius: 8px;">
                <h3 style="margin-top: 0;">{{ d.path }} - {{ d.property_name }}</h3>
                <p><strong>Type:</strong> {{ d.discrepancy_type.value }}</p>
                <p><strong>Constraint:</strong> {{ d.constraint_type }}</p>
                <p><strong>Spec Value:</strong> <code>{{ d.spec_value }}</code></p>
                <p><strong>API Behavior:</strong> <code>{{ d.api_behavior }}</code></p>
                {% if d.recommendation %}
                <p><strong>Recommendation:</strong> {{ d.recommendation }}</p>
                {% endif %}
            </div>
            {% endfor %}
        </div>
        {% endif %}
    </div>
</body>
</html>
"""


def create_report_generator(config: dict) -> ReportGenerator:
    """Create a report generator from configuration."""
    report_config = ReportConfig(
        output_dir=Path(config.get("output_dir", "reports")),
        formats=config.get("formats", ["json", "html", "markdown"]),
        include_examples=config.get("include_examples", True),
        max_examples_per_issue=config.get("max_examples_per_issue", 5),
    )
    return ReportGenerator(report_config)
