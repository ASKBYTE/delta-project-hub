import sqlite3, os, hashlib, json, time, re
from flask import Flask, request, jsonify, send_from_directory, g
from flask_cors import CORS
from werkzeug.utils import secure_filename
from datetime import datetime
import secrets

app = Flask(__name__, static_folder='static', static_url_path='/static')
CORS(app, supports_credentials=True)
app.secret_key = secrets.token_hex(32)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, 'delta.db')
UPLOAD_FOLDER = os.path.join(BASE_DIR, 'static', 'uploads')
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

def get_db():
    if 'db' not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        # WAL mode disabled for cloud compatibility
    return g.db

@app.teardown_appcontext
def close_db(e=None):
    db = g.pop('db', None)
    if db: db.close()

def get_setting(key, default=''):
    db = get_db()
    r = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return r['value'] if r else default

def row_to_dict(row):
    if row is None: return None
    d = dict(row)
    for k in ['tags','images']:
        if k in d and d[k]:
            try: d[k] = json.loads(d[k])
            except: pass
    return d

def rows_to_list(rows): return [row_to_dict(r) for r in rows]

def get_session():
    sid = request.cookies.get('session_id')
    if not sid: sid = secrets.token_hex(16)
    return sid

def set_cookie(resp, sid):
    resp.set_cookie('session_id', sid, max_age=86400*30, httponly=True, samesite='Lax')
    return resp

def verify_admin():
    token = request.headers.get('Authorization','').replace('Bearer ','')
    if not token: return False
    db = get_db()
    user = db.execute("SELECT email FROM users WHERE role='admin'").fetchone()
    if not user: return False
    return token == hashlib.sha256((user['email'] + app.secret_key[:16]).encode()).hexdigest()

def make_token(email):
    return hashlib.sha256((email + app.secret_key[:16]).encode()).hexdigest()

def add_notification(ntype, title, message, data={}):
    try:
        db = get_db()
        db.execute("INSERT INTO notifications(type,title,message,data) VALUES(?,?,?,?)",
                   (ntype, title, message, json.dumps(data)))
        db.commit()
    except Exception as e:
        print(f"DB notification error: {e}")
    # CallMeBot WhatsApp notification (fire-and-forget)
    try:
        import urllib.request as _ur, urllib.parse as _up
        phone = get_setting('whatsapp_notify', '971527513861').replace('+','').replace(' ','')
        apikey = get_setting('callmebot_apikey', '8093036')
        wa_msg = f"Delta Project Hub Alert\n{title}\n{message[:200]}"
        encoded = _up.quote(wa_msg)
        url = f"https://api.callmebot.com/whatsapp.php?phone={phone}&text={encoded}&apikey={apikey}"
        req = _ur.Request(url)
        req.add_header('User-Agent','Mozilla/5.0')
        _ur.urlopen(req, timeout=8)
    except Exception as e:
        print(f"CallMeBot error (non-critical): {e}")

# ── SERVE FRONTEND ──
@app.route('/')
def index():
    return send_from_directory(BASE_DIR, 'index.html')

@app.route('/<path:path>')
def catch_all(path):
    static_path = os.path.join(os.path.dirname(__file__), path)
    if os.path.exists(static_path) and os.path.isfile(static_path):
        return send_from_directory(os.path.dirname(__file__), path)
    return send_from_directory(BASE_DIR, 'index.html')

# ── SETTINGS API ──
@app.route('/api/settings', methods=['GET'])
def get_settings():
    db = get_db()
    rows = db.execute("SELECT key,value,label FROM settings ORDER BY key").fetchall()
    return jsonify({r['key']: {'value': r['value'], 'label': r['label'] or r['key']} for r in rows})

@app.route('/api/settings', methods=['PUT'])
def update_settings():
    if not verify_admin(): return jsonify({'error':'Unauthorized'}), 401
    db = get_db()
    for key, val in request.json.items():
        db.execute("INSERT OR REPLACE INTO settings(key,value,label) VALUES(?,?,COALESCE((SELECT label FROM settings WHERE key=?),?))",
                   (key, val, key, key))
    db.commit()
    return jsonify({'success': True})

