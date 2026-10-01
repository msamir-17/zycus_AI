"""
run.py -- Entry point for The Bookable Payable pipeline.

Usage:
    python run.py [--docs documents/] [--out output/] [--master-data master_data/]

For each PDF in the documents folder, runs the full pipeline
(render -> classify -> extract -> ground -> resolve -> verify -> format)
and writes one output/<pdf_stem>.json per input <pdf_stem>.pdf.

Conforms strictly to AUTODRAFT_SCHEMA.md.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

# Ensure 'src' is in python search path
_REPO_ROOT = Path(__file__).resolve().parent
_SRC_DIR = _REPO_ROOT / "src"
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from bookable.pipeline import process_document
from bookable.resolve import MasterDataIndex


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="The Bookable Payable -- End-to-End Invoice Processing CLI"
    )
    parser.add_argument(
        "--docs",
        type=str,
        default="documents",
        help="Path to documents folder or single PDF file (default: documents/)",
    )
    parser.add_argument(
        "--out",
        type=str,
        default="output",
        help="Destination directory for output JSON files (default: output/)",
    )
    parser.add_argument(
        "--master-data",
        type=str,
        default="master_data",
        help="Path to master data directory (default: master_data/)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Optional limit on number of documents to process",
    )
    return parser.parse_args()


def discover_pdfs(docs_path: Path) -> list[Path]:
    """Find all PDF files under the specified path, sorted alphabetically."""
    if not docs_path.exists():
        raise FileNotFoundError(f"Documents path not found: {docs_path}")

    if docs_path.is_file() and docs_path.suffix.lower() == ".pdf":
        return [docs_path]

    if docs_path.is_dir():
        pdfs = sorted(docs_path.glob("*.pdf"), key=lambda p: p.name.lower())
        return pdfs

    raise ValueError(f"Path is neither a directory nor a PDF file: {docs_path}")


def main() -> int:
    args = parse_args()
    docs_path = Path(args.docs)
    out_dir = Path(args.out)
    master_data_dir = Path(args.master_data)

    print("=" * 70)
    print("The Bookable Payable -- Execution Pipeline")
    print(f"Documents:   {docs_path}")
    print(f"Output:      {out_dir}")
    print(f"Master Data: {master_data_dir}")
    print("=" * 70)

    try:
        pdfs = discover_pdfs(docs_path)
    except Exception as e:
        print(f"Error discovering documents: {e}", file=sys.stderr)
        return 1

    if not pdfs:
        print(f"No PDF files found at {docs_path}", file=sys.stderr)
        return 1

    if args.limit and args.limit > 0:
        pdfs = pdfs[:args.limit]

    out_dir.mkdir(parents=True, exist_ok=True)

    # Initialize master data index once for all documents
    try:
        master_index = MasterDataIndex(master_data_dir)
    except Exception as e:
        print(f"Failed to load master data from {master_data_dir}: {e}", file=sys.stderr)
        return 1

    total_docs = len(pdfs)
    start_time = time.time()

    processed_count = 0
    total_payables = 0
    total_declined = 0
    exact_matches = 0
    erp_mismatches = 0
    errors = 0

    for idx, pdf in enumerate(pdfs, 1):
        rel_name = pdf.name
        print(f"[{idx:02d}/{total_docs:02d}] Processing {rel_name}...", end=" ", flush=True)
        doc_start = time.time()

        try:
            final_output, verifications = process_document(
                pdf_path=pdf,
                master_index=master_index,
                out_dir=out_dir
            )
            elapsed = time.time() - doc_start
            processed_count += 1

            n_pay = len(final_output.get("payables", []))
            n_dec = len(final_output.get("declined", []))
            total_payables += n_pay
            total_declined += n_dec

            if n_pay == 0 and n_dec > 0:
                reason = final_output["declined"][0].get("reason", "Decline")
                short_r = (reason[:40] + "...") if len(reason) > 40 else reason
                print(f"[DECLINED] ({short_r}) ({elapsed:.1f}s)")
            else:
                matches = sum(1 for v in verifications if v.get("is_match"))
                exact_matches += matches
                erp_mismatches += (n_pay - matches)
                match_str = f"ERP: {matches}/{n_pay} match"
                print(f"[OK] {n_pay} payable(s) [{match_str}] ({elapsed:.1f}s)")

        except Exception as e:
            errors += 1
            print(f"[ERROR] {e}")

    total_time = time.time() - start_time
    print("=" * 70)
    print("Summary:")
    print(f"  Processed documents: {processed_count}/{total_docs}")
    print(f"  Payables emitted:    {total_payables}")
    print(f"  Declined documents:  {total_declined}")
    print(f"  ERP Exact Matches:   {exact_matches}")
    print(f"  ERP Delta/Review:    {erp_mismatches}")
    if errors > 0:
        print(f"  Fatal Errors:        {errors}")
    print(f"  Total Duration:      {total_time:.2f}s")
    print(f"  Outputs saved in:    {out_dir.resolve()}")
    print("=" * 70)

    return 1 if (errors > 0 and processed_count == 0) else 0


if __name__ == "__main__":
    sys.exit(main())
