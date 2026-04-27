import sqlite3, os, hashlib, json, time, re, csv, io
from flask import Flask, request, jsonify, send_from_directory, g, Response
from flask_cors import CORS
from werkzeug.utils import secure_filename
from datetime import datetime
import secrets, urllib.request, urllib.parse

# ── App Setup ──────────────────────────────────────────────────────────────
app = Flask(__name__, static_folder='static', static_url_path='/static')
CORS(app, supports_credentials=True)
app.secret_key = os.environ.get('SECRET_KEY', secrets.token_hex(32))

BASE_DIR     = os.path.dirname(os.path.abspath(__file__))
DB_PATH      = os.path.join(BASE_DIR, 'delta.db')
UPLOAD_DIR   = os.path.join(BASE_DIR, 'static', 'uploads')
os.makedirs(UPLOAD_DIR, exist_ok=True)

# ── Database ───────────────────────────────────────────────────────────────
def get_db():
    if 'db' not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db

@app.teardown_appcontext
def close_db(e=None):
    db = g.pop('db', None)
    if db: db.close()

def row_to_dict(row):
    if row is None: return None
    d = dict(row)
    for k in ['tags', 'images', 'items', 'data']:
        if k in d and isinstance(d[k], str):
            try: d[k] = json.loads(d[k])
            except: pass
    return d

def rows_to_list(rows): return [row_to_dict(r) for r in (rows or [])]

def get_setting(key, default=''):
    try:
        db  = get_db()
        row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row['value'] if row else default
    except: return default

# ── Auth ───────────────────────────────────────────────────────────────────
def make_token(email):
    return hashlib.sha256((email + app.secret_key[:16]).encode()).hexdigest()

def verify_admin():
    token = request.headers.get('Authorization', '').replace('Bearer ', '').strip()
    if not token: return False
    db   = get_db()
    user = db.execute("SELECT email FROM users WHERE role='admin'").fetchone()
    return user and token == make_token(user['email'])

def hash_pw(pw): return hashlib.sha256(pw.encode()).hexdigest()

# ── Notifications ──────────────────────────────────────────────────────────
def notify(ntype, title, message, data={}):
    try:
        db = get_db()
        db.execute("INSERT INTO notifications(type,title,message,data) VALUES(?,?,?,?)",
                   (ntype, title, message, json.dumps(data)))
        db.commit()
    except Exception as e: print(f"DB notify error: {e}")
    # CallMeBot WhatsApp
    try:
        phone  = get_setting('whatsapp_notify', '971527513861').replace('+','').replace(' ','')
        apikey = get_setting('callmebot_apikey', '8093036')
        msg    = f"Delta Hub Alert\n{title}\n{message[:200]}"
        url    = f"https://api.callmebot.com/whatsapp.php?phone={phone}&text={urllib.parse.quote(msg)}&apikey={apikey}"
        req    = urllib.request.Request(url)
        req.add_header('User-Agent', 'Mozilla/5.0')
        urllib.request.urlopen(req, timeout=6)
    except Exception as e: print(f"CallMeBot error: {e}")

def auto_save_customer(name, phone, email='', source='website'):
    if not phone: return
    try:
        db = get_db()
        ex = db.execute("SELECT id FROM customers WHERE phone=?", (phone,)).fetchone()
        if ex:
            db.execute("UPDATE customers SET name=COALESCE(?,name),email=COALESCE(?,email),updated_at=datetime('now') WHERE id=?",
                       (name or None, email or None, ex['id']))
        else:
            db.execute("INSERT INTO customers(name,email,phone,source) VALUES(?,?,?,?)",
                       (name, email, phone, source))
        db.commit()
    except Exception as e: print(f"Auto-save customer error: {e}")

# ── Serve Pages ────────────────────────────────────────────────────────────
@app.route('/')
def index(): return send_from_directory(BASE_DIR, 'index.html')

@app.route('/admin')
def admin_page(): return send_from_directory(BASE_DIR, 'admin.html')

@app.route('/<path:path>')
def catch_all(path):
    fp = os.path.join(BASE_DIR, path)
    if os.path.exists(fp) and os.path.isfile(fp):
        return send_from_directory(BASE_DIR, path)
    return send_from_directory(BASE_DIR, 'index.html')

# ── AUTH API ───────────────────────────────────────────────────────────────
@app.route('/api/auth/login', methods=['POST'])
def login():
    d    = request.json or {}
    db   = get_db()
    user = db.execute("SELECT * FROM users WHERE email=? AND password_hash=?",
                      (d.get('email',''), hash_pw(d.get('password','')))).fetchone()
    if not user: return jsonify({'error': 'Invalid credentials'}), 401
    token = make_token(user['email']) if user['role'] == 'admin' else None
    return jsonify({'success': True, 'name': user['name'], 'role': user['role'], 'token': token})

