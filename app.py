"""
Eastern Aero Pty Ltd — Auto Quote System
A Rotabull-style aviation parts quoting application.
"""

from flask import Flask, render_template, request, redirect, url_for, jsonify, flash, send_file
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from werkzeug.security import generate_password_hash, check_password_hash
import sqlite3
import pandas as pd
import os
import re
import imaplib
import email
from email.header import decode_header
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime
from functools import wraps
import urllib.request
import json
import mimetypes
import traceback

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'aero-quote-secret-change-in-production')

# ─── Flask-Login ─────────────────────────────────────────────────────────────
login_manager = LoginManager(app)
login_manager.login_view = 'login'
login_manager.login_message = 'Please log in to access this page.'
login_manager.login_message_category = 'error'

class User(UserMixin):
    def __init__(self, id, username, email, role):
        self.id       = id
        self.username = username
        self.email    = email
        self.role     = role

    @property
    def is_admin(self):
        return self.role == 'admin'

@login_manager.user_loader
def load_user(user_id):
    conn = get_db()
    row  = conn.execute('SELECT * FROM users WHERE id=? AND active=1', (user_id,)).fetchone()
    conn.close()
    if row:
        return User(row['id'], row['username'], row['email'], row['role'])
    return None

def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not current_user.is_authenticated or not current_user.is_admin:
            flash('Admin access required.', 'error')
            return redirect(url_for('dashboard'))
        return f(*args, **kwargs)
    return decorated

@app.context_processor
def inject_now():
    return {'now': datetime.now()}

@app.template_filter('clean_desc')
def clean_desc_filter(s):
    """Strip 'Quantity: X' bleed-through from parsed descriptions."""
    if not s:
        return '—'
    cleaned = re.sub(r'\s*\bQuantity[\s:]+\d+\b.*$', '', s, flags=re.I).strip()
    return cleaned or '—'

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Use persistent volume path on Railway (set DB_PATH env var), fallback to local
_db_dir  = os.environ.get('DB_PATH', os.path.join(BASE_DIR, 'data'))
os.makedirs(_db_dir, exist_ok=True)
DATABASE  = os.path.join(_db_dir, 'quotes.db')

UPLOAD_FOLDER = os.path.join(_db_dir, 'uploads')
os.makedirs(UPLOAD_FOLDER, exist_ok=True)


# ─── Database ────────────────────────────────────────────────────────────────

