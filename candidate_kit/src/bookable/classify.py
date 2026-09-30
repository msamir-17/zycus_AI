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
import json
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


def classify_pdf(pdf_path: str | Path, client: Optional[genai.Client] = None, model_name: Optional[str] = None) -> Dict[str, Any]:
    """
    Classify a single PDF document using Gemini Vision.
    Returns structured dict containing document_type, decision, page_segmentation, etc.
    """
    path = Path(pdf_path)
    if not path.exists():
        raise FileNotFoundError(f"PDF file not found: {path}")
        
    if client is None:
        client = genai.Client()
    if model_name is None:
        model_name = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
        
    page_data, images = render_pdf_for_classification(path)
    total_pages = len(page_data)

    # Prompt construction
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
        "4. Document Count: Integer count of distinct bookable payable documents found (0 if DECLINE).\n"
        "5. Page Segmentation: Map pages to document sections or separate invoices.\n"
        "6. Rationale: Clear plain-English / Hinglish explanation detailing WHY this classification was chosen based on visible features.\n\n"
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
    
    contents = [prompt]
    for idx, (p_info, img) in enumerate(zip(page_data, images)):
        contents.append(f"--- PAGE {p_info['page_num']} ---")
        if p_info['has_text']:
            contents.append(f"Extracted Text Layer Snippet:\n{p_info['text'][:500]}")
        contents.append(img)
        
    config = types.GenerateContentConfig(
        response_mime_type="application/json",
        temperature=0.1
    )
    
    response = call_gemini_with_retry(client, model_name, contents, config=config)
    
    try:
        result = json.loads(response.text)
        result["filename"] = path.name
        return result
    except json.JSONDecodeError as e:
        return {
            "filename": path.name,
            "document_type": "UNSURE",
            "decision": "MANUAL_REVIEW",
            "document_count": 0,
            "page_segmentation": [],
            "decline_reason": "",
            "rationale": f"Failed to parse JSON response from Gemini: {e}\nRaw output: {response.text[:300]}"
        }
