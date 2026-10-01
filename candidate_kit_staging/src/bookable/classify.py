"""
src/bookable/classify.py

Responsibility: Classify document type and perform page segmentation before field extraction.

Determines for a given PDF:
  - document_type: INVOICE | CREDIT_MEMO | CUSTOMS_DOCUMENT | STATEMENT | NON_PAYABLE | OTHER
  - decision: PROCESS | DECLINE | SPLIT | MANUAL_REVIEW
  - document_count: integer (0 if non-payable, 1+ if bookable payables present)
  - page_segmentation: list of page segments mapped to document sections
  - decline_reason: string explanation if decision is DECLINE
  - rationale: clear Hinglish / English explanation grounded in visible content
"""

import os
import time
import io
import hashlib
from pathlib import Path
from typing import Dict, Any, List, Optional
import pymupdf as fitz
from PIL import Image
from dotenv import load_dotenv
from google import genai
from google.genai import types
from google.genai.errors import ServerError, ClientError

load_dotenv()


def call_gemini_with_retry(client: genai.Client, model: str, contents: list, config: Optional[types.GenerateContentConfig] = None, max_retries: int = 4) -> Any:
    """Execute Gemini API call with exponential backoff for 503 / 429 transient errors."""
    for attempt in range(max_retries):
        try:
            return client.models.generate_content(
                model=model,
                contents=contents,
                config=config
            )
        except (ServerError, ClientError) as e:
            err_msg = str(e)
            if "503" in err_msg or "429" in err_msg or "TEMPORARY" in err_msg.upper() or "UNAVAILABLE" in err_msg.upper():
                wait_seconds = (attempt + 1) * 4
                print(f"[Retry Warning] Gemini API hit rate limit / 503 error. Waiting {wait_seconds}s before attempt {attempt+2}/{max_retries}...")
                time.sleep(wait_seconds)
            else:
                raise e
    raise RuntimeError(f"Gemini API call failed after {max_retries} retries due to persistent rate limiting or server error.")