# ── SOCIAL MEDIA API ──
@app.route('/api/social', methods=['GET'])
def get_social():
    db = get_db()
    rows = db.execute("SELECT * FROM social_media ORDER BY sort_order").fetchall()
    return jsonify(rows_to_list(rows))

@app.route('/api/social', methods=['POST'])
def add_social():
    if not verify_admin(): return jsonify({'error':'Unauthorized'}), 401
    db = get_db()
    d = request.json
    db.execute("INSERT OR REPLACE INTO social_media(platform,url,icon,active,sort_order) VALUES(?,?,?,?,?)",
               (d['platform'], d['url'], d.get('icon','🔗'), d.get('active',1), d.get('sort_order',0)))
    db.commit()
    return jsonify({'success': True})

@app.route('/api/social/<int:sid>', methods=['PUT'])
def update_social(sid):
    if not verify_admin(): return jsonify({'error':'Unauthorized'}), 401
    db = get_db()
    d = request.json
    db.execute("UPDATE social_media SET platform=?,url=?,icon=?,active=?,sort_order=? WHERE id=?",
               (d['platform'], d['url'], d.get('icon','🔗'), d.get('active',1), d.get('sort_order',0), sid))
    db.commit()
    return jsonify({'success': True})

@app.route('/api/social/<int:sid>', methods=['DELETE'])
def delete_social(sid):
    if not verify_admin(): return jsonify({'error':'Unauthorized'}), 401
    db = get_db()
    db.execute("DELETE FROM social_media WHERE id=?", (sid,))
    db.commit()
    return jsonify({'success': True})

# ── NOTIFICATIONS API ──
@app.route('/api/notifications', methods=['GET'])
def get_notifications():
    if not verify_admin(): return jsonify({'error':'Unauthorized'}), 401
    db = get_db()
    rows = db.execute("SELECT * FROM notifications ORDER BY created_at DESC LIMIT 50").fetchall()
    return jsonify(rows_to_list(rows))

@app.route('/api/notifications/read', methods=['PUT'])
def mark_read():
    if not verify_admin(): return jsonify({'error':'Unauthorized'}), 401
    db = get_db()
    db.execute("UPDATE notifications SET read_flag=1")
    db.commit()
    return jsonify({'success': True})

@app.route('/api/notifications/unread-count', methods=['GET'])
def unread_count():
    if not verify_admin(): return jsonify({'count':0})
    db = get_db()
    count = db.execute("SELECT COUNT(*) FROM notifications WHERE read_flag=0").fetchone()[0]
    return jsonify({'count': count})

# ── CATEGORIES API ──
@app.route('/api/categories', methods=['GET'])
def get_categories():
    db = get_db()
    rows = db.execute("""SELECT c.*, COUNT(p.id) as project_count
        FROM categories c LEFT JOIN projects p ON p.category_id=c.id
        GROUP BY c.id ORDER BY c.name""").fetchall()
    return jsonify(rows_to_list(rows))

# ── PROJECTS API ──
@app.route('/api/projects', methods=['GET'])
def get_projects():
    db = get_db()
    cat = request.args.get('category','')
    search = request.args.get('search','')
    sort = request.args.get('sort','default')
    min_p = request.args.get('min_price', 0, type=float)
    max_p = request.args.get('max_price', 999999, type=float)
    featured = request.args.get('featured','')
    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 12, type=int)
    currency = request.args.get('currency','INR')

    price_col = 'price_aed' if currency == 'AED' else 'price_inr'
    old_col = 'old_price_aed' if currency == 'AED' else 'old_price_inr'

    q = f"""SELECT p.*, c.name as category_name, c.slug as category_slug,
            p.{price_col} as price, p.{old_col} as old_price
            FROM projects p LEFT JOIN categories c ON p.category_id=c.id
            WHERE p.{price_col} BETWEEN ? AND ?"""
    params = [min_p, max_p]

    if cat:
        q += " AND (c.slug=? OR p.tags LIKE ?)"
        params += [cat, f'%{cat}%']
    if search:
        q += " AND (p.title LIKE ? OR p.description LIKE ? OR p.tags LIKE ?)"
        params += [f'%{search}%', f'%{search}%', f'%{search}%']
    if featured == '1':
        q += " AND p.featured=1"

    order_map = {'price-asc':f'p.{price_col} ASC','price-desc':f'p.{price_col} DESC',
                 'newest':'p.created_at DESC','popular':'p.views DESC'}
    q += f" ORDER BY {order_map.get(sort,'p.featured DESC, p.created_at DESC')}"

    total = db.execute(f"SELECT COUNT(*) FROM ({q})", params).fetchone()[0]
    offset = (page-1)*per_page
    rows = db.execute(q + f" LIMIT {per_page} OFFSET {offset}", params).fetchall()
    return jsonify({'projects': rows_to_list(rows), 'total': total, 'page': page, 'per_page': per_page})