@app.route('/api/auth/change-password', methods=['POST'])
def change_password():
    if not verify_admin(): return jsonify({'error': 'Unauthorized'}), 401
    pw = (request.json or {}).get('password', '')
    if len(pw) < 6: return jsonify({'error': 'Password too short'}), 400
    db = get_db()
    db.execute("UPDATE users SET password_hash=? WHERE role='admin'", (hash_pw(pw),))
    db.commit()
    return jsonify({'success': True})

# ── SETTINGS API ───────────────────────────────────────────────────────────
@app.route('/api/settings', methods=['GET'])
def get_settings():
    db   = get_db()
    rows = db.execute("SELECT key,value,label FROM settings ORDER BY key").fetchall()
    return jsonify({r['key']: {'value': r['value'], 'label': r['label'] or r['key']} for r in rows})

@app.route('/api/settings', methods=['PUT'])
def update_settings():
    if not verify_admin(): return jsonify({'error': 'Unauthorized'}), 401
    db = get_db()
    for key, val in (request.json or {}).items():
        db.execute("INSERT INTO settings(key,value,label) VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                   (key, val, key))
    db.commit()
    return jsonify({'success': True})

# ── SOCIAL API ─────────────────────────────────────────────────────────────
@app.route('/api/social', methods=['GET'])
def get_social():
    return jsonify(rows_to_list(get_db().execute("SELECT * FROM social_media ORDER BY sort_order").fetchall()))

@app.route('/api/social', methods=['POST'])
def add_social():
    if not verify_admin(): return jsonify({'error': 'Unauthorized'}), 401
    d  = request.json or {}
    db = get_db()
    db.execute("INSERT INTO social_media(platform,url,icon,active,sort_order) VALUES(?,?,?,?,?)",
               (d['platform'], d['url'], d.get('icon','🔗'), d.get('active',1), d.get('sort_order',0)))
    db.commit()
    return jsonify({'success': True})

@app.route('/api/social/<int:sid>', methods=['PUT'])
def update_social(sid):
    if not verify_admin(): return jsonify({'error': 'Unauthorized'}), 401
    d  = request.json or {}
    db = get_db()
    db.execute("UPDATE social_media SET platform=?,url=?,icon=?,active=?,sort_order=? WHERE id=?",
               (d['platform'], d['url'], d.get('icon','🔗'), d.get('active',1), d.get('sort_order',0), sid))
    db.commit()
    return jsonify({'success': True})

@app.route('/api/social/<int:sid>', methods=['DELETE'])
def delete_social(sid):
    if not verify_admin(): return jsonify({'error': 'Unauthorized'}), 401
    db = get_db()
    db.execute("DELETE FROM social_media WHERE id=?", (sid,))
    db.commit()
    return jsonify({'success': True})

# ── CATEGORIES API ─────────────────────────────────────────────────────────
@app.route('/api/categories', methods=['GET'])
def get_categories():
    db   = get_db()
    rows = db.execute("""SELECT c.*, COUNT(p.id) as project_count
        FROM categories c LEFT JOIN projects p ON p.category_id=c.id
        GROUP BY c.id ORDER BY c.name""").fetchall()
    return jsonify(rows_to_list(rows))

@app.route('/api/categories', methods=['POST'])
def add_category():
    if not verify_admin(): return jsonify({'error': 'Unauthorized'}), 401
    d    = request.json or {}
    db   = get_db()
    slug = re.sub(r'[^a-z0-9-]', '-', d['name'].lower())
    db.execute("INSERT OR IGNORE INTO categories(name,slug,icon) VALUES(?,?,?)",
               (d['name'], slug, d.get('icon','📁')))
    db.commit()
    return jsonify({'success': True})

@app.route('/api/categories/<int:cid>', methods=['DELETE'])
def delete_category(cid):
    if not verify_admin(): return jsonify({'error': 'Unauthorized'}), 401
    db = get_db()
    db.execute("DELETE FROM categories WHERE id=?", (cid,))
    db.commit()
    return jsonify({'success': True})

# ── PROJECTS API ───────────────────────────────────────────────────────────
@app.route('/api/projects', methods=['GET'])
def get_projects():
    db       = get_db()
    cat      = request.args.get('category', '')
    search   = request.args.get('search', '')
    sort     = request.args.get('sort', 'default')
    featured = request.args.get('featured', '')
    page     = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 12, type=int)

    q      = """SELECT p.*, c.name as category_name, c.slug as category_slug
                FROM projects p LEFT JOIN categories c ON p.category_id=c.id WHERE 1=1"""
    params = []

    if cat:
        q += " AND (c.slug=? OR p.tags LIKE ?)"
        params += [cat, f'%{cat}%']
    if search:
        q += " AND (p.title LIKE ? OR p.description LIKE ? OR p.tags LIKE ?)"
        params += [f'%{search}%', f'%{search}%', f'%{search}%']
    if featured == '1':
        q += " AND p.featured=1"

    order_map = {'price-asc': 'p.price_inr ASC', 'price-desc': 'p.price_inr DESC',
                 'newest': 'p.created_at DESC', 'popular': 'p.views DESC'}
    q += f" ORDER BY {order_map.get(sort, 'p.featured DESC, p.created_at DESC')}"

    total  = db.execute(f"SELECT COUNT(*) FROM ({q})", params).fetchone()[0]
    offset = (page - 1) * per_page
    rows   = db.execute(q + f" LIMIT {per_page} OFFSET {offset}", params).fetchall()
    return jsonify({'projects': rows_to_list(rows), 'total': total, 'page': page, 'per_page': per_page})

