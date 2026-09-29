"""
run.py -- Entry point for The Bookable Payable pipeline.

Usage:
    python run.py [--docs documents/] [--out output/]

For each PDF in the documents folder, runs the full pipeline
(render -> classify -> extract -> ground -> resolve -> verify)
and writes one output/X.json per input X.pdf.

See README.md for the single documented command.
"""
