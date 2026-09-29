"""
ground.py

Responsibility: Validate and ground extracted raw values to what the document actually says.

This is the firewall between extraction and assembly. It:
  - Normalises number formats (locale-specific separators -> dot-decimal).
  - Confirms that every value used in the autodraft can be traced back to the document.
  - Flags missing required fields and unresolvable ambiguities.
  - Does NOT invent or adjust values to make the total balance. If a value is absent,
    it stays absent; if a total does not foot, that is recorded as an anomaly, not patched.

Rule 1 lives here: only document-grounded values pass through.
"""