@app.route('/api/projects/<slug>', methods=['GET'])
def get_project(slug):
    db = get_db()
    p  = db.execute("""SELECT p.*, c.name as category_name
        FROM projects p LEFT JOIN categories c ON p.category_id=c.id WHERE p.slug=?""", (slug,)).fetchone()
    if not p: return jsonify({'error': 'Not found'}), 404
    db.execute("UPDATE projects SET views=views+1 WHERE slug=?", (slug,)); db.commit()
    pd                = row_to_dict(p)
    pd['reviews']     = rows_to_list(db.execute("SELECT * FROM reviews WHERE project_id=? AND approved=1 ORDER BY created_at DESC", (p['id'],)).fetchall())
    pd['related']     = rows_to_list(db.execute("SELECT * FROM projects WHERE category_id=? AND id!=? LIMIT 4", (p['category_id'], p['id'])).fetchall())
    return jsonify(pd)

def _make_slug(title):
    return re.sub(r'-+', '-', re.sub(r'[^a-z0-9-]', '-', title.lower()))[:80]

def _project_prices(d):
    rate   = float(get_setting('inr_to_aed', '0.044')) * 3
    p_inr  = float(d.get('price_inr', d.get('price', 0)) or 0)
    p_aed  = float(d.get('price_aed', 0) or 0) or round(p_inr * rate, 2)
    op_inr = float(d.get('old_price_inr', d.get('old_price', 0) or 0) or 0) or None
    op_aed_raw = float(d.get('old_price_aed', 0) or 0)
    op_aed = op_aed_raw or (round(op_inr * rate, 2) if op_inr else None)
    return p_inr, round(p_aed,2), op_inr, op_aed

