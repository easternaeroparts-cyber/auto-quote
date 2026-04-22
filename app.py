"""
Eastern Aero Parts — Auto Quote System
A Rotabull-style aviation parts quoting application.
"""

from flask import Flask, render_template, request, redirect, url_for, jsonify, flash
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
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            quote_number   TEXT UNIQUE,
            rfq_id         INTEGER,
            status         TEXT DEFAULT 'draft',
            markup_percent REAL DEFAULT 30,
            total_amount   REAL DEFAULT 0,
            notes          TEXT,
            valid_days     INTEGER DEFAULT 30,
            created_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            sent_at        TIMESTAMP,
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

        CREATE TABLE IF NOT EXISTS users (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            username      TEXT UNIQUE NOT NULL,
            email         TEXT,
            password_hash TEXT NOT NULL,
            role          TEXT DEFAULT 'staff',
            active        INTEGER DEFAULT 1,
            created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    ''')

    defaults = {
        'company_name':    'Eastern Aero Parts',
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
    Detect if an email is a forwarded message and extract:
    - The original sender's name and email
    - The original body content
    Returns (original_name, original_email, original_body) or (None, None, body)
    """
    # Common forwarded message markers
    fwd_markers = [
        r'[-]+\s*Forwarded [Mm]essage\s*[-]+',
        r'[-]+\s*Original [Mm]essage\s*[-]+',
        r'Begin forwarded message:',
        r'[-]+\s*Forwarded by',
    ]

    fwd_start = None
    for marker in fwd_markers:
        m = re.search(marker, body)
        if m:
            fwd_start = m.start()
            break

    if fwd_start is None:
        return None, None, body  # Not a forwarded email

    fwd_content = body[fwd_start:]

    # Extract original From
    from_match = re.search(r'From:\s*([^\n<]+?)?\s*[<]?([\w.+\-]+@[\w\-]+\.[a-zA-Z]+)[>]?', fwd_content, re.I)
    orig_name  = from_match.group(1).strip().strip('"') if from_match and from_match.group(1) else ''
    orig_email = from_match.group(2).strip() if from_match else ''

    # Get the body after the forwarded header block (skip To/Date/Subject lines)
    header_end = re.search(r'\n\s*\n', fwd_content)
    orig_body  = fwd_content[header_end.end():] if header_end else fwd_content

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


def gen_quote_number():
    conn = get_db()
    n = conn.execute('SELECT COUNT(*) FROM quotes').fetchone()[0]
    conn.close()
    return f"QTE-{datetime.now().strftime('%Y%m%d')}-{n+1:04d}"


def parse_rfq_text(text):
    """
    Extract part numbers + quantities from pasted email / RFQ body.
    Handles P/N: XXX / QTY: N patterns and tab/pipe/comma delimited tables.
    """
    items = []
    seen  = set()
    lines = text.split('\n')

    PN_PAT   = re.compile(r'(?:P/?N|PART\s*(?:NO\.?|NUMBER|#)?|PN)[:\s]+([A-Z0-9][A-Z0-9\-/\.]{1,24})', re.I)
    QTY_PAT  = re.compile(r'(?:QTY|QUANTITY|Q\'?TY|QUAN)[:\s]+(\d+)', re.I)
    DESC_PAT = re.compile(r'(?:DESC(?:RIPTION)?|NOM)[:\s]+([^\n\r,|]{3,60})', re.I)
    COND_PAT = re.compile(r'(?:COND(?:ITION)?)[:\s]+([A-Z]{1,4})', re.I)

    def add(pn, desc='', qty=1, cond='SV'):
        pn = pn.upper().strip()
        if pn and pn not in seen:
            seen.add(pn)
            items.append({'part_number': pn, 'description': desc.strip().title(),
                          'quantity': qty, 'condition': cond.upper()})

    for i, line in enumerate(lines):
        lu = line.upper().strip()
        if not lu:
            continue

        m_pn = PN_PAT.search(line)
        if m_pn:
            pn   = m_pn.group(1).strip().upper()
            qty  = 1
            desc = ''
            cond = 'SV'

            m_q = QTY_PAT.search(line)
            if not m_q and i + 1 < len(lines):
                m_q = QTY_PAT.search(lines[i + 1])
            if m_q:
                try: qty = int(m_q.group(1))
                except: pass

            m_d = DESC_PAT.search(line)
            if m_d: desc = m_d.group(1)

            m_c = COND_PAT.search(line)
            if m_c: cond = m_c.group(1)

            add(pn, desc, qty, cond)
            continue

        # Delimited rows: PN | Desc | Qty | Cond
        for delim in ('\t', '|', ','):
            if delim in line:
                cols = [c.strip() for c in line.split(delim)]
                if len(cols) >= 2:
                    potential = cols[0].upper()
                    if re.match(r'^[A-Z0-9][A-Z0-9\-/\.]{2,20}$', potential):
                        desc = cols[1] if len(cols) > 1 else ''
                        qty  = 1
                        cond = 'SV'
                        if len(cols) > 2:
                            try: qty = int(re.search(r'\d+', cols[2]).group())
                            except: pass
                        if len(cols) > 3:
                            cond = cols[3].strip().upper() or 'SV'
                        add(potential, desc, qty, cond)
                break
    return items


def match_inventory(part_number, conn):
    """Return best inventory match for a part number (exact → partial)."""
    clean = re.sub(r'[\s\-/]', '', part_number.upper())
    row = conn.execute(
        "SELECT * FROM inventory WHERE UPPER(REPLACE(REPLACE(REPLACE(part_number,' ',''),'-',''),'/',''))=?",
        (clean,)
    ).fetchone()
    if not row:
        row = conn.execute(
            "SELECT * FROM inventory WHERE UPPER(part_number) LIKE ?",
            (f'%{part_number.upper()}%',)
        ).fetchone()
    return row


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
        parts = conn.execute(
            "SELECT * FROM inventory WHERE UPPER(part_number) LIKE ? OR UPPER(description) LIKE ? ORDER BY part_number",
            (f'%{q.upper()}%', f'%{q.upper()}%')
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
        df.columns = [c.upper().strip() for c in df.columns]

        COL = {}
        for c in df.columns:
            cu = c.upper()
            if any(x in cu for x in ['PART','P/N','PN','PARTNO','PART_NUM','PART NO']):
                COL.setdefault('part_number', c)
            elif 'DESC' in cu:
                COL.setdefault('description', c)
            elif 'COND' in cu:
                COL.setdefault('condition', c)
            elif cu in ('QTY','QUANTITY','STOCK','ON HAND','ONHAND','QTY ON HAND'):
                COL.setdefault('quantity', c)
            elif 'COST' in cu:
                COL.setdefault('unit_cost', c)
            elif 'PRICE' in cu or 'SELL' in cu:
                COL.setdefault('unit_price', c)
            elif 'LOC' in cu or 'BIN' in cu or 'SHELF' in cu:
                COL.setdefault('location', c)
            elif cu in ('UOM','UNIT OF MEASURE'):
                COL.setdefault('uom', c)

        if 'part_number' not in COL:
            flash('Cannot find a Part Number column. Name it "Part Number", "P/N", or "PN".', 'error')
            return redirect(url_for('inventory'))

        conn  = get_db()
        mode  = request.form.get('mode', 'merge')
        if mode == 'replace':
            conn.execute('DELETE FROM inventory')

        added = updated = 0
        for _, row in df.iterrows():
            pn = str(row[COL['part_number']]).strip().upper()
            if not pn or pn in ('NAN', 'NONE', ''):
                continue

            def g(key, default=''):
                col = COL.get(key)
                if not col: return default
                val = row.get(col, default)
                return default if str(val).upper() in ('NAN','NONE','') else val

            desc  = str(g('description', '')).strip().title()
            cond  = str(g('condition', 'SV')).strip().upper()
            loc   = str(g('location', '')).strip()
            uom   = str(g('uom', 'EA')).strip() or 'EA'
            try: qty = int(float(g('quantity', 0)))
            except: qty = 0
            try: cost = float(g('unit_cost', 0))
            except: cost = 0.0
            try: price = float(g('unit_price', 0))
            except: price = 0.0

            exists = conn.execute('SELECT id FROM inventory WHERE part_number=?', (pn,)).fetchone()
            if exists:
                conn.execute(
                    'UPDATE inventory SET description=?,condition=?,quantity=?,unit_cost=?,unit_price=?,location=?,uom=?,updated_at=CURRENT_TIMESTAMP WHERE part_number=?',
                    (desc, cond, qty, cost, price, loc, uom, pn))
                updated += 1
            else:
                conn.execute(
                    'INSERT INTO inventory (part_number,description,condition,quantity,unit_cost,unit_price,location,uom) VALUES (?,?,?,?,?,?,?,?)',
                    (pn, desc, cond, qty, cost, price, loc, uom))
                added += 1

        conn.commit()
        conn.close()
        flash(f'Inventory updated — {added} added, {updated} updated.', 'success')
    except Exception as e:
        flash(f'Error reading file: {e}', 'error')

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


@app.route('/inventory/edit/<int:pid>', methods=['POST'])
@login_required
def edit_part(pid):
    conn = get_db()
    conn.execute(
        'UPDATE inventory SET part_number=?,description=?,condition=?,quantity=?,unit_cost=?,unit_price=?,location=?,uom=?,updated_at=CURRENT_TIMESTAMP WHERE id=?',
        (request.form['part_number'].upper(), request.form.get('description',''),
         request.form.get('condition','SV'), int(request.form.get('quantity',0)),
         float(request.form.get('unit_cost',0)), float(request.form.get('unit_price',0)),
         request.form.get('location',''), request.form.get('uom','EA'), pid))
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
    conn.close()
    if not rfq:
        flash('RFQ not found.', 'error')
        return redirect(url_for('rfq_list'))
    return render_template('rfq_detail.html', rfq=rfq, items=items, quotes=quotes, settings=settings)


@app.route('/rfqs/<int:rfq_id>/quote', methods=['POST'])
@login_required
def create_quote(rfq_id):
    conn     = get_db()
    settings = get_settings()
    rfq      = conn.execute('SELECT * FROM rfqs WHERE id=?', (rfq_id,)).fetchone()
    items    = conn.execute('SELECT * FROM rfq_items WHERE rfq_id=?', (rfq_id,)).fetchall()

    markup     = float(request.form.get('markup', settings.get('default_markup', 30)))
    valid_days = int(request.form.get('valid_days', settings.get('quote_valid_days', 30)))
    notes      = request.form.get('notes', '')
    quote_no   = gen_quote_number()

    conn.execute(
        'INSERT INTO quotes (quote_number,rfq_id,status,markup_percent,valid_days,notes) VALUES (?,?,?,?,?,?)',
        (quote_no, rfq_id, 'draft', markup, valid_days, notes))
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
    conn     = get_db()
    quote    = conn.execute('SELECT * FROM quotes WHERE id=?', (quote_id,)).fetchone()
    rfq      = conn.execute('SELECT * FROM rfqs WHERE id=?', (quote['rfq_id'],)).fetchone()
    items    = conn.execute('SELECT * FROM quote_items WHERE quote_id=?', (quote_id,)).fetchall()
    settings = get_settings()
    conn.close()
    return render_template('quote_view.html', quote=quote, rfq=rfq, items=items, settings=settings)


@app.route('/quotes/<int:quote_id>/update-item', methods=['POST'])
@login_required
def update_quote_item(quote_id):
    data  = request.get_json()
    iid   = data['item_id']
    price = float(data.get('unit_price', 0))
    qty   = int(data.get('quantity_requested', 1))
    notes = data.get('notes', '')
    ext   = round(price * qty, 2)

    conn = get_db()
    conn.execute(
        'UPDATE quote_items SET unit_price=?,quantity_requested=?,extended_price=?,notes=? WHERE id=? AND quote_id=?',
        (price, qty, ext, notes, iid, quote_id))
    total = conn.execute('SELECT COALESCE(SUM(extended_price),0) FROM quote_items WHERE quote_id=?', (quote_id,)).fetchone()[0]
    conn.execute('UPDATE quotes SET total_amount=? WHERE id=?', (total, quote_id))
    conn.commit()
    conn.close()
    return jsonify({'success': True, 'extended': ext, 'total': total})


@app.route('/quotes/<int:quote_id>/send', methods=['POST'])
@login_required
def send_quote(quote_id):
    conn     = get_db()
    quote    = conn.execute('SELECT * FROM quotes WHERE id=?', (quote_id,)).fetchone()
    rfq      = conn.execute('SELECT * FROM rfqs WHERE id=?', (quote['rfq_id'],)).fetchone()
    items    = conn.execute('SELECT * FROM quote_items WHERE quote_id=?', (quote_id,)).fetchall()
    settings = get_settings()
    conn.close()

    to_email = rfq['customer_email']
    if not to_email:
        flash('No customer email on file.', 'error')
        return redirect(url_for('quote_view', quote_id=quote_id))

    html = build_quote_email(quote, rfq, items, settings)
    try:
        msg = MIMEMultipart('alternative')
        msg['Subject'] = f"Quotation {quote['quote_number']} — {settings['company_name']}"
        msg['From']    = settings['smtp_user']
        msg['To']      = to_email
        msg.attach(MIMEText(html, 'html'))

        with smtplib.SMTP(settings['smtp_host'], int(settings['smtp_port'])) as srv:
            srv.starttls()
            srv.login(settings['smtp_user'], settings['smtp_pass'])
            srv.send_message(msg)

        conn = get_db()
        conn.execute("UPDATE quotes SET status='sent', sent_at=CURRENT_TIMESTAMP WHERE id=?", (quote_id,))
        conn.commit()
        conn.close()
        flash(f'Quote sent to {to_email}!', 'success')
    except Exception as e:
        flash(f'Email failed: {e}', 'error')

    return redirect(url_for('quote_view', quote_id=quote_id))


def build_quote_email(quote, rfq, items, settings):
    rows = ''
    for it in items:
        ok    = bool(it['matched'])
        color = '#1e7e34' if ok else '#dc3545'
        stat  = '✓ In Stock' if ok else '✗ Not Available'
        rows += f"""
        <tr>
          <td>{it['part_number']}</td>
          <td>{it['description'] or '—'}</td>
          <td style="text-align:center">{it['condition'] or '—'}</td>
          <td style="text-align:center">{it['quantity_requested']}</td>
          <td style="text-align:center">{it['quantity_available']}</td>
          <td style="text-align:right">${it['unit_price']:.2f}</td>
          <td style="text-align:right"><strong>${it['extended_price']:.2f}</strong></td>
          <td style="color:{color};font-weight:bold">{stat}</td>
        </tr>"""
    td = 'padding:8px 10px;border:1px solid #ddd;'
    return f"""<html><body style="font-family:Arial,sans-serif;color:#222;max-width:820px;margin:auto">
<table width="100%" style="background:#1a3c6e;padding:16px;margin-bottom:20px"><tr>
  <td><span style="color:#fff;font-size:22px;font-weight:bold">✈ {settings['company_name']}</span></td>
  <td style="text-align:right;color:#aed6f1;font-size:13px">{settings.get('company_email','')}<br>{settings.get('company_phone','')}</td>
</tr></table>
<h2 style="color:#1a3c6e;margin-bottom:4px">QUOTATION — {quote['quote_number']}</h2>
<p style="color:#555;margin-top:4px">Date: {quote['created_at'][:10]} &nbsp;|&nbsp; Valid: {quote['valid_days']} days &nbsp;|&nbsp; Ref: {rfq['rfq_number']}</p>
<p><strong>To:</strong> {rfq['customer_name'] or ''} — {rfq['company'] or ''}</p>
<table width="100%" cellspacing="0" style="border-collapse:collapse;margin-top:16px">
  <thead><tr style="background:#1a3c6e;color:#fff">
    <th style="{td}">Part Number</th><th style="{td}">Description</th>
    <th style="{td}">Cond</th><th style="{td}">Qty Req</th><th style="{td}">Qty Avail</th>
    <th style="{td}">Unit Price</th><th style="{td}">Extended</th><th style="{td}">Status</th>
  </tr></thead>
  <tbody style="font-size:14px">{rows}</tbody>
  <tfoot><tr style="background:#f0f4f8">
    <td colspan="6" style="{td}text-align:right;font-weight:bold">TOTAL (USD):</td>
    <td style="{td}text-align:right;font-weight:bold;font-size:16px">${quote['total_amount']:.2f}</td>
    <td style="{td}"></td>
  </tr></tfoot>
</table>
{f"<p><strong>Notes:</strong> {quote['notes']}</p>" if quote['notes'] else ''}
<p style="color:#888;font-size:12px;margin-top:30px;border-top:1px solid #ddd;padding-top:12px">
All prices in USD. Quote valid for {quote['valid_days']} days from date of issue.<br>
All parts are certified unless otherwise stated. Terms: COD unless prior credit arrangement established.
</p></body></html>"""


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
    """Fetch UNSEEN + 10 most recent message IDs from a given IMAP folder."""
    try:
        status, _ = mail.select(folder)
        if status != 'OK':
            return []
    except Exception:
        return []
    try:
        _, unseen_ids = mail.search(None, 'UNSEEN')
        _, all_recent = mail.search(None, 'ALL')
    except Exception:
        return []
    recent_10 = list(reversed(all_recent[0].split()))[:10]
    unseen_list = unseen_ids[0].split()
    seen = set()
    result = []
    for mid in (unseen_list + recent_10):
        if mid not in seen:
            seen.add(mid)
            result.append((folder, mid))
    return result


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
        try:
            _, data = mail.fetch(mid, '(RFC822)')
            if not data or not data[0] or not isinstance(data[0], tuple):
                continue
            raw_msg = data[0][1]
            msg = email.message_from_bytes(raw_msg)
        except Exception:
            continue

        # Get unique message ID to avoid duplicates
        message_id = msg.get('Message-ID', '') or msg.get('Message-Id', '')
        if not message_id:
            # Fallback: use date + from as unique key
            message_id = f"{msg.get('Date','')}-{msg.get('From','')}"
        message_id = message_id.strip()

        # Skip already imported emails
        already = conn.execute(
            'SELECT 1 FROM imported_emails WHERE message_id=?', (message_id,)).fetchone()
        if already:
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

        # Body
        body = ''
        if msg.is_multipart():
            for part in msg.walk():
                ct = part.get_content_type()
                if ct == 'text/plain':
                    body = part.get_payload(decode=True).decode('utf-8', errors='ignore')
                    break
                if ct == 'text/html' and not body:
                    raw_html = part.get_payload(decode=True).decode('utf-8', errors='ignore')
                    body = re.sub(r'<[^>]+>', ' ', raw_html)
        else:
            body = msg.get_payload(decode=True).decode('utf-8', errors='ignore') or ''

        # Handle forwarded emails — extract original sender and content
        fwd_name, fwd_email, parse_body = extract_forwarded_content(body)
        is_forwarded = fwd_name is not None

        if is_forwarded:
            # Use original sender info if available
            if fwd_email:
                cust_email = fwd_email
            if fwd_name:
                cust_name = fwd_name
            # Add forwarded note to subject
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
            'need parts', 'looking for', 'do you have']

        has_rfq_keywords = any(kw in combined for kw in rfq_keywords)

        # Always treat emails from PartsBase as RFQs
        from_partsbase = 'rfqs@partsbase.com' in from_raw.lower()

        # Import if: from PartsBase, forwarded, (to rfq@ + keywords), or has keywords
        is_rfq = from_partsbase or is_forwarded or (addressed_to_rfq and has_rfq_keywords) or has_rfq_keywords
        parsed = parse_rfq_text(parse_body)

        if parsed or is_rfq:
            rfq_no = gen_rfq_number()
            source = 'email-forwarded' if is_forwarded else 'email'
            conn.execute(
                'INSERT INTO rfqs (rfq_number,customer_name,customer_email,company,source,notes,raw_email) VALUES (?,?,?,?,?,?,?)',
                (rfq_no, cust_name, cust_email, '', source, subject, body[:6000]))
            rfq_id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]

            for item in parsed:
                conn.execute(
                    'INSERT INTO rfq_items (rfq_id,part_number,description,quantity,condition) VALUES (?,?,?,?,?)',
                    (rfq_id, item['part_number'], item['description'], item['quantity'], item['condition']))
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
                    'smtp_host','smtp_port','smtp_user','smtp_pass']:
            conn.execute('INSERT OR REPLACE INTO settings VALUES (?,?)', (key, request.form.get(key,'')))
        conn.commit()
        conn.close()
        flash('Settings saved.', 'success')
        return redirect(url_for('settings_page'))
    return render_template('settings.html', s=get_settings())


# ─── Main ────────────────────────────────────────────────────────────────────

# Initialize DB on startup (works with both direct run and gunicorn)
with app.app_context():
    init_db()

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    debug = os.environ.get('FLASK_ENV') != 'production'
    print('\n  ✈  Eastern Aero Parts — Auto Quote System')
    print(f'     Open  http://localhost:{port}  in your browser\n')
    app.run(host='0.0.0.0', debug=debug, port=port)