def get_db():
    conn = sqlite3.connect(DATABASE, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA busy_timeout=30000')
    return conn


def init_db():
    conn = get_db()
    conn.executescript('''
        CREATE TABLE IF NOT EXISTS inventory (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            part_number  TEXT NOT NULL,
            description  TEXT,
            condition    TEXT DEFAULT 'SV',
            quantity     INTEGER DEFAULT 0,
            unit_cost    REAL DEFAULT 0,
            unit_price   REAL DEFAULT 0,
            location     TEXT,
            uom          TEXT DEFAULT 'EA',
            updated_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS rfqs (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            rfq_number     TEXT UNIQUE,
            customer_name  TEXT,
            customer_email TEXT,
            company        TEXT,
            phone          TEXT,
            source         TEXT DEFAULT 'web',
            status         TEXT DEFAULT 'pending',
            notes          TEXT,
            raw_email      TEXT,
            email_message_id TEXT,
            created_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS rfq_items (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            rfq_id      INTEGER,
            part_number TEXT,
            description TEXT,
            quantity    INTEGER DEFAULT 1,
            condition   TEXT DEFAULT 'any',
            FOREIGN KEY (rfq_id) REFERENCES rfqs(id)
        );

        CREATE TABLE IF NOT EXISTS quotes (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            quote_number        TEXT UNIQUE,
            rfq_id              INTEGER,
            status              TEXT DEFAULT 'draft',
            markup_percent      REAL DEFAULT 30,
            total_amount        REAL DEFAULT 0,
            notes               TEXT,
            valid_days          INTEGER DEFAULT 30,
            currency            TEXT DEFAULT 'USD',
            exchange_loan_fee   REAL DEFAULT 0,
            entry_date          TEXT,
            overhaul_price_est  REAL DEFAULT 0,
            core_price          REAL DEFAULT 0,
            outright_amount     REAL DEFAULT 0,
            days_core_return    INTEGER DEFAULT 30,
            fee_billings_count  INTEGER DEFAULT 1,
            billing_start_date  TEXT,
            billing_interval_days INTEGER DEFAULT 30,
            deposit_amount      REAL DEFAULT 0,
            core_due_date       TEXT,
            schedule_billing_date TEXT,
            periodic_billing_amt  REAL DEFAULT 0,
            cogs_percent        REAL DEFAULT 0,
            cogs_amount         REAL DEFAULT 0,
            created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            sent_at             TIMESTAMP,
            FOREIGN KEY (rfq_id) REFERENCES rfqs(id)
        );

        CREATE TABLE IF NOT EXISTS quote_items (
            id                 INTEGER PRIMARY KEY AUTOINCREMENT,
            quote_id           INTEGER,
            part_number        TEXT,
            description        TEXT,
            condition          TEXT,
            quantity_requested INTEGER,
            quantity_available INTEGER DEFAULT 0,
            unit_price         REAL DEFAULT 0,
            extended_price     REAL DEFAULT 0,
            matched            INTEGER DEFAULT 0,
            notes              TEXT,
            lead_time          TEXT DEFAULT 'Stock',
            price_type         TEXT DEFAULT 'Outright',
            warranty           TEXT DEFAULT '3 Months',
            trace_to           TEXT,
            tag_type           TEXT,
            tagged_by          TEXT,
            FOREIGN KEY (quote_id) REFERENCES quotes(id)
        );

        CREATE TABLE IF NOT EXISTS settings (
            key   TEXT PRIMARY KEY,
            value TEXT
        );

        CREATE TABLE IF NOT EXISTS imported_emails (
            message_id  TEXT PRIMARY KEY,
            imported_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS blocked_senders (
            email       TEXT PRIMARY KEY,
            reason      TEXT,
            blocked_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS users (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            username      TEXT UNIQUE NOT NULL,
            email         TEXT,
            password_hash TEXT NOT NULL,
            role          TEXT DEFAULT 'staff',
            active        INTEGER DEFAULT 1,
            created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS vendors (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            name            TEXT NOT NULL,
            phone           TEXT,
            fax             TEXT,
            email           TEXT,
            website         TEXT,
            payment_method  TEXT DEFAULT 'Check',
            terms           TEXT DEFAULT '30 days',
            credit_limit    REAL DEFAULT 0,
            balance         REAL DEFAULT 0,
            account_number  TEXT,
            min_po          REAL DEFAULT 0,
            tax_id          TEXT,
            tax_percent     REAL DEFAULT 0,
            gl_account      TEXT DEFAULT '1200 | Inventory - rotables',
            status          TEXT DEFAULT 'Active',
            currency        TEXT DEFAULT 'USD',
            tags            TEXT,
            notes           TEXT,
            billing_name    TEXT,
            billing_address TEXT,
            billing_city    TEXT,
            billing_state   TEXT,
            billing_zip     TEXT,
            billing_country TEXT DEFAULT 'USA',
            shipping_name   TEXT,
            shipping_address TEXT,
            shipping_city   TEXT,
            shipping_state  TEXT,
            shipping_zip    TEXT,
            shipping_country TEXT DEFAULT 'USA',
            created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS customers (
            id                      INTEGER PRIMARY KEY AUTOINCREMENT,
            name                    TEXT NOT NULL,
            phone                   TEXT,
            fax                     TEXT,
            email                   TEXT,
            payment_method          TEXT DEFAULT 'Check',
            payment_terms           TEXT DEFAULT 'COD',
            credit_limit            REAL DEFAULT 0,
            balance                 REAL DEFAULT 0,
            hourly_rate             REAL DEFAULT 100,
            tax_id                  TEXT,
            tax_percent             REAL DEFAULT 0,
            vat_number              TEXT,
            date_format             TEXT DEFAULT 'mm-yyyy',
            sales_person            TEXT,
            purchasing_person       TEXT,
            customer_service_rep    TEXT,
            shipping_service        TEXT,
            status                  TEXT DEFAULT 'Active',
            required_part_categories TEXT,
            currency                TEXT DEFAULT 'USD',
            tags                    TEXT,
            statement_notes         TEXT,
            invoice_notes           TEXT,
            notes                   TEXT,
            related_vendor_id       INTEGER,
            billing_name            TEXT,
            billing_address         TEXT,
            billing_city            TEXT,
            billing_state           TEXT,
            billing_zip             TEXT,
            billing_country         TEXT DEFAULT 'USA',
            shipping_name           TEXT,
            shipping_address        TEXT,
            shipping_city           TEXT,
            shipping_state          TEXT,
            shipping_zip            TEXT,
            shipping_country        TEXT DEFAULT 'USA',
            created_at              TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS invoice_attachments (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            invoice_id  INTEGER NOT NULL,
            label       TEXT DEFAULT 'Customer PO',
            filename    TEXT NOT NULL,
            filepath    TEXT NOT NULL,
            mimetype    TEXT,
            uploaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (invoice_id) REFERENCES invoices(id)
        );

        CREATE TABLE IF NOT EXISTS contacts (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            entity_type TEXT NOT NULL,
            entity_id   INTEGER NOT NULL,
            first_name  TEXT,
            last_name   TEXT,
            title       TEXT,
            email       TEXT,
            phone       TEXT,
            mobile      TEXT,
            is_primary  INTEGER DEFAULT 0,
            notes       TEXT,
            created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS purchase_orders (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            po_number        TEXT UNIQUE,
            vendor_name      TEXT,
            vendor_address   TEXT,
            ship_to_name     TEXT DEFAULT 'Eastern Aero Pty Ltd',
            ship_to_address  TEXT DEFAULT '10 Composure St\nMorayfield QLD 4506 Australia',
            date             TEXT,
            ship_date        TEXT,
            terms            TEXT DEFAULT 'Net 30',
            ship_via         TEXT,
            shipping_account TEXT,
            subtotal         REAL DEFAULT 0,
            shipping         REAL DEFAULT 0,
            tax_rate         REAL DEFAULT 0,
            sales_tax        REAL DEFAULT 0,
            grand_total      REAL DEFAULT 0,
            notes            TEXT,
            status           TEXT DEFAULT 'draft',
            created_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS po_items (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            po_id       INTEGER,
            part_number TEXT,
            description TEXT,
            condition   TEXT,
            quantity    REAL DEFAULT 1,
            unit_price  REAL DEFAULT 0,
            total_price REAL DEFAULT 0,
            FOREIGN KEY (po_id) REFERENCES purchase_orders(id)
        );

        CREATE TABLE IF NOT EXISTS invoices (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            invoice_number   TEXT UNIQUE,
            invoice_for      TEXT,
            customer_name    TEXT,
            customer_address TEXT,
            reference        TEXT,
            due_date         TEXT,
            subtotal         REAL DEFAULT 0,
            adjustments      REAL DEFAULT 0,
            grand_total      REAL DEFAULT 0,
            notes            TEXT,
            status           TEXT DEFAULT 'draft',
            created_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS invoice_items (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            invoice_id  INTEGER,
            part_number TEXT,
            description TEXT,
            condition   TEXT,
            quantity    REAL DEFAULT 1,
            unit_price  REAL DEFAULT 0,
            total_price REAL DEFAULT 0,
            FOREIGN KEY (invoice_id) REFERENCES invoices(id)
        );

        CREATE TABLE IF NOT EXISTS packing_slips (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            ps_number        TEXT UNIQUE,
            date             TEXT,
            terms            TEXT,
            po_number        TEXT,
            invoice_number   TEXT,
            ship_date        TEXT,
            ship_via         TEXT,
            shipping_account TEXT,
            vendor_name      TEXT,
            vendor_address   TEXT,
            ship_to_name     TEXT,
            ship_to_address  TEXT,
            notes            TEXT,
            pallet_dims      TEXT,
            weight_lbs       REAL DEFAULT 0,
            status           TEXT DEFAULT 'draft',
            created_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS ps_items (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            ps_id             INTEGER,
            part_number       TEXT,
            description       TEXT,
            serial_number     TEXT,
            quantity          REAL DEFAULT 1,
            country_of_origin TEXT DEFAULT 'USA',
            hs_code           TEXT,
            FOREIGN KEY (ps_id) REFERENCES packing_slips(id)
        );

        CREATE TABLE IF NOT EXISTS repair_orders (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            ro_number        TEXT UNIQUE,
            vendor_name      TEXT,
            vendor_address   TEXT,
            ship_to_name     TEXT DEFAULT 'Eastern Aero Pty Ltd',
            ship_to_address  TEXT DEFAULT '10 Composure St\nMorayfield QLD 4506 Australia',
            date             TEXT,
            ship_date        TEXT,
            terms            TEXT DEFAULT 'Net 30',
            ship_via         TEXT,
            shipping_account TEXT,
            subtotal         REAL DEFAULT 0,
            shipping         REAL DEFAULT 0,
            tax_rate         REAL DEFAULT 0,
            sales_tax        REAL DEFAULT 0,
            grand_total      REAL DEFAULT 0,
            notes            TEXT,
            status           TEXT DEFAULT 'draft',
            created_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS ro_items (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            ro_id          INTEGER,
            part_number    TEXT,
            description    TEXT,
            serial_number  TEXT,
            quantity       REAL DEFAULT 1,
            work_requested TEXT,
            avg_cost       REAL DEFAULT 0,
            total_price    REAL DEFAULT 0,
            FOREIGN KEY (ro_id) REFERENCES repair_orders(id)
        );
    ''')

    defaults = {
        'company_name':    'Eastern Aero Pty Ltd',
        'company_email':   'easternaeroparts@gmail.com',
        'company_phone':   '',
        'company_address': '',
        'default_markup':  '30',
        'quote_valid_days':'30',
        'imap_host':       'imap.gmail.com',
        'imap_port':       '993',
        'imap_user':       'easternaeroparts@gmail.com',
        'imap_pass':       '',
        'imap_folder':     'INBOX',
        'smtp_host':       'smtp.gmail.com',
        'smtp_port':       '587',
        'smtp_user':       'easternaeroparts@gmail.com',
        'smtp_pass':       '',
        'resend_api_key':    '',
    }
    for k, v in defaults.items():
        conn.execute('INSERT OR IGNORE INTO settings VALUES (?,?)', (k, v))

    # Create default admin if no users exist
    count = conn.execute('SELECT COUNT(*) FROM users').fetchone()[0]
    if count == 0:
        conn.execute(
            'INSERT INTO users (username, email, password_hash, role) VALUES (?,?,?,?)',
            ('admin', 'easternaeroparts@gmail.com',
             generate_password_hash('admin123'), 'admin'))
        print('  Default admin created: username=admin  password=admin123')
        print('  ⚠  Change this password immediately in Settings → Users.')

    # Migrations — add columns that may not exist in older databases
    migrations = [
        "ALTER TABLE rfqs ADD COLUMN email_message_id TEXT",
        "CREATE TABLE IF NOT EXISTS blocked_senders (email TEXT PRIMARY KEY, reason TEXT, blocked_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)",
        "ALTER TABLE quotes ADD COLUMN currency TEXT DEFAULT 'USD'",
        "ALTER TABLE quotes ADD COLUMN exchange_loan_fee REAL DEFAULT 0",
        "ALTER TABLE quotes ADD COLUMN entry_date TEXT",
        "ALTER TABLE quotes ADD COLUMN overhaul_price_est REAL DEFAULT 0",
        "ALTER TABLE quotes ADD COLUMN core_price REAL DEFAULT 0",
        "ALTER TABLE quotes ADD COLUMN outright_amount REAL DEFAULT 0",
        "ALTER TABLE quotes ADD COLUMN days_core_return INTEGER DEFAULT 30",
        "ALTER TABLE quotes ADD COLUMN fee_billings_count INTEGER DEFAULT 1",
        "ALTER TABLE quotes ADD COLUMN billing_start_date TEXT",
        "ALTER TABLE quotes ADD COLUMN billing_interval_days INTEGER DEFAULT 30",
        "ALTER TABLE quotes ADD COLUMN deposit_amount REAL DEFAULT 0",
        "ALTER TABLE quotes ADD COLUMN core_due_date TEXT",
        "ALTER TABLE quotes ADD COLUMN schedule_billing_date TEXT",
        "ALTER TABLE quotes ADD COLUMN periodic_billing_amt REAL DEFAULT 0",
        "ALTER TABLE quotes ADD COLUMN cogs_percent REAL DEFAULT 0",
        "ALTER TABLE quotes ADD COLUMN cogs_amount REAL DEFAULT 0",
        "ALTER TABLE quote_items ADD COLUMN lead_time TEXT DEFAULT 'Stock'",
        "ALTER TABLE quote_items ADD COLUMN price_type TEXT DEFAULT 'Outright'",
        "ALTER TABLE quote_items ADD COLUMN warranty TEXT DEFAULT '3 Months'",
        "ALTER TABLE quote_items ADD COLUMN trace_to TEXT",
        "ALTER TABLE quote_items ADD COLUMN tag_type TEXT",
        "ALTER TABLE quote_items ADD COLUMN tagged_by TEXT",
        "INSERT OR IGNORE INTO settings VALUES ('resend_api_key','')",
        "ALTER TABLE rfqs ADD COLUMN website TEXT",
        """CREATE TABLE IF NOT EXISTS customer_profiles (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            email          TEXT UNIQUE NOT NULL,
            name           TEXT,
            company        TEXT,
            phone          TEXT,
            website        TEXT,
            updated_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""",
        "ALTER TABLE rfqs ADD COLUMN customer_ref TEXT",
        "ALTER TABLE rfqs ADD COLUMN address TEXT",
        "ALTER TABLE customer_profiles ADD COLUMN address TEXT",
        "ALTER TABLE quote_items ADD COLUMN no_quote INTEGER DEFAULT 0",
        """CREATE TABLE IF NOT EXISTS quote_attachments (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            quote_id    INTEGER NOT NULL,
            filename    TEXT NOT NULL,
            filepath    TEXT NOT NULL,
            mimetype    TEXT,
            uploaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (quote_id) REFERENCES quotes(id)
        )""",
        "ALTER TABLE quote_attachments ADD COLUMN part_number TEXT",
        "ALTER TABLE quote_attachments ADD COLUMN serial_number TEXT",
        "ALTER TABLE quote_attachments ADD COLUMN verified INTEGER DEFAULT 0",
    ]
    # ERP vendor/customer migrations — extend existing vendors table if needed
    erp_migrations = [
        "ALTER TABLE vendors ADD COLUMN fax TEXT",
        "ALTER TABLE vendors ADD COLUMN website TEXT",
        "ALTER TABLE vendors ADD COLUMN payment_method TEXT DEFAULT 'Check'",
        "ALTER TABLE vendors ADD COLUMN terms TEXT DEFAULT '30 days'",
        "ALTER TABLE vendors ADD COLUMN credit_limit REAL DEFAULT 0",
        "ALTER TABLE vendors ADD COLUMN balance REAL DEFAULT 0",
        "ALTER TABLE vendors ADD COLUMN account_number TEXT",
        "ALTER TABLE vendors ADD COLUMN min_po REAL DEFAULT 0",
        "ALTER TABLE vendors ADD COLUMN tax_id TEXT",
        "ALTER TABLE vendors ADD COLUMN tax_percent REAL DEFAULT 0",
        "ALTER TABLE vendors ADD COLUMN gl_account TEXT DEFAULT '1200 | Inventory - rotables'",
        "ALTER TABLE vendors ADD COLUMN status TEXT DEFAULT 'Active'",
        "ALTER TABLE vendors ADD COLUMN currency TEXT DEFAULT 'USD'",
        "ALTER TABLE vendors ADD COLUMN tags TEXT",
        "ALTER TABLE vendors ADD COLUMN billing_name TEXT",
        "ALTER TABLE vendors ADD COLUMN billing_address TEXT",
        "ALTER TABLE vendors ADD COLUMN billing_city TEXT",
        "ALTER TABLE vendors ADD COLUMN billing_state TEXT",
        "ALTER TABLE vendors ADD COLUMN billing_zip TEXT",
        "ALTER TABLE vendors ADD COLUMN billing_country TEXT DEFAULT 'USA'",
        "ALTER TABLE vendors ADD COLUMN shipping_name TEXT",
        "ALTER TABLE vendors ADD COLUMN shipping_address TEXT",
        "ALTER TABLE vendors ADD COLUMN shipping_city TEXT",
        "ALTER TABLE vendors ADD COLUMN shipping_state TEXT",
        "ALTER TABLE vendors ADD COLUMN shipping_zip TEXT",
        "ALTER TABLE vendors ADD COLUMN shipping_country TEXT DEFAULT 'USA'",
        # ERP tables — safety net in case executescript didn't reach them on an existing DB
        """CREATE TABLE IF NOT EXISTS customers (
            id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, phone TEXT, fax TEXT,
            email TEXT, payment_method TEXT DEFAULT 'Check', payment_terms TEXT DEFAULT 'COD',
            credit_limit REAL DEFAULT 0, balance REAL DEFAULT 0, hourly_rate REAL DEFAULT 100,
            tax_id TEXT, tax_percent REAL DEFAULT 0, vat_number TEXT, date_format TEXT DEFAULT 'mm-yyyy',
            sales_person TEXT, purchasing_person TEXT, customer_service_rep TEXT, shipping_service TEXT,
            status TEXT DEFAULT 'Active', required_part_categories TEXT, currency TEXT DEFAULT 'USD',
            tags TEXT, statement_notes TEXT, invoice_notes TEXT, notes TEXT, related_vendor_id INTEGER,
            billing_name TEXT, billing_address TEXT, billing_city TEXT, billing_state TEXT,
            billing_zip TEXT, billing_country TEXT DEFAULT 'USA', shipping_name TEXT, shipping_address TEXT,
            shipping_city TEXT, shipping_state TEXT, shipping_zip TEXT, shipping_country TEXT DEFAULT 'USA',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""",
        """CREATE TABLE IF NOT EXISTS contacts (
            id INTEGER PRIMARY KEY AUTOINCREMENT, entity_type TEXT NOT NULL, entity_id INTEGER NOT NULL,
            first_name TEXT, last_name TEXT, title TEXT, email TEXT, phone TEXT, mobile TEXT,
            is_primary INTEGER DEFAULT 0, notes TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""",
        """CREATE TABLE IF NOT EXISTS invoice_attachments (
            id INTEGER PRIMARY KEY AUTOINCREMENT, invoice_id INTEGER NOT NULL,
            label TEXT DEFAULT 'Customer PO', filename TEXT NOT NULL, filepath TEXT NOT NULL,
            mimetype TEXT, uploaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""",
        """CREATE TABLE IF NOT EXISTS purchase_orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT, po_number TEXT UNIQUE, vendor_name TEXT,
            vendor_address TEXT, ship_to_name TEXT DEFAULT 'Eastern Aero Pty Ltd',
            ship_to_address TEXT, date TEXT, ship_date TEXT, terms TEXT DEFAULT 'Net 30',
            ship_via TEXT, shipping_account TEXT, subtotal REAL DEFAULT 0, shipping REAL DEFAULT 0,
            tax_rate REAL DEFAULT 0, sales_tax REAL DEFAULT 0, grand_total REAL DEFAULT 0,
            notes TEXT, status TEXT DEFAULT 'draft', created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""",
        """CREATE TABLE IF NOT EXISTS po_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT, po_id INTEGER, part_number TEXT,
            description TEXT, condition TEXT, quantity REAL DEFAULT 1,
            unit_price REAL DEFAULT 0, total_price REAL DEFAULT 0)""",
        """CREATE TABLE IF NOT EXISTS invoices (
            id INTEGER PRIMARY KEY AUTOINCREMENT, invoice_number TEXT UNIQUE, invoice_for TEXT,
            customer_name TEXT, customer_address TEXT, reference TEXT, due_date TEXT,
            subtotal REAL DEFAULT 0, adjustments REAL DEFAULT 0, grand_total REAL DEFAULT 0,
            notes TEXT, status TEXT DEFAULT 'draft', created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""",
        """CREATE TABLE IF NOT EXISTS invoice_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT, invoice_id INTEGER, part_number TEXT,
            description TEXT, condition TEXT, quantity REAL DEFAULT 1,
            unit_price REAL DEFAULT 0, total_price REAL DEFAULT 0)""",
        """CREATE TABLE IF NOT EXISTS packing_slips (
            id INTEGER PRIMARY KEY AUTOINCREMENT, ps_number TEXT UNIQUE, date TEXT, terms TEXT,
            po_number TEXT, invoice_number TEXT, ship_date TEXT, ship_via TEXT, shipping_account TEXT,
            vendor_name TEXT, vendor_address TEXT, ship_to_name TEXT, ship_to_address TEXT,
            notes TEXT, pallet_dims TEXT, weight_lbs REAL DEFAULT 0, status TEXT DEFAULT 'draft',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""",
        """CREATE TABLE IF NOT EXISTS ps_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT, ps_id INTEGER, part_number TEXT, description TEXT,
            serial_number TEXT, quantity REAL DEFAULT 1, country_of_origin TEXT DEFAULT 'USA', hs_code TEXT)""",
        """CREATE TABLE IF NOT EXISTS repair_orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT, ro_number TEXT UNIQUE, vendor_name TEXT,
            vendor_address TEXT, ship_to_name TEXT DEFAULT 'Eastern Aero Pty Ltd', ship_to_address TEXT,
            date TEXT, ship_date TEXT, terms TEXT DEFAULT 'Net 30', ship_via TEXT, shipping_account TEXT,
            subtotal REAL DEFAULT 0, shipping REAL DEFAULT 0, tax_rate REAL DEFAULT 0,
            sales_tax REAL DEFAULT 0, grand_total REAL DEFAULT 0, notes TEXT,
            status TEXT DEFAULT 'draft', created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""",
        """CREATE TABLE IF NOT EXISTS ro_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT, ro_id INTEGER, part_number TEXT, description TEXT,
            serial_number TEXT, quantity REAL DEFAULT 1, work_requested TEXT,
            avg_cost REAL DEFAULT 0, total_price REAL DEFAULT 0)""",
        # ALTER TABLE migrations for ERP tables — adds any missing columns to existing tables
        "ALTER TABLE purchase_orders ADD COLUMN vendor_name TEXT",
        "ALTER TABLE purchase_orders ADD COLUMN vendor_address TEXT",
        "ALTER TABLE purchase_orders ADD COLUMN ship_to_name TEXT DEFAULT 'Eastern Aero Pty Ltd'",
        "ALTER TABLE purchase_orders ADD COLUMN ship_to_address TEXT",
        "ALTER TABLE purchase_orders ADD COLUMN date TEXT",
        "ALTER TABLE purchase_orders ADD COLUMN ship_date TEXT",
        "ALTER TABLE purchase_orders ADD COLUMN terms TEXT DEFAULT 'Net 30'",
        "ALTER TABLE purchase_orders ADD COLUMN ship_via TEXT",
        "ALTER TABLE purchase_orders ADD COLUMN shipping_account TEXT",
        "ALTER TABLE purchase_orders ADD COLUMN subtotal REAL DEFAULT 0",
        "ALTER TABLE purchase_orders ADD COLUMN shipping REAL DEFAULT 0",
        "ALTER TABLE purchase_orders ADD COLUMN tax_rate REAL DEFAULT 0",
        "ALTER TABLE purchase_orders ADD COLUMN sales_tax REAL DEFAULT 0",
        "ALTER TABLE purchase_orders ADD COLUMN grand_total REAL DEFAULT 0",
        "ALTER TABLE purchase_orders ADD COLUMN notes TEXT",
        "ALTER TABLE purchase_orders ADD COLUMN status TEXT DEFAULT 'draft'",
        "ALTER TABLE purchase_orders ADD COLUMN created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP",
        "ALTER TABLE po_items ADD COLUMN part_number TEXT",
        "ALTER TABLE po_items ADD COLUMN description TEXT",
        "ALTER TABLE po_items ADD COLUMN serial_number TEXT",
        "ALTER TABLE po_items ADD COLUMN condition TEXT",
        "ALTER TABLE po_items ADD COLUMN quantity REAL DEFAULT 1",
        "ALTER TABLE po_items ADD COLUMN unit_price REAL DEFAULT 0",
        "ALTER TABLE po_items ADD COLUMN total_price REAL DEFAULT 0",
        "ALTER TABLE invoices ADD COLUMN invoice_number TEXT",
        "ALTER TABLE invoices ADD COLUMN invoice_for TEXT",
        "ALTER TABLE invoices ADD COLUMN customer_name TEXT",
        "ALTER TABLE invoices ADD COLUMN customer_address TEXT",
        "ALTER TABLE invoices ADD COLUMN reference TEXT",
        "ALTER TABLE invoices ADD COLUMN due_date TEXT",
        "ALTER TABLE invoices ADD COLUMN subtotal REAL DEFAULT 0",
        "ALTER TABLE invoices ADD COLUMN adjustments REAL DEFAULT 0",
        "ALTER TABLE invoices ADD COLUMN grand_total REAL DEFAULT 0",
        "ALTER TABLE invoices ADD COLUMN notes TEXT",
        "ALTER TABLE invoices ADD COLUMN status TEXT DEFAULT 'draft'",
        "ALTER TABLE invoices ADD COLUMN created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP",
        "ALTER TABLE invoice_items ADD COLUMN part_number TEXT",
        "ALTER TABLE invoice_items ADD COLUMN description TEXT",
        "ALTER TABLE invoice_items ADD COLUMN serial_number TEXT",
        "ALTER TABLE invoice_items ADD COLUMN condition TEXT",
        "ALTER TABLE invoice_items ADD COLUMN quantity REAL DEFAULT 1",
        "ALTER TABLE invoice_items ADD COLUMN unit_price REAL DEFAULT 0",
        "ALTER TABLE invoice_items ADD COLUMN total_price REAL DEFAULT 0",
        "ALTER TABLE packing_slips ADD COLUMN ps_number TEXT",
        "ALTER TABLE packing_slips ADD COLUMN date TEXT",
        "ALTER TABLE packing_slips ADD COLUMN terms TEXT",
        "ALTER TABLE packing_slips ADD COLUMN po_number TEXT",
        "ALTER TABLE packing_slips ADD COLUMN invoice_number TEXT",
        "ALTER TABLE packing_slips ADD COLUMN ship_date TEXT",
        "ALTER TABLE packing_slips ADD COLUMN ship_via TEXT",
        "ALTER TABLE packing_slips ADD COLUMN shipping_account TEXT",
        "ALTER TABLE packing_slips ADD COLUMN vendor_name TEXT",
        "ALTER TABLE packing_slips ADD COLUMN vendor_address TEXT",
        "ALTER TABLE packing_slips ADD COLUMN ship_to_name TEXT",
        "ALTER TABLE packing_slips ADD COLUMN ship_to_address TEXT",
        "ALTER TABLE packing_slips ADD COLUMN notes TEXT",
        "ALTER TABLE packing_slips ADD COLUMN pallet_dims TEXT",
        "ALTER TABLE packing_slips ADD COLUMN weight_lbs REAL DEFAULT 0",
        "ALTER TABLE packing_slips ADD COLUMN status TEXT DEFAULT 'draft'",
        "ALTER TABLE packing_slips ADD COLUMN created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP",
        "ALTER TABLE ps_items ADD COLUMN part_number TEXT",
        "ALTER TABLE ps_items ADD COLUMN description TEXT",
        "ALTER TABLE ps_items ADD COLUMN serial_number TEXT",
        "ALTER TABLE ps_items ADD COLUMN quantity REAL DEFAULT 1",
        "ALTER TABLE ps_items ADD COLUMN country_of_origin TEXT DEFAULT 'USA'",
        "ALTER TABLE ps_items ADD COLUMN hs_code TEXT",
        "ALTER TABLE repair_orders ADD COLUMN vendor_name TEXT",
        "ALTER TABLE repair_orders ADD COLUMN vendor_address TEXT",
        "ALTER TABLE repair_orders ADD COLUMN ship_to_name TEXT DEFAULT 'Eastern Aero Pty Ltd'",
        "ALTER TABLE repair_orders ADD COLUMN ship_to_address TEXT",
        "ALTER TABLE repair_orders ADD COLUMN date TEXT",
        "ALTER TABLE repair_orders ADD COLUMN ship_date TEXT",
        "ALTER TABLE repair_orders ADD COLUMN terms TEXT DEFAULT 'Net 30'",
        "ALTER TABLE repair_orders ADD COLUMN ship_via TEXT",
        "ALTER TABLE repair_orders ADD COLUMN shipping_account TEXT",
        "ALTER TABLE repair_orders ADD COLUMN subtotal REAL DEFAULT 0",
        "ALTER TABLE repair_orders ADD COLUMN shipping REAL DEFAULT 0",
        "ALTER TABLE repair_orders ADD COLUMN tax_rate REAL DEFAULT 0",
        "ALTER TABLE repair_orders ADD COLUMN sales_tax REAL DEFAULT 0",
        "ALTER TABLE repair_orders ADD COLUMN grand_total REAL DEFAULT 0",
        "ALTER TABLE repair_orders ADD COLUMN notes TEXT",
        "ALTER TABLE repair_orders ADD COLUMN status TEXT DEFAULT 'draft'",
        "ALTER TABLE repair_orders ADD COLUMN created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP",
        "ALTER TABLE ro_items ADD COLUMN part_number TEXT",
        "ALTER TABLE ro_items ADD COLUMN description TEXT",
        "ALTER TABLE ro_items ADD COLUMN serial_number TEXT",
        "ALTER TABLE ro_items ADD COLUMN quantity REAL DEFAULT 1",
        "ALTER TABLE ro_items ADD COLUMN work_requested TEXT",
        "ALTER TABLE ro_items ADD COLUMN avg_cost REAL DEFAULT 0",
        "ALTER TABLE ro_items ADD COLUMN total_price REAL DEFAULT 0",
        "ALTER TABLE invoice_attachments ADD COLUMN label TEXT DEFAULT 'Customer PO'",
        "ALTER TABLE invoice_attachments ADD COLUMN filename TEXT",
        "ALTER TABLE invoice_attachments ADD COLUMN filepath TEXT",
        "ALTER TABLE invoice_attachments ADD COLUMN mimetype TEXT",
        "ALTER TABLE invoice_attachments ADD COLUMN uploaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP",
        # customers table column migrations
        "ALTER TABLE customers ADD COLUMN phone TEXT",
        "ALTER TABLE customers ADD COLUMN fax TEXT",
        "ALTER TABLE customers ADD COLUMN email TEXT",
        "ALTER TABLE customers ADD COLUMN payment_method TEXT DEFAULT 'Check'",
        "ALTER TABLE customers ADD COLUMN payment_terms TEXT DEFAULT 'COD'",
        "ALTER TABLE customers ADD COLUMN credit_limit REAL DEFAULT 0",
        "ALTER TABLE customers ADD COLUMN balance REAL DEFAULT 0",
        "ALTER TABLE customers ADD COLUMN hourly_rate REAL DEFAULT 100",
        "ALTER TABLE customers ADD COLUMN tax_id TEXT",
        "ALTER TABLE customers ADD COLUMN tax_percent REAL DEFAULT 0",
        "ALTER TABLE customers ADD COLUMN vat_number TEXT",
        "ALTER TABLE customers ADD COLUMN date_format TEXT DEFAULT 'mm-yyyy'",
        "ALTER TABLE customers ADD COLUMN sales_person TEXT",
        "ALTER TABLE customers ADD COLUMN purchasing_person TEXT",
        "ALTER TABLE customers ADD COLUMN customer_service_rep TEXT",
        "ALTER TABLE customers ADD COLUMN shipping_service TEXT",
        "ALTER TABLE customers ADD COLUMN status TEXT DEFAULT 'Active'",
        "ALTER TABLE customers ADD COLUMN required_part_categories TEXT",
        "ALTER TABLE customers ADD COLUMN currency TEXT DEFAULT 'USD'",
        "ALTER TABLE customers ADD COLUMN tags TEXT",
        "ALTER TABLE customers ADD COLUMN statement_notes TEXT",
        "ALTER TABLE customers ADD COLUMN invoice_notes TEXT",
        "ALTER TABLE customers ADD COLUMN notes TEXT",
        "ALTER TABLE customers ADD COLUMN related_vendor_id INTEGER",
        "ALTER TABLE customers ADD COLUMN billing_name TEXT",
        "ALTER TABLE customers ADD COLUMN billing_address TEXT",
        "ALTER TABLE customers ADD COLUMN billing_city TEXT",
        "ALTER TABLE customers ADD COLUMN billing_state TEXT",
        "ALTER TABLE customers ADD COLUMN billing_zip TEXT",
        "ALTER TABLE customers ADD COLUMN billing_country TEXT DEFAULT 'USA'",
        "ALTER TABLE customers ADD COLUMN shipping_name TEXT",
        "ALTER TABLE customers ADD COLUMN shipping_address TEXT",
        "ALTER TABLE customers ADD COLUMN shipping_city TEXT",
        "ALTER TABLE customers ADD COLUMN shipping_state TEXT",
        "ALTER TABLE customers ADD COLUMN shipping_zip TEXT",
        "ALTER TABLE customers ADD COLUMN shipping_country TEXT DEFAULT 'USA'",
        "ALTER TABLE customers ADD COLUMN created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP",
        # contacts table column migrations
        "ALTER TABLE contacts ADD COLUMN entity_type TEXT",
        "ALTER TABLE contacts ADD COLUMN entity_id INTEGER",
        "ALTER TABLE contacts ADD COLUMN first_name TEXT",
        "ALTER TABLE contacts ADD COLUMN last_name TEXT",
        "ALTER TABLE contacts ADD COLUMN title TEXT",
        "ALTER TABLE contacts ADD COLUMN email TEXT",
        "ALTER TABLE contacts ADD COLUMN phone TEXT",
        "ALTER TABLE contacts ADD COLUMN mobile TEXT",
        "ALTER TABLE contacts ADD COLUMN is_primary INTEGER DEFAULT 0",
        "ALTER TABLE contacts ADD COLUMN notes TEXT",
        "ALTER TABLE contacts ADD COLUMN created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP",
        # Inventory extra fields from stockline CSV
        "ALTER TABLE inventory ADD COLUMN manufacturer TEXT",
        "ALTER TABLE inventory ADD COLUMN alt_part_number TEXT",
        "ALTER TABLE inventory ADD COLUMN serial_number TEXT",
        "ALTER TABLE inventory ADD COLUMN item_group TEXT",
        "ALTER TABLE inventory ADD COLUMN qty_reserved INTEGER DEFAULT 0",
        "ALTER TABLE inventory ADD COLUMN qty_avail INTEGER DEFAULT 0",
        "ALTER TABLE inventory ADD COLUMN traceable_to TEXT",
        "ALTER TABLE inventory ADD COLUMN tag_type TEXT",
        "ALTER TABLE inventory ADD COLUMN cert_number TEXT",
        "ALTER TABLE inventory ADD COLUMN rec_date TEXT",
        "ALTER TABLE inventory ADD COLUMN exp_date TEXT",
        "ALTER TABLE inventory ADD COLUMN sl_number TEXT",
    ]
    migrations = migrations + erp_migrations

    for sql in migrations:
        try:
            conn.execute(sql)
        except Exception:
            pass  # Column/table already exists

    conn.commit()
    conn.close()


def get_settings():
    conn = get_db()
    rows = conn.execute('SELECT key, value FROM settings').fetchall()
    conn.close()
    return {r['key']: r['value'] for r in rows}


# ─── Forwarded Email Parser ──────────────────────────────────────────────────

def extract_forwarded_content(body):
    """
    Detect a forwarded email and extract everything after the divider line as the
    original email. Treats it identically to a directly received email:
    - Extracts original sender name + email from the forwarded headers
    - Extracts original subject, date if present
    - Returns the full original body (everything after the header block)
      so it can be parsed for parts, signature, customer info etc.

    Returns (original_name, original_email, original_body) or (None, None, body)
    """
    # All common forwarded message divider patterns
    FWD_MARKERS = re.compile(
        r'(-{3,}\s*(?:Forwarded [Mm]essage|Original [Mm]essage|Forwarded by)[^-]*-{3,}'
        r'|Begin forwarded message\s*:)',
        re.I
    )

    m = FWD_MARKERS.search(body)
    if not m:
        return None, None, body  # Not a forwarded email

    # Everything after the divider line, strip leading whitespace/newlines
    after_divider = body[m.end():].lstrip('\r\n')

    # ── Parse the forwarded header block ─────────────────────────────────────
    # Header lines look like:  "From: Name <email>"  "Date: ..."  "Subject: ..."  "To: ..."
    # They end at the first blank line after at least one header was found
    HEADER_LINE = re.compile(
        r'^[ \t]*(From|Date|Subject|To|Cc|Reply-To)\s*:\s*(.+)$', re.I | re.MULTILINE)

    orig_name  = ''
    orig_email = ''
    orig_subj  = ''

    lines = after_divider.splitlines(keepends=True)
    found_header = False
    pos = 0
    for line in lines:
        stripped = line.strip()
        if not stripped:
            if found_header:
                # Blank line after headers = end of header block
                pos += len(line)
                break
            else:
                # Leading blank line before headers — skip
                pos += len(line)
                continue
        hm = HEADER_LINE.match(line)
        if hm:
            found_header = True
            key, val = hm.group(1).lower(), hm.group(2).strip()
            if key == 'from':
                em = re.search(r'[\w.+\-]+@[\w\-]+\.[a-zA-Z]+', val)
                if em:
                    orig_email = em.group()
                name_part = re.sub(r'<[^>]+>', '', val).strip().strip('"').strip("'")
                if name_part and name_part.lower() != orig_email.lower():
                    orig_name = name_part
            elif key == 'subject':
                orig_subj = val
        pos += len(line)

    # Original body = everything after the blank line that ends the header block
    orig_body = after_divider[pos:].strip()

    # If no blank-line boundary found, take everything after divider as body
    if not orig_body:
        orig_body = after_divider.strip()

    return orig_name, orig_email, orig_body


# ─── Helpers ─────────────────────────────────────────────────────────────────

def gen_rfq_number():
    import random
    conn = get_db()
    while True:
        candidate = f"RFQ-{datetime.now().strftime('%Y%m%d%H%M%S')}-{random.randint(1000,9999)}"
        exists = conn.execute('SELECT 1 FROM rfqs WHERE rfq_number=?', (candidate,)).fetchone()
        if not exists:
            conn.close()
            return candidate


def get_quote_prefix(quote_id, conn):
    """
    Determine quote number prefix based on price types of all line items:
      QEX — all items are Exchange
      QOR — all items are Outright
      QTE — mixed, or any other combination
    """
    items = conn.execute(
        'SELECT price_type FROM quote_items WHERE quote_id=?', (quote_id,)
    ).fetchall()
    types = set((it['price_type'] or 'Outright').strip() for it in items)
    if types == {'Exchange'}:
        return 'QEX'
    if types == {'Outright'}:
        return 'QOR'
    return 'QTE'


def gen_quote_number(prefix='QTE'):
    conn = get_db()
    n = conn.execute('SELECT COUNT(*) FROM quotes').fetchone()[0]
    conn.close()
    return f"{prefix}-{datetime.now().strftime('%Y%m%d')}-{n+1:04d}"


def refresh_quote_number(quote_id, conn):
    """
    After items are edited, recalculate and persist the correct prefix on the quote number.
    Keeps the date+sequence suffix intact, only swaps the prefix.
    """
    prefix = get_quote_prefix(quote_id, conn)
    row = conn.execute('SELECT quote_number FROM quotes WHERE id=?', (quote_id,)).fetchone()
    if not row:
        return
    old = row['quote_number'] or ''
    # Replace only the prefix (first segment before the first dash+date)
    parts = old.split('-', 1)
    new_number = f"{prefix}-{parts[1]}" if len(parts) == 2 else old
    if new_number != old:
        conn.execute('UPDATE quotes SET quote_number=? WHERE id=?', (new_number, quote_id))


def parse_rfq_text(text):
    """
    Extract part numbers, descriptions, and quantities from RFQ email body.
    Handles:
      - Inline patterns: P/N: XXX  QTY: N
      - Table formats with header row (S/N | Description | Part Number | Qty | Unit)
      - Various delimiters: tab, pipe, comma, multiple spaces
    """
    items = []
    seen  = set()

    # ── Strip email signature and quoted reply content ────────────────────────
    # Email signatures start with "-- " (RFC 3676) or "--\n" on a line by itself,
    # or with common sign-off words. Quoted replies start with "> " etc.
    SIG_SEP = re.compile(
        r'(?m)'
        r'(?:^--\s*$'                                              # RFC sig delimiter
        r'|^-{3,}\s*$'                                            # --- alone on line
        r'|^_{3,}\s*$'                                            # ___ alone on line
        r'|^-{3,}\s*(?:original\s+message|forwarded\s+message)'   # Outlook separators
        r'|^On .{5,120} wrote:\s*$'                               # Gmail/Outlook thread
        r'|^>+\s)',                                               # quoted lines
        re.I
    )
    m_sep = SIG_SEP.search(text)
    if m_sep:
        text = text[:m_sep.start()]

    lines = [l.rstrip() for l in text.split('\n')]

    # Words that are definitely NOT part numbers
    SKIP_WORDS = {
        'GREETINGS', 'DEAR', 'HELLO', 'HI', 'REGARDS', 'THANKS', 'THANK',
        'SINCERELY', 'BEST', 'NUMBER', 'DESCRIPTION', 'QUANTITY', 'CONDITION',
        'UNIT', 'PART', 'ITEM', 'SN', 'SNO', 'SER', 'SERIAL', 'NOTE', 'NOTES',
        'PLEASE', 'FIND', 'ATTACHED', 'BELOW', 'ABOVE', 'FOLLOWING', 'REQUEST',
        'QUOTE', 'QUOTATION', 'RFQ', 'ORDER', 'PURCHASE', 'PROCUREMENT',
        'FORWARD', 'FWD', 'RE', 'FROM', 'SUBJECT', 'DATE', 'TO', 'CC',
    }

    # Regex patterns for inline key:value format
    PN_PAT   = re.compile(r'(?:P/?N|PART\s*(?:NO\.?|NUMBER|#)|PN\b)[:\s]+([A-Z0-9][A-Z0-9\-/\.]{2,24})', re.I)
    QTY_PAT  = re.compile(r'(?:QTY|QUANTITY|Q\'?TY|QUAN)[:\s]+(\d+)', re.I)
    DESC_PAT = re.compile(r'(?:DESC(?:RIPTION)?|NOM)[:\s]+([^\n\r,|]{3,60})', re.I)
    COND_PAT = re.compile(r'(?:COND(?:ITION)?)[:\s]+([A-Z]{1,4})', re.I)

    # Pattern for a real part number: alphanumeric with dashes, at least one digit
    VALID_PN = re.compile(r'^[A-Z0-9][A-Z0-9\-/\.]{1,24}$')

    def is_valid_pn(s):
        s = s.upper().strip()
        if s in SKIP_WORDS:
            return False
        if not VALID_PN.match(s):
            return False
        if not re.search(r'\d', s):  # Must contain at least one digit
            return False
        return True

    def add(pn, desc='', qty=1, cond='SV'):
        pn = pn.upper().strip()
        if pn and pn not in seen and is_valid_pn(pn):
            seen.add(pn)
            items.append({'part_number': pn, 'description': desc.strip().title(),
                          'quantity': qty, 'condition': cond.upper()})

    # ── Step 1: Try to detect a table with a header row ──────────────────────
    HEADER_KEYWORDS = re.compile(
        r'(?:part[\s_]?n(?:o|umber|r)?\.?|p/?n|description|desc|qty|quantity|s/?n\.?)',
        re.I)

    # Pattern that means this line is an inline key:value, NOT a table header
    # e.g. "PN: HC-B3TN-3D" or "Part No: 12345" — value is on the same line
    INLINE_KV = re.compile(
        r'(?:P/?N|PART\s*(?:NO\.?|NUMBER|#))[:\s]+[A-Z0-9]', re.I)

    header_idx = None
    col_pn = col_desc = col_qty = col_cond = None

    for i, line in enumerate(lines):
        # Skip lines that are clearly inline key:value (not a header row)
        if INLINE_KV.search(line):
            continue
        if HEADER_KEYWORDS.search(line):
            # Try to split this header row
            for delim in ('\t', '|', ','):
                if delim in line:
                    cols = [c.strip().upper() for c in line.split(delim)]
                    break
            else:
                # Try multiple-space split
                cols = re.split(r'\s{2,}', line.strip().upper())

            if len(cols) >= 2:
                for ci, col in enumerate(cols):
                    col_clean = re.sub(r'[^A-Z/]', '', col)
                    if re.search(r'P/?N|PART.*N|PN', col_clean):
                        col_pn = ci
                    elif re.search(r'DESC', col_clean):
                        col_desc = ci
                    elif re.search(r'QTY|QUAN', col_clean):
                        col_qty = ci
                    elif re.search(r'COND', col_clean):
                        col_cond = ci

                if col_pn is not None:
                    header_idx = i
                    break

    if header_idx is not None:
        # Parse rows after the header
        for line in lines[header_idx + 1:]:
            if not line.strip():
                continue
            # Split same way as header
            for delim in ('\t', '|', ','):
                if delim in line:
                    cols = [c.strip() for c in line.split(delim)]
                    break
            else:
                cols = re.split(r'\s{2,}', line.strip())

            if len(cols) <= col_pn:
                continue
            pn   = cols[col_pn].strip().upper()
            desc = cols[col_desc].strip() if col_desc is not None and col_desc < len(cols) else ''
            qty  = 1
            cond = 'SV'
            if col_qty is not None and col_qty < len(cols):
                try: qty = int(re.search(r'\d+', cols[col_qty]).group())
                except: pass
            if col_cond is not None and col_cond < len(cols):
                cond = cols[col_cond].strip().upper() or 'SV'
            add(pn, desc, qty, cond)
        if items:
            return items  # Successfully parsed table — don't fall through

    # ── Step 2: Bullet-list format: (QTY) DESCRIPTION - PART_NUMBER ─────────
    # Matches lines like:
    #   • (20) SNAP VENT - CC3251F
    #   (10) VISTA VENT - 13-03500
    #   - (06) BETA SWITCH - C6NE1047-1
    # The separator between description and PN is " - " (space-dash-space).
    # The PN itself may contain dashes (e.g. 13-03500, C6NE1047-1).
    BULLET_LINE = re.compile(
        r'^[\s\u2022\-\*]*'   # optional bullet / dash / whitespace prefix
        r'\((\d+)\)'          # (QTY)
        r'\s+'
        r'(.+?)'              # description (non-greedy)
        r'\s+-\s+'            # " - " separator
        r'([A-Z0-9][A-Z0-9\-/\.]{1,29})'  # part number (may contain dashes)
        r'\s*$',
        re.I
    )
    bullet_items = []
    for line in lines:
        m = BULLET_LINE.match(line.strip())
        if not m:
            continue
        qty_raw, desc_raw, pn_raw = m.group(1), m.group(2), m.group(3)
        pn = pn_raw.strip().upper()
        if not is_valid_pn(pn):
            continue
        try:
            qty = int(qty_raw)
        except ValueError:
            qty = 1
        bullet_items.append({'part_number': pn,
                              'description': desc_raw.strip().title(),
                              'quantity': qty,
                              'condition': 'SV'})
    if bullet_items:
        # De-duplicate against seen set and return
        for it in bullet_items:
            if it['part_number'] not in seen:
                seen.add(it['part_number'])
                items.append(it)
        if items:
            return items

    # ── Step 3: Vertical / one-value-per-line table format ───────────────────
    # Handles emails where each column header AND each value is on its own line:
    #   S/N.          Description      Part Number    Qty    Unit
    #   1             BRACKET-400      C6E1065-3      3      EA
    # (each word on a separate line, columns cycling through)
    VERTICAL_HEADERS = {
        'S/N': 'sn', 'S/N.': 'sn', 'SNO': 'sn', 'SN': 'sn', 'NO': 'sn', 'NO.': 'sn',
        'DESCRIPTION': 'desc', 'DESC': 'desc',
        'PART NUMBER': 'pn', 'PART NO': 'pn', 'PARTNO': 'pn', 'PART NO.': 'pn',
        'P/N': 'pn', 'PN': 'pn',
        'QTY': 'qty', 'QUANTITY': 'qty',
        'UNIT': 'unit', 'UOM': 'unit',
        'CONDITION': 'cond', 'COND': 'cond',
    }
    stripped_lines = [l.strip() for l in lines if l.strip()]
    col_order = []
    header_end = None
    for i, sl in enumerate(stripped_lines):
        key = sl.upper().rstrip('.')
        if key in VERTICAL_HEADERS:
            col_order.append(VERTICAL_HEADERS[key])
        else:
            if len(col_order) >= 3 and 'pn' in col_order:
                header_end = i
                break
            else:
                col_order = []  # reset if interrupted before completing

    if header_end and col_order and 'pn' in col_order:
        data_lines = stripped_lines[header_end:]
        num_cols = len(col_order)
        # Group data lines into rows of num_cols
        for row_start in range(0, len(data_lines) - num_cols + 1, num_cols):
            row = data_lines[row_start:row_start + num_cols]
            if len(row) < num_cols:
                break
            row_dict = dict(zip(col_order, row))
            pn = row_dict.get('pn', '').upper().strip()
            desc = row_dict.get('desc', '').strip()
            qty = 1
            cond = 'SV'
            try: qty = int(re.search(r'\d+', row_dict.get('qty', '1')).group())
            except: pass
            cond_val = row_dict.get('cond', 'SV').strip().upper()
            if cond_val: cond = cond_val
            add(pn, desc, qty, cond)
        if items:
            return items

    # ── Step 3: PartsBase / structured block format ──────────────────────────
    # Handles: "Part No: 23048-004M  Alt Part No:  Description:STARTER GENERATOR"
    #          "Condition: OH  Quantity: 1  Currency: dollars"
    # Search the entire text as one block for all Part No occurrences
    BLOCK_PN   = re.compile(r'Part\s*No[:\s]+([A-Z0-9][A-Z0-9\-/\.]{2,24})', re.I)
    BLOCK_DESC = re.compile(r'Description[:\s]+([A-Z0-9][^\n\r:]{2,60}?)(?:\s{2,}|\t|\s*(?:Quantity|Condition|Part|Alt)\b|$)', re.I)
    BLOCK_QTY  = re.compile(r'Quantity[:\s]+(\d+)', re.I)
    BLOCK_COND = re.compile(r'Condition[:\s]+([A-Z]{1,4})', re.I)

    block_pns = list(BLOCK_PN.finditer(text))
    if block_pns:
        for m in block_pns:
            pn = m.group(1).strip().upper()
            if not is_valid_pn(pn):
                continue
            # Search a window of text around the part number match for other fields
            start = max(0, m.start() - 50)
            end   = min(len(text), m.end() + 400)
            window = text[start:end]
            desc  = ''
            qty   = 1
            cond  = 'SV'
            md = BLOCK_DESC.search(window)
            if md: desc = md.group(1).strip()
            mq = BLOCK_QTY.search(window)
            if mq:
                try: qty = int(mq.group(1))
                except: pass
            mc = BLOCK_COND.search(window)
            if mc: cond = mc.group(1).strip().upper()
            add(pn, desc, qty, cond)
        if items:
            return items

    # ── Step 3: Fall back to inline key:value scanning ───────────────────────
    for i, line in enumerate(lines):
        lu = line.upper().strip()
        if not lu or any(skip in lu.split() for skip in SKIP_WORDS):
            continue

        m_pn = PN_PAT.search(line)
        if m_pn:
            pn   = m_pn.group(1).strip().upper()
            qty  = 1
            desc = ''
            cond = 'SV'
            # Extract condition from COND: label OR from parentheses on the same
            # line after the PN, e.g. "PN: HC-B3TN-3D        (OH)"
            m_c = COND_PAT.search(line)
            if m_c:
                cond = m_c.group(1).upper()
            else:
                # Look for (XX) parenthetical condition code after the PN match
                rest = line[m_pn.end():]
                m_paren = re.search(r'\(\s*([A-Z]{1,4})\s*\)', rest, re.I)
                if m_paren:
                    cond = m_paren.group(1).upper()
            m_q = QTY_PAT.search(line)
            if not m_q and i + 1 < len(lines):
                m_q = QTY_PAT.search(lines[i + 1])
            if m_q:
                try: qty = int(m_q.group(1))
                except: pass
            m_d = DESC_PAT.search(line)
            if m_d: desc = m_d.group(1)
            add(pn, desc, qty, cond)
            continue

        # Delimited row fallback: first column must be a valid PN
        for delim in ('\t', '|', ','):
            if delim in line:
                cols = [c.strip() for c in line.split(delim)]
                if len(cols) >= 2 and is_valid_pn(cols[0]):
                    desc = cols[1] if len(cols) > 1 else ''
                    qty  = 1
                    cond = 'SV'
                    if len(cols) > 2:
                        try: qty = int(re.search(r'\d+', cols[2]).group())
                        except: pass
                    if len(cols) > 3:
                        cond = cols[3].strip().upper() or 'SV'
                    add(cols[0].upper(), desc, qty, cond)
                break

    return items


def match_inventory(part_number, conn):
    """
    Return the best inventory match for a part number using a tiered strategy:
      1. Exact match (case-insensitive, ignoring spaces/dashes/slashes)
      2. Inventory PN contains the queried PN as a substring
      3. Queried PN contains an inventory PN as a substring
      4. Token-overlap: if the cleaned tokens of both PNs share the same core token

    Returns the matched inventory row (sqlite3.Row) or None.
    Also returns a 'match_confidence' key if you access result['_confidence'].
    """
    if not part_number:
        return None

    clean = re.sub(r'[\s\-/\.]', '', part_number.upper())

    # Tier 1: exact stripped match
    row = conn.execute(
        "SELECT * FROM inventory WHERE UPPER(REPLACE(REPLACE(REPLACE(REPLACE(part_number,' ',''),'-',''),'/',''),'.',''))=?",
        (clean,)
    ).fetchone()
    if row:
        return row

    # Tier 2: inventory PN contains queried PN (e.g. "HC-B3TN-3D" in "HC-B3TN-3D-MOD")
    row = conn.execute(
        "SELECT * FROM inventory WHERE UPPER(part_number) LIKE ?",
        (f'%{part_number.upper()}%',)
    ).fetchone()
    if row:
        return row

    # Tier 3: queried PN contains an inventory PN (e.g. longer queried than stored)
    # Fetch all and check in Python (only if inventory is reasonably small)
    rows = conn.execute(
        "SELECT * FROM inventory WHERE length(part_number) >= 4"
    ).fetchall()
    for r in rows:
        inv_clean = re.sub(r'[\s\-/\.]', '', (r['part_number'] or '').upper())
        if inv_clean and len(inv_clean) >= 4 and inv_clean in clean:
            return r

    # Tier 4: token overlap — split both PNs on non-alphanumeric boundaries
    # and check if the longest shared token is >= 5 chars
    q_tokens = set(re.split(r'[^A-Z0-9]', part_number.upper()))
    q_tokens = {t for t in q_tokens if len(t) >= 5}
    if q_tokens:
        for r in rows:
            inv_tokens = set(re.split(r'[^A-Z0-9]', (r['part_number'] or '').upper()))
            inv_tokens = {t for t in inv_tokens if len(t) >= 5}
            if q_tokens & inv_tokens:   # non-empty intersection
                return r

    return None


# ─── Routes: Dashboard ───────────────────────────────────────────────────────

# ─── Auth Routes ─────────────────────────────────────────────────────────────

@app.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        conn = get_db()
        row  = conn.execute('SELECT * FROM users WHERE username=? AND active=1', (username,)).fetchone()
        conn.close()
        if row and check_password_hash(row['password_hash'], password):
            user = User(row['id'], row['username'], row['email'], row['role'])
            login_user(user, remember=request.form.get('remember') == 'on')
            return redirect(request.args.get('next') or url_for('dashboard'))
        flash('Invalid username or password.', 'error')
    return render_template('login.html')


@app.route('/logout')
@login_required
def logout():
    logout_user()
    flash('You have been logged out.', 'success')
    return redirect(url_for('login'))


# ─── User Management (admin only) ────────────────────────────────────────────

@app.route('/users')
@login_required
@admin_required
def user_list():
    conn  = get_db()
    users = conn.execute('SELECT * FROM users ORDER BY created_at DESC').fetchall()
    conn.close()
    return render_template('users.html', users=users)


@app.route('/users/add', methods=['POST'])
@login_required
@admin_required
def user_add():
    username = request.form.get('username', '').strip()
    email_   = request.form.get('email', '').strip()
    password = request.form.get('password', '')
    role     = request.form.get('role', 'staff')
    if not username or not password:
        flash('Username and password are required.', 'error')
        return redirect(url_for('user_list'))
    try:
        conn = get_db()
        conn.execute(
            'INSERT INTO users (username, email, password_hash, role) VALUES (?,?,?,?)',
            (username, email_, generate_password_hash(password), role))
        conn.commit()
        conn.close()
        flash(f'User "{username}" created.', 'success')
    except Exception:
        flash(f'Username "{username}" already exists.', 'error')
    return redirect(url_for('user_list'))


@app.route('/users/<int:uid>/toggle', methods=['POST'])
@login_required
@admin_required
def user_toggle(uid):
    if uid == current_user.id:
        flash('You cannot deactivate your own account.', 'error')
        return redirect(url_for('user_list'))
    conn = get_db()
    conn.execute('UPDATE users SET active = 1 - active WHERE id=?', (uid,))
    conn.commit()
    conn.close()
    flash('User status updated.', 'success')
    return redirect(url_for('user_list'))


@app.route('/users/<int:uid>/reset-password', methods=['POST'])
@login_required
@admin_required
def user_reset_password(uid):
    new_pw = request.form.get('password', '')
    if not new_pw:
        flash('Password cannot be empty.', 'error')
        return redirect(url_for('user_list'))
    conn = get_db()
    conn.execute('UPDATE users SET password_hash=? WHERE id=?', (generate_password_hash(new_pw), uid))
    conn.commit()
    conn.close()
    flash('Password updated.', 'success')
    return redirect(url_for('user_list'))


@app.route('/account', methods=['GET', 'POST'])
@login_required
def account():
    if request.method == 'POST':
        old_pw  = request.form.get('old_password', '')
        new_pw  = request.form.get('new_password', '')
        conn    = get_db()
        row     = conn.execute('SELECT * FROM users WHERE id=?', (current_user.id,)).fetchone()
        if not check_password_hash(row['password_hash'], old_pw):
            flash('Current password is incorrect.', 'error')
        elif len(new_pw) < 6:
            flash('New password must be at least 6 characters.', 'error')
        else:
            conn.execute('UPDATE users SET password_hash=? WHERE id=?',
                         (generate_password_hash(new_pw), current_user.id))
            conn.commit()
            flash('Password changed successfully.', 'success')
        conn.close()
        return redirect(url_for('account'))
    return render_template('account.html')


# ─── Routes: Dashboard ───────────────────────────────────────────────────────

@app.route('/')
@login_required
def dashboard():
    conn = get_db()
    stats = {
        'total_rfqs':    conn.execute("SELECT COUNT(*) FROM rfqs").fetchone()[0],
        'pending_rfqs':  conn.execute("SELECT COUNT(*) FROM rfqs WHERE status='pending'").fetchone()[0],
        'total_quotes':  conn.execute("SELECT COUNT(*) FROM quotes").fetchone()[0],
        'sent_quotes':   conn.execute("SELECT COUNT(*) FROM quotes WHERE status='sent'").fetchone()[0],
        'total_parts':   conn.execute("SELECT COUNT(*) FROM inventory").fetchone()[0],
        'inventory_val': conn.execute("SELECT COALESCE(SUM(unit_price*quantity),0) FROM inventory").fetchone()[0],
    }
    recent_rfqs = conn.execute("""
        SELECT r.*, COUNT(i.id) item_count
        FROM rfqs r LEFT JOIN rfq_items i ON r.id=i.rfq_id
        GROUP BY r.id ORDER BY r.created_at DESC LIMIT 8
    """).fetchall()
    recent_quotes = conn.execute("""
        SELECT q.*, r.customer_name, r.company
        FROM quotes q LEFT JOIN rfqs r ON q.rfq_id=r.id
        ORDER BY q.created_at DESC LIMIT 8
    """).fetchall()
    conn.close()
    return render_template('dashboard.html', stats=stats,
                           recent_rfqs=recent_rfqs, recent_quotes=recent_quotes)


# ─── Routes: Inventory ───────────────────────────────────────────────────────

@app.route('/inventory')
@login_required
def inventory():
    q = request.args.get('q', '')
    conn = get_db()
    if q:
        like = f'%{q.upper()}%'
        parts = conn.execute(
            """SELECT * FROM inventory
               WHERE UPPER(part_number)     LIKE ?
                  OR UPPER(description)     LIKE ?
                  OR UPPER(manufacturer)    LIKE ?
                  OR UPPER(alt_part_number) LIKE ?
                  OR UPPER(item_group)      LIKE ?
               ORDER BY part_number""",
            (like, like, like, like, like)
        ).fetchall()
    else:
        parts = conn.execute("SELECT * FROM inventory ORDER BY part_number").fetchall()
    total = conn.execute("SELECT COUNT(*) FROM inventory").fetchone()[0]
    conn.close()
    return render_template('inventory.html', parts=parts, total=total, q=q)


@app.route('/inventory/upload', methods=['POST'])
@login_required
def upload_inventory():
    if 'file' not in request.files or not request.files['file'].filename:
        flash('No file selected.', 'error')
        return redirect(url_for('inventory'))

    f = request.files['file']
    path = os.path.join(UPLOAD_FOLDER, f.filename)
    f.save(path)

    try:
        df = pd.read_csv(path) if f.filename.lower().endswith('.csv') else pd.read_excel(path)
        # Keep original column names for display; work with uppercased copy for detection
        orig_cols = list(df.columns)
        upper_cols = [c.strip().upper() for c in orig_cols]
        df.columns = upper_cols  # replace with uppercased names

        def detect_col(candidates, exclude_patterns=None):
            """Return first column name whose uppercased form contains any candidate string.
               exclude_patterns: list of substrings that would DISQUALIFY a column if present."""
            for col in upper_cols:
                if exclude_patterns and any(ex in col for ex in exclude_patterns):
                    continue
                if any(cand in col for cand in candidates):
                    return col
            return None

        COL = {}
        # Part number: "PN" but NOT "PN DESCRIPTION" or "ALT PN"
        COL['part_number'] = detect_col(['PART NO', 'PART NUM', 'PARTNO', 'P/N'],
                                         exclude_patterns=['DESC', 'ALT', 'REVISED']) \
                          or detect_col(['^PN$', 'PN '],
                                         exclude_patterns=['DESC', 'ALT', 'REVISED'])
        # Fallback: find exact "PN" column
        if not COL['part_number']:
            for col in upper_cols:
                if col.strip() == 'PN':
                    COL['part_number'] = col
                    break

        # Description: must contain DESC, prefer "PN DESCRIPTION" or "DESCRIPTION"
        COL['description'] = detect_col(['PN DESCRIPTION', 'PART DESCRIPTION', 'DESCRIPTION', ' DESC'])
        if not COL['description']:
            COL['description'] = detect_col(['DESC'])

        # Alt Part Number
        COL['alt_pn'] = detect_col(['ALT PN', 'ALT PART', 'ALTERNATE PN', 'ALTERNATE PART',
                                     'REVISED PN', 'REVISED PART'])

        # Condition
        COL['condition'] = detect_col(['COND'])

        # Quantity on hand (prefer "QTY OH", "QTY ON HAND", "QTY AVAIL", then generic QTY)
        COL['quantity'] = (detect_col(['QTY OH', 'QTY ON HAND', 'ON HAND', 'ONHAND', 'QTY AVAIL'])
                        or detect_col(['QTY', 'QUANTITY', 'STOCK']))

        # Reserved qty
        COL['qty_reserved'] = detect_col(['QTY RESERVED', 'RESERVED'])

        # Available qty
        COL['qty_avail'] = detect_col(['QTY AVAIL', 'AVAIL'])

        # Unit cost
        COL['unit_cost'] = detect_col(['UNIT COST', 'COST'])

        # Unit price / sell price
        COL['unit_price'] = detect_col(['UNIT PRICE', 'SELL PRICE', 'LIST PRICE', 'PRICE'])

        # Location
        COL['location'] = detect_col(['LOCATION', 'LOC', 'BIN', 'SHELF', 'WAREHOUSE'])

        # UOM
        COL['uom'] = detect_col(['UOM', 'UNIT OF MEASURE', 'UNIT MEASURE'])

        # Manufacturer
        COL['manufacturer'] = detect_col(['MANUFACTURER', 'MFR', 'MFG', 'MAKE', 'VENDOR'])

        # Item group / category
        COL['item_group'] = detect_col(['ITEM GROUP', 'ITEM_GROUP', 'CATEGORY', 'GROUP'])

        # Serial number
        COL['serial_number'] = detect_col(['SER NUM', 'SERIAL NUM', 'SERIAL NO', 'SER NO', 'SERIAL'])

        # Traceability
        COL['traceable_to'] = detect_col(['TRACEABLE', 'TRACE TO', 'OBTAINED FROM'])

        # Tag info
        COL['tag_type']    = detect_col(['TAG TYPE'])
        COL['cert_number'] = detect_col(['CERT NUM', 'CERTIFIED NUM', 'CERT NO'])

        # Dates
        COL['rec_date'] = detect_col(['REC\'D DATE', 'RECEIVED DATE', 'REC DATE', 'RECV DATE'])
        COL['exp_date'] = detect_col(['EXP DATE', 'EXPIRY', 'EXPIRATION'])

        # Stockline number
        COL['sl_number'] = detect_col(['SL NUM', 'STOCKLINE NUM', 'SL NO'])

        if not COL['part_number']:
            flash('Cannot find a Part Number column. Expected column named "PN", "P/N", or "Part Number".', 'error')
            return redirect(url_for('inventory'))

        conn = get_db()
        mode = request.form.get('mode', 'merge')
        if mode == 'replace':
            conn.execute('DELETE FROM inventory')

        def gv(key, default=''):
            """Get value for a mapped column, returning default for NaN/None/empty."""
            col = COL.get(key)
            if not col:
                return default
            val = df[col].iloc[row_idx] if col in df.columns else default
            s = str(val).strip()
            return default if s.upper() in ('NAN', 'NONE', '', 'N/A') else s

        added = updated = 0
        for row_idx in range(len(df)):
            pn = str(df[COL['part_number']].iloc[row_idx]).strip().upper()
            if not pn or pn in ('NAN', 'NONE', ''):
                continue

            desc     = gv('description', '').strip().title()
            alt_pn   = gv('alt_pn', '').strip().upper()
            cond     = gv('condition', 'SV').strip().upper() or 'SV'
            loc      = gv('location', '').strip()
            uom      = gv('uom', 'EA').strip() or 'EA'
            mfr      = gv('manufacturer', '').strip().title()
            grp      = gv('item_group', '').strip().title()
            ser      = gv('serial_number', '').strip()
            trace    = gv('traceable_to', '').strip()
            tag_type = gv('tag_type', '').strip()
            cert_num = gv('cert_number', '').strip()
            rec_date = gv('rec_date', '').strip()
            exp_date = gv('exp_date', '').strip()
            sl_num   = gv('sl_number', '').strip()

            try: qty      = int(float(gv('quantity', 0)))
            except: qty   = 0
            try: qty_res  = int(float(gv('qty_reserved', 0)))
            except: qty_res = 0
            try: qty_avail = int(float(gv('qty_avail', qty)))
            except: qty_avail = qty
            try: cost  = float(gv('unit_cost', 0))
            except: cost = 0.0
            try: price = float(gv('unit_price', 0))
            except: price = 0.0

            exists = conn.execute('SELECT id FROM inventory WHERE part_number=?', (pn,)).fetchone()
            if exists:
                conn.execute('''
                    UPDATE inventory SET
                        description=?, alt_part_number=?, condition=?,
                        quantity=?, qty_reserved=?, qty_avail=?,
                        unit_cost=?, unit_price=?, location=?, uom=?,
                        manufacturer=?, item_group=?, serial_number=?,
                        traceable_to=?, tag_type=?, cert_number=?,
                        rec_date=?, exp_date=?, sl_number=?,
                        updated_at=CURRENT_TIMESTAMP
                    WHERE part_number=?''',
                    (desc, alt_pn, cond,
                     qty, qty_res, qty_avail,
                     cost, price, loc, uom,
                     mfr, grp, ser,
                     trace, tag_type, cert_num,
                     rec_date, exp_date, sl_num,
                     pn))
                updated += 1
            else:
                conn.execute('''
                    INSERT INTO inventory
                        (part_number, description, alt_part_number, condition,
                         quantity, qty_reserved, qty_avail,
                         unit_cost, unit_price, location, uom,
                         manufacturer, item_group, serial_number,
                         traceable_to, tag_type, cert_number,
                         rec_date, exp_date, sl_number)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                    (pn, desc, alt_pn, cond,
                     qty, qty_res, qty_avail,
                     cost, price, loc, uom,
                     mfr, grp, ser,
                     trace, tag_type, cert_num,
                     rec_date, exp_date, sl_num))
                added += 1

        conn.commit()
        conn.close()
        flash(f'Inventory updated — {added} added, {updated} updated.', 'success')
    except Exception as e:
        import traceback
        flash(f'Error reading file: {e}\n{traceback.format_exc()}', 'error')

    return redirect(url_for('inventory'))


@app.route('/inventory/delete/<int:pid>', methods=['POST'])
@login_required
def delete_part(pid):
    conn = get_db()
    conn.execute('DELETE FROM inventory WHERE id=?', (pid,))
    conn.commit()
    conn.close()
    flash('Part removed.', 'success')
    return redirect(url_for('inventory'))


@app.route('/inventory/part/<int:pid>')
@login_required
def part_detail(pid):
    """Detail view for a single part — shows linked RFQs, Quotes, POs, Invoices, ROs."""
    conn = get_db()
    part = conn.execute('SELECT * FROM inventory WHERE id=?', (pid,)).fetchone()
    if not part:
        flash('Part not found.', 'error')
        conn.close()
        return redirect(url_for('inventory'))

    pn = part['part_number']

    # RFQs that requested this PN
    rfqs = conn.execute("""
        SELECT r.id, r.rfq_number, r.customer_name, r.company,
               r.created_at, r.status, i.quantity, i.condition
        FROM rfq_items i
        JOIN rfqs r ON i.rfq_id = r.id
        WHERE UPPER(i.part_number) = ?
        ORDER BY r.created_at DESC
    """, (pn,)).fetchall()

    # Quotes
    quotes = conn.execute("""
        SELECT q.id, q.quote_number, r.customer_name, r.company,
               q.created_at, q.status,
               i.quantity_requested, i.unit_price, i.condition
        FROM quote_items i
        JOIN quotes q ON i.quote_id = q.id
        LEFT JOIN rfqs r ON q.rfq_id = r.id
        WHERE UPPER(i.part_number) = ?
        ORDER BY q.created_at DESC
    """, (pn,)).fetchall()

    # Purchase Orders
    pos = conn.execute("""
        SELECT p.id, p.po_number, p.vendor_name, p.created_at, p.status,
               i.quantity, i.unit_price, i.condition, i.serial_number
        FROM po_items i
        JOIN purchase_orders p ON i.po_id = p.id
        WHERE UPPER(i.part_number) = ?
        ORDER BY p.created_at DESC
    """, (pn,)).fetchall()

    # Invoices
    invoices = conn.execute("""
        SELECT inv.id, inv.invoice_number, inv.invoice_for, inv.created_at,
               i.quantity, i.unit_price, i.condition, i.serial_number
        FROM invoice_items i
        JOIN invoices inv ON i.invoice_id = inv.id
        WHERE UPPER(i.part_number) = ?
        ORDER BY inv.created_at DESC
    """, (pn,)).fetchall()

    # Repair Orders
    ros = conn.execute("""
        SELECT r.id, r.ro_number, r.customer_name, r.created_at, r.status,
               i.description, i.quantity, i.condition
        FROM ro_items i
        JOIN repair_orders r ON i.ro_id = r.id
        WHERE UPPER(i.part_number) = ?
        ORDER BY r.created_at DESC
    """, (pn,)).fetchall()

    conn.close()
    return render_template('part_detail.html',
        part=part, rfqs=rfqs, quotes=quotes,
        pos=pos, invoices=invoices, ros=ros)


@app.route('/inventory/edit/<int:pid>', methods=['POST'])
@login_required
def edit_part(pid):
    conn = get_db()
    try:
        qty_avail = int(request.form.get('qty_avail', request.form.get('quantity', 0)))
    except:
        qty_avail = 0
    conn.execute('''
        UPDATE inventory SET
            part_number=?, alt_part_number=?, description=?, manufacturer=?,
            condition=?, quantity=?, qty_avail=?,
            unit_cost=?, unit_price=?, location=?, uom=?,
            updated_at=CURRENT_TIMESTAMP
        WHERE id=?''',
        (request.form['part_number'].strip().upper(),
         request.form.get('alt_part_number', '').strip().upper(),
         request.form.get('description', ''),
         request.form.get('manufacturer', ''),
         request.form.get('condition', 'SV').strip().upper(),
         int(request.form.get('quantity', 0)),
         qty_avail,
         float(request.form.get('unit_cost', 0)),
         float(request.form.get('unit_price', 0)),
         request.form.get('location', ''),
         request.form.get('uom', 'EA'),
         pid))
    conn.commit()
    conn.close()
    return jsonify({'success': True})


# ─── Routes: RFQs ────────────────────────────────────────────────────────────

@app.route('/rfqs')
@login_required
def rfq_list():
    status = request.args.get('status', '')
    conn = get_db()
    query = """
        SELECT r.*, COUNT(i.id) item_count
        FROM rfqs r LEFT JOIN rfq_items i ON r.id=i.rfq_id
        {where}
        GROUP BY r.id ORDER BY r.created_at DESC
    """
    if status:
        rows = conn.execute(query.format(where='WHERE r.status=?'), (status,)).fetchall()
    else:
        rows = conn.execute(query.format(where='')).fetchall()
    conn.close()
    return render_template('rfq_list.html', rfqs=rows, status_filter=status)


@app.route('/rfqs/new', methods=['GET', 'POST'])
@login_required
def rfq_new():
    if request.method == 'POST':
        conn   = get_db()
        rfq_no = gen_rfq_number()
        conn.execute(
            'INSERT INTO rfqs (rfq_number,customer_name,customer_email,company,phone,source,notes,raw_email) VALUES (?,?,?,?,?,?,?,?)',
            (rfq_no,
             request.form.get('customer_name',''),
             request.form.get('customer_email',''),
             request.form.get('company',''),
             request.form.get('phone',''),
             request.form.get('source','web'),
             request.form.get('notes',''),
             request.form.get('raw_email','')))
        rfq_id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]

        pns  = request.form.getlist('part_number[]')
        qtys = request.form.getlist('quantity[]')
        descs= request.form.getlist('description[]')
        conds= request.form.getlist('condition[]')

        added = 0
        if any(p.strip() for p in pns):
            for i, pn in enumerate(pns):
                pn = pn.strip().upper()
                if not pn: continue
                try: qty = int(qtys[i]) if i < len(qtys) else 1
                except: qty = 1
                desc = descs[i] if i < len(descs) else ''
                cond = conds[i] if i < len(conds) else 'SV'
                conn.execute(
                    'INSERT INTO rfq_items (rfq_id,part_number,description,quantity,condition) VALUES (?,?,?,?,?)',
                    (rfq_id, pn, desc, qty, cond))
                added += 1

        raw = request.form.get('raw_email', '')
        if added == 0 and raw:
            for item in parse_rfq_text(raw):
                conn.execute(
                    'INSERT INTO rfq_items (rfq_id,part_number,description,quantity,condition) VALUES (?,?,?,?,?)',
                    (rfq_id, item['part_number'], item['description'], item['quantity'], item['condition']))
                added += 1

        conn.commit()
        conn.close()
        flash(f'RFQ {rfq_no} created with {added} part(s).', 'success')
        return redirect(url_for('rfq_detail', rfq_id=rfq_id))

    return render_template('rfq_new.html')


@app.route('/rfqs/<int:rfq_id>')
@login_required
def rfq_detail(rfq_id):
    conn  = get_db()
    rfq   = conn.execute('SELECT * FROM rfqs WHERE id=?', (rfq_id,)).fetchone()
    items = conn.execute('SELECT * FROM rfq_items WHERE rfq_id=?', (rfq_id,)).fetchall()
    quotes= conn.execute('SELECT * FROM quotes WHERE rfq_id=? ORDER BY created_at DESC', (rfq_id,)).fetchall()
    settings = get_settings()

    # Pre-fill: priority 1 — most recent quote that shares a part number with this RFQ
    prev_quote = None
    if items:
        part_numbers = [it['part_number'] for it in items]
        placeholders = ','.join('?' * len(part_numbers))
        prev_quote = conn.execute(f'''
            SELECT q.* FROM quotes q
            JOIN quote_items qi ON qi.quote_id = q.id
            WHERE qi.part_number IN ({placeholders}) AND q.rfq_id != ?
            ORDER BY q.created_at DESC LIMIT 1
        ''', (*part_numbers, rfq_id)).fetchone()

    # Pre-fill: priority 2 (fallback) — most recent quote for the same customer email
    if not prev_quote and rfq and rfq['customer_email']:
        prev_quote = conn.execute('''
            SELECT q.* FROM quotes q
            JOIN rfqs r ON q.rfq_id = r.id
            WHERE r.customer_email = ? AND q.rfq_id != ?
            ORDER BY q.created_at DESC LIMIT 1
        ''', (rfq['customer_email'], rfq_id)).fetchone()

    conn.close()
    if not rfq:
        flash('RFQ not found.', 'error')
        return redirect(url_for('rfq_list'))
    return render_template('rfq_detail.html', rfq=rfq, items=items, quotes=quotes,
                           settings=settings, prev_quote=prev_quote)


@app.route('/rfqs/<int:rfq_id>/quote', methods=['POST'])
@login_required
def create_quote(rfq_id):
    conn     = get_db()
    settings = get_settings()
    rfq      = conn.execute('SELECT * FROM rfqs WHERE id=?', (rfq_id,)).fetchone()

    # ── Guard: if a quote already exists for this RFQ, go there — don't duplicate ──
    existing = conn.execute(
        'SELECT id FROM quotes WHERE rfq_id=? ORDER BY created_at DESC LIMIT 1',
        (rfq_id,)).fetchone()
    if existing:
        conn.close()
        flash('A quote already exists for this RFQ. Redirected to it.', 'info')
        return redirect(url_for('quote_view', quote_id=existing['id']))

    items    = conn.execute('SELECT * FROM rfq_items WHERE rfq_id=?', (rfq_id,)).fetchall()

    markup     = float(request.form.get('markup', settings.get('default_markup', 30)))
    valid_days = int(request.form.get('valid_days', settings.get('quote_valid_days', 30)))
    notes      = request.form.get('notes', '')
    today      = datetime.now().strftime('%Y-%m-%d')
    quote_no   = gen_quote_number()

    def fval(key, default=0):
        try: return float(request.form.get(key, default) or default)
        except: return default
    def ival(key, default=0):
        try: return int(request.form.get(key, default) or default)
        except: return default

    billing_interval = ival('billing_interval_days', 30)
    billing_start    = request.form.get('billing_start_date', today)
    fee_count        = ival('fee_billings_count', 1)
    exch_fee         = fval('exchange_loan_fee')
    periodic_amt     = exch_fee  # periodic billing = exchange fee per billing
    cogs_pct         = fval('cogs_percent')
    outright         = fval('outright_amount')
    cogs_amt         = round(outright * cogs_pct / 100, 2) if cogs_pct else fval('cogs_amount')

    conn.execute('''INSERT INTO quotes
        (quote_number, rfq_id, status, markup_percent, valid_days, notes, currency,
         exchange_loan_fee, entry_date, overhaul_price_est, core_price, outright_amount,
         days_core_return, fee_billings_count, billing_start_date, billing_interval_days,
         deposit_amount, core_due_date, schedule_billing_date, periodic_billing_amt,
         cogs_percent, cogs_amount)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
        (quote_no, rfq_id, 'draft', markup, valid_days, notes,
         request.form.get('currency', 'USD'),
         exch_fee,
         request.form.get('entry_date', today),
         fval('overhaul_price_est'),
         fval('core_price'),
         outright,
         ival('days_core_return', 30),
         fee_count,
         billing_start,
         billing_interval,
         fval('deposit_amount'),
         request.form.get('core_due_date', ''),
         request.form.get('schedule_billing_date', billing_start),
         periodic_amt,
         cogs_pct,
         cogs_amt))
    quote_id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]

    total = 0
    for item in items:
        inv = match_inventory(item['part_number'], conn)
        if inv:
            base  = inv['unit_price'] if inv['unit_price'] > 0 else inv['unit_cost']
            price = base * (1 + markup / 100) if inv['unit_price'] == 0 and base > 0 else base
            ext   = round(price * item['quantity'], 2)
            total += ext
            conn.execute(
                'INSERT INTO quote_items (quote_id,part_number,description,condition,quantity_requested,quantity_available,unit_price,extended_price,matched) VALUES (?,?,?,?,?,?,?,?,1)',
                (quote_id, item['part_number'],
                 item['description'] or inv['description'] or '',
                 inv['condition'], item['quantity'], inv['quantity'],
                 round(price, 2), ext))
        else:
            conn.execute(
                'INSERT INTO quote_items (quote_id,part_number,description,condition,quantity_requested,quantity_available,unit_price,extended_price,matched) VALUES (?,?,?,?,?,0,0,0,0)',
                (quote_id, item['part_number'], item['description'] or '', 'N/A', item['quantity']))

    conn.execute('UPDATE quotes SET total_amount=? WHERE id=?', (round(total, 2), quote_id))
    conn.execute("UPDATE rfqs SET status='quoted' WHERE id=?", (rfq_id,))
    conn.commit()
    conn.close()

    flash(f'Quote {quote_no} created!', 'success')
    return redirect(url_for('quote_view', quote_id=quote_id))