@app.route('/api/projects', methods=['POST'])
def create_project():
    if not verify_admin(): return jsonify({'error': 'Unauthorized'}), 401
    d                          = request.json or {}
    p_inr, p_aed, op_inr, op_aed = _project_prices(d)
    slug                       = _make_slug(d.get('title','project'))
    db                         = get_db()
    # Ensure unique slug
    base = slug
    for i in range(1, 20):
        if not db.execute("SELECT id FROM projects WHERE slug=?", (slug,)).fetchone(): break
        slug = f"{base}-{i}"
    cur = db.execute("""INSERT INTO projects(title,slug,description,abstract,aim,tech_stack,
        price_inr,price_aed,old_price_inr,old_price_aed,badge,badge_type,category_id,tags,
        image_url,level,hardware_components,software_tools,featured,in_stock)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (d.get('title'), slug, d.get('description'), d.get('abstract'), d.get('aim'),
         d.get('tech_stack'), p_inr, p_aed, op_inr, op_aed, d.get('badge',''),
         d.get('badge_type',''), d.get('category_id'), json.dumps(d.get('tags',[])),
         d.get('image_url'), d.get('level','Final Year'), d.get('hardware_components'),
         d.get('software_tools'), d.get('featured',0), d.get('in_stock',1)))
    db.commit()
    return jsonify({'success': True, 'id': cur.lastrowid})

@app.route('/api/projects/<int:pid>', methods=['PUT'])
def update_project(pid):
    if not verify_admin(): return jsonify({'error': 'Unauthorized'}), 401
    d                          = request.json or {}
    p_inr, p_aed, op_inr, op_aed = _project_prices(d)
    db                         = get_db()
    db.execute("""UPDATE projects SET title=?,description=?,abstract=?,aim=?,tech_stack=?,
        price_inr=?,price_aed=?,old_price_inr=?,old_price_aed=?,badge=?,badge_type=?,
        category_id=?,tags=?,image_url=?,level=?,hardware_components=?,software_tools=?,
        featured=?,in_stock=?,updated_at=datetime('now') WHERE id=?""",
        (d.get('title'), d.get('description'), d.get('abstract'), d.get('aim'),
         d.get('tech_stack'), p_inr, p_aed, op_inr, op_aed, d.get('badge',''),
         d.get('badge_type',''), d.get('category_id'), json.dumps(d.get('tags',[])),
         d.get('image_url'), d.get('level','Final Year'), d.get('hardware_components'),
         d.get('software_tools'), d.get('featured',0), d.get('in_stock',1), pid))
    db.commit()
    return jsonify({'success': True})

@app.route('/api/projects/<int:pid>', methods=['DELETE'])
def delete_project(pid):
    if not verify_admin(): return jsonify({'error': 'Unauthorized'}), 401
    db = get_db()
    db.execute("DELETE FROM projects WHERE id=?", (pid,)); db.commit()
    return jsonify({'success': True})

@app.route('/api/projects/export', methods=['GET'])
def export_projects():
    if not verify_admin(): return jsonify({'error': 'Unauthorized'}), 401
    db   = get_db()
    rows = db.execute("""SELECT p.title,p.description,p.price_inr,p.price_aed,
        p.old_price_inr,p.old_price_aed,p.tech_stack,p.software_tools,p.hardware_components,
        p.level,c.name as category,p.badge,p.tags,p.image_url,p.featured,p.in_stock
        FROM projects p LEFT JOIN categories c ON p.category_id=c.id
        ORDER BY p.created_at DESC""").fetchall()
    out = io.StringIO()
    w   = csv.writer(out)
    w.writerow(['Title','Description','Price INR','Price AED','Old Price INR','Old Price AED',
                'Tech Stack','Software Tools','Hardware','Level','Category','Badge','Tags',
                'Image URL','Featured','In Stock'])
    for r in rows:
        row = list(r)
        row[12] = ','.join(json.loads(row[12]) if row[12] else [])
        w.writerow(row)
    return Response(out.getvalue(), mimetype='text/csv',
                    headers={'Content-Disposition': 'attachment;filename=projects.csv'})

# ── CART API ───────────────────────────────────────────────────────────────
def get_session():
    sid = request.cookies.get('sid') or secrets.token_hex(16)
    return sid

def set_sid(resp, sid):
    resp.set_cookie('sid', sid, max_age=86400*30, httponly=True, samesite='Lax')
    return resp

@app.route('/api/cart', methods=['GET'])
def get_cart():
    sid   = get_session()
    db    = get_db()
    items = db.execute("""SELECT c.id,c.quantity,p.id as project_id,p.title,
        p.price_inr,p.price_aed,p.image_url,p.slug,p.level
        FROM cart c JOIN projects p ON c.project_id=p.id WHERE c.session_id=?""", (sid,)).fetchall()
    resp  = jsonify({'items': rows_to_list(items)})
    return set_sid(resp, sid)

@app.route('/api/cart', methods=['POST'])
def add_to_cart():
    sid = get_session(); db = get_db()
    d   = request.json or {}
    pid = d.get('project_id'); qty = d.get('quantity', 1)
    ex  = db.execute("SELECT id,quantity FROM cart WHERE session_id=? AND project_id=?", (sid, pid)).fetchone()
    if ex: db.execute("UPDATE cart SET quantity=? WHERE id=?", (ex['quantity']+qty, ex['id']))
    else:  db.execute("INSERT INTO cart(session_id,project_id,quantity) VALUES(?,?,?)", (sid, pid, qty))
    db.commit()
    p = db.execute("SELECT title FROM projects WHERE id=?", (pid,)).fetchone()
    if p: notify('cart','🛒 Cart Add', f"Project: {p['title']}", {'pid': pid})
    resp = jsonify({'success': True})
    return set_sid(resp, sid)

@app.route('/api/cart/<int:cid>', methods=['PATCH'])
def patch_cart(cid):
    sid = get_session(); db = get_db()
    qty = (request.json or {}).get('quantity', 1)
    if qty <= 0: db.execute("DELETE FROM cart WHERE id=? AND session_id=?", (cid, sid))
    else:        db.execute("UPDATE cart SET quantity=? WHERE id=? AND session_id=?", (qty, cid, sid))
    db.commit()
    return set_sid(jsonify({'success': True}), sid)

@app.route('/api/cart/<int:cid>', methods=['DELETE'])
def remove_from_cart(cid):
    sid = get_session(); db = get_db()
    db.execute("DELETE FROM cart WHERE id=? AND session_id=?", (cid, sid)); db.commit()
    return set_sid(jsonify({'success': True}), sid)

@app.route('/api/cart/clear', methods=['DELETE'])
def clear_cart():
    sid = get_session(); db = get_db()
    db.execute("DELETE FROM cart WHERE session_id=?", (sid,)); db.commit()
    return set_sid(jsonify({'success': True}), sid)

# ── WISHLIST API ───────────────────────────────────────────────────────────
@app.route('/api/wishlist', methods=['GET'])
def get_wishlist():
    sid   = get_session(); db = get_db()
    items = db.execute("""SELECT w.id,p.id as project_id,p.title,p.price_inr,
        p.price_aed,p.image_url,p.slug,p.badge FROM wishlist w
        JOIN projects p ON w.project_id=p.id WHERE w.session_id=?""", (sid,)).fetchall()
    resp  = jsonify({'items': rows_to_list(items)})
    return set_sid(resp, sid)

@app.route('/api/wishlist', methods=['POST'])
def toggle_wishlist():
    sid = get_session(); db = get_db()
    pid = (request.json or {}).get('project_id')
    ex  = db.execute("SELECT id FROM wishlist WHERE session_id=? AND project_id=?", (sid, pid)).fetchone()
    if ex: db.execute("DELETE FROM wishlist WHERE id=?", (ex['id'],)); action = 'removed'
    else:  db.execute("INSERT OR IGNORE INTO wishlist(session_id,project_id) VALUES(?,?)", (sid, pid)); action = 'added'
    db.commit()
    return set_sid(jsonify({'success': True, 'action': action}), sid)

# ── ORDERS API ─────────────────────────────────────────────────────────────
@app.route('/api/orders', methods=['POST'])
def place_order():
    sid = get_session(); db = get_db()
    d   = request.json or {}
    num = f"DPH{int(time.time())}"
    items  = d.get('items', [])
    total  = sum(i.get('price', 0) * i.get('quantity', 1) for i in items)
    db.execute("""INSERT INTO orders(order_number,customer_name,customer_email,
        customer_phone,items,subtotal,total,payment_method,notes,address)
        VALUES(?,?,?,?,?,?,?,?,?,?)""",
        (num, d.get('name'), d.get('email'), d.get('phone'),
         json.dumps(items), total, total, d.get('payment_method','whatsapp'),
         d.get('notes',''), d.get('address','')))
    auto_save_customer(d.get('name'), d.get('phone'), d.get('email'), 'order')
    db.execute("DELETE FROM cart WHERE session_id=?", (sid,))
    db.commit()
    titles = ', '.join(i.get('title','') for i in items)
    notify('order', '🛒 New Order!',
           f"Order #{num}\nCustomer: {d.get('name')}\nPhone: {d.get('phone')}\nItems: {titles}\nTotal: ₹{total:,.0f}",
           {'order_number': num})
    return jsonify({'success': True, 'order_number': num})

@app.route('/api/orders', methods=['GET'])
def get_orders():
    if not verify_admin(): return jsonify({'error': 'Unauthorized'}), 401
    db   = get_db()
    rows = db.execute("SELECT * FROM orders ORDER BY created_at DESC").fetchall()
    return jsonify(rows_to_list(rows))

@app.route('/api/orders/<num>', methods=['PUT'])
def update_order(num):
    if not verify_admin(): return jsonify({'error': 'Unauthorized'}), 401
    d  = request.json or {}
    db = get_db()
    db.execute("UPDATE orders SET status=?,payment_status=?,updated_at=datetime('now') WHERE order_number=?",
               (d.get('status'), d.get('payment_status'), num))
    db.commit()
    return jsonify({'success': True})

# ── ENQUIRIES API ──────────────────────────────────────────────────────────
@app.route('/api/enquiries', methods=['POST'])
def submit_enquiry():
    d  = request.json or {}
    db = get_db()
    db.execute("INSERT INTO enquiries(name,email,phone,subject,message,project_id) VALUES(?,?,?,?,?,?)",
               (d.get('name'), d.get('email'), d.get('phone'),
                d.get('subject','General'), d.get('message'), d.get('project_id')))
    auto_save_customer(d.get('name'), d.get('phone'), d.get('email'), 'enquiry')
    db.commit()
    proj_title = ''
    if d.get('project_id'):
        p = db.execute("SELECT title FROM projects WHERE id=?", (d['project_id'],)).fetchone()
        if p: proj_title = p['title']
    notify('enquiry', '📧 New Enquiry!',
           f"From: {d.get('name')}\nPhone: {d.get('phone')}\nEmail: {d.get('email','—')}\nProject: {proj_title or 'General'}\n\n{d.get('message','')}",
           {'name': d.get('name'), 'phone': d.get('phone')})
    return jsonify({'success': True})

@app.route('/api/enquiries', methods=['GET'])
def get_enquiries():
    if not verify_admin(): return jsonify({'error': 'Unauthorized'}), 401
    db   = get_db()
    rows = db.execute("""SELECT e.*,p.title as project_title FROM enquiries e
        LEFT JOIN projects p ON e.project_id=p.id ORDER BY e.created_at DESC""").fetchall()
    return jsonify(rows_to_list(rows))

@app.route('/api/enquiries/<int:eid>', methods=['PUT'])
def update_enquiry(eid):
    if not verify_admin(): return jsonify({'error': 'Unauthorized'}), 401
    db = get_db()
    db.execute("UPDATE enquiries SET status=? WHERE id=?", ((request.json or {}).get('status'), eid))
    db.commit()
    return jsonify({'success': True})

# ── CUSTOMERS API ──────────────────────────────────────────────────────────
@app.route('/api/customers', methods=['GET'])
def get_customers():
    if not verify_admin(): return jsonify({'error': 'Unauthorized'}), 401
    return jsonify(rows_to_list(get_db().execute("SELECT * FROM customers ORDER BY created_at DESC").fetchall()))

@app.route('/api/customers', methods=['POST'])
def add_customer():
    d   = request.json or {}
    db  = get_db()
    ex  = db.execute("SELECT id FROM customers WHERE phone=?", (d.get('phone',''),)).fetchone() if d.get('phone') else None
    if ex:
        db.execute("UPDATE customers SET name=COALESCE(?,name),email=COALESCE(?,email),updated_at=datetime('now') WHERE id=?",
                   (d.get('name'), d.get('email'), ex['id']))
        cid = ex['id']
    else:
        cur = db.execute("INSERT INTO customers(name,email,phone,source,notes) VALUES(?,?,?,?,?)",
                         (d.get('name'), d.get('email'), d.get('phone'), d.get('source','manual'), d.get('notes','')))
        cid = cur.lastrowid
    db.commit()
    return jsonify({'success': True, 'id': cid})

@app.route('/api/customers/<int:cid>', methods=['PUT'])
def update_customer(cid):
    if not verify_admin(): return jsonify({'error': 'Unauthorized'}), 401
    d  = request.json or {}
    db = get_db()
    db.execute("UPDATE customers SET name=?,email=?,phone=?,notes=?,updated_at=datetime('now') WHERE id=?",
               (d.get('name'), d.get('email'), d.get('phone'), d.get('notes'), cid))
    db.commit()
    return jsonify({'success': True})

@app.route('/api/customers/<int:cid>', methods=['DELETE'])
def delete_customer(cid):
    if not verify_admin(): return jsonify({'error': 'Unauthorized'}), 401
    db = get_db()
    db.execute("DELETE FROM customers WHERE id=?", (cid,)); db.commit()
    return jsonify({'success': True})

@app.route('/api/customers/export', methods=['GET'])
def export_customers():
    if not verify_admin(): return jsonify({'error': 'Unauthorized'}), 401
    db   = get_db()
    rows = db.execute("SELECT name,email,phone,source,notes,created_at FROM customers ORDER BY created_at DESC").fetchall()
    out  = io.StringIO()
    w    = csv.writer(out)
    w.writerow(['Name','Email','Phone','Source','Notes','Created At'])
    for r in rows: w.writerow(list(r))
    return Response(out.getvalue(), mimetype='text/csv',
                    headers={'Content-Disposition': 'attachment;filename=customers.csv'})

@app.route('/api/customers/import', methods=['POST'])
def import_customers():
    if not verify_admin(): return jsonify({'error': 'Unauthorized'}), 401
    db    = get_db(); count = 0
    for row in (request.json or {}).get('rows', []):
        phone = row.get('phone') or row.get('Phone','')
        name  = row.get('name')  or row.get('Name','')
        email = row.get('email') or row.get('Email','')
        if not phone: continue
        ex = db.execute("SELECT id FROM customers WHERE phone=?", (phone,)).fetchone()
        if ex: db.execute("UPDATE customers SET name=COALESCE(?,name) WHERE id=?", (name, ex['id']))
        else:  db.execute("INSERT INTO customers(name,email,phone,source) VALUES(?,?,?,'import')", (name,email,phone))
        count += 1
    db.commit()
    return jsonify({'success': True, 'imported': count})

# ── NOTIFICATIONS API ──────────────────────────────────────────────────────
@app.route('/api/notifications', methods=['GET'])
def get_notifications():
    if not verify_admin(): return jsonify({'error': 'Unauthorized'}), 401
    rows = get_db().execute("SELECT * FROM notifications ORDER BY created_at DESC LIMIT 60").fetchall()
    return jsonify(rows_to_list(rows))

@app.route('/api/notifications/unread-count', methods=['GET'])
def unread_count():
    if not verify_admin(): return jsonify({'count': 0})
    c = get_db().execute("SELECT COUNT(*) FROM notifications WHERE read_flag=0").fetchone()[0]
    return jsonify({'count': c})

@app.route('/api/notifications/read', methods=['PUT'])
def mark_read():
    if not verify_admin(): return jsonify({'error': 'Unauthorized'}), 401
    db = get_db()
    db.execute("UPDATE notifications SET read_flag=1"); db.commit()
    return jsonify({'success': True})

# ── NEWSLETTER API ─────────────────────────────────────────────────────────
@app.route('/api/newsletter', methods=['POST'])
def subscribe():
    email = (request.json or {}).get('email','')
    db    = get_db()
    try:
        db.execute("INSERT INTO newsletter(email) VALUES(?)", (email,)); db.commit()
        notify('newsletter','📨 Newsletter Subscribe', f"Email: {email}")
        return jsonify({'success': True})
    except: return jsonify({'success': False, 'message': 'Already subscribed'})

# ── REVIEWS API ────────────────────────────────────────────────────────────
@app.route('/api/reviews', methods=['POST'])
def add_review():
    d  = request.json or {}
    db = get_db()
    db.execute("INSERT INTO reviews(project_id,user_name,user_email,rating,review) VALUES(?,?,?,?,?)",
               (d.get('project_id'), d.get('name'), d.get('email'), d.get('rating'), d.get('review')))
    db.commit()
    return jsonify({'success': True})

@app.route('/api/reviews/<int:rid>/approve', methods=['PUT'])
def approve_review(rid):
    if not verify_admin(): return jsonify({'error': 'Unauthorized'}), 401
    db = get_db()
    db.execute("UPDATE reviews SET approved=1 WHERE id=?", (rid,)); db.commit()
    return jsonify({'success': True})

@app.route('/api/reviews/pending', methods=['GET'])
def pending_reviews():
    if not verify_admin(): return jsonify({'error': 'Unauthorized'}), 401
    rows = get_db().execute("""SELECT r.*,p.title as project_title FROM reviews r
        LEFT JOIN projects p ON r.project_id=p.id WHERE r.approved=0
        ORDER BY r.created_at DESC""").fetchall()
    return jsonify(rows_to_list(rows))

# ── ADMIN STATS API ────────────────────────────────────────────────────────
@app.route('/api/admin/stats', methods=['GET'])
def admin_stats():
    if not verify_admin(): return jsonify({'error': 'Unauthorized'}), 401
    db = get_db()
    stats = {
        'total_projects':   db.execute("SELECT COUNT(*) FROM projects").fetchone()[0],
        'total_orders':     db.execute("SELECT COUNT(*) FROM orders").fetchone()[0],
        'pending_orders':   db.execute("SELECT COUNT(*) FROM orders WHERE status='pending'").fetchone()[0],
        'total_enquiries':  db.execute("SELECT COUNT(*) FROM enquiries").fetchone()[0],
        'new_enquiries':    db.execute("SELECT COUNT(*) FROM enquiries WHERE status='new'").fetchone()[0],
        'total_customers':  db.execute("SELECT COUNT(*) FROM customers").fetchone()[0],
        'newsletter_count': db.execute("SELECT COUNT(*) FROM newsletter").fetchone()[0],
        'unread_notif':     db.execute("SELECT COUNT(*) FROM notifications WHERE read_flag=0").fetchone()[0],
        'total_revenue':    db.execute("SELECT COALESCE(SUM(total),0) FROM orders WHERE payment_status='paid'").fetchone()[0],
    }
    recent_orders = db.execute("SELECT * FROM orders ORDER BY created_at DESC LIMIT 5").fetchall()
    stats['recent_orders'] = rows_to_list(recent_orders)
    return jsonify(stats)

# ── UPLOAD API ─────────────────────────────────────────────────────────────
@app.route('/api/upload', methods=['POST'])
def upload_image():
    if not verify_admin(): return jsonify({'error': 'Unauthorized'}), 401
    if 'file' not in request.files: return jsonify({'error': 'No file'}), 400
    f     = request.files['file']
    fname = f"{int(time.time())}_{secure_filename(f.filename)}"
    f.save(os.path.join(UPLOAD_DIR, fname))
    return jsonify({'url': f'/static/uploads/{fname}'})

# ── DATABASE INIT ──────────────────────────────────────────────────────────
@app.route('/api/newsletter/list', methods=['GET'])
def list_newsletter():
    if not verify_admin(): return jsonify({'error':'Unauthorized'}), 401
    rows = get_db().execute("SELECT * FROM newsletter ORDER BY created_at DESC").fetchall()
    return jsonify(rows_to_list(rows))

def init_db():
    db = get_db()
    db.executescript("""
    CREATE TABLE IF NOT EXISTS categories (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL, slug TEXT UNIQUE NOT NULL,
        icon TEXT DEFAULT '📁', description TEXT,
        created_at TEXT DEFAULT (datetime('now'))
    );
    CREATE TABLE IF NOT EXISTS projects (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL, slug TEXT UNIQUE NOT NULL,
        description TEXT, abstract TEXT, aim TEXT, tech_stack TEXT,
        price_inr REAL DEFAULT 0, price_aed REAL DEFAULT 0,
        old_price_inr REAL, old_price_aed REAL,
        badge TEXT DEFAULT '', badge_type TEXT DEFAULT '',
        category_id INTEGER, tags TEXT DEFAULT '[]',
        image_url TEXT, images TEXT DEFAULT '[]',
        level TEXT DEFAULT 'Final Year',
        hardware_components TEXT, software_tools TEXT,
        in_stock INTEGER DEFAULT 1, featured INTEGER DEFAULT 0,
        views INTEGER DEFAULT 0,
        created_at TEXT DEFAULT (datetime('now')),
        updated_at TEXT DEFAULT (datetime('now'))
    );
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL, email TEXT UNIQUE NOT NULL,
        phone TEXT, password_hash TEXT NOT NULL,
        role TEXT DEFAULT 'customer',
        created_at TEXT DEFAULT (datetime('now'))
    );
    CREATE TABLE IF NOT EXISTS orders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        order_number TEXT UNIQUE NOT NULL,
        customer_name TEXT, customer_email TEXT, customer_phone TEXT,
        items TEXT NOT NULL DEFAULT '[]',
        subtotal REAL DEFAULT 0, total REAL DEFAULT 0,
        status TEXT DEFAULT 'pending',
        payment_method TEXT DEFAULT 'whatsapp',
        payment_status TEXT DEFAULT 'pending',
        notes TEXT, address TEXT,
        created_at TEXT DEFAULT (datetime('now')),
        updated_at TEXT DEFAULT (datetime('now'))
    );
    CREATE TABLE IF NOT EXISTS cart (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT NOT NULL, project_id INTEGER,
        quantity INTEGER DEFAULT 1,
        created_at TEXT DEFAULT (datetime('now'))
    );
    CREATE TABLE IF NOT EXISTS wishlist (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT NOT NULL, project_id INTEGER,
        created_at TEXT DEFAULT (datetime('now')),
        UNIQUE(session_id, project_id)
    );
    CREATE TABLE IF NOT EXISTS reviews (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        project_id INTEGER, user_name TEXT NOT NULL,
        user_email TEXT, rating INTEGER NOT NULL,
        review TEXT, approved INTEGER DEFAULT 0,
        created_at TEXT DEFAULT (datetime('now'))
    );
    CREATE TABLE IF NOT EXISTS enquiries (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL, email TEXT, phone TEXT,
        subject TEXT, message TEXT, project_id INTEGER,
        status TEXT DEFAULT 'new',
        created_at TEXT DEFAULT (datetime('now'))
    );
    CREATE TABLE IF NOT EXISTS customers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT, email TEXT, phone TEXT,
        source TEXT DEFAULT 'website', notes TEXT,
        created_at TEXT DEFAULT (datetime('now')),
        updated_at TEXT DEFAULT (datetime('now'))
    );
    CREATE TABLE IF NOT EXISTS newsletter (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        email TEXT UNIQUE NOT NULL,
        created_at TEXT DEFAULT (datetime('now'))
    );
    CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY, value TEXT NOT NULL,
        label TEXT DEFAULT '',
        updated_at TEXT DEFAULT (datetime('now'))
    );
    CREATE TABLE IF NOT EXISTS social_media (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        platform TEXT UNIQUE NOT NULL, url TEXT NOT NULL,
        icon TEXT DEFAULT '🔗', active INTEGER DEFAULT 1,
        sort_order INTEGER DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS notifications (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        type TEXT NOT NULL, title TEXT NOT NULL,
        message TEXT NOT NULL, data TEXT DEFAULT '{}',
        read_flag INTEGER DEFAULT 0,
        created_at TEXT DEFAULT (datetime('now'))
    );
    """)

    # Seed admin user
    if not db.execute("SELECT id FROM users WHERE role='admin'").fetchone():
        db.execute("INSERT INTO users(name,email,phone,password_hash,role) VALUES(?,?,?,?,?)",
                   ('Admin','admin@deltaprojecthub.com','+971527513861',
                    hash_pw('admin123'),'admin'))

    # Seed default settings
    defaults = [
        ('phone_uae','+971 527 513 861','UAE Phone'),
        ('phone_india','+91 867 534 1000','India Phone'),
        ('whatsapp','+971527513861','WhatsApp'),
        ('email','info.deltaprojectsolution@gmail.com','Email'),
        ('address_india','4/987, Thiruchampalli, Tamil Nadu – 609309','India Address'),
        ('address_uae','Falah Building 34, Alfalah Street, Abu Dhabi City','UAE Address'),
        ('inr_to_aed','0.044','INR→AED Base Rate'),
        ('site_name','Delta Project & Solution','Site Name'),
        ('tagline','Engineering Projects Delivered to Your Door','Tagline'),
        ('whatsapp_notify','971527513861','WhatsApp Notify Number'),
        ('callmebot_apikey','8093036','CallMeBot API Key'),
        ('instagram','https://instagram.com/deltaprojectsolution','Instagram'),
        ('facebook','https://facebook.com/deltaprojectsolution','Facebook'),
        ('youtube','https://youtube.com/@deltaprojectsolution','YouTube'),
        ('linkedin','https://linkedin.com/company/deltaprojectsolution','LinkedIn'),
        ('whatsapp_link','https://wa.me/971527513861','WhatsApp Link'),
    ]
    for key, val, label in defaults:
        db.execute("INSERT OR IGNORE INTO settings(key,value,label) VALUES(?,?,?)", (key,val,label))

    # Seed social media
    socials = [
        ('Instagram','https://instagram.com/deltaprojectsolution','📸',1,1),
        ('Facebook','https://facebook.com/deltaprojectsolution','📘',1,2),
        ('YouTube','https://youtube.com/@deltaprojectsolution','📺',1,3),
        ('LinkedIn','https://linkedin.com/company/deltaprojectsolution','💼',1,4),
        ('WhatsApp','https://wa.me/971527513861','💬',1,5),
    ]
    for plat, url, icon, active, order in socials:
        db.execute("INSERT OR IGNORE INTO social_media(platform,url,icon,active,sort_order) VALUES(?,?,?,?,?)",
                   (plat,url,icon,active,order))

    db.commit()

# ── STARTUP ────────────────────────────────────────────────────────────────
with app.app_context():
    init_db()

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
