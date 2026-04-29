"""
agents_routes.py  —  Flask Blueprint: AI Agent API endpoints
=============================================================

Registers nine JSON endpoints under /api/ai/:

  POST  /api/ai/parse-rfq                  — parse raw email text → parts list
  POST  /api/ai/auto-quote/<rfq_id>        — auto-fill draft quote for an RFQ
  POST  /api/ai/draft-reply/<quote_id>     — draft a reply email for a quote
  GET   /api/ai/customer-insights/<email>  — customer history + AI insights card
  POST  /api/ai/classify-spam              — classify incoming email as spam or genuine RFQ
  POST  /api/ai/review-quote/<quote_id>    — sanity-check a quote before sending
  POST  /api/ai/followup-email/<quote_id>  — draft a follow-up for a stale sent quote
  POST  /api/ai/match-customer             — match RFQ sender to existing customer profile
  GET   /api/ai/inventory-health           — AI-powered inventory audit

To activate, add these two lines to app.py (anywhere after `app = Flask(__name__)`):

    from agents_routes import agents_bp
    app.register_blueprint(agents_bp)

All endpoints require login (@login_required from Flask-Login).
"""

from flask import Blueprint, request, jsonify, current_app
from flask_login import login_required, current_user

# ── import all agent functions ────────────────────────────────────────────────
from ai_agents import (
    parse_rfq_with_ai,
    auto_quote_rfq,
    draft_reply_email,
    customer_insights,
    classify_rfq_spam,
    review_quote_before_send,
    draft_followup_email,
    match_customer_from_rfq,
    inventory_health_check,
)

# ── import DB helpers from app.py ─────────────────────────────────────────────
# These are re-used from the parent app — keep imports lazy to avoid circular refs.
def _get_app_db():
    from app import get_db
    return get_db()

def _get_app_settings():
    from app import get_settings
    return get_settings()

def _get_database_path():
    """Return the DATABASE path used by the parent app."""
    import app as _app
    return _app.DATABASE


agents_bp = Blueprint("agents", __name__, url_prefix="/api/ai")


# ─────────────────────────────────────────────────────────────────────────────
# POST /api/ai/parse-rfq
# Body: { "body": "<raw email text>" }
# Returns: { "items": [ {part_number, description, quantity, condition}, ... ] }
# ─────────────────────────────────────────────────────────────────────────────
@agents_bp.route("/parse-rfq", methods=["POST"])
@login_required
def api_parse_rfq():
    """
    Use Claude to extract a structured parts list from a raw email body.

    Useful as a smarter drop-in for the regex-based parse_rfq_text() function,
    especially for non-standard or natural-language RFQ emails.
    """
    data = request.get_json(silent=True) or {}
    body = data.get("body", "").strip()
    if not body:
        return jsonify({"error": "No email body provided."}), 400

    items = parse_rfq_with_ai(body)
    return jsonify({"items": items, "count": len(items)})


# ─────────────────────────────────────────────────────────────────────────────
# POST /api/ai/auto-quote/<rfq_id>
# Body (optional): { "markup_percent": 35 }
# Returns: { "quote_items": [ {...}, ... ], "rfq_id": N }
# ─────────────────────────────────────────────────────────────────────────────
@agents_bp.route("/auto-quote/<int:rfq_id>", methods=["POST"])
@login_required
def api_auto_quote(rfq_id):
    """
    Generate draft quote line items for an existing RFQ.

    The response can be displayed to the user for review and then saved
    to quote_items via the existing /quotes/<id>/update-item routes.
    """
    data = request.get_json(silent=True) or {}
    settings = _get_app_settings()

    try:
        markup = float(data.get("markup_percent") or settings.get("default_markup") or 30)
    except (ValueError, TypeError):
        markup = 30.0

    db_path = _get_database_path()

    # Verify the RFQ exists
    conn = _get_app_db()
    rfq = conn.execute("SELECT id FROM rfqs WHERE id=?", (rfq_id,)).fetchone()
    conn.close()
    if not rfq:
        return jsonify({"error": f"RFQ {rfq_id} not found."}), 404

    quote_items = auto_quote_rfq(rfq_id, db_path, markup_percent=markup)
    return jsonify({"rfq_id": rfq_id, "quote_items": quote_items, "count": len(quote_items)})