# ─── Routes: Quotes ──────────────────────────────────────────────────────────

@app.route('/quotes/new', methods=['GET', 'POST'])
@login_required
def quote_new():
    """Create a standalone quote not tied to any RFQ email."""
    settings = get_settings()
    if request.method == 'POST':
        # Create a bare RFQ-like stub so quote_view works unchanged
        conn = get_db()
        cur = conn.execute('''
            INSERT INTO rfqs (rfq_number, customer_name, customer_email, company, phone,
                              source, status, notes)
            VALUES (?,?,?,?,?,?,?,?)
        ''', (
            f"MANUAL-{datetime.now().strftime('%Y%m%d%H%M%S')}",
            request.form.get('customer_name', '').strip(),
            request.form.get('customer_email', '').strip(),
            request.form.get('company', '').strip(),
            request.form.get('phone', '').strip(),
            'manual',
            'quoted',
            request.form.get('notes', '').strip(),
        ))
        rfq_id = cur.lastrowid

        markup  = float(request.form.get('markup_percent') or settings.get('default_markup', 30))
        currency = request.form.get('currency', 'USD')
        valid_days = int(request.form.get('valid_days') or settings.get('quote_valid_days', 30))

        q_num = gen_quote_number('QTE')
        qcur = conn.execute('''
            INSERT INTO quotes (quote_number, rfq_id, status, markup_percent,
                                total_amount, notes, valid_days, currency)
            VALUES (?,?,?,?,?,?,?,?)
        ''', (q_num, rfq_id, 'draft', markup, 0, '', valid_days, currency))
        quote_id = qcur.lastrowid

        # Save line items
        pns    = request.form.getlist('pn[]')
        descs  = request.form.getlist('desc[]')
        conds  = request.form.getlist('cond[]')
        qtys   = request.form.getlist('qty[]')
        prices = request.form.getlist('price[]')
        leads  = request.form.getlist('lead[]')
        ptypes = request.form.getlist('ptype[]')

        total = 0.0
        for i, pn in enumerate(pns):
            if not pn.strip():
                continue
            qty   = int(qtys[i] or 1) if i < len(qtys) else 1
            price = float(prices[i] or 0) if i < len(prices) else 0
            ext   = round(qty * price, 2)
            total += ext
            conn.execute('''
                INSERT INTO quote_items
                  (quote_id, part_number, description, condition,
                   quantity_requested, quantity_available, unit_price, extended_price,
                   matched, lead_time, price_type, warranty)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            ''', (
                quote_id,
                pn.strip().upper(),
                descs[i].strip() if i < len(descs) else '',
                conds[i].strip() if i < len(conds) else 'SV',
                qty, 0, price, ext, 0,
                leads[i].strip() if i < len(leads) else 'Stock',
                ptypes[i].strip() if i < len(ptypes) else 'Outright',
                '3 Months',
            ))

        conn.execute('UPDATE quotes SET total_amount=? WHERE id=?', (round(total, 2), quote_id))
        conn.commit()
        conn.close()
        flash(f'Quote {q_num} created.', 'success')
        return redirect(url_for('quote_view', quote_id=quote_id))

    return render_template('quote_new.html', settings=settings)


