# PROJECT_BRIEF.md — "The Bookable Payable" (Zycus AI/ML Graduate Assignment)

> **Hinglish TL;DR (for the human owner):** Ye file aapke project ka single source of truth hai. Har AI tool (Antigravity, ChatGPT, Gemini, Claude) ko kaam shuru karne se pehle ye file padhne ko bolo. Isme assignment ke rules, kit ke facts, architecture, step-by-step plan aur "kya nahi karna" sab hai. Jo cheezein abhi verify nahi hui, unhe **UNVERIFIED** likha hai. Progress log (Section 17) har step ke baad update karte raho. `DESIGN.md` aap khud apne shabdon me likhoge, AI se nahi.

---

## 0. How an AI agent must use this file

You are helping a fresher build a take-home assignment. Follow these rules in every session:

1. **Read this whole file first.** Then read `README.md`, `AUTODRAFT_SCHEMA.md`, `erp.py`, `master_data/README.md` in the repo. **Where this file and those files disagree, the repo files win.** Field names below are a convenience copy; `AUTODRAFT_SCHEMA.md` is the source of truth.
2. **Work on ONE step at a time.** When a step is done, summarise what you did, show real outputs, and **stop and wait** for the human's go-ahead.
3. **Never modify `erp.py`.** Import it (`from erp import erp_book`) or wrap it. The graders use the original.
4. **Do not install packages or change files outside the current step's scope** without asking.
5. **No per-file hardcoding.** Never branch on a filename or filename prefix (`INV-`, `HLD-`, `DU-`). No lists of special cases per document. See Section 7.
6. **Explain in simple language.** The human must be able to defend every decision in an interview. Do not generate `DESIGN.md` content for them.
7. **Never put secrets (API keys) in code or in the submission zip.** Use `.env` (git-ignored) and an `.env.example`.
8. If you are unsure whether something is true, say **"I am not sure"** and propose how to check it. Do not guess facts about the documents.

---

## 1. Assignment facts