# ─────────────────────────────────────────────────────────────────────────────
# POST /api/ai/draft-reply/<quote_id>
# Returns: { "subject": "...", "body": "..." }
# ─────────────────────────────────────────────────────────────────────────────
@agents_bp.route("/draft-reply/<int:quote_id>", methods=["POST"])
@login_required
def api_draft_reply(quote_id):
    """
    Generate a professional plain-text reply email for a quote.

    The returned body can be shown in a preview/edit modal before sending,
    giving the user a chance to tweak before dispatch.
    """
    conn = _get_app_db()
    quote = conn.execute("SELECT * FROM quotes WHERE id=?", (quote_id,)).fetchone()
    if not quote:
        conn.close()
        return jsonify({"error": f"Quote {quote_id} not found."}), 404

    rfq = None
    if quote["rfq_id"]:
        rfq = conn.execute("SELECT * FROM rfqs WHERE id=?", (quote["rfq_id"],)).fetchone()

    items = conn.execute(
        "SELECT * FROM quote_items WHERE quote_id=?", (quote_id,)
    ).fetchall()
    conn.close()

    settings = _get_app_settings()

    rfq_dict   = dict(rfq) if rfq else {}
    quote_dict = dict(quote)
    items_list = [dict(i) for i in items]

    body = draft_reply_email(quote_dict, rfq_dict, items_list, settings)
    if not body:
        return jsonify({"error": "AI draft generation failed — check ANTHROPIC_API_KEY."}), 500

    # Build a sensible subject line
    customer = rfq_dict.get("customer_name") or rfq_dict.get("company") or "Customer"
    subject = (
        f"Quote {quote_dict.get('quote_number', '')} — "
        f"Eastern Aero Pty Ltd / {customer}"
    )

    return jsonify({"subject": subject, "body": body})


# ─────────────────────────────────────────────────────────────────────────────
# GET /api/ai/customer-insights/<email>
# Returns: { summary, total_rfqs, win_rate_pct, top_parts, recommended_actions, ... }
# ─────────────────────────────────────────────────────────────────────────────
@agents_bp.route("/customer-insights/<path:email>", methods=["GET"])
@login_required
def api_customer_insights(email):
    """
    Return an AI-generated insight card for a customer email address.

    Useful as a sidebar panel on the customer detail page or RFQ view —
    gives the sales rep a quick read on who the customer is and what to do next.
    """
    email = email.strip().lower()
    if not email or "@" not in email:
        return jsonify({"error": "A valid email address is required."}), 400

    db_path = _get_database_path()
    insights = customer_insights(email, db_path)

    if "error" in insights:
        return jsonify(insights), 500

    return jsonify(insights)


# ─────────────────────────────────────────────────────────────────────────────
# POST /api/ai/classify-spam
# Body: { "body": "...", "sender_email": "...", "sender_name": "...", "subject": "..." }
# Returns: { "is_spam": bool, "confidence": float, "reason": str }
# ─────────────────────────────────────────────────────────────────────────────
@agents_bp.route("/classify-spam", methods=["POST"])
@login_required
def api_classify_spam():
    """
    Classify an incoming email as genuine RFQ or spam/junk.

    Call this during the email-fetch flow before creating an RFQ record.
    If is_spam=true and confidence > 0.85, skip inserting the email into rfqs.
    """
    data = request.get_json(silent=True) or {}
    body         = data.get("body", "")
    sender_email = data.get("sender_email", "")
    sender_name  = data.get("sender_name", "")
    subject      = data.get("subject", "")

    if not body and not subject:
        return jsonify({"error": "No email content provided."}), 400

    result = classify_rfq_spam(body, sender_email, sender_name, subject)
    return jsonify(result)


# ─────────────────────────────────────────────────────────────────────────────
# POST /api/ai/review-quote/<quote_id>
# Returns: { "ok": bool, "score": int, "issues": [...], "warnings": [...] }
# ─────────────────────────────────────────────────────────────────────────────
@agents_bp.route("/review-quote/<int:quote_id>", methods=["POST"])
@login_required
def api_review_quote(quote_id):
    """
    Run a pre-send sanity check on a quote.

    Show the result in a confirmation modal before the user clicks "Send".
    Block sending only when ok=false (has blocking issues).
    """
    conn = _get_app_db()
    quote = conn.execute("SELECT * FROM quotes WHERE id=?", (quote_id,)).fetchone()
    if not quote:
        conn.close()
        return jsonify({"error": f"Quote {quote_id} not found."}), 404

    items = conn.execute(
        "SELECT * FROM quote_items WHERE quote_id=?", (quote_id,)
    ).fetchall()
    conn.close()

    quote_dict = dict(quote)
    items_list = [dict(i) for i in items]

    result = review_quote_before_send(quote_dict, items_list)
    return jsonify(result)