@app.route('/quotes')
@login_required
def quote_list():
    conn = get_db()
    rows = conn.execute("""
        SELECT q.*, r.customer_name, r.company, r.customer_email
        FROM quotes q LEFT JOIN rfqs r ON q.rfq_id=r.id
        ORDER BY q.created_at DESC
    """).fetchall()
    conn.close()
    return render_template('quote_list.html', quotes=rows)


@app.route('/quotes/<int:quote_id>')
@login_required
def quote_view(quote_id):
    conn        = get_db()
    quote       = conn.execute('SELECT * FROM quotes WHERE id=?', (quote_id,)).fetchone()
    if not quote:
        conn.close()
        flash('Quote not found.', 'error')
        return redirect(url_for('quote_list'))
    rfq         = conn.execute('SELECT * FROM rfqs WHERE id=?', (quote['rfq_id'],)).fetchone() if quote['rfq_id'] else None
    items       = conn.execute('SELECT * FROM quote_items WHERE quote_id=?', (quote_id,)).fetchall()
    attachments = conn.execute('SELECT * FROM quote_attachments WHERE quote_id=? ORDER BY uploaded_at', (quote_id,)).fetchall()
    settings    = get_settings()
    conn.close()
    # For standalone quotes without an RFQ, create a minimal stub so the template works
    if rfq is None:
        import collections
        stub = collections.defaultdict(str)
        stub['id']             = 0
        stub['customer_name']  = ''
        stub['customer_email'] = ''
        stub['company']        = ''
        stub['phone']          = ''
        stub['source']         = 'manual'
        stub['raw_email']      = ''
        stub['customer_ref']   = ''
        stub['website']        = ''
        stub['address']        = ''
        rfq = stub
    return render_template('quote_view.html', quote=quote, rfq=rfq, items=items,
                           attachments=attachments, settings=settings)


@app.route('/quotes/<int:quote_id>/convert-to-invoice', methods=['POST'])
@login_required
def quote_convert_to_invoice(quote_id):
    """Create an invoice pre-populated with all data from the quote."""
    conn  = get_db()
    quote = conn.execute('SELECT * FROM quotes WHERE id=?', (quote_id,)).fetchone()
    if not quote:
        conn.close()
        flash('Quote not found.', 'error')
        return redirect(url_for('quote_list'))

    rfq = conn.execute('SELECT * FROM rfqs WHERE id=?', (quote['rfq_id'],)).fetchone() if quote['rfq_id'] else None
    items = conn.execute(
        'SELECT * FROM quote_items WHERE quote_id=? AND (no_quote IS NULL OR no_quote=0)',
        (quote_id,)
    ).fetchall()

    # Build customer info from RFQ
    customer_name    = (rfq['customer_name'] if rfq else '') or ''
    customer_company = (rfq['company']       if rfq else '') or ''
    customer_email   = (rfq['customer_email'] if rfq else '') or ''
    customer_ref     = (rfq['customer_ref']  if rfq else '') or ''

    # invoice_for = customer name / company
    invoice_for = customer_company or customer_name

    # Build customer address block
    address_parts = []
    if customer_name and customer_company:
        address_parts.append(customer_name)
    if rfq and rfq['address']:
        address_parts.append(rfq['address'])
    elif customer_email:
        address_parts.append(customer_email)
    customer_address = '\n'.join(address_parts)

    # Calculate totals
    subtotal = sum((i['extended_price'] or 0) for i in items)

    inv_number = _next_erp_number('INV', 'invoices', 'invoice_number')

    cur = conn.execute('''
        INSERT INTO invoices
          (invoice_number, invoice_for, customer_name, customer_address,
           reference, due_date, subtotal, adjustments, grand_total, notes, status)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)
    ''', (
        inv_number,
        invoice_for,
        customer_name,
        customer_address,
        customer_ref or quote['quote_number'],
        '',   # due_date — left for user to fill
        round(subtotal, 2),
        0,
        round(subtotal, 2),
        f"Converted from Quote {quote['quote_number']}",
        'draft'
    ))
    inv_id = cur.lastrowid

    # Copy line items
    for item in items:
        conn.execute('''
            INSERT INTO invoice_items
              (invoice_id, part_number, description, condition, quantity, unit_price, total_price)
            VALUES (?,?,?,?,?,?,?)
        ''', (
            inv_id,
            item['part_number'] or '',
            item['description'] or '',
            item['condition']   or '',
            item['quantity_requested'] or 1,
            item['unit_price']  or 0,
            item['extended_price'] or 0,
        ))

    conn.commit()
    conn.close()
    flash(f'Invoice {inv_number} created from Quote {quote["quote_number"]}.', 'success')
    return redirect(url_for('invoice_edit', inv_id=inv_id))


@app.route('/quotes/<int:quote_id>/update-item', methods=['POST'])
@login_required
def update_quote_item(quote_id):
    data  = request.get_json()
    iid   = data['item_id']
    no_quote = 1 if data.get('no_quote') else 0
    # When No Quote, price and extended are forced to 0
    price = 0.0 if no_quote else float(data.get('unit_price', 0))
    qty   = int(data.get('quantity_requested', 1))
    notes = data.get('notes', '')
    ext   = 0.0 if no_quote else round(price * qty, 2)

    conn = get_db()
    conn.execute('''UPDATE quote_items SET
        unit_price=?, quantity_requested=?, extended_price=?, notes=?,
        lead_time=?, price_type=?, warranty=?, trace_to=?, tag_type=?, tagged_by=?, condition=?, no_quote=?
        WHERE id=? AND quote_id=?''',
        (price, qty, ext, notes,
         data.get('lead_time', 'Stock'),
         data.get('price_type', 'Outright'),
         data.get('warranty', '3 Months'),
         data.get('trace_to', ''),
         data.get('tag_type', ''),
         data.get('tagged_by', ''),
         data.get('condition', 'SV'),
         no_quote,
         iid, quote_id))
    total = conn.execute('SELECT COALESCE(SUM(extended_price),0) FROM quote_items WHERE quote_id=?', (quote_id,)).fetchone()[0]
    conn.execute('UPDATE quotes SET total_amount=? WHERE id=?', (total, quote_id))
    refresh_quote_number(quote_id, conn)
    new_qnum = conn.execute('SELECT quote_number FROM quotes WHERE id=?', (quote_id,)).fetchone()['quote_number']
    conn.commit()
    conn.close()
    return jsonify({'success': True, 'extended': ext, 'total': total, 'quote_number': new_qnum, 'no_quote': no_quote})


@app.route('/quotes/<int:quote_id>/add-item', methods=['POST'])
@login_required
def add_quote_item(quote_id):
    """Manually add a line item to an existing quote."""
    data       = request.get_json()
    pn         = (data.get('part_number') or '').strip().upper()
    if not pn:
        return jsonify({'success': False, 'error': 'Part number required'})

    description = (data.get('description') or '').strip()
    qty         = max(1, int(data.get('quantity', 1)))
    unit_price  = round(float(data.get('unit_price', 0)), 2)
    lead_time   = (data.get('lead_time') or 'Stock').strip()
    condition   = (data.get('condition') or 'SV').strip()
    price_type  = (data.get('price_type') or 'Outright').strip()
    extended    = round(unit_price * qty, 2)

    conn = get_db()

    # Check inventory for a match
    inv = conn.execute(
        'SELECT * FROM inventory WHERE UPPER(part_number)=? LIMIT 1', (pn,)
    ).fetchone()
    matched   = inv is not None
    qty_avail = inv['quantity'] if matched else 0
    if matched and not description:
        description = inv['description'] or ''

    conn.execute('''INSERT INTO quote_items
        (quote_id, part_number, description, quantity_requested,
         unit_price, extended_price, lead_time, condition, price_type,
         quantity_available, matched)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)''',
        (quote_id, pn, description, qty,
         unit_price, extended, lead_time, condition, price_type,
         qty_avail, 1 if matched else 0))

    item_id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
    total   = conn.execute(
        'SELECT COALESCE(SUM(extended_price),0) FROM quote_items WHERE quote_id=?',
        (quote_id,)).fetchone()[0]
    conn.execute('UPDATE quotes SET total_amount=? WHERE id=?', (total, quote_id))
    refresh_quote_number(quote_id, conn)
    conn.commit()
    conn.close()

    return jsonify({
        'success': True,
        'item_id': item_id,
        'extended': extended,
        'total': total,
        'matched': matched,
        'qty_avail': qty_avail,
    })


def _compress_file(filepath, ext, mime):
    """Compress an uploaded file in-place. JPEG/GIF via Pillow, PDF via pypdf."""
    try:
        if ext in ('.jpg', '.jpeg'):
            from PIL import Image
            img = Image.open(filepath)
            # Preserve EXIF if present
            exif = img.info.get('exif', b'')
            img.save(filepath, format='JPEG', optimize=True, quality=85,
                     exif=exif if exif else b'')
        elif ext == '.gif':
            from PIL import Image
            img = Image.open(filepath)
            img.save(filepath, format='GIF', optimize=True)
        elif ext == '.pdf':
            from pypdf import PdfReader, PdfWriter
            reader = PdfReader(filepath)
            writer = PdfWriter()
            for page in reader.pages:
                page.compress_content_streams()
                writer.add_page(page)
            # Write to a temp file then replace
            tmp_path = filepath + '.tmp'
            with open(tmp_path, 'wb') as f_out:
                writer.write(f_out)
            # Only replace if compression actually made it smaller
            if os.path.getsize(tmp_path) < os.path.getsize(filepath):
                os.replace(tmp_path, filepath)
            else:
                os.remove(tmp_path)
    except Exception as e:
        print(f'[Compress] Could not compress {filepath}: {e}')


@app.route('/quotes/<int:quote_id>/attach', methods=['POST'])
@login_required
def attach_file(quote_id):
    """Upload a file attachment for a quote, with compression + PN/SN tagging."""
    f = request.files.get('file')
    if not f or not f.filename:
        return jsonify({'success': False, 'error': 'No file provided'})

    ALLOWED_EXTENSIONS = {'.pdf', '.jpg', '.jpeg', '.gif'}
    ext = os.path.splitext(f.filename)[1].lower()
    if ext not in ALLOWED_EXTENSIONS:
        return jsonify({'success': False, 'error': 'Only PDF, JPEG and GIF files are allowed'})

    # PN and SN from form data
    part_number   = (request.form.get('part_number') or '').strip().upper()
    serial_number = (request.form.get('serial_number') or '').strip().upper()

    # Sanitise filename
    safe_name = re.sub(r'[^\w\.\-]', '_', f.filename)
    upload_dir = os.path.join(UPLOAD_FOLDER, str(quote_id))
    os.makedirs(upload_dir, exist_ok=True)
    filepath = os.path.join(upload_dir, safe_name)
    f.save(filepath)

    # Compress in-place
    mime = mimetypes.guess_type(safe_name)[0] or 'application/octet-stream'
    _compress_file(filepath, ext, mime)

    conn = get_db()
    conn.execute(
        '''INSERT INTO quote_attachments
           (quote_id, filename, filepath, mimetype, part_number, serial_number, verified)
           VALUES (?,?,?,?,?,?,0)''',
        (quote_id, safe_name, filepath, mime, part_number or None, serial_number or None))
    conn.commit()
    att_id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
    conn.close()

    # Look for a matching attachment from a previous quote (PN+SN required)
    suggestion = None
    if part_number and serial_number:
        conn2 = get_db()
        prev = conn2.execute(
            '''SELECT qa.*, q.quote_number FROM quote_attachments qa
               JOIN quotes q ON q.id = qa.quote_id
               WHERE qa.part_number=? AND qa.serial_number=? AND qa.quote_id != ?
               ORDER BY qa.uploaded_at DESC LIMIT 1''',
            (part_number, serial_number, quote_id)).fetchone()
        conn2.close()
        if prev:
            suggestion = {
                'id':           prev['id'],
                'filename':     prev['filename'],
                'quote_number': prev['quote_number'],
            }

    return jsonify({
        'success':      True,
        'id':           att_id,
        'filename':     safe_name,
        'mime':         mime,
        'part_number':  part_number,
        'serial_number':serial_number,
        'verified':     0,
        'suggestion':   suggestion,
    })


@app.route('/quotes/<int:quote_id>/attach/<int:att_id>/view')
@login_required
def view_attachment(quote_id, att_id):
    """Serve an attachment file for in-browser viewing."""
    conn = get_db()
    row  = conn.execute(
        'SELECT * FROM quote_attachments WHERE id=? AND quote_id=?', (att_id, quote_id)).fetchone()
    conn.close()
    if not row:
        return 'Not found', 404
    return send_file(row['filepath'], mimetype=row['mimetype'],
                     as_attachment=False, download_name=row['filename'])


@app.route('/quotes/<int:quote_id>/attach/<int:att_id>/verify', methods=['POST'])
@login_required
def verify_attachment(quote_id, att_id):
    """Mark an attachment as verified (user opened and closed it)."""
    conn = get_db()
    conn.execute(
        'UPDATE quote_attachments SET verified=1 WHERE id=? AND quote_id=?', (att_id, quote_id))
    conn.commit()
    conn.close()
    return jsonify({'success': True, 'verified': 1})


@app.route('/quotes/<int:quote_id>/attach/<int:att_id>/delete', methods=['POST'])
@login_required
def delete_attachment(quote_id, att_id):
    conn = get_db()
    row  = conn.execute(
        'SELECT * FROM quote_attachments WHERE id=? AND quote_id=?', (att_id, quote_id)).fetchone()
    if row:
        try:
            os.remove(row['filepath'])
        except Exception:
            pass
        conn.execute('DELETE FROM quote_attachments WHERE id=?', (att_id,))
        conn.commit()
    conn.close()
    return jsonify({'success': True})