def render_pdf_for_classification(pdf_path: Path, max_pages: int = 20) -> tuple[List[Dict[str, Any]], List[Image.Image]]:
    """
    Render PDF pages to PIL Images and extract embedded text layer.
    For multi-page PDFs, resizes images to keep payload size lightweight.
    """
    doc = fitz.open(pdf_path)
    page_data = []
    images = []
    
    total_pages = len(doc)
    # Select page indices to render: if total > max_pages, select representative pages
    if total_pages <= max_pages:
        page_indices = list(range(total_pages))
    else:
        # First 5, middle 5, last 5
        page_indices = sorted(list(set(
            list(range(5)) + 
            list(range(total_pages // 2 - 2, total_pages // 2 + 3)) + 
            list(range(total_pages - 5, total_pages))
        )))

    for p_idx in page_indices:
        page = doc[p_idx]
        text_layer = page.get_text("text").strip()
        
        # Determine render resolution: 150 DPI for short docs, 100 DPI for multi-page docs
        dpi = 100 if total_pages > 3 else 150
        pix = page.get_pixmap(dpi=dpi)
        img = Image.open(io.BytesIO(pix.tobytes("jpeg", jpg_quality=85)))
        
        # Max side constraint: 1000px
        img.thumbnail((1000, 1000))
        
        page_info = {
            "page_num": p_idx + 1,
            "text": text_layer,
            "has_text": len(text_layer) > 20
        }
        page_data.append(page_info)
        images.append(img)
        
    doc.close()
    return page_data, images


from bookable.llm import call_vision_llm


def classify_pdf(pdf_path: str | Path, model_name: Optional[str] = None) -> Dict[str, Any]:
    """
    Classify a single PDF document using the unified vision LLM layer with automatic fallback.
    Returns structured dict containing document_type, decision, page_segmentation, etc.
    """
    path = Path(pdf_path)
    if not path.exists():
        raise FileNotFoundError(f"PDF file not found: {path}")

    page_data, images = render_pdf_for_classification(path)
    total_pages = len(page_data)

    # Prompt construction with Section 9 general segmentation rule
    prompt = (
        "You are an expert Accounts Payable (AP) Document Classification & Page Segmentation Engine for an ERP system.\n"
        f"Document filename: {path.name}\n"
        f"Total pages rendered: {total_pages}\n\n"
        "TASK:\n"
        "1. Examine the visual layout and text of all pages.\n"
        "2. Determine the Document Type:\n"
        "   - 'INVOICE': Vendor commercial invoice for goods/services purchased.\n"
        "   - 'CREDIT_MEMO': Credit note / credit memo adjusting payable balance.\n"
        "   - 'CUSTOMS_DOCUMENT': Customs declaration, customs consolidated invoice, or border manifest (NON-PAYABLE for AP).\n"
        "   - 'STATEMENT': Account statement, summary billing statement without line item charge ledger (NON-PAYABLE).\n"
        "   - 'NON_PAYABLE': Shipping note, purchase order, internal checklist, or non-payable attachment.\n"
        "3. Make a Processing Decision:\n"
        "   - 'PROCESS': Bookable payable ready for extraction.\n"
        "   - 'DECLINE': Non-payable document (customs declaration, statement, internal form).\n"
        "   - 'SPLIT': Multi-invoice document containing separate distinct payables.\n"
        "   - 'MANUAL_REVIEW': Uncertain or ambiguous layout.\n"
        "5. MIXED-DOCUMENT & ATTACHMENT SEGMENTATION RULE:\n"
        "   - When a multi-page document contains mixed page types (e.g. commercial invoice followed by shipping notes,\n"
        "     air waybills, cartage advice, cargo documents, or customs clearance receipts):\n"
        "     * Inspect leading/early pages for explicit billing demand signals: header titled 'TAX INVOICE' / 'INVOICE',\n"
        "       invoice/reference number, seller and customer details, payment terms or due date, and itemized charges/total.\n"
        "     * If early pages form an authentic commercial payable, classify the document as 'PROCESS'.\n"
        "     * Group the commercial invoice pages into the payable segment (type: 'INVOICE', pages: [1..k]).\n"
        "     * Classify subsequent transport/customs/cargo pages as attachments (type: 'SUPPORTING_DOC').\n"
        "     * Do NOT let the majority of supporting pages override an explicit commercial invoice segment.\n"
        "     * Use page content only; evidence of a demand for payment is still strictly required.\n"
        "     * If the document consists purely of shipping notes or customs forms with NO demand for payment, classify as 'DECLINE'.\n"
        "6. GENERAL SEGMENTATION RULE (Rule 9):\n"
        "   A new payable begins ONLY when there is a new invoice number OR a new primary seller/billing party.\n"
        "   Later pages that repeat the same PO/reference/amount and belong to a third party (e.g. utility bill, delivery note,\n"
        "   or backup statement attached as pass-through documentation) are ATTACHMENTS of the parent payable, NOT separate payables.\n"
        "   Do NOT double-count the same amount. Group such attached pages together with the parent payable into ONE segment [1..N].\n"
        "7. Rationale: Clear plain-English explanation detailing WHY this classification was chosen based on visible features.\n\n"
        "Respond ONLY in valid JSON matching this schema:\n"
        "{\n"
        '  "filename": "' + path.name + '",\n'
        '  "document_type": "INVOICE | CREDIT_MEMO | CUSTOMS_DOCUMENT | STATEMENT | NON_PAYABLE | OTHER",\n'
        '  "decision": "PROCESS | DECLINE | SPLIT | MANUAL_REVIEW",\n'
        '  "document_count": 1,\n'
        '  "page_segmentation": [\n'
        '    {\n'
        '      "segment_id": 1,\n'
        '      "type": "INVOICE | CREDIT_MEMO | SUPPORTING_DOC | DECLINED_DOC",\n'
        '      "pages": [1],\n'
        '      "description": "Short section summary"\n'
        '    }\n'
        '  ],\n'
        '  "decline_reason": "Explanation if decision is DECLINE, else empty string",\n'
        '  "rationale": "Clear grounded explanation"\n'
        "}"
    )

    text_hints = [f"Page {p['page_num']}: {p['text'][:600]}" for p in page_data if p["has_text"]]

    pdf_sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
    rendered_pages = [p["page_num"] for p in page_data]

    # Check v2_mixed_doc_segmentation cache first, falling back to v1_classify
    from bookable.llm import CACHE_DIR, compute_source_cache_key
    model_str = model_name or os.getenv("GEMINI_MODEL", "gemini-3.1-flash-lite")
    v2_key = compute_source_cache_key(pdf_sha256, rendered_pages, "v2_mixed_doc_segmentation", model_str, "v4_quality_fixes")
    prompt_ver = "v2_mixed_doc_segmentation" if (CACHE_DIR / f"{v2_key}.json").exists() else "v1_classify"

    cache_ctx = {
        "pdf_sha256": pdf_sha256,
        "pages": rendered_pages,
        "prompt_version": prompt_ver,
        "model": model_str,
        "schema_version": "v4_quality_fixes"
    }

    try:
        result, usage = call_vision_llm(
            prompt, images, text_hints=text_hints,
            gemini_model=model_name, openai_model=model_name,
            detail="low", cache_context=cache_ctx
        )
        result["filename"] = path.name
        result["_llm_usage"] = usage
        return result
    except Exception as e:
        return {
            "filename": path.name,
            "document_type": "UNSURE",
            "decision": "MANUAL_REVIEW",
            "document_count": 0,
            "page_segmentation": [],
            "decline_reason": str(e),
            "rationale": f"LLM execution error: {e}",
            "_llm_usage": {"provider": "none", "error": str(e)}
        }