# ─────────────────────────────────────────────────────────────────────────────
# POST /api/ai/followup-email/<quote_id>
# Returns: { "subject": "...", "body": "...", "days_since_sent": N }
# ─────────────────────────────────────────────────────────────────────────────
@agents_bp.route("/followup-email/<int:quote_id>", methods=["POST"])
@login_required
def api_followup_email(quote_id):
    """
    Draft a polite follow-up email for a sent quote with no response.

    The returned body can be shown in an editable modal.
    """
    from datetime import datetime, timezone

    conn = _get_app_db()
    quote = conn.execute("SELECT * FROM quotes WHERE id=?", (quote_id,)).fetchone()
    if not quote:
        conn.close()
        return jsonify({"error": f"Quote {quote_id} not found."}), 404

    rfq = None
    if quote["rfq_id"]:
        rfq = conn.execute("SELECT * FROM rfqs WHERE id=?", (quote["rfq_id"],)).fetchone()
    conn.close()

    settings   = _get_app_settings()
    quote_dict = dict(quote)
    rfq_dict   = dict(rfq) if rfq else {}

    # Calculate days since sent
    days_since_sent = 0
    sent_at_str = quote_dict.get("sent_at") or quote_dict.get("created_at") or ""
    if sent_at_str:
        try:
            sent_dt = datetime.fromisoformat(sent_at_str.replace("Z", "+00:00"))
            if sent_dt.tzinfo is None:
                sent_dt = sent_dt.replace(tzinfo=timezone.utc)
            days_since_sent = max(0, (datetime.now(timezone.utc) - sent_dt).days)
        except Exception:
            pass

    body = draft_followup_email(quote_dict, rfq_dict, days_since_sent, settings)
    if not body:
        return jsonify({"error": "Failed to draft follow-up — check ANTHROPIC_API_KEY."}), 500

    customer = rfq_dict.get("customer_name") or quote_dict.get("customer_name") or "Customer"
    subject = (
        f"Following Up — Quote {quote_dict.get('quote_number', '')} / {customer}"
    )

    return jsonify({
        "subject":        subject,
        "body":           body,
        "days_since_sent": days_since_sent,
    })


# ─────────────────────────────────────────────────────────────────────────────
# POST /api/ai/match-customer
# Body: { "name": "...", "email": "...", "company": "..." }
# Returns: { "matched": bool, "customer_id": int|null, "confidence": float, "reason": str }
# ─────────────────────────────────────────────────────────────────────────────
@agents_bp.route("/match-customer", methods=["POST"])
@login_required
def api_match_customer():
    """
    Try to match an incoming RFQ sender to an existing customer profile.

    Call this when creating a new RFQ manually or during email import.
    If confidence > 0.8, auto-link the RFQ to the matched customer.
    """
    data = request.get_json(silent=True) or {}
    name    = data.get("name", "").strip()
    email   = data.get("email", "").strip()
    company = data.get("company", "").strip()

    if not email and not name and not company:
        return jsonify({"error": "Provide at least one of: name, email, company."}), 400

    conn = _get_app_db()
    profiles = [dict(r) for r in conn.execute(
        "SELECT id, name, email, company FROM customer_profiles ORDER BY name"
    ).fetchall()]
    conn.close()

    result = match_customer_from_rfq(name, email, company, profiles)
    return jsonify(result)


# ─────────────────────────────────────────────────────────────────────────────
# GET /api/ai/inventory-health
# Returns: { "findings": [ {severity, part_number, issue, suggestion}, ... ], "counts": {...} }
# ─────────────────────────────────────────────────────────────────────────────
@agents_bp.route("/inventory-health", methods=["GET"])
@login_required
def api_inventory_health():
    """
    Run an AI-powered inventory health audit.

    Returns a list of findings sorted by severity (errors first).
    Useful for a dashboard widget or a dedicated Inventory Health page.
    """
    db_path = _get_database_path()
    findings = inventory_health_check(db_path)

    # Summarise counts by severity
    counts = {"error": 0, "warning": 0, "info": 0}
    for f in findings:
        sev = f.get("severity", "info")
        counts[sev] = counts.get(sev, 0) + 1

    # Sort: errors first, then warnings, then info
    order = {"error": 0, "warning": 1, "info": 2}
    findings.sort(key=lambda f: order.get(f.get("severity", "info"), 3))

    return jsonify({"findings": findings, "counts": counts})


# ─────────────────────────────────────────────────────────────────────────────
# GET /api/ai/health
# Returns basic status for verifying the blueprint is wired up.
# ─────────────────────────────────────────────────────────────────────────────
@agents_bp.route("/health", methods=["GET"])
@login_required
def api_ai_health():
    import os
    key_set = bool(os.environ.get("ANTHROPIC_API_KEY"))
    return jsonify({
        "status": "ok" if key_set else "degraded",
        "anthropic_key_configured": key_set,
        "agents": [
            "parse-rfq",
            "auto-quote",
            "draft-reply",
            "customer-insights",
            "classify-spam",
            "review-quote",
            "followup-email",
            "match-customer",
            "inventory-health",
        ],
    })