| Item | Value |
|---|---|
| Company / role | Zycus — AI/ML Graduate selection assignment |
| Assignment name | "The Bookable Payable" |
| Submission deadline | **Friday, 2 Oct 2026** (reply to the recruiter's email with a zip) |
| Submit | One zip: working code, README with a single run command (script or Dockerfile), `output/*.json` for the open documents, `DESIGN.md` (≤ 3 pages) |
| Evaluated on | Implementation quality, understanding of AI/ML concepts, problem-solving approach, code quality, execution |
| Next stage | Final interview at Zycus office — the human must be able to explain everything |

**One-line goal:** for each PDF in `documents/`, emit the records an accounting system (ERP) can book, such that the ERP's own recomputation from our parts equals what the document genuinely says is owed — and also set aside what is *not* a payable.

---

## 2. The kit (verified by inspection)

```
candidate_kit/
├── README.md               the official brief (read fully)
├── AUTODRAFT_SCHEMA.md     exact output record shape
├── erp.py                  ERP recompute oracle (sealed, stdlib only, Python 3.10+)
├── example_check.py        shows how to call erp_book on one payable
├── sample_autodraft.json   one worked payable (books 438.0 EUR)
├── documents/              42 PDFs: INV-xx (28), HLD-xx (5), DU-xx (9)
└── master_data/            suppliers.json, tax_master.json, chart_of_books.json,
                            payment_terms.json, po_master.json (+ README)
```

Master data sizes (samples only; real tenants have 100k–millions of rows):
- **suppliers.json:** 14 suppliers (countries DE, EE, GH, KE, MY, PT, ZA)
- **tax_master.json:** 34 taxes; fields `code, country, tax_type, rate, name` (e.g. Ghana: VAT 15, NHIL 2.5, GETFund 2.5, COVID 1, CST 5)
- **chart_of_books.json:** 1 company "Bolt Group" (code `BOLTGROUP` in the schema example) with 6 business units: EE001, EE004, GH001, MY001, ZA001, GB001
- **payment_terms.json:** 10 terms (Immediate … Net_90, Monthly_in_advance)
- **po_master.json:** 2 POs

**Important about the documents:**
- Mostly **scanned page images**; a few have a text layer (e.g. INV-36, INV-31).
- **Nothing is labelled.** The prefixes `INV` / `HLD` / `DU` do **not** reliably tell the document type (e.g. `HLD-01` is a Thai invoice, `DU-05` is a Singapore tax invoice, `DU-08` is a German payment reminder "Mahnung"). **Never use filenames as features.**
- Many currencies, several languages, 1–35 lines per document, multi-page PDFs (DU-02 has 20 pages, INV-31 has 3).
- Some documents are graded in the open; others are **held back** and contain situations the open set does not, including at least one never seen before.

---

## 3. The oracle `erp.py` (how the ERP builds the gross)

`erp_book(payable) -> {"will_book_gross": float, "currency": str}`. It returns only the number — never whether it is right. Read `erp.py` yourself; summary (verify against the file):

```
line_base   = qty × unit_price, minus discount
              - discount_percentage (if > 0) wins over discount amount
              - else amount discount is applied per unit: (unit_price − disc/|qty|) × qty
              - round to 2 decimals
line_taxes  = per line: explicit taxes[] (or shorthand tax_rate/tax_amount on the line)
              - if tax_amount is empty/0 and rate > 0: derived = round2(line_base × rate / 100)
              - explicit tax_amount is used as-is (may be negative = withholding)
net_base    = Σ line_base − |header discount_amount|
header_tax  = same logic over header taxes[] but on net_base (no shorthand form)
other       = freight_charges + insurance_charges + extra_charges + excise_duties
gross       = round2(Σ line_base − header_discount + Σ line_taxes + header_tax + other)
```

Consequences (all confirmed in Step 2 experiments unless marked UNVERIFIED):
- The printed `total`, `subtotal`, `gross_total`, `total_tax_amount` are **ignored** by the ERP (they are "as printed" documentation). The ERP rebuilds from qty, unit_price, discounts, taxes, charges.
- `num()` does **no locale parsing**: `"1.234,56"` parses wrong. Emit dot-decimal only.
- `unit_price` must be **NET (tax-exclusive)**.
- Tax **placement changes the base**: line tax → that line's base; header tax → `net_base` (after header discount).
- Freight as a *line item* is part of `Σ line_base` (so header tax applies to it); freight in `freight_charges` is added after tax.
- Percentage discount beats amount discount if both set.
- Credit memos: positive magnitudes + `invoice_type: "CREDIT_MEMO"`; check `erp.py`'s sign handling.

### Ways a faithful copy can still book wrong (checklist for every document)
1. Tax placed on wrong level (line vs header) → different base.
2. Header discount changes the header-tax base; moving it to lines changes the tax.
3. Discount amount put in percentage field (or vice versa).
4. Explicit `tax_amount` vs rate-derived amount differ (compound/cascading levies → give explicit amount).
5. Freight/levy as a line item vs a charge field.
6. Withholding tax sign (must be negative).
7. Credit memo sign (keep positive).
8. Printed line `total` ≠ qty × unit_price (ERP ignores `total`).
9. Multiple per-line rates collapsed into one blended header rate.
10. **Tax-inclusive prices** (document shows gross unit prices): the ERP needs NET. Only derive net if the document itself prints the net or the tax split — otherwise flag, do not invent (Rule 1).
11. Rounding: per-line rounding vs document-level rounding; unit prices with more than 2 decimals.
12. Zero-rate / reverse-charge taxes: keep the tax entry (name, rate 0, amount 0.00) when the document shows it.

---

## 4. Output contract and schema quick reference

One command runs the system over `documents/` and writes `output/X.json` for each `X.pdf`:

```jsonc
{
  "file": "X.pdf",
  "payables": [ <payable>, ... ],      // 0..N bookable payables
  "declined": [ { "doc_type": "...", "reason": "..." } ]   // not payables
}
```

Payable fields (copy of `AUTODRAFT_SCHEMA.md` — re-check the original):
- `invoice_number`, `invoice_date` (ISO YYYY-MM-DD), `due_date`, `invoice_type` (`INVOICE` | `CREDIT_MEMO`), `currency`
- `supplier`: `name`, `supplier_id` (master code or `""`), `address`, `vat_id`
- `buyer`: `company_code`, `business_unit_code`, `location_code` (master codes or `""`)
- `payment_term_id` (master code), `po_number` (raw, as printed), `po_id` (matched master code or `""`)
- Totals as printed: `gross_total`, `subtotal` (optional), `total_tax_amount`
- Header charges/discount: `discount_amount`, `freight_charges`, `insurance_charges`, `extra_charges`, `excise_duties`
- `taxes[]` (header): `tax_type`, `tax_name`, `tax_rate` (percent, no `%`), `tax_amount` (may be `""` to let ERP derive, may be negative), `tax_type_code` (master code or `""`)
- `line_items[]`: `description`, `item_type` (GOODS|SERVICE|FREIGHT|TAX), `uom`, `quantity`, `unit_price` (NET), `total` (as printed), `discount` (amount magnitude), `discount_percentage`, `tax_rate`, `tax_amount`, `taxes[]`

Grading looks at **structure**, not only the total: tax placement, each tax's name/rate/amount, components kept decomposed (not pre-summed). Same total with wrong structure = wrong.

---

## 5. The three rules (enforced by graders, and also clues)

1. **Every value emitted must appear on the document.** A number that exists only to make the total balance disqualifies that payable. If tempted to invent a figure, re-read the document instead: something unusual is there (hidden discount, compound tax, charge in another field) — or the page simply does not contain the answer.
2. **Every code must be a real master-data match.** Blank is a legitimate answer; a fabricated code is not.
3. **Any correction must survive being wrong.** Some documents look like they need a fix but are correct. Change a record only if there is an independent fact *in the document* that justifies it — "it makes the total foot" is not such a fact.

Implementation implications: no "balancing" loop that adjusts numbers toward the printed gross; grounding check on every extracted value; honest blanks for unmatched codes.

---

## 6. Working hypothesis about "the shift" (UNVERIFIED — the human must form their own view)

The official README says the failures look unrelated until you change how you picture *what a document is*. A plausible reading (hypothesis only): **a document is not a form of fields; it is a small calculation** that derives one printed total from parts (quantities, prices, discounts, taxes at different levels, charges, withholdings). The job is to recover that calculation faithfully — including *where* each part sits — so that the ERP's recomputation reproduces the printed total for the right reasons. Use the ERP mismatch as a *signal to re-read the document*, never as a target to fit.

The human should test this hypothesis against the failing documents and write their own conclusion in `DESIGN.md` Q1.

---

## 7. Anti-patterns (do NOT do these)

- Branching by filename/prefix, or keeping a list of per-document special cases.
- Adjusting, plugging or "rounding off" numbers so the ERP total matches the printed gross.
- Translating descriptions/names (keep original language; Rule 1).
- Fabricating master codes; fuzzy-matching with a low threshold and accepting it silently.
- Deciding "decline" from the billed party not being a business unit (see Section 9 — many real payables are addressed to parties not in the sample masters; buyer codes are simply left blank then).
- Full-scan loops over master data per lookup (design for 100k–millions of rows: build dict/inverted indexes; exact VAT-ID/IBAN first, then normalised-name index, then fuzzy on a narrowed candidate set).
- Pre-summing taxes/discounts/charges, or moving a tax between line and header to make arithmetic easier.
- Copying LLM text into `DESIGN.md`.
- Committing `.env`, API keys, `cache/` or `scratch/` to the zip.

---

## 8. Architecture

```
PDF
 ├─ render: PyMuPDF → page images (≈200 DPI) + text layer if present
 ├─ OCR text (Tesseract/PaddleOCR/EasyOCR — evidence only, for grounding)
 ├─ classify + segment: per page → doc type; group pages into payables / attachments / declined
 ├─ extract: vision LLM (OpenAI, structured outputs against the schema) per payable
 │            → verbatim values, taxes at the level shown, components decomposed
 ├─ ground: each extracted number/name must be findable in the document text/image evidence
 ├─ resolve: deterministic master-data matching (indexed) → codes or ""
 ├─ verify: erp_book(payable) vs printed gross_total
 │            ├ match → emit
 │            └ mismatch → diagnose which component differs, re-read the page with
 │                         targeted questions; still mismatch → emit as-is + flag in a report
 └─ output/X.json  (+ a run report listing flags/mismatches)
```

Roles: **LLM = understanding** (what is this page, where is the tax, which line is a discount). **Code = everything checkable** (arithmetic with `Decimal`, matching, validation, schema checks). **OCR = evidence**, not the parser (rule-based OCR parsing breeds per-layout if-else branches).

Model choice (decided, verify current availability/pricing on the official OpenAI page): a vision-capable **mini** model (e.g. GPT-5.4 mini) for extraction; escalate only hard pages to a stronger model; cache every LLM response by content hash so reruns cost nothing.

---

## 9. Classification and segmentation rules

A page/document is **not a payable** (→ `declined[]` with an evidence-based reason) when it is not a demand for payment: payment reminders/dunning (e.g. "Mahnung"), account statements, delivery notes, packing lists, quotes/pro-forma, remittance slips, cover pages, pure customs paperwork **if** it is clearly not the billing document, or a duplicate copy of a payable already captured.

Segmentation rule (from the INV-31 audit): **a new payable starts only with a new invoice number or a new seller/billing party.** Later pages that repeat the same PO/reference/amount and belong to a third party (e.g. a utility bill attached as backup) are **attachments of the parent payable**, not separate payables. Do not double-count the same amount.

Decline-reason rule: cite evidence from the document text (e.g. "reminder of unpaid invoices, no new charges"). **Do not** decline because the billed party (buyer) is not one of the sample business units — many real invoices are addressed to parties outside the sample masters; in that case leave `buyer` codes `""`.

### Known document findings (verified unless marked)
| Document | Finding |
|---|---|
| INV-31 | **One payable, pages 1–3.** Page 1: invoice #39015, 100 Airport KPG III LLC → AmeriHealth Caritas, "Electric 04/27/18–05/16/18", 17,657.53, PO#16299. Pages 2–3: attached PECO utility bill (same 17,657.53). The earlier "PECO is a separate payable" split was a false split. |
| INV-36 | Has a text layer; invoice with a remittance section (keep remittance details out of line items). |
| INV-01 | German invoice, 0% VAT. |
| HLD-01 | Thai-language invoice. Filename prefix tells nothing. |
| DU-05 / DU-05s | Singapore tax invoice (and a variant) — **UNVERIFIED** relationship between the two; check for duplicate. |
| DU-08 | German payment reminder (Mahnung) → likely declined. |
| DU-11 | Credit memo plausible (pilot finding) → `CREDIT_MEMO`, positive magnitudes. **UNVERIFIED.** |
| DU-02 | **UNRESOLVED.** 20 pages, scanned. Pages are "Customs Consolidated Invoice" and "Customs Detailed Invoice" under one invoice number (554701215, dated 01/06/2026), seller Novatek U.S. LLC, buyer Redwater Mobility BV (Belgium), ship-to Turkey, consolidated total 37,534.94 EUR. Open questions: is this a commercial invoice or customs paperwork only? Do the detailed per-delivery pages sum to the consolidated total (→ one payable, not many, avoid double counting)? Decide from document evidence only, then record the decision and its reason. |

---

## 10. Extraction rules

- Extract **verbatim**; keep descriptions, names, addresses in the original language.
- Emit numbers **dot-decimal**, dates **ISO**, percent rates without `%`.
- Put each tax **where the document puts it**: per-line rates stay on lines (three different rates across lines = three line-level taxes, not one blended header rate); a single header tax stays at header.
- Keep discount, freight, insurance, levies, excise as separate components in their own fields.
- Line `total` = as printed; `unit_price` = NET. If the document prints only tax-inclusive prices and no net/tax split, do not invent — flag it.
- Withholding tax → **negative** `tax_amount`. Credit memo → positive magnitudes + `invoice_type: CREDIT_MEMO`.
- Use explicit `tax_amount` when the printed amount is what matters (e.g. compound levies, rounding differences); leave `""` to let the ERP derive from rate.
- Missing/unreadable field → `""`. Never infer a value from the total.

---

## 11. Master-data resolution strategy

Deterministic code only (no LLM). Build indexes once at startup.

| Target | Strategy |
|---|---|
| `supplier_id` | 1) exact normalised VAT ID / tax ID; 2) exact IBAN if printed; 3) normalised-name index (strip legal suffixes GmbH/Ltd/Sdn Bhd/OU, punctuation, case); 4) fuzzy on the narrowed candidate set with a conservative threshold *and* a corroborating field (country, address) — else `""` |
| `buyer.*` | match bill-to name/address against `chart_of_books.json` BUs/locations; no match → `""` |
| `tax_type_code` | match by country + tax_type + rate (+ name); no match → `""` |
| `payment_term_id` | match printed terms text ("Net 30", "due on receipt") to `payment_terms.json`; if no text, optionally derive only from an exact due_date − invoice_date match and treat as low-confidence; else `""` |
| `po_id` | `po_number` stays as printed; `po_id` only if that PO exists in `po_master.json` (a PO not in the master is a non-ERP reference) |