@app.route('/quotes/<int:quote_id>/send', methods=['POST'])
@login_required
def send_quote(quote_id):
    conn        = get_db()
    quote       = conn.execute('SELECT * FROM quotes WHERE id=?', (quote_id,)).fetchone()
    rfq         = conn.execute('SELECT * FROM rfqs WHERE id=?', (quote['rfq_id'],)).fetchone()
    items       = conn.execute('SELECT * FROM quote_items WHERE quote_id=?', (quote_id,)).fetchall()
    attachments = conn.execute(
        'SELECT * FROM quote_attachments WHERE quote_id=? AND verified=1', (quote_id,)).fetchall()
    settings    = get_settings()
    conn.close()

    # Allow override email from the send modal (e.g. entered from the quote list)
    to_email = request.form.get('override_email', '').strip() or rfq['customer_email']
    if not to_email:
        flash('No customer email on file. Enter an email address to send.', 'error')
        return redirect(url_for('quote_view', quote_id=quote_id))

    smtp_host = settings.get('smtp_host', 'smtp.gmail.com').strip()
    smtp_port = int(settings.get('smtp_port', 587))
    smtp_user = settings.get('smtp_user', '').strip()
    smtp_pass = settings.get('smtp_pass', '').strip()

    resend_key_check = (os.environ.get('RESEND_API_KEY') or settings.get('resend_api_key', '')).strip()
    if not resend_key_check and (not smtp_user or not smtp_pass):
        flash('Email is not configured. Go to Settings and enter your SMTP or Resend API credentials.', 'error')
        return redirect(url_for('quote_view', quote_id=quote_id))

    # Convert SQLite rows to plain dicts so they're safe to use in background thread
    quote_d    = dict(quote)
    rfq_d      = dict(rfq)
    items_d    = [dict(i) for i in items]
    settings_d = dict(settings)
    attachments_d = [dict(a) for a in attachments]

    # Mark as sending immediately so the page returns right away
    conn2 = get_db()
    conn2.execute("UPDATE quotes SET status='sending' WHERE id=?", (quote_id,))
    conn2.commit()
    conn2.close()

    import threading

    def do_send():
        sent = False
        last_err = None

        # Build email HTML inside the thread so logo fetch doesn't block the request
        try:
            html = build_quote_email(quote_d, rfq_d, items_d, settings_d, attachments_d)
        except Exception as e:
            print(f'[Email] build_quote_email failed: {e}')
            html = f'<p>Quote {quote_d["quote_number"]} — please contact us for details.</p>'

        # ── 1. Try Resend HTTP API first (works on Railway, no SMTP ports needed) ──
        resend_key = (
            os.environ.get('RESEND_API_KEY') or
            settings_d.get('resend_api_key', '')
        ).strip()

        company   = settings_d.get('company_name', 'Eastern Aero Pty Ltd')
        from_addr = f"{company} <sales@eastern-aero.com>"
        subject_line = f"Quotation {quote_d['quote_number']} — {company}"

        # Always BCC a copy to sales@eastern-aero.com
        internal_bcc = 'sales@eastern-aero.com'
        all_recipients = [to_email, internal_bcc]

        from email.mime.base import MIMEBase
        from email import encoders as _encoders
        msg = MIMEMultipart('mixed')
        msg['Subject'] = subject_line
        msg['From']    = from_addr
        msg['To']      = to_email
        msg['Bcc']     = internal_bcc   # hidden copy to sales
        # HTML body goes in an alternative sub-part
        alt = MIMEMultipart('alternative')
        alt.attach(MIMEText(html, 'html'))
        msg.attach(alt)
        # Attach uploaded files
        for att in attachments_d:
            try:
                with open(att['filepath'], 'rb') as fh:
                    part = MIMEBase('application', 'octet-stream')
                    part.set_payload(fh.read())
                _encoders.encode_base64(part)
                part.add_header('Content-Disposition', 'attachment',
                                filename=att['filename'])
                if att.get('mimetype'):
                    part.set_type(att['mimetype'])
                msg.attach(part)
            except Exception as e:
                print(f'[Email] Could not attach {att["filename"]}: {e}')

        if resend_key:
            # ── 1. Resend SMTP relay — use non-standard ports (2465/2587)
            #       that Railway does not block, authenticated with API key
            resend_attempts = [
                ('ssl',      'smtp.resend.com', 2465),
                ('starttls', 'smtp.resend.com', 2587),
                ('ssl',      'smtp.resend.com', 465),
                ('starttls', 'smtp.resend.com', 587),
            ]
            for mode, host, port in resend_attempts:
                try:
                    print(f'[Email] Trying Resend SMTP {mode}:{port}')
                    if mode == 'ssl':
                        with smtplib.SMTP_SSL(host, port, timeout=15) as srv:
                            srv.login('resend', resend_key)
                            # sendmail with explicit recipient list delivers BCC
                            srv.sendmail(from_addr, all_recipients, msg.as_string())
                    else:
                        with smtplib.SMTP(host, port, timeout=15) as srv:
                            srv.ehlo(); srv.starttls(); srv.ehlo()
                            srv.login('resend', resend_key)
                            srv.sendmail(from_addr, all_recipients, msg.as_string())
                    sent = True
                    print(f'[Email] Sent via Resend SMTP {mode}:{port} (BCC: {internal_bcc})')
                    break
                except Exception as e:
                    last_err = e
                    print(f'[Email] Resend SMTP {mode}:{port} failed: {e}')
                    continue

            # ── 1b. Try Resend REST API as fallback (supports bcc natively)
            if not sent:
                try:
                    print(f'[Email] Trying Resend API from={from_addr} to={to_email}')
                    email_id = send_via_resend(
                        resend_key, from_addr, to_email, subject_line, html,
                        bcc=internal_bcc)
                    if email_id:
                        sent = True
                        print(f'[Email] Sent via Resend API. ID={email_id}')
                except Exception as e:
                    last_err = e
                    print(f'[Email] Resend API failed: {e}')

        # ── 2. Fall back to your own SMTP if Resend not configured or failed ────
        if not sent and smtp_user and smtp_pass:
            attempts = []
            if smtp_port == 465:
                attempts = [('ssl', smtp_host, 465), ('starttls', smtp_host, 587)]
            else:
                attempts = [('starttls', smtp_host, smtp_port), ('ssl', smtp_host, 465)]

            for mode, host, port in attempts:
                try:
                    if mode == 'ssl':
                        with smtplib.SMTP_SSL(host, port, timeout=15) as srv:
                            srv.login(smtp_user, smtp_pass)
                            srv.sendmail(from_addr, all_recipients, msg.as_string())
                    else:
                        with smtplib.SMTP(host, port, timeout=15) as srv:
                            srv.ehlo(); srv.starttls(); srv.ehlo()
                            srv.login(smtp_user, smtp_pass)
                            srv.sendmail(from_addr, all_recipients, msg.as_string())
                    sent = True
                    print(f'[Email] Sent via SMTP {mode}:{port} (BCC: {internal_bcc})')
                    break
                except Exception as e:
                    last_err = e

        db = get_db()
        if sent:
            db.execute("UPDATE quotes SET status='sent', sent_at=CURRENT_TIMESTAMP WHERE id=?", (quote_id,))
            print(f'[Email] Quote {quote_d["quote_number"]} sent to {to_email}')
        else:
            db.execute("UPDATE quotes SET status='draft' WHERE id=?", (quote_id,))
            print(f'[Email] FAILED to send {quote_d["quote_number"]}: {last_err}')
        db.commit()
        db.close()

    thread = threading.Thread(target=do_send, daemon=True)
    thread.start()

    flash(f'Sending quote to {to_email}… Refresh in a few seconds to confirm it was sent.', 'success')
    return redirect(url_for('quote_view', quote_id=quote_id))


