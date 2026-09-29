"""
render.py

Responsibility: Convert a raw PDF file into an extractable text/image representation.

For text-based PDFs, extract the embedded text layer directly.
For image-based (scanned) PDFs, render each page to an image so the vision LLM can read it.
Returns a list of page representations (text strings or base64-encoded images)
that the extract module can consume.
"""