Scale: dict lookups O(1); avoid per-lookup full scans; fuzzy (rapidfuzz) only on a blocked candidate set (e.g. same country/first tokens).

---

## 12. Verification and retry policy

- After building a payable, call `erp_book()` and compare to the printed gross (use `Decimal` for comparisons, not float equality).
- **Allowed on mismatch:** re-read the document (new evidence) with targeted questions — "is this tax on the line or the header?", "is there a discount line?", "is this price tax-inclusive?", "is a charge listed separately?". Log what was found.
- **Not allowed:** editing numbers to force a match; inventing missing components; auto-flipping signs without an explicit in-document signal (Rule 3).
- If still mismatched: emit the faithful record, mark it in a run report (`output/_report.json`: doc, payable, erp_gross, printed_gross, suspected cause). Honest failure beats a fudged pass.
- Grounding check: every emitted number/name/date must be locatable in OCR/text evidence (normalised for locale formats); ungrounded values are dropped or flagged, never kept silently.

---

## 13. Tech stack and environment

- Python 3.10+. `erp.py` is stdlib-only.
- Suggested deps (add to `requirements.txt` only when needed): `pymupdf`, `pydantic`, `rapidfuzz`, `openai`, `python-dotenv`, `pytest`; one OCR engine (Tesseract / EasyOCR / PaddleOCR — Tesseract was weak on Thai; OCR is for grounding, not parsing).
- Config via `.env` (`OPENAI_API_KEY`, model name); provide `.env.example`. Never commit real keys.
- Cache LLM responses under `cache/` keyed by content hash + prompt version. Rerunning must not re-bill.
- Numbers: use `decimal.Decimal` for any arithmetic/comparison the system does itself.
- Reproducibility: the evaluators will re-run it. Provide a `Dockerfile` **and** a one-line command in `README.md`, e.g. `python run.py --input documents --output output`.

