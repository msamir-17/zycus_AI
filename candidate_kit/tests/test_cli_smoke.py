"""
tests/test_cli_smoke.py

Smoke test for the CLI entrypoint run.py:
  1. Tests that `python run.py --help` exits with code 0 and displays CLI usage.
  2. Tests that `python run.py --docs <sample.pdf> --out <temp_dir>` executes cleanly,
     creates the expected JSON output file, and exits with code 0.
"""

import json
import subprocess
import sys
import tempfile
from pathlib import Path


def test_cli_help():
    """Verify `python run.py --help` displays usage without error."""
    result = subprocess.run(
        [sys.executable, "run.py", "--help"],
        capture_output=True,
        text=True,
        cwd=Path(__file__).resolve().parent.parent,
    )
    assert result.returncode == 0
    assert "The Bookable Payable" in result.stdout
    assert "--docs" in result.stdout
    assert "--out" in result.stdout


def test_cli_execution_single_doc():
    """Verify `python run.py` executes end-to-end on a single PDF and writes output JSON."""
    repo_root = Path(__file__).resolve().parent.parent
    sample_pdf = repo_root / "documents" / "DU-02.pdf"
    if not sample_pdf.exists():
        # Fallback to any existing PDF
        all_pdfs = list((repo_root / "documents").glob("*.pdf"))
        assert all_pdfs, "No test PDFs found in documents/"
        sample_pdf = all_pdfs[0]

    with tempfile.TemporaryDirectory() as tmpdir:
        result = subprocess.run(
            [
                sys.executable,
                "run.py",
                "--docs",
                str(sample_pdf),
                "--out",
                str(tmpdir),
                "--limit",
                "1",
            ],
            capture_output=True,
            text=True,
            cwd=repo_root,
        )

        assert result.returncode == 0, f"run.py failed:\n{result.stderr}\n{result.stdout}"

        expected_json = Path(tmpdir) / f"{sample_pdf.stem}.json"
        assert expected_json.exists(), f"Expected output {expected_json} was not created!"

        with open(expected_json, "r", encoding="utf-8") as f:
            data = json.load(f)

        assert data["file"] == sample_pdf.name
        assert "payables" in data
        assert "declined" in data
