"""
ai_agents.py  —  AI Agents for Eastern Aero Auto Quote System
==============================================================

Nine Claude-powered agents that plug into the existing Flask / SQLite app:

  1. RFQ Parser Agent          — extract structured parts list from any email body
  2. Auto-Quoter Agent         — build a draft quote from RFQ items + live inventory
  3. Reply Drafter Agent       — write a professional quote response email
  4. Customer Insights Agent   — summarise a customer's history and suggest actions
  5. Spam Classifier Agent     — detect junk/automated RFQ emails before they enter inbox
  6. Quote Sanity Check Agent  — validate a quote before sending (prices, fields, markup)
  7. Follow-up Email Agent     — draft a chaser email for stale sent quotes
  8. Customer Auto-Match Agent — link an incoming RFQ sender to an existing customer profile
  9. Inventory Health Agent    — flag low stock, missing costs, duplicates, stale records

Usage (from app.py or agents_routes.py):
    from ai_agents import (
        parse_rfq_with_ai, auto_quote_rfq, draft_reply_email, customer_insights,
        classify_rfq_spam, review_quote_before_send, draft_followup_email,
        match_customer_from_rfq, inventory_health_check,
    )

Requires:
    pip install anthropic
    ANTHROPIC_API_KEY environment variable set.
"""

import os
import json
import sqlite3
import re
from datetime import datetime

import anthropic

# ── Client & model ────────────────────────────────────────────────────────────
_client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

# Use Haiku for fast / high-volume work; override with ANTHROPIC_MODEL env var
_FAST_MODEL   = os.environ.get("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001")
_QUALITY_MODEL = os.environ.get("ANTHROPIC_QUALITY_MODEL", "claude-sonnet-4-6")