def send_via_resend(api_key, from_addr, to_addr, subject, html_body, bcc=None):
    """
    Send email via Resend HTTP API.
    Works on Railway (no SMTP port restrictions).
    Docs: https://resend.com/docs/api-reference/emails/send-email
    """
    import urllib.error
    payload_dict = {
        'from': from_addr,
        'to': [to_addr],
        'subject': subject,
        'html': html_body,
    }
    if bcc:
        payload_dict['bcc'] = [bcc] if isinstance(bcc, str) else bcc
    payload = json.dumps(payload_dict).encode('utf-8')
    req = urllib.request.Request(
        'https://api.resend.com/emails',
        data=payload,
        headers={
            'Authorization': f'Bearer {api_key}',
            'Content-Type': 'application/json',
        },
        method='POST'
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            result = json.loads(resp.read())
            return result.get('id')  # returns email ID if successful
    except urllib.error.HTTPError as e:
        # Capture the full Resend error message for debugging
        body = e.read().decode('utf-8', errors='ignore')
        raise Exception(f"Resend HTTP {e.code}: {body}")


# Hardcoded Eastern Aero logo as base64 — no external fetch needed
_LOGO_DATA_URI = "data:image/png;base64,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"

def _fetch_logo_b64(url):
    """Return hardcoded base64 logo data URI."""
    return _LOGO_DATA_URI


def _strip_html(html):
    """Strip HTML tags and decode entities, returning clean plain text."""
    # Remove scripts and styles entirely
    html = re.sub(r'<(script|style)[^>]*>.*?</\1>', ' ', html, flags=re.I | re.S)
    # Replace block-level tags with newlines
    html = re.sub(r'<(br|tr|p|div|li)[^>]*/?>',  '\n', html, flags=re.I)
    html = re.sub(r'</(p|div|tr|table|ul|ol)>', '\n', html, flags=re.I)
    # Remove all remaining tags
    html = re.sub(r'<[^>]+>', ' ', html)
    # Decode common HTML entities
    entities = {'&nbsp;': ' ', '&amp;': '&', '&lt;': '<', '&gt;': '>', '&quot;': '"', '&#39;': "'"}
    for ent, ch in entities.items():
        html = html.replace(ent, ch)
    # Collapse whitespace
    html = re.sub(r'[ \t]+', ' ', html)
    html = re.sub(r'\n{3,}', '\n\n', html)
    return html.strip()


def _parse_partsbase_html(html, subject):
    """
    Parse a PartsBase Quick Quote Request HTML email.
    Returns dict with keys: company, contact, phone, email, address,
                            customer_ref, parts (list of dicts)
    """
    result = {'parts': []}

    # Extract RFQ number from subject or HTML
    m = re.search(r'#(\d{6,})', subject + ' ' + html)
    if m:
        result['customer_ref'] = 'PartsBase RFQ #' + m.group(1)

    # Helper: get cell text after a label
    def cell_after(label, src):
        pat = re.escape(label) + r'[^<]*</(?:td|span)[^>]*>\s*<(?:td|span)[^>]*>\s*<(?:b|span|strong)?[^>]*>\s*([^<]+)'
        mm = re.search(pat, src, re.I | re.S)
        return mm.group(1).strip() if mm else ''

    # Strip for easier parsing
    txt = _strip_html(html)
    lines = [l.strip() for l in txt.splitlines() if l.strip()]

    # Regex helpers on raw HTML (more reliable for structured tables)
    def after_label_html(label):
        pat = label + r'\s*</(?:td|span)[^>]*>.*?<(?:b|strong)[^>]*>([^<]+)</(?:b|strong)'
        mm = re.search(pat, html, re.I | re.S)
        return mm.group(1).strip() if mm else ''

    result['company'] = after_label_html('Company:')
    result['contact'] = after_label_html('Contact:')
    result['phone']   = after_label_html('Phone:')
    result['email']   = after_label_html('Email:')

    # Address: City + State + ZIP + Country
    city    = after_label_html('City:')
    state   = after_label_html('State:')
    zipcode = after_label_html('ZIP:')
    country = after_label_html('Country:')
    addr_line = after_label_html('Address:')
    addr_parts = [p for p in [addr_line, city, state + ' ' + zipcode, country] if p.strip()]
    result['address'] = ', '.join(addr_parts)

    # Parts: find Part No, Description, Condition, Quantity blocks
    part_blocks = re.findall(
        r'Part\s*No[:\s]*</(?:td|span)[^>]*>.*?<(?:b|strong)[^>]*>([^<]+)</(?:b|strong)[^>]*>.*?'
        r'Description[:\s]*</(?:td|span)[^>]*>.*?<(?:b|strong)[^>]*>([^<]+)</(?:b|strong)[^>]*>.*?'
        r'Condition[:\s]*</(?:td|span)[^>]*>.*?<(?:b|strong)[^>]*>([^<]+)</(?:b|strong)[^>]*>.*?'
        r'Quantity[:\s]*</(?:td|span)[^>]*>.*?<(?:b|strong)[^>]*>([^<]+)</(?:b|strong)',
        html, re.I | re.S
    )
    for pn, desc, cond, qty in part_blocks:
        try:
            qty_int = int(re.search(r'\d+', qty).group())
        except Exception:
            qty_int = 1
        result['parts'].append({
            'part_number': pn.strip().upper(),
            'description': desc.strip(),
            'condition':   cond.strip().upper(),
            'quantity':    qty_int,
        })

    # Fallback: scrape plain text for parts if HTML regex missed them
    if not result['parts']:
        pn_m = re.search(r'Part\s*No[:\s]+([A-Z0-9\-]+)', txt, re.I)
        desc_m = re.search(r'Description[:\s]+([^\n]+)', txt, re.I)
        cond_m = re.search(r'Condition[:\s]+([A-Z]+)', txt, re.I)
        qty_m  = re.search(r'Quantity[:\s]+(\d+)', txt, re.I)
        if pn_m:
            result['parts'].append({
                'part_number': pn_m.group(1).strip().upper(),
                'description': desc_m.group(1).strip() if desc_m else '',
                'condition':   cond_m.group(1).strip().upper() if cond_m else 'SV',
                'quantity':    int(qty_m.group(1)) if qty_m else 1,
            })

    return result


def _extract_customer_ref(subject, body=''):
    """
    Try to extract the customer's own RFQ/PO/reference number from the email subject or body.
    Examples handled:
      "Fwd: TARA RFQ 036"           → "TARA RFQ 036"
      "RFQ-2025-1234"               → "RFQ-2025-1234"
      "Re: PO #4521 Parts Request"  → "PO #4521"
      "Inquiry Ref: ABC-99"         → "ABC-99"
    """
    # Strip common email prefixes (Fwd:, Re:, Fw:, etc.)
    cleaned = re.sub(r'^(?:(?:fwd?|re)\s*:\s*)+', '', subject, flags=re.I).strip()

    # Patterns to find an explicit ref number — captured group must contain a digit
    REF_PATTERNS = [
        r'\b(?:rfq|rq|po|p\.o|ref(?:erence)?|req(?:uest)?|order|enq(?:uiry)?)\s*[#:\-]?\s*([\w\-\/\.]*\d[\w\-\/\.]*)',
        r'#\s*([\w\-]*\d[\w\-]*)',
    ]
    for text in [cleaned, body[:500]]:
        for pat in REF_PATTERNS:
            m = re.search(pat, text, re.I)
            if m:
                if text is cleaned:
                    return cleaned  # use full cleaned subject as the ref label
                else:
                    return m.group(0).strip()

    # Fallback: use the cleaned subject if it contains a digit (looks like a real ref)
    if cleaned and len(cleaned) <= 60 and re.search(r'\d', cleaned):
        return cleaned
    return ''


def _parse_email_signature(body, sender_name='', sender_email=''):
    """
    Find the email signature block (after Best Regards / Thanks etc.)
    and extract name, company, phone, email, website, address from labelled lines.
    Handles markdown bold (*text*), emoji-prefixed lines (📞 Phone:), and plain bare values.
    """
    result = {}

    def _strip_decorations(s):
        """Strip markdown asterisks, emoji, and collapse whitespace."""
        # Remove emoji (unicode ranges for common emoji blocks)
        s = re.sub(r'[\U0001F300-\U0001FFFF\U00002600-\U000027BF\U0000FE00-\U0000FEFF]', '', s)
        s = re.sub(r'\*+', '', s)          # remove markdown bold/italic
        s = re.sub(r'\s{2,}', ' ', s)      # collapse multiple spaces
        return s.strip()

    # ── 1. Locate signature block ─────────────────────────────────────────────
    # Sign-off words that typically precede the signature
    SIGNOFF = re.compile(
        r'^\s*\*{0,2}\s*(?:best\s+regards?|kind\s+regards?|regards?|sincerely'
        r'|thanks?(?:\s+and\s+regards?)?|cheers|warm\s+regards?|thank\s+you'
        r'|atenciosamente|cordialmente|abraços?'          # Portuguese sign-offs
        r'|yours\s+(?:truly|sincerely|faithfully))[,\.]?\s*\*{0,2}\s*$',
        re.I | re.MULTILINE
    )
    # Also detect sig block by RFC 3676 "-- " delimiter or 3+ dashes alone
    SIG_DELIM = re.compile(r'(?m)^(?:--\s*|-{3,}|_{3,})\s*$')
    m_signoff = SIGNOFF.search(body)
    m_delim   = SIG_DELIM.search(body)
    if m_signoff and (not m_delim or m_signoff.start() <= m_delim.start()):
        sig_block = body[m_signoff.end():]
    elif m_delim:
        sig_block = body[m_delim.end():]
    else:
        sig_block = body  # no signoff found — scan whole body

    # Take first 40 non-empty lines, stripping decorations
    raw_lines = [l.strip() for l in sig_block.splitlines() if l.strip()][:40]
    lines = [_strip_decorations(l) for l in raw_lines]

    # ── 2. Patterns ───────────────────────────────────────────────────────────
    # Labels that may be preceded by emoji (already stripped above)
    PH_LABEL  = re.compile(
        r'^(?:p|m|ph|phone|tel|mobile|cell|direct|office|fax)[:\s\.]+(.+)$', re.I)
    EM_LABEL  = re.compile(
        r'^(?:e|e[\-]?mail|email)[:\s]+(.+)$', re.I)
    WEB_LABEL = re.compile(
        r'^(?:w|web(?:site)?|url|www)[:\s]+(.+)$', re.I)
    ADDR_LABEL = re.compile(
        r'^(?:addr(?:ess)?|endereço|location|loc)[:\s]+(.+)$', re.I)

    PH_BARE  = re.compile(r'^(\+{0,2}[\d][\d\s\-\.\(\)\/]{5,25}\d)$')
    WEB_BARE = re.compile(r'^((?:https?://|www\.)\S+)$', re.I)
    EMAIL_BARE = re.compile(r'^([\w.+\-]+@[\w\-]+\.[a-zA-Z]{2,})$')

    CO_SUFFIX = re.compile(
        r'\b(?:Pvt\.?\s*Ltd\.?|Pty\.?\s*Ltd\.?|Ltd\.?|LLC\.?|Inc\.?|Corp\.?'
        r'|GmbH|SAS|BV|PLC|Limited|Incorporated|Airlines?|Airways?|Aviation'
        r'|Aerospace|Parts\s*(?:Inc\.?)?|Spares|Supply|Services?|Solutions?'
        r'|International|Group|Holdings?)\b', re.I
    )
    # Role/job title keywords — these lines are titles, not company names
    ROLE_KW = re.compile(
        r'\b(?:manager|director|officer|executive|engineer|purchasing|procurement'
        r'|sales|assistente|gerente|compras|supervisor|analyst|coordinator'
        r'|specialist|representative|agent|buyer|vendas|comercial|técnico)\b', re.I
    )
    # Street address indicators
    STREET_KW = re.compile(
        r'\b(?:rua|av(?:enida)?|street|st\.|road|rd\.|blvd|boulevard|dr\.|drive'
        r'|lane|ln\.|way|court|ct\.|place|pl\.|square|sq\.|suite|ste\.'
        r'|andar|sala|lote|quadra|setor|jardim|jd\.?|bairro)\b', re.I
    )
    # Postal code patterns: US (12345 or 12345-6789), BR (12345-678), AU (1234), etc.
    POSTCODE = re.compile(
        r'\b(?:\d{5}(?:-\d{3,4})?|\d{4}(?:\s?[A-Z]{2})?|[A-Z]{1,2}\d[A-Z\d]?\s?\d[A-Z]{2})\b'
    )

    # Words that indicate a greeting/salutation (not a person's name)
    GREETING_WORDS = {
        'good', 'morning', 'afternoon', 'evening', 'hello', 'hi', 'hey',
        'dear', 'greetings', 'salutations', 'sir', 'madam', 'team',
        'all', 'everyone', 'there', 'folks', 'gentlemen',
    }

    addr_lines = []   # accumulate address lines

    for i, line in enumerate(lines):
        if not line:
            continue

        # ── Name: first 1-2 lines that look like a person's full name
        if 'sig_name' not in result and i <= 1:
            if re.match(r'^[A-ZÀ-Ö][a-zà-ö]+(?:\s+(?:de|da|dos?|las?|van|von|el)?\s*[A-ZÀ-Öa-zà-ö][a-zà-ö]+){1,3}$', line):
                words_lower = {w.lower() for w in line.split()}
                if not (words_lower & GREETING_WORDS):
                    result['sig_name'] = line

        # ── Role/title (line 2-4, has role keywords, not already captured as name)
        if 'role' not in result and 0 < i <= 3 and ROLE_KW.search(line):
            if not CO_SUFFIX.search(line):   # avoid "Sales Inc" being a role
                result['role'] = line

        # ── Phone
        if 'phone' not in result:
            lm = PH_LABEL.match(line)
            if lm:
                val = lm.group(1).strip()
                # Strip mailto-style angle brackets and take first number
                val = re.sub(r'<[^>]+>', '', val).strip()
                val = re.split(r'\s+or\s+', val, maxsplit=1)[0].strip()
                result['phone'] = val
            elif PH_BARE.match(line):
                result['phone'] = line

        # ── Email
        if 'email' not in result:
            em = EM_LABEL.match(line)
            if em:
                val = em.group(1).strip()
                angle = re.search(r'<([\w.+\-]+@[\w\-]+\.[a-zA-Z]{2,})>', val)
                if angle:
                    result['email'] = angle.group(1)
                else:
                    # Take first email-like token
                    tok = re.search(r'[\w.+\-]+@[\w\-]+\.[a-zA-Z]{2,}', val)
                    if tok:
                        result['email'] = tok.group()
            elif EMAIL_BARE.match(line):
                result['email'] = line
            else:
                # Handle "address <mailto:address>" style lines
                mailto = re.search(
                    r'([\w.+\-]+@[\w\-]+\.[a-zA-Z]{2,})'
                    r'(?:<mailto:([\w.+\-]+@[\w\-]+\.[a-zA-Z]{2,})>)?',
                    line
                )
                if mailto and not any(lbl in line.lower()[:10]
                                      for lbl in ('phone', 'tel', 'web', 'www', 'fax')):
                    result['email'] = mailto.group(1)

        # ── Website
        if 'website' not in result:
            wm = WEB_LABEL.match(line)
            if wm:
                val = wm.group(1).strip()
                # Strip angle bracket URL duplicates like "www.x.com<https://www.x.com>"
                val = re.sub(r'<https?://[^>]+>', '', val).strip()
                result['website'] = val
            elif WEB_BARE.match(line):
                raw_url = line
                clean_url = re.sub(r'<https?://[^>]+>', '', raw_url).strip()
                skip = ['unsubscribe', 'track', 'click', 'pixel']
                if not any(s in clean_url.lower() for s in skip):
                    result['website'] = clean_url

        # ── Company: line with a business suffix
        if 'company' not in result and CO_SUFFIX.search(line):
            if line.lower() != (sender_name or '').lower() and not ROLE_KW.search(line):
                result['company'] = line

        # ── Address accumulation: lines that look like street addresses or postcodes
        if STREET_KW.search(line) or (POSTCODE.search(line) and len(line) <= 60):
            # Don't re-add lines already captured as company/phone/email/website
            if (line not in (result.get('company',''), result.get('phone',''),
                             result.get('email',''), result.get('website',''))
                    and 'phone' not in line.lower()[:6]):
                addr_lines.append(line)

    # ── Assemble address ──────────────────────────────────────────────────────
    if addr_lines:
        result['address'] = ', '.join(addr_lines)

    # ── Company fallback: derive from email domain ────────────────────────────
    if 'company' not in result and sender_email and '@' in sender_email:
        domain = sender_email.split('@')[1].lower()
        # Strip TLD(s) and common generic domains
        GENERIC_DOMAINS = {'gmail', 'yahoo', 'hotmail', 'outlook', 'icloud',
                           'aol', 'protonmail', 'zoho', 'yandex'}
        base = domain.split('.')[0]
        if base not in GENERIC_DOMAINS:
            # Convert "aviationpartsinc" → "Aviation Parts Inc"
            # Split on common word boundaries (camelCase, numbers, separators)
            parts = re.sub(r'([a-z])([A-Z])', r'\1 \2', base)
            parts = re.sub(r'[\-_]', ' ', parts)
            result['company'] = parts.title()

    return result


def _build_to_block(rfq):
    """Build a multi-line HTML 'To:' block from all filled RFQ contact fields."""
    def _get(key):
        try:
            return (rfq.get(key) if isinstance(rfq, dict) else rfq[key]) or ''
        except Exception:
            return ''

    name    = _get('customer_name')
    company = _get('company')
    email   = _get('customer_email')
    phone   = _get('phone')
    website = _get('website')
    address = _get('address')

    # Remove customer name prefix from company if accidentally captured together
    if company and name and company.lower().startswith(name.lower()):
        company = company[len(name):].lstrip(' \t-—').strip()

    lines = []
    if name:
        lines.append(f'<strong style="font-size:14px;color:#111827">{name}</strong>')
    if company and company != name:
        lines.append(f'<span>{company}</span>')
    if email:
        lines.append(f'<a href="mailto:{email}" style="color:#1a3c6e;text-decoration:none">{email}</a>')
    if phone:
        lines.append(f'<span>Tel: {phone}</span>')
    if website:
        ws = website if website.startswith('http') else f'https://{website}'
        lines.append(f'<a href="{ws}" style="color:#1a3c6e;text-decoration:none">{website}</a>')
    if address:
        # Render each address line separately
        for addr_line in address.strip().splitlines():
            addr_line = addr_line.strip()
            if addr_line:
                lines.append(f'<span style="color:#6b7280">{addr_line}</span>')

    inner = '<br>\n  '.join(lines) if lines else '—'
    return f'<p style="font-size:13px;margin:4px 0 12px;line-height:1.8;color:#374151">{inner}</p>'


def build_quote_email(quote, rfq, items, settings, attachments_d=None):
    attachments_d = attachments_d or []
    currency = quote['currency'] if quote['currency'] else 'USD'
    td  = 'padding:9px 12px;border-bottom:1px solid #e5e7eb;'
    thd = f'padding:9px 12px;text-align:left;font-weight:600;color:#374151;border-bottom:2px solid #d1d5db;'

    # Use publicly hosted logo URL — Gmail blocks base64 data URIs in email
    # The logo is served as a static file from the Railway deployment
    logo_src = 'https://quote.eastern-aero.com/static/logo.png'

    item_blocks = ''
    for it in items:
        no_quote = bool(it.get('no_quote'))
        qty_str  = f"{it['quantity_requested']} EA"
        lead     = it['lead_time'] or 'Stock'
        ptype    = it['price_type'] or 'Outright'
        warranty = it['warranty'] or '—'
        trace    = it['trace_to'] or it['description'] or '—'
        tag_type = it['tag_type'] or '—'
        tagged   = it['tagged_by'] or '—'

        # Clean description
        desc_raw   = (it['description'] or '—')
        desc_clean = re.sub(r'\s*\bQuantity[\s:]+\d+\b.*$', '', desc_raw, flags=re.I).strip() or '—'
        desc_short = desc_clean[:60]

        if no_quote:
            # No Quote row — greyed out with clear "NO QUOTE" label
            nq_row_style = 'background:#f3f4f6;opacity:0.85'
            nq_td = f'padding:9px 12px;border-bottom:1px solid #e5e7eb;color:#9ca3af'
            item_blocks += f"""
<table width="100%" cellspacing="0" style="border-collapse:collapse;margin-bottom:24px;font-size:14px;{nq_row_style}">
  <thead>
    <tr style="background:#f3f4f6">
      <th style="{thd}color:#9ca3af">Part</th>
      <th style="{thd}color:#9ca3af">Description</th>
      <th style="{thd}color:#9ca3af">Qty</th>
      <th style="{thd}color:#9ca3af">CC</th>
      <th style="{thd}color:#9ca3af">Lead Time</th>
      <th style="{thd}text-align:right;color:#9ca3af" colspan="2">Status</th>
    </tr>
  </thead>
  <tbody>
    <tr>
      <td style="{nq_td};font-weight:600;text-decoration:line-through">{it['part_number']}</td>
      <td style="{nq_td}">{desc_short}</td>
      <td style="{nq_td}">{qty_str}</td>
      <td style="{nq_td}">{it['condition'] or 'SV'}</td>
      <td style="{nq_td}">—</td>
      <td colspan="2" style="{nq_td}text-align:right">
        <span style="display:inline-block;background:#dc2626;color:#ffffff;font-weight:700;
                     font-size:12px;padding:3px 10px;border-radius:4px;letter-spacing:.5px">
          NO QUOTE
        </span>
      </td>
    </tr>
  </tbody>
</table>
<hr style="border:none;border-top:1px solid #e5e7eb;margin:0 0 16px">"""
        else:
            unit_p = f"${it['unit_price']:,.2f}"
            line_t = f"${it['extended_price']:,.2f}"
            item_blocks += f"""
<table width="100%" cellspacing="0" style="border-collapse:collapse;margin-bottom:24px;font-size:14px">
  <thead>
    <tr style="background:#f9fafb">
      <th style="{thd}">Part</th>
      <th style="{thd}">Description</th>
      <th style="{thd}">Qty</th>
      <th style="{thd}">CC</th>
      <th style="{thd}">Lead Time</th>
      <th style="{thd}text-align:right">Unit Price ({currency})</th>
      <th style="{thd}text-align:right">Line Total ({currency})</th>
    </tr>
  </thead>
  <tbody>
    <tr>
      <td style="{td}font-weight:600">{it['part_number']}</td>
      <td style="{td};color:#6b7280">{desc_short}</td>
      <td style="{td}">{qty_str}</td>
      <td style="{td}">{it['condition'] or 'SV'}</td>
      <td style="{td}">{lead}</td>
      <td style="{td}text-align:right">{unit_p}</td>
      <td style="{td}text-align:right;font-weight:600">{line_t}</td>
    </tr>
    <tr>
      <td colspan="6" style="padding:4px 12px 12px;text-align:right;color:#6b7280;font-size:13px">
        Price Type: {ptype}
      </td>
    </tr>
    <tr>
      <td colspan="6" style="padding:0 12px 14px">
        <table cellspacing="0" style="font-size:13px;color:#374151">
          <tr><td style="padding:2px 16px 2px 0;color:#6b7280">Warranty:</td><td>{warranty}</td></tr>
          <tr><td style="padding:2px 16px 2px 0;color:#6b7280">Trace To:</td><td>{trace}</td></tr>
          <tr><td style="padding:2px 16px 2px 0;color:#6b7280">Tag Type:</td><td>{tag_type}</td></tr>
          <tr><td style="padding:2px 16px 2px 0;color:#6b7280">Tagged by:</td><td>{tagged}</td></tr>
        </table>
      </td>
    </tr>
  </tbody>
</table>
<hr style="border:none;border-top:1px solid #e5e7eb;margin:0 0 16px">"""

    return f"""<!DOCTYPE html>
<html lang="en" xmlns="http://www.w3.org/1999/xhtml">
<head>
<meta http-equiv="Content-Type" content="text/html; charset=UTF-8">
<meta name="color-scheme" content="light only">
<meta name="supported-color-schemes" content="light only">
<style>
  :root {{ color-scheme: light only; }}
  body {{ background-color: #ffffff !important; color: #111827 !important; }}
</style>
</head>
<body bgcolor="#ffffff" style="background-color:#ffffff !important;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Arial,sans-serif;color:#111827;max-width:760px;margin:32px auto;padding:0 16px">
<!-- Logo header -->
<table width="100%" cellspacing="0" style="margin-bottom:20px">
  <tr>
    <td>
      <img src="{logo_src}"
           alt="Eastern Aero" height="60"
           style="height:60px;max-width:220px;object-fit:contain">
    </td>
    <td style="text-align:right;vertical-align:middle">
      <span style="font-size:11px;color:#6b7280">
        Date: {quote['created_at'][:10]} &nbsp;|&nbsp; Valid: {quote['valid_days']} days &nbsp;|&nbsp; {currency}
      </span>
    </td>
  </tr>
</table>

<h1 style="font-size:22px;font-weight:700;margin-bottom:6px;color:#111827">
  Quote Ref# {quote['quote_number']}
</h1>
<p style="margin:0 0 16px;font-size:13px;color:#6b7280;font-style:italic">
  Prices are in {currency} and valid for {quote['valid_days']} days, subject to availability.
  For AOG support please call <a href="tel:+13213419779" style="color:#1a3c6e;text-decoration:none">+1 321-341-9779</a>
  or WhatsApp <a href="tel:+17722032109" style="color:#1a3c6e;text-decoration:none">+1 772-203-2109</a>.
</p>
{_build_to_block(rfq)}

<hr style="border:none;border-top:1px solid #e5e7eb;margin:16px 0">

{item_blocks}

<table width="100%" cellspacing="0" style="margin-top:8px">
  <tr>
    <td style="text-align:right;font-size:16px;font-weight:700;padding:8px 0">
      Total: ${quote['total_amount']:,.2f}
    </td>
  </tr>
</table>

{f'<p style="margin-top:16px;color:#374151"><strong>Notes:</strong> {quote["notes"]}</p>' if quote['notes'] else ''}

<hr style="border:none;border-top:1px solid #e5e7eb;margin:24px 0">

<!-- Signature -->
<table width="100%" cellspacing="0" style="font-size:13px">
  <tr>
    <td style="vertical-align:top;width:55%">
      <div style="border-top:3px solid #1a3c6e;padding-top:10px">
        <p style="margin:0 0 2px;font-weight:700;font-size:15px">James Cook</p>
        <p style="margin:0 0 10px;color:#555;font-size:13px">Sales Manager</p>
        <img src="{logo_src}"
             alt="Eastern Aero" height="40"
             style="height:40px;max-width:160px;object-fit:contain;margin-bottom:10px;display:block">
        <p style="margin:4px 0;font-size:13px">
          <strong style="color:#1a3c6e">Office:</strong>
          <a href="tel:+13213419779" style="color:#1a3c6e;text-decoration:none">+1 321-341-9779</a>
        </p>
        <p style="margin:4px 0;font-size:13px">
          <strong style="color:#1a3c6e">Cell / WhatsApp:</strong>
          <a href="tel:+17722032109" style="color:#1a3c6e;text-decoration:none">+1 772-203-2109</a>
        </p>
        <p style="margin:4px 0;font-size:12px;color:#555">Morayfield QLD 4506 Australia</p>
        <p style="margin:4px 0;font-size:12px;color:#555">Morayfield, QLD 4506</p>
        <p style="margin:4px 0;font-size:12px;color:#555">Kathmandu, Nepal, 44600</p>
        <p style="margin:6px 0 0;font-size:13px">
          <a href="http://www.eastern-aero.com" style="color:#1a3c6e">www.eastern-aero.com</a>
        </p>
      </div>
    </td>
    <td style="vertical-align:top;width:45%;padding-left:20px">
      <p style="font-weight:700;margin-bottom:6px">Attachments</p>
      {''.join(f'<p style="margin:2px 0;font-size:13px;color:#374151"><i>📎 {a["filename"]}</i></p>' for a in attachments_d) if attachments_d else '<p style="margin:0;color:#6b7280;font-size:13px">No attachments</p>'}
    </td>
  </tr>
</table>

<p style="margin-top:24px;color:#9ca3af;font-size:11px;text-align:center">
  All prices in {currency}. Quote valid for {quote['valid_days']} days from date of issue.<br>
  All parts are certified unless otherwise stated. Terms: COD unless prior credit arrangement established.
</p>

<p style="margin-top:16px;padding-top:12px;border-top:1px solid #e5e7eb;color:#9ca3af;font-size:11px;text-align:center">
  Proudly Made in Nepal &#x1F1F3;&#x1F1F5; by Eastern Aero Nepal Pvt Ltd &nbsp;|&nbsp;
  Learn more about <a href="https://www.eastern-aero.com" style="color:#9ca3af;text-decoration:underline">Eastern Aero Nepal</a>
</p>
</body></html>"""


# ─── Routes: RFQ Delete ──────────────────────────────────────────────────────

@app.route('/rfqs/<int:rfq_id>/delete', methods=['POST'])
@login_required
def rfq_delete(rfq_id):
    conn = get_db()
    rfq = conn.execute('SELECT * FROM rfqs WHERE id=?', (rfq_id,)).fetchone()
    if rfq:
        # Mark the email's message_id so it won't be re-imported on next fetch
        msg_id = rfq['email_message_id']
        if msg_id:
            conn.execute('INSERT OR IGNORE INTO imported_emails (message_id) VALUES (?)', (msg_id,))
        conn.execute('DELETE FROM rfq_items WHERE rfq_id=?', (rfq_id,))
        conn.execute('DELETE FROM rfqs WHERE id=?', (rfq_id,))
        conn.commit()
    conn.close()
    flash('RFQ deleted. That email will not be imported again.', 'success')
    return redirect(url_for('rfq_list'))


# ─── Routes: Email Fetch (IMAP) ──────────────────────────────────────────────

@app.route('/rfqs/fetch-email', methods=['POST'])
@login_required
def fetch_email_rfqs():
    settings = get_settings()
    if not settings.get('imap_pass'):
        flash('Configure IMAP credentials in Settings first.', 'error')
        return redirect(url_for('rfq_list'))
    try:
        n = _fetch_imap(settings)
        flash(f'Fetched {n} new RFQ email(s) from inbox.', 'success')
    except Exception as e:
        flash(f'Email fetch failed: {e}', 'error')
    return redirect(url_for('rfq_list'))


def _fetch_from_folder(mail, folder):
    """
    Fetch emails from the folder: UNSEEN + anything received in the last 14 days.
    Since rfq@eastern-aero.com is a dedicated RFQ inbox we cast a wide net and
    rely on the imported_emails dedup table to avoid re-processing.
    """
    from datetime import timedelta
    try:
        status, _ = mail.select(folder)
        if status != 'OK':
            return []
    except Exception:
        return []

    msg_ids = set()

    # 1. All UNSEEN messages
    try:
        _, data = mail.search(None, 'UNSEEN')
        for mid in (data[0].split() if data and data[0] else []):
            msg_ids.add(mid)
    except Exception:
        pass

    # 2. Everything received in the last 14 days (catches already-read emails)
    since_date = (datetime.now() - timedelta(days=14)).strftime('%d-%b-%Y')
    try:
        _, data = mail.search(None, 'SINCE', since_date)
        for mid in (data[0].split() if data and data[0] else []):
            msg_ids.add(mid)
    except Exception:
        pass

    return [(folder, mid) for mid in msg_ids]


def _fetch_imap(settings):
    import socket
    socket.setdefaulttimeout(30)  # 30s timeout per IMAP operation
    mail = imaplib.IMAP4_SSL(
        settings.get('imap_host', 'imap.gmail.com'),
        int(settings.get('imap_port', 993)))
    mail.login(settings['imap_user'], settings['imap_pass'])

    # Always read INBOX plus any configured label (if different)
    folders_to_check = ['INBOX']
    extra_folder = settings.get('imap_folder', 'INBOX').strip()
    if extra_folder and extra_folder.upper() != 'INBOX':
        folders_to_check.append(extra_folder)

    # Collect (folder, msg_id) pairs, deduplicated by message-id later
    folder_msg_pairs = []
    for folder in folders_to_check:
        folder_msg_pairs.extend(_fetch_from_folder(mail, folder))

    conn = get_db()
    count = 0

    for folder, mid in folder_msg_pairs:
        mail.select(folder)

        # Full download
        try:
            _, data = mail.fetch(mid, '(RFC822)')
            if not data or not data[0] or not isinstance(data[0], tuple):
                continue
            raw_msg = data[0][1]
            msg = email.message_from_bytes(raw_msg)
        except Exception:
            continue

        # Extract Message-ID for deduplication
        message_id = (msg.get('Message-ID') or '').strip()

        # Skip already-imported messages
        if message_id:
            already = conn.execute(
                'SELECT 1 FROM imported_emails WHERE message_id=?', (message_id,)).fetchone()
            if already:
                continue

        # Skip emails from blocked senders (previously deleted as non-RFQ)
        sender_check = re.search(r'[\w.+\-]+@[\w\-]+\.[a-zA-Z]+', msg.get('From', ''))
        if sender_check:
            blocked = conn.execute(
                'SELECT 1 FROM blocked_senders WHERE email=?',
                (sender_check.group().lower(),)).fetchone()
            if blocked:
                conn.execute('INSERT OR IGNORE INTO imported_emails (message_id) VALUES (?)', (message_id,))
                conn.commit()
                continue

        # Subject
        subj_raw = msg.get('Subject', 'RFQ')
        subj_dec = decode_header(subj_raw)[0][0]
        subject  = subj_dec.decode('utf-8', errors='ignore') if isinstance(subj_dec, bytes) else str(subj_dec)

        # Sender
        from_raw   = msg.get('From', '')
        em_match   = re.search(r'[\w.+\-]+@[\w\-]+\.[a-zA-Z]+', from_raw)
        cust_email = em_match.group() if em_match else from_raw
        cust_name  = from_raw.split('<')[0].strip().strip('"') if '<' in from_raw else ''

        # Body — always collect BOTH plain text and HTML parts.
        # We need HTML for PartsBase table parsing, plain for text parsing.
        body = ''
        raw_html_body = ''
        if msg.is_multipart():
            for part in msg.walk():
                ct = part.get_content_type()
                if ct == 'text/plain' and not body:
                    try:
                        body = part.get_payload(decode=True).decode('utf-8', errors='ignore')
                    except Exception:
                        pass
                elif ct == 'text/html' and not raw_html_body:
                    try:
                        raw_html_body = part.get_payload(decode=True).decode('utf-8', errors='ignore')
                    except Exception:
                        pass
        else:
            ct = msg.get_content_type()
            if ct == 'text/html':
                raw_html_body = msg.get_payload(decode=True).decode('utf-8', errors='ignore') or ''
            else:
                body = msg.get_payload(decode=True).decode('utf-8', errors='ignore') or ''
        if not body and raw_html_body:
            body = _strip_html(raw_html_body)

        # Always treat emails from PartsBase as RFQs (direct)
        from_partsbase = 'rfqs@partsbase.com' in from_raw.lower()

        # Handle forwarded emails — extract original sender and content
        fwd_name, fwd_email, parse_body = extract_forwarded_content(body)
        is_forwarded = fwd_name is not None

        # ── Detect PartsBase content inside a forwarded email ─────────────────
        # Pattern: someone forwarded a PartsBase Quick Quote Request to rfq@
        PB_FWD_MARKERS = [
            'partsbase quick quote request',
            'rfqs@partsbase.com',
            'partsbase.com on behalf of',
            'quick quote request #',
        ]
        body_lower = (body + ' ' + subject).lower()
        is_forwarded_partsbase = is_forwarded and any(m in body_lower for m in PB_FWD_MARKERS)

        if is_forwarded_partsbase:
            # Treat exactly like a direct PartsBase email
            from_partsbase = True
            is_forwarded   = False
            parse_body     = body  # full body for keyword check; HTML parser does the real work
            # Don't use fwd_name/fwd_email — the "From" in the forward header is
            # rfq@eastern-aero.com or an internal address, not the real customer.
            # The real customer comes from the PartsBase HTML table.

        elif is_forwarded:
            # Regular forwarded email — use sender info from the forwarded header
            if fwd_email:
                cust_email = fwd_email
            if fwd_name:
                cust_name = fwd_name
            if not subject.lower().startswith('fwd') and not subject.lower().startswith('fw'):
                subject = f'[Forwarded] {subject}'
        else:
            parse_body = body

        # Decide if it looks like an RFQ
        to_header = (msg.get('To', '') + ' ' + msg.get('Delivered-To', '') + ' ' + msg.get('X-Original-To', '')).lower()
        addressed_to_rfq = 'rfq@eastern-aero.com' in to_header

        combined = (subject + ' ' + parse_body).lower()

        # Keywords that identify a genuine RFQ from a customer
        rfq_keywords = [
            'rfq', 'request for quote', 'request for quotation',
            'quote', 'quotation', 'part no', 'part number', 'part #',
            'p/n', 'pn:', 'p/n:', 'description', 'quantity', 'qty',
            'parts needed', 'availability', 'aircraft part', 'aviation part',
            'aog', 'nsn', 'pricing', 'price request', 'stock', 'lead time',
            'need parts', 'looking for', 'do you have',
            'partsbase', 'quick quote request']

        has_rfq_keywords = any(kw in combined for kw in rfq_keywords)

        # ── Sender blocklist: known non-RFQ automated senders ────────────────
        BLOCKED_DOMAINS = [
            'google.com', 'gmail.com', 'googlemail.com',
            'accounts.google.com', 'notifications.google.com',
            'mailer-daemon', 'postmaster', 'noreply', 'no-reply',
            'donotreply', 'do-not-reply', 'bounce', 'maildaemon',
            'microsoft.com', 'linkedin.com', 'facebook.com',
            'twitter.com', 'instagram.com', 'amazon.com',
        ]
        sender_blocked = any(b in cust_email.lower() for b in BLOCKED_DOMAINS)

        # ── Final RFQ determination ───────────────────────────────────────────
        # PartsBase and forwarded emails always qualify.
        # Direct emails to rfq@ must have RFQ keywords AND not be from a blocked sender.
        is_rfq = (
            from_partsbase
            or is_forwarded
            or (addressed_to_rfq and has_rfq_keywords and not sender_blocked)
        )
        parsed = parse_rfq_text(parse_body)

        if parsed or is_rfq:
            rfq_no = gen_rfq_number()
            source = 'partsbase' if from_partsbase else ('email-forwarded' if is_forwarded else 'email')

            cust_company = ''
            cust_phone   = ''
            cust_website = ''
            cust_address = ''
            customer_ref = ''

            # ── PartsBase: parse structured HTML directly ─────────────────────
            if from_partsbase and raw_html_body:
                pb = _parse_partsbase_html(raw_html_body, subject)
                cust_name    = pb.get('contact') or cust_name
                cust_company = pb.get('company', '')
                cust_phone   = pb.get('phone', '')
                cust_address = pb.get('address', '')
                customer_ref = pb.get('customer_ref', '')
                # Use email from PartsBase HTML if sender is partsbase.com
                pb_email = pb.get('email', '')
                if pb_email:
                    cust_email = pb_email
                # Override parsed parts with PartsBase structured parts
                if pb.get('parts'):
                    parsed = pb['parts']
            else:
                # ── Parse email signature block for contact info ──────────────
                sig_info = _parse_email_signature(parse_body, cust_name, cust_email)
                cust_company = sig_info.get('company', '')
                cust_phone   = sig_info.get('phone', '')
                cust_website = sig_info.get('website', '')
                cust_address = sig_info.get('address', '')
                sig_name = sig_info.get('sig_name', '')
                if sig_name and sig_name.lower() != cust_name.lower():
                    cust_name = sig_name
                sig_email = sig_info.get('email', '')
                if sig_email and not cust_email:
                    cust_email = sig_email
                # ── Final fallback: derive name from email address ─────────────
                # If cust_name is still empty or just looks like a greeting,
                # use the part before @ in the email address
                GREETING_WORDS_CHECK = {
                    'good', 'morning', 'afternoon', 'evening', 'hello', 'hi',
                    'dear', 'greetings', 'hey', 'sir', 'madam',
                }
                name_words = {w.lower() for w in cust_name.split()}
                if not cust_name or (name_words & GREETING_WORDS_CHECK):
                    if cust_email and '@' in cust_email:
                        # Convert "john.smith" → "John Smith"
                        prefix = cust_email.split('@')[0]
                        cust_name = ' '.join(
                            w.capitalize() for w in re.split(r'[._\-]', prefix)
                        ) or cust_email
                # ── Extract customer reference from subject/body ──────────────
                customer_ref = _extract_customer_ref(subject, parse_body)

            # ── Check customer_profiles for previously saved/corrected info ──
            if cust_email:
                profile = conn.execute(
                    'SELECT * FROM customer_profiles WHERE email=?',
                    (cust_email.lower(),)
                ).fetchone()
                if profile:
                    cust_name    = profile['name']    or cust_name
                    cust_company = profile['company'] or cust_company
                    cust_phone   = profile['phone']   or cust_phone
                    cust_website = profile['website'] or cust_website
                    cust_address = profile['address'] or cust_address

            conn.execute(
                'INSERT INTO rfqs (rfq_number,customer_name,customer_email,company,phone,website,address,source,notes,raw_email,email_message_id,customer_ref) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                (rfq_no, cust_name, cust_email, cust_company, cust_phone, cust_website, cust_address, source, subject, body[:6000], message_id, customer_ref))
            rfq_id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]

            for item in parsed:
                pn   = item['part_number']
                desc = item['description']
                cond = item['condition']

                # ── Cross-reference against inventory ─────────────────────────
                # If the parsed PN matches something in stock, use the
                # canonical PN from inventory (correct capitalisation, dashes etc.)
                # and fill in description if the email didn't provide one.
                inv_match = match_inventory(pn, conn)
                if inv_match:
                    pn   = inv_match['part_number']          # canonical PN
                    desc = desc or inv_match.get('description') or desc
                    # Use inventory condition only if email didn't supply one
                    if cond in ('SV', '') and inv_match.get('condition'):
                        cond = inv_match['condition']

                conn.execute(
                    'INSERT INTO rfq_items (rfq_id,part_number,description,quantity,condition) VALUES (?,?,?,?,?)',
                    (rfq_id, pn, desc, item['quantity'], cond))
            count += 1

        # Always mark this message as imported so we don't check it again
        try:
            conn.execute('INSERT OR IGNORE INTO imported_emails (message_id) VALUES (?)', (message_id,))
        except Exception:
            pass

    conn.commit()
    conn.close()
    mail.close()
    mail.logout()
    return count


# ─── Routes: API / Settings ──────────────────────────────────────────────────


@app.route('/api/parse-text', methods=['POST'])
@login_required
def api_parse_text():
    text  = request.get_json(force=True).get('text', '')
    items = parse_rfq_text(text)
    return jsonify({'items': items})


@app.route('/api/last-quote-for-pn')
@login_required
def api_last_quote_for_pn():
    pn          = request.args.get('pn', '').strip().upper()
    exclude_qid = request.args.get('exclude_quote', 0, type=int)
    if not pn:
        return jsonify({'found': False})
    conn = get_db()
    row  = conn.execute('''
        SELECT qi.unit_price, qi.condition, qi.lead_time, qi.price_type,
               qi.warranty, qi.trace_to, qi.tag_type, qi.tagged_by,
               q.quote_number, q.created_at
        FROM quote_items qi
        JOIN quotes q ON qi.quote_id = q.id
        WHERE qi.part_number = ? AND q.id != ? AND q.status = 'sent'
        ORDER BY q.created_at DESC LIMIT 1
    ''', (pn, exclude_qid)).fetchone()
    conn.close()
    if not row:
        return jsonify({'found': False})
    return jsonify({
        'found':        True,
        'quote_number': row['quote_number'],
        'date':         row['created_at'][:10],
        'unit_price':   row['unit_price'],
        'condition':    row['condition'] or 'SV',
        'lead_time':    row['lead_time'] or 'Stock',
        'price_type':   row['price_type'] or 'Outright',
        'warranty':     row['warranty'] or '3 Months',
        'trace_to':     row['trace_to'] or '',
        'tag_type':     row['tag_type'] or '',
        'tagged_by':    row['tagged_by'] or '',
    })


@app.route('/api/update-customer-ref', methods=['POST'])
@login_required
def api_update_customer_ref():
    data   = request.get_json(force=True)
    rfq_id = data.get('rfq_id')
    ref    = (data.get('ref') or '').strip()
    if not rfq_id:
        return jsonify({'ok': False, 'error': 'Missing rfq_id'}), 400
    conn = get_db()
    conn.execute('UPDATE rfqs SET customer_ref=? WHERE id=?', (ref, rfq_id))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})


