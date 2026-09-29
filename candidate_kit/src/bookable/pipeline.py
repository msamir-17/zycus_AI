"""
pipeline.py

Responsibility: Orchestrate the full end-to-end pipeline for one PDF.

Given the path to a PDF file and the loaded master-data indexes, runs:
  render  -> classify  -> extract  -> ground  -> resolve  -> verify

Returns the output object for that file in the contract format:
  {
    "file": "X.pdf",
    "payables": [ <payable>, ... ],
    "declined": [ { "doc_type": "...", "reason": "..." }, ... ]
  }

Also handles caching of intermediate results (rendered pages, extraction outputs)
to avoid redundant LLM calls when re-running on the same document.
"""
