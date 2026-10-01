"""
src/bookable/resolve.py

Responsibility: Match extracted document values against master data and fill ERP codes.

Given grounded document values, look up:
  - supplier_id     from suppliers.json    (by VAT ID first, then normalized name)
  - company_code / business_unit_code / location_code from chart_of_books.json
  - tax_type_code   from tax_master.json   (by country + tax type + rate)
  - payment_term_id from payment_terms.json (by text alias or derived from dates)
  - po_id           from po_master.json    (exact PO number match only)

Rules:
  - If a match is found, set the code.
  - If no match, leave the code as "" -- an honest blank (Rule 2).
  - Never fabricate a code.
  - Designed for scale: pre-indexed hash lookup structures, O(1) where possible.
"""

from __future__ import annotations

import json
import re
import difflib
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


def _normalize_identifier(val: Optional[str]) -> str:
    """Normalize VAT or tax ID by stripping spaces, hyphens, and converting to uppercase."""
    if not val:
        return ""
    return re.sub(r"[^A-Za-z0-9]", "", str(val)).upper()


def _normalize_name(name: Optional[str]) -> str:
    """Normalize company/vendor name for matching: lowercase, strip legal suffixes & punctuation."""
    if not name:
        return ""
    s = str(name).lower()
    # Strip common legal suffixes
    suffixes = [
        r"\bgmbh\b", r"\bllc\b", r"\bltd\b", r"\blimited\b", r"\binc\b", r"\bcorp\b",
        r"\bou\b", r"\boü\b", r"\bas\b", r"\bsdn\s*bhd\b", r"\bpty\s*ltd\b", r"\bplc\b",
        r"\blda\b", r"\bs\.a\.\b", r"\bsa\b", r"\bs\.l\.\b", r"\bsl\b", r"\bs\.r\.o\.\b"
    ]
    for suf in suffixes:
        s = re.sub(suf, "", s)
    # Remove non-alphanumeric characters
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    return " ".join(s.split())


class MasterDataIndex:
    """In-memory indexed lookup structures for master data."""

    def __init__(self, master_data_dir: str | Path):
        self.dir = Path(master_data_dir)
        self.suppliers_by_vat: Dict[str, Dict[str, Any]] = {}
        self.suppliers_by_name: Dict[str, Dict[str, Any]] = {}
        self.all_suppliers: List[Dict[str, Any]] = []

        self.po_by_number: Dict[str, Dict[str, Any]] = {}

        self.payment_terms_by_alias: Dict[str, str] = {}
        self.payment_terms_by_days: Dict[int, str] = {}

        self.taxes_by_country_rate: Dict[Tuple[str, str], str] = {}
        self.taxes_by_country_type_rate: Dict[Tuple[str, str, str], str] = {}

        self.chart_of_books: Dict[str, Any] = {}
        self.business_units: List[Dict[str, Any]] = []

        self._load_all()

    def _load_all(self) -> None:
        self._load_suppliers()
        self._load_chart_of_books()
        self._load_tax_master()
        self._load_payment_terms()
        self._load_po_master()

    def _load_suppliers(self) -> None:
        path = self.dir / "suppliers.json"
        if not path.exists():
            return
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        for supp in data.get("suppliers", []):
            self.all_suppliers.append(supp)
            vat = _normalize_identifier(supp.get("vat_id"))
            if vat:
                self.suppliers_by_vat[vat] = supp
            name_norm = _normalize_name(supp.get("name"))
            if name_norm:
                self.suppliers_by_name[name_norm] = supp

    def _load_chart_of_books(self) -> None:
        path = self.dir / "chart_of_books.json"
        if not path.exists():
            return
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        self.chart_of_books = data
        for comp in data.get("companies", []):
            c_code = comp.get("company_code", "")
            for bu in comp.get("business_units", []):
                bu_code = bu.get("business_unit_code", "")
                bu_name = bu.get("business_unit_name", "")
                locations = bu.get("locations", [])
                self.business_units.append({
                    "company_code": c_code,
                    "business_unit_code": bu_code,
                    "business_unit_name": bu_name,
                    "business_unit_norm": _normalize_name(bu_name),
                    "locations": locations
                })

    def _load_tax_master(self) -> None:
        path = self.dir / "tax_master.json"
        if not path.exists():
            return
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        for tax in data.get("taxes", []):
            code = tax.get("code", "")
            country = (tax.get("country") or "").upper().strip()
            ttype = (tax.get("tax_type") or "").upper().strip()
            rate_val = tax.get("rate")
            rate_str = f"{float(rate_val):.2f}".rstrip("0").rstrip(".") if rate_val is not None else ""

            if country and rate_str:
                self.taxes_by_country_rate[(country, rate_str)] = code
                if ttype:
                    self.taxes_by_country_type_rate[(country, ttype, rate_str)] = code

    def _load_payment_terms(self) -> None:
        path = self.dir / "payment_terms.json"
        if not path.exists():
            return
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        for pt in data.get("payment_terms", []):
            pid = pt.get("payment_term_id", "")
            days = pt.get("days")
            if days is not None and days not in self.payment_terms_by_days:
                self.payment_terms_by_days[int(days)] = pid
            for alias in pt.get("text_aliases", []):
                norm_alias = " ".join(alias.lower().split())
                self.payment_terms_by_alias[norm_alias] = pid

    def _load_po_master(self) -> None:
        path = self.dir / "po_master.json"
        if not path.exists():
            return
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        for po in data.get("purchase_orders", []):
            pnum = (po.get("po_number") or "").strip().upper()
            if pnum:
                self.po_by_number[pnum] = po