---

## 14. Repository structure

```
candidate_kit/
├── src/bookable/
│   ├── render.py      PDF → page images + text layer (+ OCR)
│   ├── classify.py    page type + segmentation into payables/attachments/declined
│   ├── extract.py     vision-LLM extraction against the schema
│   ├── ground.py      grounding check (Rule 1)
│   ├── resolve.py     indexed master-data matching (Rule 2)
│   ├── verify.py      erp_book comparison, diagnosis, retry policy (Rule 3)
│   └── pipeline.py    orchestrates everything per PDF
├── tests/             unit tests (normalisation, resolve, verify, segmentation rule)
├── cache/             LLM cache (git-ignored)
├── output/            generated X.json files (+ _report.json)
├── scratch/           throwaway experiments (git-ignored)
├── run.py             the single command
├── requirements.txt
├── Dockerfile
├── .env.example
├── DESIGN.md          written by the human, in their own words
├── README.md          how to run (one command)
└── PROJECT_BRIEF.md   this file
```

---

## 15. Roadmap (time-boxed for the 2 Oct deadline)

Status legend: ✅ done · ⚠️ partly done · ⏳ todo. **Update the log in Section 17 after each step.**

| # | Step | Exit criterion |
|---|---|---|
| 1 | Read-only inspection of kit and `erp.py` | Agent explains formula + edge cases; human agrees |
| 2 | Oracle experiments + repo skeleton | 9 hand-made payables show how placement/discount/freight/tax change the gross; `git init` done |
| 3 | PDF inspection + rendering | Page images + text layer per PDF; OCR text saved |
| 4A | Classification / segmentation pilot | INV-31 = 1 payable (pages 1–3); reminders declined; segmentation rule in Section 9 implemented without filename logic |
| 4B | Extraction + resolve + verify on 3 pilot docs (e.g. INV-36, INV-01, DU-11) | Table of erp_gross vs printed_gross, with reasons for any mismatch |
| 5 | Run all 42 documents; group failures by **root cause** (not by document) | Report of failure families; fixes are general, not per-document |
| 6 | Grounding check + honest flags + hard-document review (DU-02, tax-inclusive, compound taxes, withholding) | Unsolvable/ambiguous documents identified with evidence |
| 7 | Tests, Dockerfile, README (one command), clean `output/` | Fresh-folder run reproduces outputs |
| 8 | Human writes `DESIGN.md` (≤ 3 pages), final zip, send email | Checklist in Section 16 is all ticked |