@app.route('/api/update-customer', methods=['POST'])
@login_required
def api_update_customer():
    """Save corrected customer info to the RFQ and to customer_profiles for reuse."""
    data     = request.get_json(force=True)
    rfq_id   = data.get('rfq_id')
    name     = (data.get('name') or '').strip()
    company  = (data.get('company') or '').strip()
    email    = (data.get('email') or '').strip().lower()
    phone    = (data.get('phone') or '').strip()
    website  = (data.get('website') or '').strip()
    address  = (data.get('address') or '').strip()

    if not rfq_id:
        return jsonify({'ok': False, 'error': 'Missing rfq_id'}), 400

    conn = get_db()
    conn.execute(
        'UPDATE rfqs SET customer_name=?, company=?, customer_email=?, phone=?, website=?, address=? WHERE id=?',
        (name, company, email, phone, website, address, rfq_id)
    )
    if email:
        conn.execute('''
            INSERT INTO customer_profiles (email, name, company, phone, website, address, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(email) DO UPDATE SET
                name=excluded.name,
                company=excluded.company,
                phone=excluded.phone,
                website=excluded.website,
                address=excluded.address,
                updated_at=CURRENT_TIMESTAMP
        ''', (email, name, company, phone, website, address))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})


@app.route('/api/test-imap', methods=['POST'])
@login_required
def api_test_imap():
    s = get_settings()
    try:
        m = imaplib.IMAP4_SSL(s.get('imap_host','imap.gmail.com'), int(s.get('imap_port',993)))
        m.login(s['imap_user'], s['imap_pass'])
        folders = m.list()[1]
        m.logout()
        return jsonify({'success': True, 'message': f'Connected! {len(folders)} folders found.'})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)})


@app.route('/api/test-smtp', methods=['POST'])
@login_required
def api_test_smtp():
    s = get_settings()
    try:
        with smtplib.SMTP(s['smtp_host'], int(s['smtp_port'])) as srv:
            srv.starttls()
            srv.login(s['smtp_user'], s['smtp_pass'])
        return jsonify({'success': True, 'message': 'SMTP connection successful!'})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)})


@app.route('/settings', methods=['GET', 'POST'])
@login_required
@admin_required
def settings_page():
    if request.method == 'POST':
        conn = get_db()
        for key in ['company_name','company_email','company_phone','company_address',
                    'default_markup','quote_valid_days',
                    'imap_host','imap_port','imap_user','imap_pass','imap_folder',
                    'smtp_host','smtp_port','smtp_user','smtp_pass','resend_api_key']:
            conn.execute('INSERT OR REPLACE INTO settings VALUES (?,?)', (key, request.form.get(key,'')))
        conn.commit()
        conn.close()
        flash('Settings saved.', 'success')
        return redirect(url_for('settings_page'))
    return render_template('settings.html', s=get_settings())


# ─── Customers ───────────────────────────────────────────────────────────────

@app.route('/customers')
@login_required
def customer_list():
    conn = get_db()
    q    = request.args.get('q','').strip()
    if q:
        customers = conn.execute(
            "SELECT * FROM customers WHERE name LIKE ? OR email LIKE ? OR phone LIKE ? ORDER BY name",
            (f'%{q}%', f'%{q}%', f'%{q}%')
        ).fetchall()
    else:
        customers = conn.execute('SELECT * FROM customers ORDER BY name').fetchall()
    conn.close()
    return render_template('customer_list.html', customers=customers, q=q)


@app.route('/customers/new', methods=['GET', 'POST'])
@login_required
def customer_new():
    if request.method == 'POST':
        conn = get_db()
        cur = conn.execute('''
            INSERT INTO customers
              (name, phone, fax, email, payment_method, payment_terms, credit_limit,
               hourly_rate, tax_id, tax_percent, vat_number, date_format,
               sales_person, purchasing_person, customer_service_rep, shipping_service,
               status, required_part_categories, currency, tags,
               statement_notes, invoice_notes, notes, related_vendor_id,
               billing_name, billing_address, billing_city, billing_state, billing_zip, billing_country,
               shipping_name, shipping_address, shipping_city, shipping_state, shipping_zip, shipping_country)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ''', (
            request.form.get('name',''),
            request.form.get('phone',''),
            request.form.get('fax',''),
            request.form.get('email',''),
            request.form.get('payment_method','Check'),
            request.form.get('payment_terms','COD'),
            float(request.form.get('credit_limit',0) or 0),
            float(request.form.get('hourly_rate',100) or 100),
            request.form.get('tax_id',''),
            float(request.form.get('tax_percent',0) or 0),
            request.form.get('vat_number',''),
            request.form.get('date_format','mm-yyyy'),
            request.form.get('sales_person',''),
            request.form.get('purchasing_person',''),
            request.form.get('customer_service_rep',''),
            request.form.get('shipping_service',''),
            request.form.get('status','Active'),
            request.form.get('required_part_categories',''),
            request.form.get('currency','USD'),
            request.form.get('tags',''),
            request.form.get('statement_notes',''),
            request.form.get('invoice_notes',''),
            request.form.get('notes',''),
            request.form.get('related_vendor_id') or None,
            request.form.get('billing_name',''),
            request.form.get('billing_address',''),
            request.form.get('billing_city',''),
            request.form.get('billing_state',''),
            request.form.get('billing_zip',''),
            request.form.get('billing_country','USA'),
            request.form.get('shipping_name',''),
            request.form.get('shipping_address',''),
            request.form.get('shipping_city',''),
            request.form.get('shipping_state',''),
            request.form.get('shipping_zip',''),
            request.form.get('shipping_country','USA'),
        ))
        cid = cur.lastrowid
        conn.commit()
        conn.close()
        flash(f'Customer created.', 'success')
        return redirect(url_for('customer_view', cid=cid))
    conn = get_db()
    vendors = conn.execute('SELECT id, name FROM vendors ORDER BY name').fetchall()
    conn.close()
    return render_template('customer_form.html', c=None, vendors=vendors, contacts=[])


@app.route('/customers/<int:cid>')
@login_required
def customer_view(cid):
    conn     = get_db()
    c        = conn.execute('SELECT * FROM customers WHERE id=?', (cid,)).fetchone()
    contacts = conn.execute("SELECT * FROM contacts WHERE entity_type='customer' AND entity_id=? ORDER BY is_primary DESC", (cid,)).fetchall()
    vendors  = conn.execute('SELECT id, name FROM vendors ORDER BY name').fetchall()
    conn.close()
    if not c:
        flash('Customer not found.', 'error')
        return redirect(url_for('customer_list'))
    return render_template('customer_form.html', c=c, vendors=vendors, contacts=contacts)


@app.route('/customers/<int:cid>/save', methods=['POST'])
@login_required
def customer_save(cid):
    conn = get_db()
    conn.execute('''
        UPDATE customers SET
          name=?, phone=?, fax=?, email=?, payment_method=?, payment_terms=?, credit_limit=?,
          hourly_rate=?, tax_id=?, tax_percent=?, vat_number=?, date_format=?,
          sales_person=?, purchasing_person=?, customer_service_rep=?, shipping_service=?,
          status=?, required_part_categories=?, currency=?, tags=?,
          statement_notes=?, invoice_notes=?, notes=?, related_vendor_id=?,
          billing_name=?, billing_address=?, billing_city=?, billing_state=?, billing_zip=?, billing_country=?,
          shipping_name=?, shipping_address=?, shipping_city=?, shipping_state=?, shipping_zip=?, shipping_country=?
        WHERE id=?
    ''', (
        request.form.get('name',''),
        request.form.get('phone',''),
        request.form.get('fax',''),
        request.form.get('email',''),
        request.form.get('payment_method','Check'),
        request.form.get('payment_terms','COD'),
        float(request.form.get('credit_limit',0) or 0),
        float(request.form.get('hourly_rate',100) or 100),
        request.form.get('tax_id',''),
        float(request.form.get('tax_percent',0) or 0),
        request.form.get('vat_number',''),
        request.form.get('date_format','mm-yyyy'),
        request.form.get('sales_person',''),
        request.form.get('purchasing_person',''),
        request.form.get('customer_service_rep',''),
        request.form.get('shipping_service',''),
        request.form.get('status','Active'),
        request.form.get('required_part_categories',''),
        request.form.get('currency','USD'),
        request.form.get('tags',''),
        request.form.get('statement_notes',''),
        request.form.get('invoice_notes',''),
        request.form.get('notes',''),
        request.form.get('related_vendor_id') or None,
        request.form.get('billing_name',''),
        request.form.get('billing_address',''),
        request.form.get('billing_city',''),
        request.form.get('billing_state',''),
        request.form.get('billing_zip',''),
        request.form.get('billing_country','USA'),
        request.form.get('shipping_name',''),
        request.form.get('shipping_address',''),
        request.form.get('shipping_city',''),
        request.form.get('shipping_state',''),
        request.form.get('shipping_zip',''),
        request.form.get('shipping_country','USA'),
        cid
    ))
    conn.commit()
    conn.close()
    flash('Customer saved.', 'success')
    return redirect(url_for('customer_view', cid=cid))


@app.route('/customers/<int:cid>/delete', methods=['POST'])
@login_required
def customer_delete(cid):
    conn = get_db()
    conn.execute("DELETE FROM contacts WHERE entity_type='customer' AND entity_id=?", (cid,))
    conn.execute('DELETE FROM customers WHERE id=?', (cid,))
    conn.commit()
    conn.close()
    flash('Customer deleted.', 'success')
    return redirect(url_for('customer_list'))


# ─── Vendors ──────────────────────────────────────────────────────────────────

@app.route('/vendors')
@login_required
def vendor_list():
    conn = get_db()
    q    = request.args.get('q','').strip()
    if q:
        vendors = conn.execute(
            "SELECT * FROM vendors WHERE name LIKE ? OR email LIKE ? OR phone LIKE ? ORDER BY name",
            (f'%{q}%', f'%{q}%', f'%{q}%')
        ).fetchall()
    else:
        vendors = conn.execute('SELECT * FROM vendors ORDER BY name').fetchall()
    conn.close()
    return render_template('vendor_list.html', vendors=vendors, q=q)


@app.route('/vendors/new', methods=['GET', 'POST'])
@login_required
def vendor_new():
    if request.method == 'POST':
        conn = get_db()
        cur = conn.execute('''
            INSERT INTO vendors
              (name, phone, fax, email, website, payment_method, terms, credit_limit,
               account_number, min_po, tax_id, tax_percent, gl_account, status, currency, tags, notes,
               billing_name, billing_address, billing_city, billing_state, billing_zip, billing_country,
               shipping_name, shipping_address, shipping_city, shipping_state, shipping_zip, shipping_country)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ''', (
            request.form.get('name',''),
            request.form.get('phone',''),
            request.form.get('fax',''),
            request.form.get('email',''),
            request.form.get('website',''),
            request.form.get('payment_method','Check'),
            request.form.get('terms','30 days'),
            float(request.form.get('credit_limit',0) or 0),
            request.form.get('account_number',''),
            float(request.form.get('min_po',0) or 0),
            request.form.get('tax_id',''),
            float(request.form.get('tax_percent',0) or 0),
            request.form.get('gl_account','1200 | Inventory - rotables'),
            request.form.get('status','Active'),
            request.form.get('currency','USD'),
            request.form.get('tags',''),
            request.form.get('notes',''),
            request.form.get('billing_name',''),
            request.form.get('billing_address',''),
            request.form.get('billing_city',''),
            request.form.get('billing_state',''),
            request.form.get('billing_zip',''),
            request.form.get('billing_country','USA'),
            request.form.get('shipping_name',''),
            request.form.get('shipping_address',''),
            request.form.get('shipping_city',''),
            request.form.get('shipping_state',''),
            request.form.get('shipping_zip',''),
            request.form.get('shipping_country','USA'),
        ))
        vid = cur.lastrowid
        conn.commit()
        conn.close()
        flash('Vendor created.', 'success')
        return redirect(url_for('vendor_view', vid=vid))
    return render_template('vendor_form.html', v=None, contacts=[])


@app.route('/vendors/<int:vid>')
@login_required
def vendor_view(vid):
    conn     = get_db()
    v        = conn.execute('SELECT * FROM vendors WHERE id=?', (vid,)).fetchone()
    contacts = conn.execute("SELECT * FROM contacts WHERE entity_type='vendor' AND entity_id=? ORDER BY is_primary DESC", (vid,)).fetchall()
    conn.close()
    if not v:
        flash('Vendor not found.', 'error')
        return redirect(url_for('vendor_list'))
    return render_template('vendor_form.html', v=v, contacts=contacts)


@app.route('/vendors/<int:vid>/save', methods=['POST'])
@login_required
def vendor_save(vid):
    conn = get_db()
    conn.execute('''
        UPDATE vendors SET
          name=?, phone=?, fax=?, email=?, website=?, payment_method=?, terms=?, credit_limit=?,
          account_number=?, min_po=?, tax_id=?, tax_percent=?, gl_account=?, status=?, currency=?, tags=?, notes=?,
          billing_name=?, billing_address=?, billing_city=?, billing_state=?, billing_zip=?, billing_country=?,
          shipping_name=?, shipping_address=?, shipping_city=?, shipping_state=?, shipping_zip=?, shipping_country=?
        WHERE id=?
    ''', (
        request.form.get('name',''),
        request.form.get('phone',''),
        request.form.get('fax',''),
        request.form.get('email',''),
        request.form.get('website',''),
        request.form.get('payment_method','Check'),
        request.form.get('terms','30 days'),
        float(request.form.get('credit_limit',0) or 0),
        request.form.get('account_number',''),
        float(request.form.get('min_po',0) or 0),
        request.form.get('tax_id',''),
        float(request.form.get('tax_percent',0) or 0),
        request.form.get('gl_account','1200 | Inventory - rotables'),
        request.form.get('status','Active'),
        request.form.get('currency','USD'),
        request.form.get('tags',''),
        request.form.get('notes',''),
        request.form.get('billing_name',''),
        request.form.get('billing_address',''),
        request.form.get('billing_city',''),
        request.form.get('billing_state',''),
        request.form.get('billing_zip',''),
        request.form.get('billing_country','USA'),
        request.form.get('shipping_name',''),
        request.form.get('shipping_address',''),
        request.form.get('shipping_city',''),
        request.form.get('shipping_state',''),
        request.form.get('shipping_zip',''),
        request.form.get('shipping_country','USA'),
        vid
    ))
    conn.commit()
    conn.close()
    flash('Vendor saved.', 'success')
    return redirect(url_for('vendor_view', vid=vid))


@app.route('/vendors/<int:vid>/delete', methods=['POST'])
@login_required
def vendor_delete(vid):
    conn = get_db()
    conn.execute("DELETE FROM contacts WHERE entity_type='vendor' AND entity_id=?", (vid,))
    conn.execute('DELETE FROM vendors WHERE id=?', (vid,))
    conn.commit()
    conn.close()
    flash('Vendor deleted.', 'success')
    return redirect(url_for('vendor_list'))


# ─── Contacts API (shared by customers and vendors) ───────────────────────────

@app.route('/api/contacts/save', methods=['POST'])
@login_required
def api_contact_save():
    data = request.get_json(force=True)
    entity_type = data.get('entity_type')
    entity_id   = data.get('entity_id')
    contact_id  = data.get('id')
    conn = get_db()
    if contact_id:
        conn.execute('''
            UPDATE contacts SET first_name=?, last_name=?, title=?, email=?, phone=?, mobile=?, is_primary=?, notes=?
            WHERE id=? AND entity_type=? AND entity_id=?
        ''', (data.get('first_name',''), data.get('last_name',''), data.get('title',''),
              data.get('email',''), data.get('phone',''), data.get('mobile',''),
              1 if data.get('is_primary') else 0, data.get('notes',''),
              contact_id, entity_type, entity_id))
    else:
        cur = conn.execute('''
            INSERT INTO contacts (entity_type, entity_id, first_name, last_name, title, email, phone, mobile, is_primary, notes)
            VALUES (?,?,?,?,?,?,?,?,?,?)
        ''', (entity_type, entity_id, data.get('first_name',''), data.get('last_name',''),
              data.get('title',''), data.get('email',''), data.get('phone',''),
              data.get('mobile',''), 1 if data.get('is_primary') else 0, data.get('notes','')))
        contact_id = cur.lastrowid
    conn.commit()
    conn.close()
    return jsonify({'ok': True, 'id': contact_id})


@app.route('/api/contacts/delete', methods=['POST'])
@login_required
def api_contact_delete():
    data = request.get_json(force=True)
    conn = get_db()
    conn.execute('DELETE FROM contacts WHERE id=?', (data.get('id'),))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})


@app.route('/api/vendors-json')
@login_required
def api_vendors_json():
    conn = get_db()
    rows = conn.execute('SELECT id, name, billing_name, billing_address, billing_city, billing_state, billing_zip, billing_country FROM vendors WHERE status=\'Active\' ORDER BY name').fetchall()
    conn.close()
    def addr(r):
        parts = []
        if r['billing_name']:    parts.append(r['billing_name'])
        if r['billing_address']: parts.append(r['billing_address'])
        city_line = ', '.join(filter(None, [r['billing_city'], (r['billing_state'] or '') + ' ' + (r['billing_zip'] or '')]))
        if city_line.strip(): parts.append(city_line.strip())
        if r['billing_country'] and r['billing_country'] != 'USA': parts.append(r['billing_country'])
        return '\n'.join(parts)
    return jsonify([{'id': r['id'], 'name': r['name'], 'address': addr(r)} for r in rows])


@app.route('/api/customers-json')
@login_required
def api_customers_json():
    conn = get_db()
    rows = conn.execute('SELECT id, name, billing_name, billing_address, billing_city, billing_state, billing_zip, billing_country, email, phone FROM customers WHERE status=\'Active\' ORDER BY name').fetchall()
    conn.close()
    def addr(r):
        parts = []
        if r['billing_name']:    parts.append(r['billing_name'])
        if r['billing_address']: parts.append(r['billing_address'])
        city_line = ', '.join(filter(None, [r['billing_city'], (r['billing_state'] or '') + ' ' + (r['billing_zip'] or '')]))
        if city_line.strip(): parts.append(city_line.strip())
        if r['billing_country'] and r['billing_country'] != 'USA': parts.append(r['billing_country'])
        return '\n'.join(parts)
    return jsonify([{'id': r['id'], 'name': r['name'], 'address': addr(r), 'email': r['email'] or '', 'phone': r['phone'] or ''} for r in rows])


@app.route('/api/invoice-items/<int:inv_id>')
@login_required
def api_invoice_items(inv_id):
    conn  = get_db()
    inv   = conn.execute('SELECT * FROM invoices WHERE id=?', (inv_id,)).fetchone()
    items = conn.execute('SELECT * FROM invoice_items WHERE invoice_id=?', (inv_id,)).fetchall()
    conn.close()
    if not inv:
        return jsonify({'ok': False}), 404
    return jsonify({
        'ok': True,
        'invoice_number': inv['invoice_number'],
        'customer_name': inv['customer_name'] or inv['invoice_for'] or '',
        'customer_address': inv['customer_address'] or '',
        'items': [{'part_number': i['part_number'], 'description': i['description'],
                   'condition': i['condition'], 'quantity': i['quantity']} for i in items]
    })


# ─── ERP: Helpers ────────────────────────────────────────────────────────────

def _next_erp_number(prefix, table, col):
    """Generate next sequential document number like PO-2024-0001."""
    year = datetime.now().year
    conn = get_db()
    pattern = f'{prefix}-{year}-%'
    row = conn.execute(
        f"SELECT {col} FROM {table} WHERE {col} LIKE ? ORDER BY {col} DESC LIMIT 1",
        (pattern,)
    ).fetchone()
    conn.close()
    if row and row[0]:
        try:
            seq = int(row[0].split('-')[-1]) + 1
        except Exception:
            seq = 1
    else:
        seq = 1
    return f'{prefix}-{year}-{seq:04d}'


# ─── ERP: Purchase Orders ─────────────────────────────────────────────────────

@app.route('/purchase-orders')
@login_required
def po_list():
    conn  = get_db()
    pos   = conn.execute('SELECT * FROM purchase_orders ORDER BY created_at DESC').fetchall()
    conn.close()
    return render_template('po_list.html', pos=pos)


@app.route('/purchase-orders/new', methods=['GET', 'POST'])
@login_required
def po_new():
    if request.method == 'POST':
        try:
            po_number = _next_erp_number('PO', 'purchase_orders', 'po_number')
            conn = get_db()
            cur = conn.execute('''
                INSERT INTO purchase_orders
                  (po_number, vendor_name, vendor_address, ship_to_name, ship_to_address,
                   date, ship_date, terms, ship_via, shipping_account,
                   subtotal, shipping, tax_rate, sales_tax, grand_total, notes, status)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ''', (
                po_number,
                request.form.get('vendor_name',''),
                request.form.get('vendor_address',''),
                request.form.get('ship_to_name','Eastern Aero Pty Ltd'),
                request.form.get('ship_to_address','10 Composure St\nMorayfield QLD 4506 Australia'),
                request.form.get('date',''),
                request.form.get('ship_date',''),
                request.form.get('terms','Net 30'),
                request.form.get('ship_via',''),
                request.form.get('shipping_account',''),
                float(request.form.get('subtotal') or 0),
                float(request.form.get('shipping') or 0),
                float(request.form.get('tax_rate') or 0),
                float(request.form.get('sales_tax') or 0),
                float(request.form.get('grand_total') or 0),
                request.form.get('notes',''),
                'draft'
            ))
            po_id = cur.lastrowid
            pns   = request.form.getlist('pn[]')
            descs = request.form.getlist('desc[]')
            sns   = request.form.getlist('sn[]')
            conds = request.form.getlist('cond[]')
            qtys  = request.form.getlist('qty[]')
            ups   = request.form.getlist('unit_price[]')
            tots  = request.form.getlist('total_price[]')
            for i, pn in enumerate(pns):
                if not pn.strip():
                    continue
                conn.execute(
                    'INSERT INTO po_items (po_id,part_number,description,serial_number,condition,quantity,unit_price,total_price) VALUES (?,?,?,?,?,?,?,?)',
                    (po_id, pn.strip(),
                     descs[i] if i < len(descs) else '',
                     sns[i] if i < len(sns) else '',
                     conds[i] if i < len(conds) else '',
                     float(qtys[i] or 0) if i < len(qtys) else 1,
                     float(ups[i] or 0) if i < len(ups) else 0,
                     float(tots[i] or 0) if i < len(tots) else 0)
                )
            conn.commit()
            conn.close()
            flash(f'Purchase Order {po_number} created.', 'success')
            return redirect(url_for('po_view', po_id=po_id))
        except Exception as e:
            app.logger.error('po_new error: %s\n%s', e, traceback.format_exc())
            flash(f'Error creating Purchase Order: {e}', 'danger')
    try:
        conn = get_db()
        vendors = conn.execute('SELECT id, name, billing_name, billing_address, billing_city, billing_state, billing_zip FROM vendors ORDER BY name').fetchall()
        conn.close()
    except Exception:
        vendors = []
    return render_template('po_form.html', po=None, items=[], vendors=vendors)


@app.route('/purchase-orders/<int:po_id>')
@login_required
def po_view(po_id):
    conn  = get_db()
    po    = conn.execute('SELECT * FROM purchase_orders WHERE id=?', (po_id,)).fetchone()
    items = conn.execute('SELECT * FROM po_items WHERE po_id=?', (po_id,)).fetchall()
    conn.close()
    if not po:
        flash('Purchase Order not found.', 'error')
        return redirect(url_for('po_list'))
    s = get_settings()
    return render_template('po_view.html', po=po, items=items, s=s)


@app.route('/purchase-orders/<int:po_id>/edit', methods=['GET', 'POST'])
@login_required
def po_edit(po_id):
    conn = get_db()
    po   = conn.execute('SELECT * FROM purchase_orders WHERE id=?', (po_id,)).fetchone()
    if not po:
        conn.close()
        flash('Purchase Order not found.', 'error')
        return redirect(url_for('po_list'))
    if request.method == 'POST':
        conn.execute('''
            UPDATE purchase_orders SET
              vendor_name=?, vendor_address=?, ship_to_name=?, ship_to_address=?,
              date=?, ship_date=?, terms=?, ship_via=?, shipping_account=?,
              subtotal=?, shipping=?, tax_rate=?, sales_tax=?, grand_total=?, notes=?
            WHERE id=?
        ''', (
            request.form.get('vendor_name',''),
            request.form.get('vendor_address',''),
            request.form.get('ship_to_name',''),
            request.form.get('ship_to_address',''),
            request.form.get('date',''),
            request.form.get('ship_date',''),
            request.form.get('terms','Net 30'),
            request.form.get('ship_via',''),
            request.form.get('shipping_account',''),
            float(request.form.get('subtotal',0) or 0),
            float(request.form.get('shipping',0) or 0),
            float(request.form.get('tax_rate',0) or 0),
            float(request.form.get('sales_tax',0) or 0),
            float(request.form.get('grand_total',0) or 0),
            request.form.get('notes',''),
            po_id
        ))
        conn.execute('DELETE FROM po_items WHERE po_id=?', (po_id,))
        pns   = request.form.getlist('pn[]')
        descs = request.form.getlist('desc[]')
        sns   = request.form.getlist('sn[]')
        conds = request.form.getlist('cond[]')
        qtys  = request.form.getlist('qty[]')
        ups   = request.form.getlist('unit_price[]')
        tots  = request.form.getlist('total_price[]')
        for i, pn in enumerate(pns):
            if not pn.strip():
                continue
            conn.execute(
                'INSERT INTO po_items (po_id,part_number,description,serial_number,condition,quantity,unit_price,total_price) VALUES (?,?,?,?,?,?,?,?)',
                (po_id, pn.strip(), descs[i] if i < len(descs) else '',
                 sns[i] if i < len(sns) else '',
                 conds[i] if i < len(conds) else '',
                 float(qtys[i]) if i < len(qtys) and qtys[i] else 1,
                 float(ups[i]) if i < len(ups) and ups[i] else 0,
                 float(tots[i]) if i < len(tots) and tots[i] else 0)
            )
        conn.commit()
        conn.close()
        flash('Purchase Order updated.', 'success')
        return redirect(url_for('po_view', po_id=po_id))
    items = conn.execute('SELECT * FROM po_items WHERE po_id=?', (po_id,)).fetchall()
    vendors = conn.execute('SELECT id, name, billing_name, billing_address, billing_city, billing_state, billing_zip FROM vendors ORDER BY name').fetchall()
    conn.close()
    return render_template('po_form.html', po=po, items=items, vendors=vendors)