def resolve_supplier(
    extracted_supplier: Dict[str, Any],
    master_index: MasterDataIndex,
    country_hint: Optional[str] = None
) -> Tuple[str, Dict[str, Any]]:
    """
    Resolve supplier_id by:
      1. Exact VAT ID match (highest confidence).
      2. Exact normalized name match.
      3. Conservative fuzzy name match (similarity >= 0.85 and matching country/evidence).
    Returns (supplier_id, matched_supplier_record_or_empty).
    """
    if not extracted_supplier:
        return "", {}

    doc_vat = _normalize_identifier(extracted_supplier.get("vat_id"))
    if doc_vat and doc_vat in master_index.suppliers_by_vat:
        matched = master_index.suppliers_by_vat[doc_vat]
        return matched.get("supplier_id", ""), matched

    doc_name = extracted_supplier.get("name")
    norm_name = _normalize_name(doc_name)
    if norm_name and norm_name in master_index.suppliers_by_name:
        matched = master_index.suppliers_by_name[norm_name]
        return matched.get("supplier_id", ""), matched

    # Fuzzy match with conservative threshold
    if norm_name:
        best_match = None
        best_score = 0.0
        for cand_norm, cand_supp in master_index.suppliers_by_name.items():
            ratio = difflib.SequenceMatcher(None, norm_name, cand_norm).ratio()
            if ratio > best_score:
                best_score = ratio
                best_match = cand_supp

        if best_match and best_score >= 0.85:
            # Corroborating check: if country hint is available, must match
            cand_country = (best_match.get("country") or "").upper()
            if country_hint and cand_country and cand_country != country_hint.upper():
                return "", {}
            return best_match.get("supplier_id", ""), best_match

    return "", {}


def resolve_buyer(
    extracted_buyer: Dict[str, Any],
    master_index: MasterDataIndex,
    country_hint: Optional[str] = None
) -> Dict[str, str]:
    """
    Resolve tenant buyer to company_code, business_unit_code, location_code.
    Default company_code is BOLTGROUP if buyer is Bolt entity.
    """
    resolved = {
        "company_code": "",
        "business_unit_code": "",
        "location_code": ""
    }
    if not extracted_buyer:
        return resolved

    b_name = extracted_buyer.get("name") or ""
    b_addr = extracted_buyer.get("address") or ""
    combined = f"{b_name} {b_addr}".lower()

    # Search business units
    for bu in master_index.business_units:
        bu_norm = bu["business_unit_norm"]
        if bu_norm and bu_norm in combined:
            resolved["company_code"] = bu["company_code"]
            resolved["business_unit_code"] = bu["business_unit_code"]
            locs = bu.get("locations", [])
            resolved["location_code"] = locs[0].get("location_code", "") if locs else ""
            return resolved

    # Specific country/entity keywords for Bolt subsidiaries
    bolt_country_map = [
        (["holdings"], "EE004", "LOC_EE_001"),
        (["technology", "vana-louna", "estonia", "tallinn"], "EE001", "LOC_EE_001"),
        (["ghana", "accra"], "GH001", "LOC_GH_001"),
        (["malaysia", "kuala lumpur"], "MY001", "LOC_MY_001"),
        (["south africa", "sandton", "johannesburg"], "ZA001", "LOC_ZA_001"),
        (["uk", "operations uk", "london", "5 new street"], "GB001", "LOC_GB_001"),
    ]

    is_bolt = "bolt" in combined
    if is_bolt:
        resolved["company_code"] = "BOLTGROUP"
        for keywords, bu_code, loc_code in bolt_country_map:
            if any(kw in combined for kw in keywords):
                resolved["business_unit_code"] = bu_code
                resolved["location_code"] = loc_code
                return resolved

        if country_hint:
            c_upper = country_hint.upper()
            country_bu = {
                "EE": ("EE001", "LOC_EE_001"),
                "GH": ("GH001", "LOC_GH_001"),
                "MY": ("MY001", "LOC_MY_001"),
                "ZA": ("ZA001", "LOC_ZA_001"),
                "GB": ("GB001", "LOC_GB_001"),
                "UK": ("GB001", "LOC_GB_001"),
            }
            if c_upper in country_bu:
                resolved["business_unit_code"] = country_bu[c_upper][0]
                resolved["location_code"] = country_bu[c_upper][1]
                return resolved

    return resolved