Time plan: aim for Steps 1–4B on Wed 30 Sep, 5–6 on Thu 1 Oct, 7–8 Thu night, Fri 2 Oct morning = buffer + send. **Priority if time runs short:** (1) correct classification and declines, (2) simple invoices booking correctly with correct structure, (3) honest flags on hard documents. A correct partial submission on time beats a perfect late one.

**Generalisation check (cheap):** pick ~8 documents at random and treat them as a private hold-out: do not inspect or debug on them until the pipeline is frozen, then run once and report the open-vs-hold-out gap. The graders measure exactly this gap.

---

## 16. Submission checklist

- [ ] `python run.py` (or Docker) runs from a fresh clone with no manual steps; documented in `README.md`
- [ ] `output/` contains one `X.json` per input PDF, schema-valid (`file`, `payables`, `declined`)
- [ ] `erp.py` unchanged (diff against the original)
- [ ] No secrets, `.env`, `cache/`, `scratch/`, `__MACOSX`, `._*` files in the zip
- [ ] Tests pass (`pytest`)
- [ ] `DESIGN.md` ≤ 3 pages, in the human's own words, answers the 3 questions:
  1. What did you eventually understand about these documents that you did not on day one?
  2. On an unseen document, what does the system actually do, and why does that generalise instead of guessing?
  3. Was there a document that could not be solved like the others? Which, and how did you know?
- [ ] Zip sent by reply to the recruiter's email before the deadline

---

## 17. Progress log (agent: update after each step)

| Step | Status | Key results / decisions | Open issues |
|---|---|---|---|
| 1 | ✅ | `erp.py` formula and edge cases documented | — |
| 2 | ✅ | Skeleton + oracle experiments | — |
| 3 | ✅ | PDFs rendered; text layer present for some | INV-31 was mislabelled as a PECO bill |
| 4A | ⚠️ | Classification pilot done | INV-31 false split (fix: Section 9 rule); DU-02 unresolved |
| 4B | ⏳ | | |
| 5 | ⏳ | | |
| 6 | ⏳ | | |
| 7 | ⏳ | | |
| 8 | ⏳ | | |

---

## 18. Prompt template for each new step (human: copy, fill in, paste to the agent)

```
Read PROJECT_BRIEF.md first. We are on STEP <n>: <name>.
Scope: <exactly what to do>. Do nothing outside this scope.
Rules: never modify erp.py; no filename-based logic; no invented values; one step only.
Show real outputs (tables, erp_gross vs printed_gross). Update Section 17.
End with: "Step <n> complete — waiting for your go-ahead."
```