@app.route('/purchase-orders/<int:po_id>/delete', methods=['POST'])
@login_required
def po_delete(po_id):
    conn = get_db()
    conn.execute('DELETE FROM po_items WHERE po_id=?', (po_id,))
    conn.execute('DELETE FROM purchase_orders WHERE id=?', (po_id,))
    conn.commit()
    conn.close()
    flash('Purchase Order deleted.', 'success')
    return redirect(url_for('po_list'))


# ─── ERP: Invoices ────────────────────────────────────────────────────────────

@app.route('/invoices')
@login_required
def invoice_list():
    conn     = get_db()
    invoices = conn.execute('SELECT * FROM invoices ORDER BY created_at DESC').fetchall()
    conn.close()
    return render_template('invoice_list.html', invoices=invoices)


@app.route('/invoices/new', methods=['GET', 'POST'])
@login_required
def invoice_new():
    if request.method == 'POST':
        try:
            inv_number = _next_erp_number('INV', 'invoices', 'invoice_number')
            conn = get_db()
            cur = conn.execute('''
                INSERT INTO invoices
                  (invoice_number, invoice_for, customer_name, customer_address,
                   reference, due_date, subtotal, adjustments, grand_total, notes, status)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)
            ''', (
                inv_number,
                request.form.get('invoice_for',''),
                request.form.get('customer_name',''),
                request.form.get('customer_address',''),
                request.form.get('reference',''),
                request.form.get('due_date',''),
                float(request.form.get('subtotal') or 0),
                float(request.form.get('adjustments') or 0),
                float(request.form.get('grand_total') or 0),
                request.form.get('notes',''),
                'draft'
            ))
            inv_id = cur.lastrowid
            pns   = request.form.getlist('pn[]')
            descs = request.form.getlist('desc[]')
            sns   = request.form.getlist('sn[]')
            conds = request.form.getlist('cond[]')
            qtys  = request.form.getlist('qty[]')
            ups   = request.form.getlist('unit_price[]')
            tots  = request.form.getlist('total_price[]')
            for i, pn in enumerate(pns):
                if not pn.strip():
                    continue
                conn.execute(
                    'INSERT INTO invoice_items (invoice_id,part_number,description,serial_number,condition,quantity,unit_price,total_price) VALUES (?,?,?,?,?,?,?,?)',
                    (inv_id, pn.strip(),
                     descs[i] if i < len(descs) else '',
                     sns[i] if i < len(sns) else '',
                     conds[i] if i < len(conds) else '',
                     float(qtys[i] or 0) if i < len(qtys) else 1,
                     float(ups[i] or 0) if i < len(ups) else 0,
                     float(tots[i] or 0) if i < len(tots) else 0)
                )
            conn.commit()
            conn.close()
            flash(f'Invoice {inv_number} created.', 'success')
            return redirect(url_for('invoice_view', inv_id=inv_id))
        except Exception as e:
            app.logger.error('invoice_new error: %s\n%s', e, traceback.format_exc())
            flash(f'Error creating Invoice: {e}', 'danger')
    try:
        conn = get_db()
        customers = conn.execute('SELECT id, name, billing_name, billing_address, billing_city, billing_state, billing_zip FROM customers ORDER BY name').fetchall()
        conn.close()
    except Exception:
        customers = []
    return render_template('invoice_form.html', inv=None, items=[], customers=customers)


@app.route('/invoices/<int:inv_id>')
@login_required
def invoice_view(inv_id):
    conn  = get_db()
    inv   = conn.execute('SELECT * FROM invoices WHERE id=?', (inv_id,)).fetchone()
    items = conn.execute('SELECT * FROM invoice_items WHERE invoice_id=?', (inv_id,)).fetchall()
    attachments = conn.execute('SELECT * FROM invoice_attachments WHERE invoice_id=? ORDER BY uploaded_at', (inv_id,)).fetchall()
    conn.close()
    if not inv:
        flash('Invoice not found.', 'error')
        return redirect(url_for('invoice_list'))
    s = get_settings()
    return render_template('invoice_view.html', inv=inv, items=items, s=s, attachments=attachments)


@app.route('/invoices/<int:inv_id>/edit', methods=['GET', 'POST'])
@login_required
def invoice_edit(inv_id):
    conn = get_db()
    inv  = conn.execute('SELECT * FROM invoices WHERE id=?', (inv_id,)).fetchone()
    if not inv:
        conn.close()
        flash('Invoice not found.', 'error')
        return redirect(url_for('invoice_list'))
    if request.method == 'POST':
        conn.execute('''
            UPDATE invoices SET
              invoice_for=?, customer_name=?, customer_address=?,
              reference=?, due_date=?, subtotal=?, adjustments=?, grand_total=?, notes=?
            WHERE id=?
        ''', (
            request.form.get('invoice_for',''),
            request.form.get('customer_name',''),
            request.form.get('customer_address',''),
            request.form.get('reference',''),
            request.form.get('due_date',''),
            float(request.form.get('subtotal',0) or 0),
            float(request.form.get('adjustments',0) or 0),
            float(request.form.get('grand_total',0) or 0),
            request.form.get('notes',''),
            inv_id
        ))
        conn.execute('DELETE FROM invoice_items WHERE invoice_id=?', (inv_id,))
        pns   = request.form.getlist('pn[]')
        descs = request.form.getlist('desc[]')
        sns   = request.form.getlist('sn[]')
        conds = request.form.getlist('cond[]')
        qtys  = request.form.getlist('qty[]')
        ups   = request.form.getlist('unit_price[]')
        tots  = request.form.getlist('total_price[]')
        for i, pn in enumerate(pns):
            if not pn.strip():
                continue
            conn.execute(
                'INSERT INTO invoice_items (invoice_id,part_number,description,serial_number,condition,quantity,unit_price,total_price) VALUES (?,?,?,?,?,?,?,?)',
                (inv_id, pn.strip(), descs[i] if i < len(descs) else '',
                 sns[i] if i < len(sns) else '',
                 conds[i] if i < len(conds) else '',
                 float(qtys[i]) if i < len(qtys) and qtys[i] else 1,
                 float(ups[i]) if i < len(ups) and ups[i] else 0,
                 float(tots[i]) if i < len(tots) and tots[i] else 0)
            )
        conn.commit()
        conn.close()
        flash('Invoice updated.', 'success')
        return redirect(url_for('invoice_view', inv_id=inv_id))
    items = conn.execute('SELECT * FROM invoice_items WHERE invoice_id=?', (inv_id,)).fetchall()
    customers = conn.execute('SELECT id, name, billing_name, billing_address, billing_city, billing_state, billing_zip FROM customers ORDER BY name').fetchall()
    conn.close()
    return render_template('invoice_form.html', inv=inv, items=items, customers=customers)


@app.route('/invoices/<int:inv_id>/delete', methods=['POST'])
@login_required
def invoice_delete(inv_id):
    conn = get_db()
    # Also remove attachments from disk
    atts = conn.execute('SELECT filepath FROM invoice_attachments WHERE invoice_id=?', (inv_id,)).fetchall()
    for a in atts:
        try: os.remove(a['filepath'])
        except Exception: pass
    conn.execute('DELETE FROM invoice_attachments WHERE invoice_id=?', (inv_id,))
    conn.execute('DELETE FROM invoice_items WHERE invoice_id=?', (inv_id,))
    conn.execute('DELETE FROM invoices WHERE id=?', (inv_id,))
    conn.commit()
    conn.close()
    flash('Invoice deleted.', 'success')
    return redirect(url_for('invoice_list'))


# ─── Invoice: Customer PO Attachments ────────────────────────────────────────

_INV_ALLOWED = {'.pdf', '.jpg', '.jpeg', '.png', '.gif', '.tif', '.tiff', '.webp'}

@app.route('/api/invoice-attachments/<int:inv_id>')
@login_required
def api_invoice_attachments(inv_id):
    conn = get_db()
    atts = conn.execute(
        'SELECT id, label, filename, mimetype, uploaded_at FROM invoice_attachments WHERE invoice_id=? ORDER BY uploaded_at',
        (inv_id,)
    ).fetchall()
    conn.close()
    return jsonify({'attachments': [dict(a) for a in atts]})


@app.route('/invoices/<int:inv_id>/upload-po', methods=['POST'])
@login_required
def invoice_upload_po(inv_id):
    conn = get_db()
    inv = conn.execute('SELECT id FROM invoices WHERE id=?', (inv_id,)).fetchone()
    conn.close()
    if not inv:
        return jsonify({'success': False, 'error': 'Invoice not found'}), 404

    f = request.files.get('file')
    if not f or not f.filename:
        return jsonify({'success': False, 'error': 'No file uploaded'})

    ext = os.path.splitext(f.filename)[1].lower()
    if ext not in _INV_ALLOWED:
        return jsonify({'success': False, 'error': f'File type {ext} not allowed. Use PDF or image.'})

    label    = (request.form.get('label') or 'Customer PO').strip()
    safe_name = re.sub(r'[^\w\.\-]', '_', f.filename)
    upload_dir = os.path.join(UPLOAD_FOLDER, 'invoices', str(inv_id))
    os.makedirs(upload_dir, exist_ok=True)
    filepath = os.path.join(upload_dir, safe_name)
    f.save(filepath)

    mime = mimetypes.guess_type(safe_name)[0] or 'application/octet-stream'
    conn = get_db()
    conn.execute(
        'INSERT INTO invoice_attachments (invoice_id, label, filename, filepath, mimetype) VALUES (?,?,?,?,?)',
        (inv_id, label, safe_name, filepath, mime))
    conn.commit()
    att_id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
    conn.close()

    return jsonify({
        'success': True, 'id': att_id,
        'filename': safe_name, 'label': label, 'mime': mime,
    })


@app.route('/invoices/<int:inv_id>/po/<int:att_id>/view')
@login_required
def invoice_po_view(inv_id, att_id):
    conn = get_db()
    att = conn.execute(
        'SELECT * FROM invoice_attachments WHERE id=? AND invoice_id=?',
        (att_id, inv_id)).fetchone()
    conn.close()
    if not att or not os.path.exists(att['filepath']):
        flash('File not found.', 'error')
        return redirect(url_for('invoice_view', inv_id=inv_id))
    return send_file(att['filepath'], mimetype=att['mimetype'],
                     download_name=att['filename'], as_attachment=False)


@app.route('/invoices/<int:inv_id>/po/<int:att_id>/delete', methods=['POST'])
@login_required
def invoice_po_delete(inv_id, att_id):
    conn = get_db()
    att = conn.execute(
        'SELECT * FROM invoice_attachments WHERE id=? AND invoice_id=?',
        (att_id, inv_id)).fetchone()
    if att:
        try: os.remove(att['filepath'])
        except Exception: pass
        conn.execute('DELETE FROM invoice_attachments WHERE id=?', (att_id,))
        conn.commit()
    conn.close()
    return jsonify({'ok': True})


# ─── ERP: Packing Slips ───────────────────────────────────────────────────────

@app.route('/packing-slips')
@login_required
def ps_list():
    conn = get_db()
    slips = conn.execute('SELECT * FROM packing_slips ORDER BY created_at DESC').fetchall()
    conn.close()
    return render_template('ps_list.html', slips=slips)


@app.route('/packing-slips/new', methods=['GET', 'POST'])
@login_required
def ps_new():
    if request.method == 'POST':
        ps_number = _next_erp_number('PS', 'packing_slips', 'ps_number')
        conn = get_db()
        cur = conn.execute('''
            INSERT INTO packing_slips
              (ps_number, date, terms, po_number, invoice_number, ship_date, ship_via,
               shipping_account, vendor_name, vendor_address, ship_to_name, ship_to_address,
               notes, pallet_dims, weight_lbs, status)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ''', (
            ps_number,
            request.form.get('date', datetime.now().strftime('%Y-%m-%d')),
            request.form.get('terms',''),
            request.form.get('po_number',''),
            request.form.get('invoice_number',''),
            request.form.get('ship_date',''),
            request.form.get('ship_via',''),
            request.form.get('shipping_account',''),
            request.form.get('vendor_name',''),
            request.form.get('vendor_address',''),
            request.form.get('ship_to_name',''),
            request.form.get('ship_to_address',''),
            request.form.get('notes',''),
            request.form.get('pallet_dims',''),
            float(request.form.get('weight_lbs',0) or 0),
            'draft'
        ))
        ps_id = cur.lastrowid
        pns   = request.form.getlist('pn[]')
        descs = request.form.getlist('desc[]')
        sns   = request.form.getlist('sn[]')
        qtys  = request.form.getlist('qty[]')
        coos  = request.form.getlist('coo[]')
        hss   = request.form.getlist('hs[]')
        for i, pn in enumerate(pns):
            if not pn.strip():
                continue
            conn.execute(
                'INSERT INTO ps_items (ps_id,part_number,description,serial_number,quantity,country_of_origin,hs_code) VALUES (?,?,?,?,?,?,?)',
                (ps_id, pn.strip(),
                 descs[i] if i < len(descs) else '',
                 sns[i] if i < len(sns) else '',
                 float(qtys[i]) if i < len(qtys) and qtys[i] else 1,
                 coos[i] if i < len(coos) else 'USA',
                 hss[i] if i < len(hss) else '')
            )
        conn.commit()
        conn.close()
        flash(f'Packing Slip {ps_number} created.', 'success')
        return redirect(url_for('ps_view', ps_id=ps_id))
    try:
        conn = get_db()
        vendors  = conn.execute('SELECT id, name, billing_name, billing_address, billing_city, billing_state, billing_zip FROM vendors ORDER BY name').fetchall()
        invoices = conn.execute("SELECT id, invoice_number, invoice_for, customer_name FROM invoices ORDER BY created_at DESC").fetchall()
        conn.close()
    except Exception:
        vendors, invoices = [], []
    return render_template('ps_form.html', ps=None, items=[], vendors=vendors, invoices=invoices)


@app.route('/packing-slips/<int:ps_id>')
@login_required
def ps_view(ps_id):
    conn  = get_db()
    ps    = conn.execute('SELECT * FROM packing_slips WHERE id=?', (ps_id,)).fetchone()
    items = conn.execute('SELECT * FROM ps_items WHERE ps_id=?', (ps_id,)).fetchall()
    conn.close()
    if not ps:
        flash('Packing Slip not found.', 'error')
        return redirect(url_for('ps_list'))
    s = get_settings()
    return render_template('ps_view.html', ps=ps, items=items, s=s)


@app.route('/packing-slips/<int:ps_id>/edit', methods=['GET', 'POST'])
@login_required
def ps_edit(ps_id):
    conn = get_db()
    ps   = conn.execute('SELECT * FROM packing_slips WHERE id=?', (ps_id,)).fetchone()
    if not ps:
        conn.close()
        flash('Packing Slip not found.', 'error')
        return redirect(url_for('ps_list'))
    if request.method == 'POST':
        conn.execute('''
            UPDATE packing_slips SET
              date=?, terms=?, po_number=?, invoice_number=?, ship_date=?, ship_via=?,
              shipping_account=?, vendor_name=?, vendor_address=?, ship_to_name=?,
              ship_to_address=?, notes=?, pallet_dims=?, weight_lbs=?
            WHERE id=?
        ''', (
            request.form.get('date',''),
            request.form.get('terms',''),
            request.form.get('po_number',''),
            request.form.get('invoice_number',''),
            request.form.get('ship_date',''),
            request.form.get('ship_via',''),
            request.form.get('shipping_account',''),
            request.form.get('vendor_name',''),
            request.form.get('vendor_address',''),
            request.form.get('ship_to_name',''),
            request.form.get('ship_to_address',''),
            request.form.get('notes',''),
            request.form.get('pallet_dims',''),
            float(request.form.get('weight_lbs',0) or 0),
            ps_id
        ))
        conn.execute('DELETE FROM ps_items WHERE ps_id=?', (ps_id,))
        pns   = request.form.getlist('pn[]')
        descs = request.form.getlist('desc[]')
        sns   = request.form.getlist('sn[]')
        qtys  = request.form.getlist('qty[]')
        coos  = request.form.getlist('coo[]')
        hss   = request.form.getlist('hs[]')
        for i, pn in enumerate(pns):
            if not pn.strip():
                continue
            conn.execute(
                'INSERT INTO ps_items (ps_id,part_number,description,serial_number,quantity,country_of_origin,hs_code) VALUES (?,?,?,?,?,?,?)',
                (ps_id, pn.strip(),
                 descs[i] if i < len(descs) else '',
                 sns[i] if i < len(sns) else '',
                 float(qtys[i]) if i < len(qtys) and qtys[i] else 1,
                 coos[i] if i < len(coos) else 'USA',
                 hss[i] if i < len(hss) else '')
            )
        conn.commit()
        conn.close()
        flash('Packing Slip updated.', 'success')
        return redirect(url_for('ps_view', ps_id=ps_id))
    items    = conn.execute('SELECT * FROM ps_items WHERE ps_id=?', (ps_id,)).fetchall()
    vendors  = conn.execute('SELECT id, name, billing_name, billing_address, billing_city, billing_state, billing_zip FROM vendors ORDER BY name').fetchall()
    invoices = conn.execute("SELECT id, invoice_number, invoice_for, customer_name FROM invoices ORDER BY created_at DESC").fetchall()
    conn.close()
    return render_template('ps_form.html', ps=ps, items=items, vendors=vendors, invoices=invoices)


@app.route('/packing-slips/<int:ps_id>/delete', methods=['POST'])
@login_required
def ps_delete(ps_id):
    conn = get_db()
    conn.execute('DELETE FROM ps_items WHERE ps_id=?', (ps_id,))
    conn.execute('DELETE FROM packing_slips WHERE id=?', (ps_id,))
    conn.commit()
    conn.close()
    flash('Packing Slip deleted.', 'success')
    return redirect(url_for('ps_list'))


# ─── ERP: Repair Orders ───────────────────────────────────────────────────────

@app.route('/repair-orders')
@login_required
def ro_list():
    conn = get_db()
    ros  = conn.execute('SELECT * FROM repair_orders ORDER BY created_at DESC').fetchall()
    conn.close()
    return render_template('ro_list.html', ros=ros)


@app.route('/repair-orders/new', methods=['GET', 'POST'])
@login_required
def ro_new():
    if request.method == 'POST':
        ro_number = _next_erp_number('RO', 'repair_orders', 'ro_number')
        conn = get_db()
        cur = conn.execute('''
            INSERT INTO repair_orders
              (ro_number, vendor_name, vendor_address, ship_to_name, ship_to_address,
               date, ship_date, terms, ship_via, shipping_account,
               subtotal, shipping, tax_rate, sales_tax, grand_total, notes, status)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ''', (
            ro_number,
            request.form.get('vendor_name',''),
            request.form.get('vendor_address',''),
            request.form.get('ship_to_name','Eastern Aero Pty Ltd'),
            request.form.get('ship_to_address','10 Composure St\nMorayfield QLD 4506 Australia'),
            request.form.get('date', datetime.now().strftime('%Y-%m-%d')),
            request.form.get('ship_date',''),
            request.form.get('terms','Net 30'),
            request.form.get('ship_via',''),
            request.form.get('shipping_account',''),
            float(request.form.get('subtotal',0) or 0),
            float(request.form.get('shipping',0) or 0),
            float(request.form.get('tax_rate',0) or 0),
            float(request.form.get('sales_tax',0) or 0),
            float(request.form.get('grand_total',0) or 0),
            request.form.get('notes',''),
            'draft'
        ))
        ro_id = cur.lastrowid
        pns   = request.form.getlist('pn[]')
        descs = request.form.getlist('desc[]')
        sns   = request.form.getlist('sn[]')
        qtys  = request.form.getlist('qty[]')
        works = request.form.getlist('work[]')
        costs = request.form.getlist('cost[]')
        tots  = request.form.getlist('total_price[]')
        for i, pn in enumerate(pns):
            if not pn.strip():
                continue
            conn.execute(
                'INSERT INTO ro_items (ro_id,part_number,description,serial_number,quantity,work_requested,avg_cost,total_price) VALUES (?,?,?,?,?,?,?,?)',
                (ro_id, pn.strip(),
                 descs[i] if i < len(descs) else '',
                 sns[i] if i < len(sns) else '',
                 float(qtys[i]) if i < len(qtys) and qtys[i] else 1,
                 works[i] if i < len(works) else '',
                 float(costs[i]) if i < len(costs) and costs[i] else 0,
                 float(tots[i]) if i < len(tots) and tots[i] else 0)
            )
        conn.commit()
        conn.close()
        flash(f'Repair Order {ro_number} created.', 'success')
        return redirect(url_for('ro_view', ro_id=ro_id))
    try:
        conn = get_db()
        vendors = conn.execute('SELECT id, name, billing_name, billing_address, billing_city, billing_state, billing_zip FROM vendors ORDER BY name').fetchall()
        conn.close()
    except Exception:
        vendors = []
    return render_template('ro_form.html', ro=None, items=[], vendors=vendors)


@app.route('/repair-orders/<int:ro_id>')
@login_required
def ro_view(ro_id):
    conn  = get_db()
    ro    = conn.execute('SELECT * FROM repair_orders WHERE id=?', (ro_id,)).fetchone()
    items = conn.execute('SELECT * FROM ro_items WHERE ro_id=?', (ro_id,)).fetchall()
    conn.close()
    if not ro:
        flash('Repair Order not found.', 'error')
        return redirect(url_for('ro_list'))
    s = get_settings()
    return render_template('ro_view.html', ro=ro, items=items, s=s)


@app.route('/repair-orders/<int:ro_id>/edit', methods=['GET', 'POST'])
@login_required
def ro_edit(ro_id):
    conn = get_db()
    ro   = conn.execute('SELECT * FROM repair_orders WHERE id=?', (ro_id,)).fetchone()
    if not ro:
        conn.close()
        flash('Repair Order not found.', 'error')
        return redirect(url_for('ro_list'))
    if request.method == 'POST':
        conn.execute('''
            UPDATE repair_orders SET
              vendor_name=?, vendor_address=?, ship_to_name=?, ship_to_address=?,
              date=?, ship_date=?, terms=?, ship_via=?, shipping_account=?,
              subtotal=?, shipping=?, tax_rate=?, sales_tax=?, grand_total=?, notes=?
            WHERE id=?
        ''', (
            request.form.get('vendor_name',''),
            request.form.get('vendor_address',''),
            request.form.get('ship_to_name',''),
            request.form.get('ship_to_address',''),
            request.form.get('date',''),
            request.form.get('ship_date',''),
            request.form.get('terms','Net 30'),
            request.form.get('ship_via',''),
            request.form.get('shipping_account',''),
            float(request.form.get('subtotal',0) or 0),
            float(request.form.get('shipping',0) or 0),
            float(request.form.get('tax_rate',0) or 0),
            float(request.form.get('sales_tax',0) or 0),
            float(request.form.get('grand_total',0) or 0),
            request.form.get('notes',''),
            ro_id
        ))
        conn.execute('DELETE FROM ro_items WHERE ro_id=?', (ro_id,))
        pns   = request.form.getlist('pn[]')
        descs = request.form.getlist('desc[]')
        sns   = request.form.getlist('sn[]')
        qtys  = request.form.getlist('qty[]')
        works = request.form.getlist('work[]')
        costs = request.form.getlist('cost[]')
        tots  = request.form.getlist('total_price[]')
        for i, pn in enumerate(pns):
            if not pn.strip():
                continue
            conn.execute(
                'INSERT INTO ro_items (ro_id,part_number,description,serial_number,quantity,work_requested,avg_cost,total_price) VALUES (?,?,?,?,?,?,?,?)',
                (ro_id, pn.strip(),
                 descs[i] if i < len(descs) else '',
                 sns[i] if i < len(sns) else '',
                 float(qtys[i]) if i < len(qtys) and qtys[i] else 1,
                 works[i] if i < len(works) else '',
                 float(costs[i]) if i < len(costs) and costs[i] else 0,
                 float(tots[i]) if i < len(tots) and tots[i] else 0)
            )
        conn.commit()
        conn.close()
        flash('Repair Order updated.', 'success')
        return redirect(url_for('ro_view', ro_id=ro_id))
    items   = conn.execute('SELECT * FROM ro_items WHERE ro_id=?', (ro_id,)).fetchall()
    vendors = conn.execute('SELECT id, name, billing_name, billing_address, billing_city, billing_state, billing_zip FROM vendors ORDER BY name').fetchall()
    conn.close()
    return render_template('ro_form.html', ro=ro, items=items, vendors=vendors)


@app.route('/repair-orders/<int:ro_id>/delete', methods=['POST'])
@login_required
def ro_delete(ro_id):
    conn = get_db()
    conn.execute('DELETE FROM ro_items WHERE ro_id=?', (ro_id,))
    conn.execute('DELETE FROM repair_orders WHERE id=?', (ro_id,))
    conn.commit()
    conn.close()
    flash('Repair Order deleted.', 'success')
    return redirect(url_for('ro_list'))


# ─── Main ────────────────────────────────────────────────────────────────────

# Initialize DB on startup (works with both direct run and gunicorn)
with app.app_context():
    init_db()

# ─── Auto Email Fetch Scheduler ──────────────────────────────────────────────
def scheduled_fetch():
    """Background job: fetch RFQ emails every 5 minutes automatically."""
    with app.app_context():
        try:
            settings = get_settings()
            if settings.get('imap_pass'):
                n = _fetch_imap(settings)
                if n:
                    print(f'[Scheduler] Auto-fetched {n} new RFQ(s)')
        except Exception as e:
            print(f'[Scheduler] Fetch error: {e}')

try:
    from apscheduler.schedulers.background import BackgroundScheduler
    scheduler = BackgroundScheduler(daemon=True)
    scheduler.add_job(scheduled_fetch, 'interval', minutes=5, id='auto_fetch')
    scheduler.start()
    print('[Scheduler] Auto email fetch started — runs every 5 minutes.')
except Exception as e:
    print(f'[Scheduler] Could not start scheduler: {e}')

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    debug = os.environ.get('FLASK_ENV') != 'production'
    print('\n  ✈  Eastern Aero Pty Ltd — Auto Quote System')
    print(f'     Open  http://localhost:{port}  in your browser\n')
    app.run(host='0.0.0.0', debug=debug, port=port)