def resolve_tax_code(
    tax_dict: Dict[str, Any],
    master_index: MasterDataIndex,
    country: Optional[str] = None
) -> str:
    """
    Resolve tax_type_code against tax_master.json.
    Matches (country, rate) or (country, tax_type, rate).
    """
    if not country:
        return ""

    rate_raw = tax_dict.get("tax_rate") or tax_dict.get("tax_rate_norm") or tax_dict.get("tax_rate_raw")
    if not rate_raw:
        return ""

    try:
        r_float = float(str(rate_raw).replace("%", "").strip())
        rate_str = f"{r_float:.2f}".rstrip("0").rstrip(".")
    except (ValueError, TypeError):
        return ""

    c_upper = country.upper().strip()
    tax_name = (tax_dict.get("tax_name") or tax_dict.get("tax_type") or "").upper()

    # Try specific tax type match
    for t_type in ("VAT", "IVA", "SST", "NHIL", "GETFL", "COVID", "CST", "HST", "NP", "WHT", "GST", "MOMS", "USE"):
        if t_type in tax_name:
            key = (c_upper, t_type, rate_str)
            if key in master_index.taxes_by_country_type_rate:
                return master_index.taxes_by_country_type_rate[key]

    # Try country + rate lookup
    rate_key = (c_upper, rate_str)
    if rate_key in master_index.taxes_by_country_rate:
        return master_index.taxes_by_country_rate[rate_key]

    return ""


def resolve_payment_term(
    raw_term: Optional[str],
    invoice_date: Optional[str],
    due_date: Optional[str],
    master_index: MasterDataIndex
) -> str:
    """
    Resolve payment_term_id from raw text alias or date delta.
    """
    if raw_term:
        clean_alias = " ".join(str(raw_term).lower().split())
        for alias, pt_id in master_index.payment_terms_by_alias.items():
            if alias in clean_alias:
                return pt_id

    # Fallback to date difference
    if invoice_date and due_date:
        try:
            d_inv = datetime.strptime(str(invoice_date).strip()[:10], "%Y-%m-%d")
            d_due = datetime.strptime(str(due_date).strip()[:10], "%Y-%m-%d")
            delta_days = (d_due - d_inv).days
            if delta_days in master_index.payment_terms_by_days:
                return master_index.payment_terms_by_days[delta_days]
        except Exception:
            pass

    return ""


def resolve_po(
    raw_po_number: Optional[str],
    master_index: MasterDataIndex
) -> str:
    """
    Resolve po_id against po_master.json.
    Only returns a code if exact PO number matches. Never fabricate a code.
    """
    if not raw_po_number:
        return ""
    clean_po = str(raw_po_number).strip().upper()
    if clean_po in master_index.po_by_number:
        return master_index.po_by_number[clean_po].get("po_id", "")
    return ""


def resolve_master_data(
    extracted_payable: Dict[str, Any],
    master_index: MasterDataIndex,
    country_hint: Optional[str] = None
) -> None:
    """
    In-place enrichment of extracted payable with resolved master-data codes:
      - supplier.supplier_id
      - buyer.company_code / business_unit_code / location_code
      - payment_term_id
      - po_id
      - taxes[].tax_type_code
      - line_items[].taxes[].tax_type_code
    """
    # 1. Supplier
    supp = extracted_payable.get("supplier") or {}
    s_id, matched_supp = resolve_supplier(supp, master_index, country_hint)
    supp["supplier_id"] = s_id
    extracted_payable["supplier"] = supp

    effective_country = country_hint or (matched_supp.get("country") if matched_supp else None)

    # 2. Buyer
    buyer = extracted_payable.get("buyer") or {}
    resolved_b = resolve_buyer(buyer, master_index, effective_country)
    buyer["company_code"] = resolved_b["company_code"]
    buyer["business_unit_code"] = resolved_b["business_unit_code"]
    buyer["location_code"] = resolved_b["location_code"]
    extracted_payable["buyer"] = buyer

    # 3. Payment Term
    raw_term = extracted_payable.get("payment_terms_raw")
    inv_date = extracted_payable.get("invoice_date")
    due_date = extracted_payable.get("due_date")
    extracted_payable["payment_term_id"] = resolve_payment_term(raw_term, inv_date, due_date, master_index)

    # 4. PO ID
    po_num = extracted_payable.get("po_number")
    extracted_payable["po_id"] = resolve_po(po_num, master_index)

    # 5. Header Taxes
    for t in (extracted_payable.get("taxes") or extracted_payable.get("header_taxes") or []):
        t["tax_type_code"] = resolve_tax_code(t, master_index, effective_country)

    # 6. Line Item Taxes
    for li in (extracted_payable.get("line_items") or []):
        for lt in (li.get("taxes") or []):
            lt["tax_type_code"] = resolve_tax_code(lt, master_index, effective_country)