MAX_TOKENS = 2048


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _get_db(database_path: str) -> sqlite3.Connection:
    """Return a WAL-mode SQLite connection with row_factory set."""
    conn = sqlite3.connect(database_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _rows_to_dicts(rows) -> list[dict]:
    return [dict(r) for r in rows]


# ─────────────────────────────────────────────────────────────────────────────
# Agent 1 — RFQ Parser
# ─────────────────────────────────────────────────────────────────────────────

PARSE_SYSTEM = """You are an aviation-parts procurement specialist.
Your job is to extract a structured parts request from an RFQ email body.

Return ONLY a JSON array. Each element must have these keys:
  part_number  : string  (the part number / PN, or "" if unknown)
  description  : string  (part description, or "" if not mentioned)
  quantity     : integer (default 1 if not stated)
  condition    : string  (one of: "OH", "SV", "AR", "NE", "NS", "FN", or "any" — default "any")

Rules:
- Include every distinct part mentioned.
- If quantity is ambiguous, default to 1.
- Normalise condition codes: Overhauled→OH, Serviceable→SV, As Removed→AR, New→NE/NS, etc.
- Strip email signatures, greetings, and footers — only output parts.
- If no parts are found, return an empty array [].
- Output raw JSON only — no markdown fences, no explanation.
"""


def parse_rfq_with_ai(email_body: str) -> list[dict]:
    """
    Use Claude to extract a structured parts list from a raw RFQ email body.

    Returns a list of dicts: [{part_number, description, quantity, condition}, ...]
    Falls back to an empty list on any error.
    """
    if not email_body or not email_body.strip():
        return []

    # Truncate very long emails to keep token cost reasonable
    body_snippet = email_body[:6000]

    try:
        response = _client.messages.create(
            model=_FAST_MODEL,
            max_tokens=MAX_TOKENS,
            system=PARSE_SYSTEM,
            messages=[{"role": "user", "content": body_snippet}],
        )
        raw = response.content[0].text.strip()
        # Strip accidental markdown fences
        raw = re.sub(r"^```[a-z]*\n?", "", raw)
        raw = re.sub(r"\n?```$", "", raw)
        items = json.loads(raw)
        if not isinstance(items, list):
            return []
        # Coerce types
        result = []
        for it in items:
            result.append({
                "part_number": str(it.get("part_number") or "").strip().upper(),
                "description": str(it.get("description") or "").strip(),
                "quantity":    max(1, int(it.get("quantity") or 1)),
                "condition":   str(it.get("condition") or "any").strip().upper(),
            })
        return result
    except Exception as exc:
        print(f"[ai_agents] parse_rfq_with_ai error: {exc}")
        return []


# ─────────────────────────────────────────────────────────────────────────────
# Agent 2 — Auto-Quoter
# ─────────────────────────────────────────────────────────────────────────────

AUTO_QUOTE_SYSTEM = """You are a senior aviation parts sales agent at Eastern Aero Pty Ltd.
Given a list of requested parts and the current inventory snapshot, produce a draft quote.

For each requested part output a JSON object with:
  part_number        : string   (use the RFQ part number)
  description        : string   (from inventory if matched, else from RFQ)
  condition          : string   (from inventory if matched, else from RFQ)
  quantity_requested : integer
  quantity_available : integer  (0 if no stock)
  unit_price         : float    (suggest a price — see rules below)
  lead_time          : string   ("Stock" if available, "2-4 weeks" if sourcing needed, etc.)
  price_type         : string   ("Outright" or "Exchange")
  warranty           : string   ("3 Months" default, "6 Months" for new parts)
  notes              : string   (any relevant note — e.g. "Exact match in stock", "Will source", "No quote")
  no_quote           : integer  (1 if we cannot supply this part at all, else 0)

Pricing rules:
- If the part is in inventory: unit_price = unit_cost × (1 + markup_percent/100).
  Round up to the nearest dollar.
- If not in stock, suggest a market-rate placeholder (set no_quote=0 if we can source it,
  or no_quote=1 if it's truly unavailable).
- Exchange parts typically carry a lower outright price plus a core charge noted in notes.

Return ONLY a JSON array. No markdown. No explanation.
"""


def auto_quote_rfq(rfq_id: int, database_path: str, markup_percent: float = 30.0) -> list[dict]:
    """
    For a given RFQ (by ID), fetch its items, match against live inventory,
    and use Claude to produce draft quote line items.

    Returns list of quote item dicts ready to insert into quote_items.
    """
    conn = _get_db(database_path)
    try:
        rfq_items = _rows_to_dicts(
            conn.execute("SELECT * FROM rfq_items WHERE rfq_id=?", (rfq_id,)).fetchall()
        )
        if not rfq_items:
            return []

        # Collect inventory matches for each requested part
        inventory_context = []
        for item in rfq_items:
            pn = (item.get("part_number") or "").upper().strip()
            inv_rows = _rows_to_dicts(conn.execute(
                """SELECT part_number, description, condition, quantity, unit_cost, unit_price,
                          location, uom, manufacturer
                   FROM inventory
                   WHERE UPPER(part_number) LIKE ?
                   LIMIT 5""",
                (f"%{pn}%",)
            ).fetchall())
            inventory_context.append({
                "requested": item,
                "inventory_matches": inv_rows,
            })

    finally:
        conn.close()

    prompt = (
        f"Markup percent: {markup_percent}\n\n"
        "Parts request with inventory matches:\n"
        + json.dumps(inventory_context, indent=2)
    )

    try:
        response = _client.messages.create(
            model=_FAST_MODEL,
            max_tokens=MAX_TOKENS * 2,
            system=AUTO_QUOTE_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = response.content[0].text.strip()
        raw = re.sub(r"^```[a-z]*\n?", "", raw)
        raw = re.sub(r"\n?```$", "", raw)
        items = json.loads(raw)
        if not isinstance(items, list):
            return []
        return items
    except Exception as exc:
        print(f"[ai_agents] auto_quote_rfq error: {exc}")
        return []


# ─────────────────────────────────────────────────────────────────────────────
# Agent 3 — Reply Drafter
# ─────────────────────────────────────────────────────────────────────────────

REPLY_DRAFT_SYSTEM = """You are a professional sales representative at Eastern Aero Pty Ltd,
an Australian aviation parts supplier.

Write a concise, professional plain-text quote response email to the customer.
The email must:
  1. Open with a polite greeting using the customer's first name (or "Sir/Madam" if unknown).
  2. Reference the RFQ number and date.
  3. Briefly introduce the quote number and validity period.
  4. List quoted parts in a clean text table: Part No | Description | Qty | Unit Price | Lead Time.
  5. Note any items that could not be quoted (no_quote=1).
  6. Close with payment terms, warranty summary, and a warm sign-off.
  7. End with the company signature block.

Keep the tone professional but friendly. Do NOT use HTML — plain text only.
Return only the email body text, starting with the greeting.
"""


def draft_reply_email(
    quote: dict,
    rfq: dict,
    items: list[dict],
    settings: dict,
) -> str:
    """
    Generate a professional plain-text quote response email.

    Args:
        quote    : quote row dict
        rfq      : rfq row dict (customer info)
        items    : list of quote_items dicts
        settings : settings dict (company_name, company_email, etc.)

    Returns a plain-text email body string.
    """
    # Build a compact summary to feed the model
    quoted_items = [i for i in items if not i.get("no_quote")]
    no_quote_items = [i for i in items if i.get("no_quote")]

    context = {
        "company_name":   settings.get("company_name", "Eastern Aero Pty Ltd"),
        "company_email":  settings.get("company_email", ""),
        "company_phone":  settings.get("company_phone", ""),
        "customer_name":  rfq.get("customer_name", ""),
        "customer_company": rfq.get("company", ""),
        "rfq_number":     rfq.get("rfq_number", ""),
        "rfq_date":       rfq.get("created_at", ""),
        "quote_number":   quote.get("quote_number", ""),
        "valid_days":     quote.get("valid_days", 30),
        "currency":       quote.get("currency", "USD"),
        "total_amount":   quote.get("total_amount", 0),
        "payment_terms":  "Net 30" if rfq.get("company") else "COD",
        "quoted_items":   [
            {
                "part_number": i.get("part_number"),
                "description": i.get("description"),
                "condition":   i.get("condition"),
                "quantity":    i.get("quantity_requested"),
                "unit_price":  i.get("unit_price"),
                "lead_time":   i.get("lead_time", "Stock"),
                "warranty":    i.get("warranty", "3 Months"),
                "price_type":  i.get("price_type", "Outright"),
            }
            for i in quoted_items
        ],
        "no_quote_items": [i.get("part_number") for i in no_quote_items],
    }

    prompt = "Write the quote reply email for the following data:\n" + json.dumps(context, indent=2)

    try:
        response = _client.messages.create(
            model=_QUALITY_MODEL,
            max_tokens=MAX_TOKENS,
            system=REPLY_DRAFT_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
        )
        return response.content[0].text.strip()
    except Exception as exc:
        print(f"[ai_agents] draft_reply_email error: {exc}")
        return ""


# ─────────────────────────────────────────────────────────────────────────────
# Agent 4 — Customer Insights
# ─────────────────────────────────────────────────────────────────────────────

INSIGHTS_SYSTEM = """You are a CRM analyst for Eastern Aero Pty Ltd, an aviation parts supplier.

Analyse the customer's RFQ and quote history and return a JSON object with:
  summary          : string  (2-3 sentence plain-English summary of who this customer is)
  total_rfqs       : int
  total_quoted     : int     (RFQs that received a quote)
  win_rate_pct     : float   (quotes sent → quotes accepted/invoiced, 0-100)
  top_parts        : list    (top 5 most-requested part numbers)
  avg_order_value  : float   (average quote total across all sent quotes)
  last_activity    : string  (ISO date of most recent RFQ or quote)
  preferred_condition : string (most frequently requested condition code)
  recommended_actions : list (up to 4 short action strings, e.g. "Follow up on QTE-20240510-0012")
  risk_flags       : list    (e.g. "No response to last 3 quotes", "Credit limit approaching")

Return ONLY a JSON object. No markdown. No explanation.
"""


def customer_insights(customer_email: str, database_path: str) -> dict:
    """
    Build an AI-powered insight card for a customer identified by email.

    Returns a dict with summary, stats, top parts, recommended actions, and risk flags.
    """
    conn = _get_db(database_path)
    try:
        # Fetch all RFQs for this customer
        rfqs = _rows_to_dicts(conn.execute(
            "SELECT * FROM rfqs WHERE LOWER(customer_email)=? ORDER BY created_at DESC LIMIT 50",
            (customer_email.lower(),)
        ).fetchall())

        if not rfqs:
            # Try partial match (company or name)
            rfqs = _rows_to_dicts(conn.execute(
                "SELECT * FROM rfqs WHERE LOWER(customer_email) LIKE ? ORDER BY created_at DESC LIMIT 50",
                (f"%{customer_email.lower().split('@')[0]}%",)
            ).fetchall())

        rfq_ids = [r["id"] for r in rfqs]

        # Fetch associated quotes
        quotes = []
        if rfq_ids:
            placeholders = ",".join("?" * len(rfq_ids))
            quotes = _rows_to_dicts(conn.execute(
                f"SELECT * FROM quotes WHERE rfq_id IN ({placeholders}) ORDER BY created_at DESC",
                rfq_ids
            ).fetchall())

        # Fetch all RFQ items to find top parts
        rfq_items = []
        if rfq_ids:
            rfq_items = _rows_to_dicts(conn.execute(
                f"SELECT * FROM rfq_items WHERE rfq_id IN ({placeholders})",
                rfq_ids
            ).fetchall())

        # Fetch customer profile if available
        profile = conn.execute(
            "SELECT * FROM customer_profiles WHERE LOWER(email)=?",
            (customer_email.lower(),)
        ).fetchone()
        profile_dict = dict(profile) if profile else {}

    finally:
        conn.close()

    # Build context for Claude
    context = {
        "customer_email":  customer_email,
        "customer_profile": profile_dict,
        "rfqs": [
            {
                "rfq_number": r.get("rfq_number"),
                "status":     r.get("status"),
                "created_at": r.get("created_at"),
                "source":     r.get("source"),
                "company":    r.get("company"),
                "customer_name": r.get("customer_name"),
            }
            for r in rfqs[:30]
        ],
        "quotes": [
            {
                "quote_number":  q.get("quote_number"),
                "status":        q.get("status"),
                "total_amount":  q.get("total_amount"),
                "currency":      q.get("currency"),
                "created_at":    q.get("created_at"),
                "sent_at":       q.get("sent_at"),
            }
            for q in quotes[:30]
        ],
        "rfq_items_sample": rfq_items[:60],
    }

    prompt = "Analyse this customer data:\n" + json.dumps(context, indent=2)

    try:
        response = _client.messages.create(
            model=_QUALITY_MODEL,
            max_tokens=MAX_TOKENS,
            system=INSIGHTS_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = response.content[0].text.strip()
        raw = re.sub(r"^```[a-z]*\n?", "", raw)
        raw = re.sub(r"\n?```$", "", raw)
        result = json.loads(raw)
        if not isinstance(result, dict):
            return {"error": "Unexpected response format"}
        return result
    except Exception as exc:
        print(f"[ai_agents] customer_insights error: {exc}")
        return {"error": str(exc)}


# ─────────────────────────────────────────────────────────────────────────────
# Agent 5 — Spam / Junk Classifier
# ─────────────────────────────────────────────────────────────────────────────

SPAM_SYSTEM = """You are a filter for an aviation parts company's email inbox.
Your job is to decide whether an incoming email is a genuine RFQ (request for quotation)
from a real buyer, or automated junk (aggregator bots, spam, out-of-office, newsletters,
delivery notifications, subscription confirmations, etc.).

Return ONLY a JSON object with:
  is_spam    : boolean  (true = junk / not a real RFQ, false = genuine inquiry)
  confidence : float    (0.0–1.0, where 1.0 = absolutely certain)
  reason     : string   (one short sentence explaining the decision)

Criteria for marking as spam / junk:
- Sender is a known aggregator bot (Locatory, ILS, PartsBase, Aviall auto-alerts, etc.)
- Email contains no actual part numbers and reads like a newsletter or notification
- Subject or body indicates an out-of-office, bounce, delivery failure, or subscription email
- Body is a templated "we found a match" alert with no human-written content
- Domain is a known spam/aggregator domain

Criteria for genuine RFQ:
- Contains at least one part number or aircraft type
- Written in first-person or on behalf of an airline / MRO / operator
- Has a specific need, quantity, or urgency

Return ONLY a JSON object. No markdown. No explanation.
"""


def classify_rfq_spam(
    email_body: str,
    sender_email: str = "",
    sender_name: str = "",
    subject: str = "",
) -> dict:
    """
    Classify an incoming email as genuine RFQ or spam/junk.

    Returns:
        {"is_spam": bool, "confidence": float, "reason": str}
    Falls back to {"is_spam": False, "confidence": 0.0, "reason": "classification error"}
    """
    if not email_body and not subject:
        return {"is_spam": False, "confidence": 0.0, "reason": "empty email"}

    snippet = f"From: {sender_name} <{sender_email}>\nSubject: {subject}\n\n{email_body[:3000]}"

    try:
        response = _client.messages.create(
            model=_FAST_MODEL,
            max_tokens=256,
            system=SPAM_SYSTEM,
            messages=[{"role": "user", "content": snippet}],
        )
        raw = response.content[0].text.strip()
        raw = re.sub(r"^```[a-z]*\n?", "", raw)
        raw = re.sub(r"\n?```$", "", raw)
        result = json.loads(raw)
        return {
            "is_spam":    bool(result.get("is_spam", False)),
            "confidence": float(result.get("confidence", 0.5)),
            "reason":     str(result.get("reason", "")),
        }
    except Exception as exc:
        print(f"[ai_agents] classify_rfq_spam error: {exc}")
        return {"is_spam": False, "confidence": 0.0, "reason": f"classification error: {exc}"}


# ─────────────────────────────────────────────────────────────────────────────
# Agent 6 — Quote Sanity Check
# ─────────────────────────────────────────────────────────────────────────────

SANITY_SYSTEM = """You are a quality-control agent for an aviation parts company.
Before a quote is sent to a customer, review it for problems.

You will receive a JSON object containing the quote header and its line items.

Return ONLY a JSON object with:
  ok       : boolean  (true = quote is ready to send, false = has blocking issues)
  score    : integer  (0-100 quality score)
  issues   : list of strings  (blocking problems that MUST be fixed before sending)
  warnings : list of strings  (non-blocking concerns worth reviewing)

Check for:
ISSUES (blocking):
  - Any line item with unit_price = 0 and no_quote = 0 (priced at zero but not marked as NQ)
  - Any line item missing a part_number (empty string)
  - Quote total = $0 with all items not marked no_quote (nothing priced at all)
  - Customer email is missing (can't send)
  - More than 50% of items are no_quote (flag as suspicious)

WARNINGS (non-blocking):
  - Any item with no description
  - Any item with quantity_requested = 0
  - Unit price seems unusually high (> $50,000 per unit) or unusually low (< $1) for non-zero items
  - lead_time is blank for any quoted item
  - warranty is blank for any quoted item
  - No valid_days set on the quote (or valid_days = 0)
  - currency is not set

Return ONLY a JSON object. No markdown. No explanation.
"""


def review_quote_before_send(quote_dict: dict, items_list: list[dict]) -> dict:
    """
    Run a pre-send sanity check on a quote.

    Args:
        quote_dict : quote row as dict (quote_number, total_amount, customer_email, currency, valid_days, …)
        items_list : list of quote_items dicts

    Returns:
        {"ok": bool, "score": int, "issues": [...], "warnings": [...]}
    """
    # Build a compact payload — drop large/irrelevant fields
    payload = {
        "quote_number":   quote_dict.get("quote_number"),
        "customer_email": quote_dict.get("customer_email") or "",
        "customer_name":  quote_dict.get("customer_name") or "",
        "total_amount":   quote_dict.get("total_amount", 0),
        "currency":       quote_dict.get("currency") or "",
        "valid_days":     quote_dict.get("valid_days", 0),
        "status":         quote_dict.get("status"),
        "items": [
            {
                "part_number":        i.get("part_number") or "",
                "description":        i.get("description") or "",
                "condition":          i.get("condition") or "",
                "quantity_requested": i.get("quantity_requested", 0),
                "unit_price":         i.get("unit_price", 0),
                "extended_price":     i.get("extended_price", 0),
                "lead_time":          i.get("lead_time") or "",
                "warranty":           i.get("warranty") or "",
                "no_quote":           int(i.get("no_quote") or 0),
            }
            for i in items_list
        ],
    }

    prompt = "Review this quote:\n" + json.dumps(payload, indent=2)

    try:
        response = _client.messages.create(
            model=_FAST_MODEL,
            max_tokens=512,
            system=SANITY_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = response.content[0].text.strip()
        raw = re.sub(r"^```[a-z]*\n?", "", raw)
        raw = re.sub(r"\n?```$", "", raw)
        result = json.loads(raw)
        return {
            "ok":       bool(result.get("ok", True)),
            "score":    int(result.get("score", 50)),
            "issues":   list(result.get("issues", [])),
            "warnings": list(result.get("warnings", [])),
        }
    except Exception as exc:
        print(f"[ai_agents] review_quote_before_send error: {exc}")
        return {"ok": True, "score": 50, "issues": [], "warnings": [f"Sanity check unavailable: {exc}"]}


# ─────────────────────────────────────────────────────────────────────────────
# Agent 7 — Follow-up Email Drafter
# ─────────────────────────────────────────────────────────────────────────────

FOLLOWUP_SYSTEM = """You are a professional sales representative at Eastern Aero Pty Ltd,
an Australian aviation parts supplier.

Write a brief, warm, plain-text follow-up email to a customer who has not yet responded
to a quote we sent them.

Rules:
  1. Open with a polite greeting using the customer's first name (or "Sir/Madam" if unknown).
  2. Reference the original quote number and the date it was sent.
  3. Politely enquire if they received the quote and whether they have any questions.
  4. Mention the validity period remaining (if still valid) or offer to reissue it.
  5. Keep it short — 3-4 paragraphs maximum.
  6. End with the company signature block.
  7. Plain text only — no HTML, no markdown.

Return only the email body, starting with the greeting.
"""


def draft_followup_email(
    quote_dict: dict,
    rfq_dict: dict,
    days_since_sent: int,
    settings: dict | None = None,
) -> str:
    """
    Draft a plain-text follow-up email for a sent quote with no customer response.

    Args:
        quote_dict      : quote row as dict
        rfq_dict        : rfq row as dict (customer info)
        days_since_sent : how many days ago the quote was sent
        settings        : app settings dict (company_name, etc.)

    Returns plain-text email body string, or "" on error.
    """
    if settings is None:
        settings = {}

    valid_days = int(quote_dict.get("valid_days") or 30)
    sent_at    = quote_dict.get("sent_at") or quote_dict.get("created_at") or ""

    # Calculate days remaining validity
    days_remaining = max(0, valid_days - days_since_sent)

    context = {
        "company_name":     settings.get("company_name", "Eastern Aero Pty Ltd"),
        "company_email":    settings.get("company_email", ""),
        "company_phone":    settings.get("company_phone", ""),
        "customer_name":    rfq_dict.get("customer_name") or quote_dict.get("customer_name") or "",
        "customer_company": rfq_dict.get("company") or quote_dict.get("company") or "",
        "quote_number":     quote_dict.get("quote_number", ""),
        "sent_date":        sent_at[:10] if sent_at else "",
        "days_since_sent":  days_since_sent,
        "valid_days":       valid_days,
        "days_remaining":   days_remaining,
        "total_amount":     quote_dict.get("total_amount", 0),
        "currency":         quote_dict.get("currency", "USD"),
    }

    prompt = "Write the follow-up email for:\n" + json.dumps(context, indent=2)

    try:
        response = _client.messages.create(
            model=_QUALITY_MODEL,
            max_tokens=MAX_TOKENS,
            system=FOLLOWUP_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
        )
        return response.content[0].text.strip()
    except Exception as exc:
        print(f"[ai_agents] draft_followup_email error: {exc}")
        return ""


# ─────────────────────────────────────────────────────────────────────────────
# Agent 8 — Customer Auto-Match
# ─────────────────────────────────────────────────────────────────────────────

MATCH_SYSTEM = """You are a CRM data-matching agent for an aviation parts company.

Given a new contact (name, email, company) from an incoming RFQ, and a list of
existing customer profiles, decide if the new contact matches an existing profile.

Return ONLY a JSON object with:
  matched        : boolean  (true = found a match)
  customer_id    : integer or null  (the matched profile's id, or null)
  confidence     : float    (0.0–1.0)
  reason         : string   (brief explanation)

Matching rules (in priority order):
  1. Exact email address match → confidence 1.0
  2. Email domain + company name similarity → confidence 0.8-0.95
  3. Full name + company similarity → confidence 0.6-0.8
  4. Company name alone → confidence 0.5 (only if unique in the list)
  5. No convincing match → matched: false, customer_id: null

Use fuzzy logic for company names (e.g. "Qantas Engineering" ≈ "Qantas Airways").
Return ONLY a JSON object. No markdown. No explanation.
"""


def match_customer_from_rfq(
    name: str,
    email: str,
    company: str,
    existing_profiles: list[dict],
) -> dict:
    """
    Try to match an RFQ sender to an existing customer profile.

    Args:
        name             : sender's name from the RFQ email
        email            : sender's email address
        company          : sender's company name
        existing_profiles: list of customer_profiles dicts from DB

    Returns:
        {"matched": bool, "customer_id": int|None, "confidence": float, "reason": str}
    """
    if not existing_profiles:
        return {"matched": False, "customer_id": None, "confidence": 0.0, "reason": "no profiles in DB"}

    # Only pass the fields the model needs to keep token count low
    slim_profiles = [
        {
            "id":      p.get("id"),
            "name":    p.get("name") or p.get("customer_name") or "",
            "email":   p.get("email") or "",
            "company": p.get("company") or "",
        }
        for p in existing_profiles[:100]  # cap at 100
    ]

    payload = {
        "incoming": {"name": name, "email": email, "company": company},
        "existing_profiles": slim_profiles,
    }

    prompt = "Match this contact:\n" + json.dumps(payload, indent=2)

    try:
        response = _client.messages.create(
            model=_FAST_MODEL,
            max_tokens=256,
            system=MATCH_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = response.content[0].text.strip()
        raw = re.sub(r"^```[a-z]*\n?", "", raw)
        raw = re.sub(r"\n?```$", "", raw)
        result = json.loads(raw)
        return {
            "matched":     bool(result.get("matched", False)),
            "customer_id": result.get("customer_id"),  # int or None
            "confidence":  float(result.get("confidence", 0.0)),
            "reason":      str(result.get("reason", "")),
        }
    except Exception as exc:
        print(f"[ai_agents] match_customer_from_rfq error: {exc}")
        return {"matched": False, "customer_id": None, "confidence": 0.0, "reason": f"error: {exc}"}


# ─────────────────────────────────────────────────────────────────────────────
# Agent 9 — Inventory Health Monitor
# ─────────────────────────────────────────────────────────────────────────────

HEALTH_SYSTEM = """You are an inventory audit assistant for an aviation parts warehouse.

You will receive a JSON snapshot of inventory records. Identify issues and return a JSON
array of health findings. Each finding is an object with:

  severity    : string  ("error", "warning", or "info")
  part_number : string  (the PN affected, or "SYSTEM" for global findings)
  issue       : string  (short description of the problem)
  suggestion  : string  (what to do about it)

Check for:
  ERRORS:
    - Parts with quantity > 0 but unit_cost = 0 or null (selling without a cost basis)
    - Duplicate part numbers (same PN appears more than once)
    - Parts with negative quantity
    - Parts with unit_price < unit_cost (selling below cost)

  WARNINGS:
    - Parts with quantity = 0 (out of stock) — list only if more than 5 exist
    - Parts with no description
    - Parts with no location set
    - Parts added more than 2 years ago with no updates (stale records)
    - Parts where condition is blank

  INFO:
    - Total inventory value (quantity × unit_cost) — include as one INFO item
    - Count of parts by condition (OH, SV, AR, NE, NS)

Keep the list focused — no more than 30 findings total. Prioritise errors first.
Return ONLY a JSON array. No markdown. No explanation.
"""


def inventory_health_check(database_path: str) -> list[dict]:
    """
    Run an AI-powered inventory health check and return a list of findings.

    Returns:
        List of {"severity", "part_number", "issue", "suggestion"} dicts.
        On error returns [{"severity": "error", "part_number": "SYSTEM", "issue": ..., "suggestion": ...}]
    """
    conn = _get_db(database_path)
    try:
        rows = _rows_to_dicts(conn.execute("""
            SELECT part_number, description, condition, quantity, unit_cost, unit_price,
                   location, uom, manufacturer, created_at, updated_at
            FROM inventory
            ORDER BY part_number
            LIMIT 500
        """).fetchall())
    finally:
        conn.close()

    if not rows:
        return [{"severity": "info", "part_number": "SYSTEM",
                 "issue": "Inventory is empty.", "suggestion": "Add parts to get started."}]

    # Pre-compute duplicate PNs so Claude doesn't have to do heavy lifting
    from collections import Counter
    pn_counts = Counter(r["part_number"] for r in rows if r["part_number"])
    duplicates = {pn for pn, cnt in pn_counts.items() if cnt > 1}

    # Enrich rows with a duplicate flag to make the model's job easier
    for r in rows:
        r["_is_duplicate"] = r["part_number"] in duplicates

    # Send a compact snapshot (drop nulls to reduce tokens)
    snapshot = json.dumps(rows[:200], indent=1)  # cap at 200 rows

    prompt = (
        f"Total records in DB: {len(rows)}\n"
        f"Records in this snapshot: {min(200, len(rows))}\n\n"
        "Inventory snapshot:\n" + snapshot
    )

    try:
        response = _client.messages.create(
            model=_FAST_MODEL,
            max_tokens=MAX_TOKENS * 2,
            system=HEALTH_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = response.content[0].text.strip()
        raw = re.sub(r"^```[a-z]*\n?", "", raw)
        raw = re.sub(r"\n?```$", "", raw)
        findings = json.loads(raw)
        if not isinstance(findings, list):
            return []
        # Normalise each finding
        return [
            {
                "severity":    str(f.get("severity", "info")).lower(),
                "part_number": str(f.get("part_number") or "SYSTEM"),
                "issue":       str(f.get("issue", "")),
                "suggestion":  str(f.get("suggestion", "")),
            }
            for f in findings
        ]
    except Exception as exc:
        print(f"[ai_agents] inventory_health_check error: {exc}")
        return [{"severity": "error", "part_number": "SYSTEM",
                 "issue": f"Health check failed: {exc}",
                 "suggestion": "Check ANTHROPIC_API_KEY and network access."}]