@app.route('/api/projects/<slug>', methods=['GET'])
def get_project(slug):
    db = get_db()
    currency = request.args.get('currency','INR')
    price_col = 'price_aed' if currency=='AED' else 'price_inr'
    old_col = 'old_price_aed' if currency=='AED' else 'old_price_inr'
    p = db.execute(f"""SELECT p.*, c.name as category_name,
        p.{price_col} as price, p.{old_col} as old_price
        FROM projects p LEFT JOIN categories c ON p.category_id=c.id WHERE p.slug=?""", (slug,)).fetchone()
    if not p: return jsonify({'error':'Not found'}), 404
    db.execute("UPDATE projects SET views=views+1 WHERE slug=?", (slug,))
    db.commit()
    pd = row_to_dict(p)
    pd['reviews'] = rows_to_list(db.execute("SELECT * FROM reviews WHERE project_id=? AND approved=1 ORDER BY created_at DESC", (p['id'],)).fetchall())
    related = db.execute(f"SELECT *, {price_col} as price FROM projects WHERE category_id=? AND id!=? LIMIT 4", (p['category_id'], p['id'])).fetchall()
    pd['related'] = rows_to_list(related)
    return jsonify(pd)

@app.route('/api/projects', methods=['POST'])
def create_project():
    if not verify_admin(): return jsonify({'error':'Unauthorized'}), 401
    db = get_db()
    d = request.json
    slug = re.sub(r'[^a-z0-9-]','-', d['title'].lower())[:80]
    # Auto-calculate AED if not provided
    rate  = float(get_setting('inr_to_aed','0.044')) * 3  # 300% display
    p_inr = float(d.get('price_inr', d.get('price', 0)) or 0)
    # Respect AED sent from frontend; auto-calc only if missing
    _paed_raw = float(d.get('price_aed', 0) or 0)
    p_aed = round(_paed_raw, 2) if _paed_raw else round(p_inr * rate, 2)
    op_inr = float(d.get('old_price_inr', d.get('old_price', 0) or 0) or 0) or None
    _oaed_raw = float(d.get('old_price_aed', 0) or 0)
    op_aed = round(_oaed_raw, 2) if _oaed_raw else (round(op_inr * rate, 2) if op_inr else None)
    db.execute("""INSERT INTO projects(title,slug,description,abstract,aim,tech_stack,
        price_inr,price_aed,old_price_inr,old_price_aed,badge,badge_type,category_id,tags,
        image_url,level,hardware_components,software_tools,featured,in_stock)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (d['title'],slug,d.get('description'),d.get('abstract'),d.get('aim'),d.get('tech_stack'),
         p_inr, p_aed, op_inr, op_aed, d.get('badge'), d.get('badge_type',''),
         d.get('category_id'), json.dumps(d.get('tags',[])), d.get('image_url'),
         d.get('level','Final Year'), d.get('hardware_components'), d.get('software_tools'),
         d.get('featured',0), d.get('in_stock',1)))
    db.commit()
    return jsonify({'success':True, 'id': db.execute("SELECT last_insert_rowid()").fetchone()[0]})

@app.route('/api/projects/<int:pid>', methods=['PUT'])
def update_project(pid):
    if not verify_admin(): return jsonify({'error':'Unauthorized'}), 401
    db = get_db()
    d = request.json
    rate  = float(get_setting('inr_to_aed','0.044')) * 3  # 300% display
    p_inr = float(d.get('price_inr', d.get('price', 0)) or 0)
    # Respect AED sent from frontend; auto-calc only if missing
    _paed_raw = float(d.get('price_aed', 0) or 0)
    p_aed = round(_paed_raw, 2) if _paed_raw else round(p_inr * rate, 2)
    op_inr = float(d.get('old_price_inr', d.get('old_price', 0) or 0) or 0) or None
    _oaed_raw = float(d.get('old_price_aed', 0) or 0)
    op_aed = round(_oaed_raw, 2) if _oaed_raw else (round(op_inr * rate, 2) if op_inr else None)
    db.execute("""UPDATE projects SET title=?,description=?,abstract=?,aim=?,tech_stack=?,
        price_inr=?,price_aed=?,old_price_inr=?,old_price_aed=?,badge=?,badge_type=?,
        category_id=?,tags=?,image_url=?,level=?,hardware_components=?,software_tools=?,
        featured=?,in_stock=?,updated_at=datetime('now') WHERE id=?""",
        (d['title'],d.get('description'),d.get('abstract'),d.get('aim'),d.get('tech_stack'),
         p_inr,p_aed,op_inr,op_aed,d.get('badge'),d.get('badge_type',''),
         d.get('category_id'),json.dumps(d.get('tags',[])),d.get('image_url'),
         d.get('level','Final Year'),d.get('hardware_components'),d.get('software_tools'),
         d.get('featured',0),d.get('in_stock',1),pid))
    db.commit()
    return jsonify({'success':True})

@app.route('/api/projects/<int:pid>', methods=['DELETE'])
def delete_project(pid):
    if not verify_admin(): return jsonify({'error':'Unauthorized'}), 401
    db = get_db()
    db.execute("DELETE FROM projects WHERE id=?", (pid,))
    db.commit()
    return jsonify({'success':True})

# ── CART API ──
@app.route('/api/cart', methods=['GET'])
def get_cart():
    sid = get_session()
    db = get_db()
    currency = request.args.get('currency','INR')
    price_col = 'price_aed' if currency=='AED' else 'price_inr'
    items = db.execute(f"""SELECT c.id, c.quantity, p.id as project_id, p.title,
        p.{price_col} as price, p.image_url, p.slug
        FROM cart c JOIN projects p ON c.project_id=p.id WHERE c.session_id=?""", (sid,)).fetchall()
    resp = jsonify({'items': rows_to_list(items)})
    return set_cookie(resp, sid)

@app.route('/api/cart', methods=['POST'])
def add_to_cart():
    sid = get_session()
    db = get_db()
    d = request.json
    pid = d.get('project_id')
    qty = d.get('quantity',1)
    ex = db.execute("SELECT id,quantity FROM cart WHERE session_id=? AND project_id=?", (sid,pid)).fetchone()
    if ex:
        db.execute("UPDATE cart SET quantity=? WHERE id=?", (ex['quantity']+qty, ex['id']))
    else:
        db.execute("INSERT INTO cart(session_id,project_id,quantity) VALUES(?,?,?)", (sid,pid,qty))
    db.commit()
    # Notify
    proj = db.execute("SELECT title FROM projects WHERE id=?", (pid,)).fetchone()
    if proj:
        add_notification('cart', '🛒 Item Added to Cart',
            f"Project: {proj['title']}\nQuantity: {qty}", {'project_id': pid})
    resp = jsonify({'success':True})
    return set_cookie(resp, sid)


@app.route('/api/cart/<int:cid>', methods=['PATCH'])
def patch_cart(cid):
    sid = get_session()
    db = get_db()
    d = request.json
    qty = d.get('quantity', 1)
    if qty <= 0:
        db.execute("DELETE FROM cart WHERE id=? AND session_id=?", (cid, sid))
    else:
        db.execute("UPDATE cart SET quantity=? WHERE id=? AND session_id=?", (qty, cid, sid))
    db.commit()
    resp = jsonify({'success': True})
    return set_cookie(resp, sid)

@app.route('/api/cart/<int:cid>', methods=['DELETE'])
def remove_from_cart(cid):
    sid = get_session()
    db = get_db()
    db.execute("DELETE FROM cart WHERE id=? AND session_id=?", (cid, sid))
    db.commit()
    return jsonify({'success':True})

@app.route('/api/cart/clear', methods=['DELETE'])
def clear_cart():
    sid = get_session()
    db = get_db()
    db.execute("DELETE FROM cart WHERE session_id=?", (sid,))
    db.commit()
    return jsonify({'success':True})

# ── WISHLIST API ──
@app.route('/api/wishlist', methods=['GET'])
def get_wishlist():
    sid = get_session()
    db = get_db()
    currency = request.args.get('currency','INR')
    price_col = 'price_aed' if currency=='AED' else 'price_inr'
    items = db.execute(f"""SELECT w.id, p.id as project_id, p.title,
        p.{price_col} as price, p.image_url, p.slug, p.badge
        FROM wishlist w JOIN projects p ON w.project_id=p.id WHERE w.session_id=?""", (sid,)).fetchall()
    resp = jsonify({'items': rows_to_list(items)})
    return set_cookie(resp, sid)

@app.route('/api/wishlist', methods=['POST'])
def toggle_wishlist():
    sid = get_session()
    db = get_db()
    pid = request.json.get('project_id')
    ex = db.execute("SELECT id FROM wishlist WHERE session_id=? AND project_id=?", (sid,pid)).fetchone()
    if ex:
        db.execute("DELETE FROM wishlist WHERE id=?", (ex['id'],))
        action = 'removed'
    else:
        db.execute("INSERT OR IGNORE INTO wishlist(session_id,project_id) VALUES(?,?)", (sid,pid))
        action = 'added'
    db.commit()
    resp = jsonify({'success':True, 'action':action})
    return set_cookie(resp, sid)

# ── ORDERS API ──
@app.route('/api/orders', methods=['POST'])
def place_order():
    sid = get_session()
    db = get_db()
    d = request.json
    order_num = f"DPH{int(time.time())}"
    items = d.get('items',[])
    subtotal = sum(i['price']*i.get('quantity',1) for i in items)
    currency = d.get('currency','INR')
    db.execute("""INSERT INTO orders(order_number,customer_name,customer_email,customer_phone,
        items,subtotal,total,payment_method,notes,address)
        VALUES(?,?,?,?,?,?,?,?,?,?)""",
        (order_num, d.get('name'), d.get('email'), d.get('phone'),
         json.dumps(items), subtotal, subtotal, d.get('payment_method','whatsapp'),
         d.get('notes'), d.get('address')))
    db.execute("DELETE FROM cart WHERE session_id=?", (sid,))
    db.commit()
    # Notification
    items_text = ', '.join([i['title'] for i in items])
    sym = 'AED' if currency=='AED' else '₹'
    add_notification('order', '🛒 New Order Placed!',
        f"Order #{order_num}\nCustomer: {d.get('name')}\nPhone: {d.get('phone')}\nItems: {items_text}\nTotal: {sym}{subtotal:,.0f}",
        {'order_number': order_num})
    return jsonify({'success':True, 'order_number': order_num})

@app.route('/api/orders', methods=['GET'])
def get_orders():
    if not verify_admin(): return jsonify({'error':'Unauthorized'}), 401
    db = get_db()
    orders = db.execute("SELECT * FROM orders ORDER BY created_at DESC").fetchall()
    result = []
    for o in orders:
        od = dict(o)
        try: od['items'] = json.loads(od['items'])
        except: pass
        result.append(od)
    return jsonify(result)

@app.route('/api/orders/<order_num>', methods=['PUT'])
def update_order(order_num):
    if not verify_admin(): return jsonify({'error':'Unauthorized'}), 401
    db = get_db()
    d = request.json
    db.execute("UPDATE orders SET status=?,payment_status=?,updated_at=datetime('now') WHERE order_number=?",
               (d.get('status'), d.get('payment_status'), order_num))
    db.commit()
    return jsonify({'success':True})

# ── REVIEWS API ──
@app.route('/api/reviews', methods=['POST'])
def add_review():
    db = get_db()
    d = request.json
    db.execute("INSERT INTO reviews(project_id,user_name,user_email,rating,review) VALUES(?,?,?,?,?)",
               (d['project_id'], d['name'], d.get('email'), d['rating'], d.get('review')))
    db.commit()
    proj = db.execute("SELECT title FROM projects WHERE id=?", (d['project_id'],)).fetchone()
    add_notification('review', '⭐ New Review Submitted',
        f"Project: {proj['title'] if proj else 'Unknown'}\nBy: {d['name']}\nRating: {'★'*d['rating']}\n{d.get('review','')}",
        {'project_id': d['project_id']})
    return jsonify({'success':True})

@app.route('/api/reviews/<int:rid>/approve', methods=['PUT'])
def approve_review(rid):
    if not verify_admin(): return jsonify({'error':'Unauthorized'}), 401
    db = get_db()
    db.execute("UPDATE reviews SET approved=1 WHERE id=?", (rid,))
    db.commit()
    return jsonify({'success':True})

# ── ENQUIRIES API ──
@app.route('/api/enquiries', methods=['POST'])
def submit_enquiry():
    db = get_db()
    d = request.json
    db.execute("INSERT INTO enquiries(name,email,phone,subject,message,project_id) VALUES(?,?,?,?,?,?)",
               (d['name'], d.get('email'), d['phone'], d.get('subject'), d.get('message'), d.get('project_id')))
    db.commit()
    proj_title = ''
    if d.get('project_id'):
        proj = db.execute("SELECT title FROM projects WHERE id=?", (d['project_id'],)).fetchone()
        if proj: proj_title = proj['title']
    add_notification('enquiry', '📧 New Customer Enquiry!',
        f"From: {d['name']}\nPhone: {d['phone']}\nEmail: {d.get('email','—')}\nProject: {proj_title or 'General'}\n\nMessage: {d.get('message','')}",
        {'name': d['name'], 'phone': d['phone']})
    return jsonify({'success':True, 'message':'Enquiry submitted!'})

@app.route('/api/enquiries', methods=['GET'])
def get_enquiries():
    if not verify_admin(): return jsonify({'error':'Unauthorized'}), 401
    db = get_db()
    rows = db.execute("""SELECT e.*, p.title as project_title FROM enquiries e
        LEFT JOIN projects p ON e.project_id=p.id ORDER BY e.created_at DESC""").fetchall()
    return jsonify(rows_to_list(rows))

@app.route('/api/enquiries/<int:eid>', methods=['PUT'])
def update_enquiry(eid):
    if not verify_admin(): return jsonify({'error':'Unauthorized'}), 401
    db = get_db()
    db.execute("UPDATE enquiries SET status=? WHERE id=?", (request.json.get('status'), eid))
    db.commit()
    return jsonify({'success':True})

# ── NEWSLETTER API ──
@app.route('/api/newsletter', methods=['POST'])
def subscribe_newsletter():
    db = get_db()
    email = request.json.get('email')
    try:
        db.execute("INSERT INTO newsletter(email) VALUES(?)", (email,))
        db.commit()
        add_notification('newsletter','📨 New Newsletter Subscriber', f"Email: {email}")
        return jsonify({'success':True})
    except:
        return jsonify({'success':False, 'message':'Already subscribed'})

# ── BLOG API ──
@app.route('/api/blog', methods=['GET'])
def get_blogs():
    db = get_db()
    rows = db.execute("SELECT * FROM blog_posts WHERE published=1 ORDER BY created_at DESC").fetchall()
    return jsonify(rows_to_list(rows))

@app.route('/api/blog/<slug>', methods=['GET'])
def get_blog(slug):
    db = get_db()
    p = db.execute("SELECT * FROM blog_posts WHERE slug=?", (slug,)).fetchone()
    if not p: return jsonify({'error':'Not found'}), 404
    db.execute("UPDATE blog_posts SET views=views+1 WHERE slug=?", (slug,))
    db.commit()
    return jsonify(row_to_dict(p))

# ── AUTH API ──
@app.route('/api/auth/register', methods=['POST'])
def register():
    db = get_db()
    d = request.json
    pw = hashlib.sha256(d['password'].encode()).hexdigest()
    try:
        db.execute("INSERT INTO users(name,email,phone,password_hash) VALUES(?,?,?,?)",
                   (d['name'], d['email'], d.get('phone'), pw))
        db.commit()
        return jsonify({'success':True})
    except:
        return jsonify({'error':'Email already registered'}), 400

@app.route('/api/auth/login', methods=['POST'])
def login():
    db = get_db()
    d = request.json
    pw = hashlib.sha256(d['password'].encode()).hexdigest()
    user = db.execute("SELECT * FROM users WHERE email=? AND password_hash=?", (d['email'],pw)).fetchone()
    if not user: return jsonify({'error':'Invalid credentials'}), 401
    token = make_token(user['email']) if user['role']=='admin' else None
    return jsonify({'success':True,'name':user['name'],'role':user['role'],'token':token})

# ── ADMIN STATS ──
@app.route('/api/admin/stats', methods=['GET'])
def admin_stats():
    if not verify_admin(): return jsonify({'error':'Unauthorized'}), 401
    db = get_db()
    stats = {
        'total_projects': db.execute("SELECT COUNT(*) FROM projects").fetchone()[0],
        'total_orders': db.execute("SELECT COUNT(*) FROM orders").fetchone()[0],
        'pending_orders': db.execute("SELECT COUNT(*) FROM orders WHERE status='pending'").fetchone()[0],
        'total_enquiries': db.execute("SELECT COUNT(*) FROM enquiries").fetchone()[0],
        'new_enquiries': db.execute("SELECT COUNT(*) FROM enquiries WHERE status='new'").fetchone()[0],
        'total_revenue_inr': db.execute("SELECT COALESCE(SUM(total),0) FROM orders WHERE payment_status='paid'").fetchone()[0],
        'newsletter_count': db.execute("SELECT COUNT(*) FROM newsletter").fetchone()[0],
        'unread_notifications': db.execute("SELECT COUNT(*) FROM notifications WHERE read_flag=0").fetchone()[0],
    }
    recent_orders = db.execute("SELECT * FROM orders ORDER BY created_at DESC LIMIT 5").fetchall()
    stats['recent_orders'] = [dict(o) for o in recent_orders]
    return jsonify(stats)

# ── UPLOAD API ──
@app.route('/api/upload', methods=['POST'])
def upload_image():
    if not verify_admin(): return jsonify({'error':'Unauthorized'}), 401
    if 'file' not in request.files: return jsonify({'error':'No file'}), 400
    f = request.files['file']
    fname = f"{int(time.time())}_{secure_filename(f.filename)}"
    f.save(os.path.join(UPLOAD_FOLDER, fname))
    return jsonify({'url': f'/static/uploads/{fname}'})


@app.route('/api/admin/change-password', methods=['POST'])
def change_password():
    if not verify_admin(): return jsonify({'error':'Unauthorized'}), 401
    db = get_db()
    d = request.json
    pw = d.get('password','')
    if len(pw) < 6: return jsonify({'error':'Password too short'}), 400
    hashed = hashlib.sha256(pw.encode()).hexdigest()
    db.execute("UPDATE users SET password_hash=? WHERE role='admin'", (hashed,))
    db.commit()
    return jsonify({'success':True})

def init_db():
    with app.app_context():
        db = get_db()
        db.executescript("""
        CREATE TABLE IF NOT EXISTS categories (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, slug TEXT UNIQUE NOT NULL, parent_id INTEGER, icon TEXT DEFAULT '📁', description TEXT, created_at TEXT DEFAULT (datetime('now')));
        CREATE TABLE IF NOT EXISTS projects (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, slug TEXT UNIQUE NOT NULL, description TEXT, abstract TEXT, aim TEXT, tech_stack TEXT, price_inr REAL DEFAULT 0, price_aed REAL DEFAULT 0, old_price_inr REAL, old_price_aed REAL, badge TEXT, badge_type TEXT DEFAULT '', category_id INTEGER, tags TEXT DEFAULT '[]', image_url TEXT, images TEXT DEFAULT '[]', level TEXT DEFAULT 'Final Year', hardware_components TEXT, software_tools TEXT, in_stock INTEGER DEFAULT 1, featured INTEGER DEFAULT 0, views INTEGER DEFAULT 0, created_at TEXT DEFAULT (datetime('now')), updated_at TEXT DEFAULT (datetime('now')));
        CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, email TEXT UNIQUE NOT NULL, phone TEXT, password_hash TEXT NOT NULL, role TEXT DEFAULT 'customer', created_at TEXT DEFAULT (datetime('now')));
        CREATE TABLE IF NOT EXISTS orders (id INTEGER PRIMARY KEY AUTOINCREMENT, order_number TEXT UNIQUE NOT NULL, user_id INTEGER, customer_name TEXT, customer_email TEXT, customer_phone TEXT, items TEXT NOT NULL, subtotal REAL, total REAL, status TEXT DEFAULT 'pending', payment_method TEXT, payment_status TEXT DEFAULT 'pending', notes TEXT, address TEXT, created_at TEXT DEFAULT (datetime('now')), updated_at TEXT DEFAULT (datetime('now')));
        CREATE TABLE IF NOT EXISTS cart (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL, project_id INTEGER, quantity INTEGER DEFAULT 1, created_at TEXT DEFAULT (datetime('now')));
        CREATE TABLE IF NOT EXISTS wishlist (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL, project_id INTEGER, created_at TEXT DEFAULT (datetime('now')), UNIQUE(session_id,project_id));
        CREATE TABLE IF NOT EXISTS reviews (id INTEGER PRIMARY KEY AUTOINCREMENT, project_id INTEGER, user_name TEXT NOT NULL, user_email TEXT, rating INTEGER NOT NULL, review TEXT, approved INTEGER DEFAULT 0, created_at TEXT DEFAULT (datetime('now')));
        CREATE TABLE IF NOT EXISTS enquiries (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, email TEXT, phone TEXT NOT NULL, subject TEXT, message TEXT, project_id INTEGER, status TEXT DEFAULT 'new', created_at TEXT DEFAULT (datetime('now')));
        CREATE TABLE IF NOT EXISTS newsletter (id INTEGER PRIMARY KEY AUTOINCREMENT, email TEXT UNIQUE NOT NULL, created_at TEXT DEFAULT (datetime('now')));
        CREATE TABLE IF NOT EXISTS blog_posts (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, slug TEXT UNIQUE NOT NULL, content TEXT, excerpt TEXT, image_url TEXT, author TEXT DEFAULT 'Delta Team', tags TEXT DEFAULT '[]', published INTEGER DEFAULT 1, views INTEGER DEFAULT 0, created_at TEXT DEFAULT (datetime('now')));
        CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL, label TEXT DEFAULT '', updated_at TEXT DEFAULT (datetime('now')));
        CREATE TABLE IF NOT EXISTS social_media (id INTEGER PRIMARY KEY AUTOINCREMENT, platform TEXT NOT NULL UNIQUE, url TEXT NOT NULL, icon TEXT DEFAULT '🔗', active INTEGER DEFAULT 1, sort_order INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS notifications (id INTEGER PRIMARY KEY AUTOINCREMENT, type TEXT NOT NULL, title TEXT NOT NULL, message TEXT NOT NULL, data TEXT DEFAULT '{}', read_flag INTEGER DEFAULT 0, created_at TEXT DEFAULT (datetime('now')));
        """)
        db.commit()

# Initialize DB on startup (important for Railway)
with app.app_context():
    init_db()

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8000))
    app.run(host='0.0.0.0', port=port, debug=False)
