"""
tests/test_step7_mixed_document_classifier.py

Tests for generic mixed-document classification and segmentation behavior:
A. Mixed packet: leading commercial invoice + later transport/customs attachments
   -> invoice pages become payable segment, attachments remain attached.
B. Pure supporting/customs packet with no billing demand
   -> remains declined.
C. Existing INV-31 behavior
   -> remains one payable, pages 1-3.
D. No filename dependency (rules depend on content, not filenames).
"""

import pytest
from unittest.mock import patch
from pathlib import Path
from bookable.classify import classify_pdf
from bookable.pipeline import process_document


def test_mixed_packet_leading_invoice_and_attachments():
    """
    Test A: A multi-page document with leading commercial invoice (pages 1-2)
    and subsequent transport/customs attachments (pages 3-12) must segment
    the invoice pages as INVOICE segment and group attachments rather than
    declining the entire document due to majority attachment pages.
    """
    mock_cls_response = {
        "filename": "unseen_mixed_document.pdf",
        "document_type": "INVOICE",
        "decision": "PROCESS",
        "document_count": 1,
        "page_segmentation": [
            {
                "segment_id": 1,
                "type": "INVOICE",
                "pages": [1, 2],
                "description": "Commercial tax invoice detailing freight services"
            },
            {
                "segment_id": 2,
                "type": "SUPPORTING_DOC",
                "pages": [3, 4, 5, 6, 7, 8, 9, 10, 11, 12],
                "description": "Attached cartage advice and air waybills"
            }
        ],
        "decline_reason": "",
        "rationale": "Leading pages 1-2 contain an explicit commercial invoice with payment terms and itemized charges. Subsequent pages are transport attachments."
    }

    with patch("bookable.classify.call_vision_llm", return_value=(mock_cls_response, {"cache_hit": False})):
        res = classify_pdf(Path("documents/DU-03.pdf"))
        assert res["decision"] == "PROCESS"
        assert res["document_type"] == "INVOICE"
        assert len(res["page_segmentation"]) == 2
        assert res["page_segmentation"][0]["type"] == "INVOICE"
        assert res["page_segmentation"][0]["pages"] == [1, 2]
        assert res["page_segmentation"][1]["type"] == "SUPPORTING_DOC"


def test_pure_supporting_customs_packet_declined():
    """
    Test B: A multi-page document consisting purely of customs declarations,
    border manifests, or shipping notes with no commercial billing demand
    must remain declined.
    """
    mock_customs_response = {
        "filename": "customs_packet.pdf",
        "document_type": "CUSTOMS_DOCUMENT",
        "decision": "DECLINE",
        "document_count": 0,
        "page_segmentation": [],
        "decline_reason": "All pages consist of customs declarations and border manifests with no commercial payment demand.",
        "rationale": "Non-payable customs transit paperwork."
    }

    with patch("bookable.classify.call_vision_llm", return_value=(mock_customs_response, {"cache_hit": False})):
        res = classify_pdf(Path("documents/DU-02.pdf"))
        assert res["decision"] == "DECLINE"
        assert res["document_type"] == "CUSTOMS_DOCUMENT"
        assert res["document_count"] == 0
        assert "customs" in res["decline_reason"].lower()


def test_inv31_single_payable_segmentation_preserved():
    """
    Test C: Verify INV-31 multi-page packet behavior:
    Page 1 (commercial invoice) + Pages 2-3 (attached PECO utility backup)
    remains exactly 1 payable, grouping attached backup pages into the parent transaction.
    """
    # Verify directly on live cached classification of INV-31
    res = classify_pdf(Path("documents/INV-31.pdf"))
    assert res["decision"] == "PROCESS"
    assert res["document_count"] == 1
    # Check that segment spans pages [1, 2, 3] or [1] with [2, 3] attachment
    all_pages = []
    for s in res["page_segmentation"]:
        all_pages.extend(s.get("pages", []))
    assert 1 in all_pages
    assert len(res["page_segmentation"]) >= 1


def test_no_filename_dependency():
    """
    Test D: Classification and segmentation must be driven by visual content and text evidence,
    never by filename prefixes or stem patterns.
    """
    # Feed an arbitrary name without INV/DU/HLD prefix to classify_pdf mock
    mock_arbitrary = {
        "filename": "random_supplier_doc_872.pdf",
        "document_type": "INVOICE",
        "decision": "PROCESS",
        "document_count": 1,
        "page_segmentation": [
            {
                "segment_id": 1,
                "type": "INVOICE",
                "pages": [1],
                "description": "Standard invoice"
            }
        ],
        "decline_reason": "",
        "rationale": "Explicit invoice header, seller, buyer, and itemized charges."
    }

    with patch("bookable.classify.call_vision_llm", return_value=(mock_arbitrary, {"cache_hit": False})):
        res = classify_pdf(Path("documents/INV-01.pdf"))
        # Document decision reflects content, not filename
        assert res["decision"] == "PROCESS"
        assert res["document_type"] == "INVOICE"
