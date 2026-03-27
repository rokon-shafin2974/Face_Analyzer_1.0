import os
import time
import csv
import io
import json
import random
import requests
import psycopg2
import psycopg2.extras
from flask import Flask, request, jsonify, session, redirect, Response, send_from_directory
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from functools import wraps
from dotenv import load_dotenv

from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

# ==========================================
# 1. INITIALIZATION
# ==========================================
load_dotenv()

app = Flask(__name__, static_folder='public', static_url_path='')
app.secret_key = os.environ.get("SECRET_KEY", "super_secret_production_key_123")

def get_user_key():
    return f"user_{session['user_id']}" if 'user_id' in session else get_remote_address()

limiter = Limiter(get_user_key, app=app, default_limits=["500 per day", "100 per hour"], storage_uri="memory://")

DATABASE_URL = os.environ.get("DATABASE_URL")
UPLOAD_FOLDER = os.path.join('public', 'uploads')
WALLPAPER_FOLDER = os.path.join('public', 'wallpapers')
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(WALLPAPER_FOLDER, exist_ok=True)

FACEPP_API_KEY = os.environ.get("FACEPP_API_KEY", "")
FACEPP_API_SECRET = os.environ.get("FACEPP_API_SECRET", "")
PUBLIC_API_KEY = os.environ.get('PUBLIC_API_KEY', 'your-secret-key')

def get_db_connection():
    return psycopg2.connect(DATABASE_URL)

# ==========================================
# 2. DB INITIALIZATION
# ==========================================
def init_db():
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("CREATE TABLE IF NOT EXISTS Users (id SERIAL PRIMARY KEY, username TEXT UNIQUE, password TEXT, role TEXT, email TEXT);")
    c.execute("CREATE TABLE IF NOT EXISTS Persons (id SERIAL PRIMARY KEY, name TEXT);")
    c.execute("CREATE TABLE IF NOT EXISTS Ratings (id SERIAL PRIMARY KEY, person_id INTEGER, rating REAL, image_path TEXT, comment TEXT, emotion TEXT, age_estimate INTEGER, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);")
    c.execute("CREATE TABLE IF NOT EXISTS Settings (key TEXT PRIMARY KEY, value TEXT);")
    
    c.execute("INSERT INTO Settings (key, value) VALUES ('rating_mode', 'ai'), ('bg_url', 'https://images.unsplash.com/photo-1618005182384-a83a8bd57fbe?q=80&w=1964&auto=format&fit=crop'), ('login_bg_url', '') ON CONFLICT DO NOTHING;")
    
    # MASTER ADMIN ENFORCEMENT: Create Shafin if the database is totally empty
    c.execute("SELECT * FROM Users ORDER BY id ASC LIMIT 1")
    if not c.fetchone():
        c.execute("INSERT INTO Users (username, password, role) VALUES ('shafin', %s, 'admin')", (generate_password_hash('29743115'),))
    
    conn.commit()
    conn.close()

# ==========================================
# 3. AUTH DECORATORS
# ==========================================
def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session: return jsonify({"error": "Unauthorized"}), 401
        return f(*args, **kwargs)
    return decorated_function

def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session or session.get('role') != 'admin': return jsonify({"error": "Admin required"}), 403
        return f(*args, **kwargs)
    return decorated_function

# ==========================================
# 4. SETTINGS & BACKGROUND ROUTES
# ==========================================
SETTINGS_FILE = 'settings.json'

def get_settings():
    if os.path.exists(SETTINGS_FILE):
        with open(SETTINGS_FILE, 'r') as f: return json.load(f)
    return {"gallery_visible": True}

@app.route('/api/settings/gallery', methods=['GET', 'POST'])
def gallery_settings():
    if request.method == 'POST':
        if session.get('role') != 'admin': return jsonify({"error": "Unauthorized"}), 403
        settings = get_settings()
        settings["gallery_visible"] = request.json.get("visible", True)
        with open(SETTINGS_FILE, 'w') as f: json.dump(settings, f)
        return jsonify({"success": True})
    return jsonify({"visible": get_settings().get("gallery_visible", True)})

@app.route('/api/settings/prompt', methods=['GET', 'POST'])
@admin_required
def handle_prompt():
    conn = get_db_connection()
    c = conn.cursor()
    if request.method == 'POST':
        prompt = request.json.get('prompt', '')
        c.execute("UPDATE Settings SET value = %s WHERE key = 'ai_prompt'", (prompt,))
        if c.rowcount == 0: c.execute("INSERT INTO Settings (key, value) VALUES ('ai_prompt', %s)", (prompt,))
        conn.commit()
        conn.close()
        return jsonify({"status": "success"})
    else:
        c.execute("SELECT value FROM Settings WHERE key = 'ai_prompt'")
        row = c.fetchone()
        conn.close()
        return jsonify({"prompt": row[0] if row else ""})

@app.route('/api/settings/mode', methods=['GET', 'POST'])
@login_required
def handle_mode():
    conn = get_db_connection()
    c = conn.cursor()
    if request.method == 'POST':
        if session.get('role') != 'admin': return jsonify({"error": "Unauthorized"}), 403
        c.execute("UPDATE Settings SET value = %s WHERE key = 'rating_mode'", (request.json.get('mode'),))
        conn.commit()
        mode = request.json.get('mode')
    else:
        c.execute("SELECT value FROM Settings WHERE key = 'rating_mode'")
        mode = c.fetchone()[0]
    conn.close()
    return jsonify({"mode": mode, "role": session.get('role')})

@app.route('/api/settings/background', methods=['GET', 'POST'])
def handle_background():
    conn = get_db_connection()
    c = conn.cursor()
    if request.method == 'POST':
        if session.get('role') != 'admin': return jsonify({"error": "Unauthorized"}), 403
        target = request.json.get('target', 'workspace')
        key = 'login_bg_url' if target == 'login' else 'bg_url'
        
        c.execute("UPDATE Settings SET value = %s WHERE key = %s", (request.json.get('bg_url'), key))
        if c.rowcount == 0: c.execute("INSERT INTO Settings (key, value) VALUES (%s, %s)", (key, request.json.get('bg_url')))
        
        conn.commit()
        conn.close()
        return jsonify({"status": "success"})
    else:
        c.execute("SELECT key, value FROM Settings WHERE key IN ('bg_url', 'login_bg_url')")
        rows = c.fetchall()
        settings = {row[0]: row[1] for row in rows}
        conn.close()
        return jsonify({
            "bg_url": settings.get('bg_url', ''),
            "login_bg_url": settings.get('login_bg_url', '')
        })

@app.route('/api/settings/background/upload', methods=['POST'])
@admin_required
def upload_background():
    if 'wallpaper' not in request.files: return jsonify({"error": "No file"}), 400
    file = request.files['wallpaper']
    target = request.form.get('target', 'workspace')
    
    if file:
        filename = f"{int(time.time())}_{secure_filename(file.filename)}"
        file.save(os.path.join(WALLPAPER_FOLDER, filename))
        bg_url = f"/wallpapers/{filename}"
        
        conn = get_db_connection()
        c = conn.cursor()
        key = 'login_bg_url' if target == 'login' else 'bg_url'
        c.execute("UPDATE Settings SET value = %s WHERE key = %s", (bg_url, key))
        if c.rowcount == 0: c.execute("INSERT INTO Settings (key, value) VALUES (%s, %s)", (key, bg_url))
        conn.commit()
        conn.close()
        return jsonify({"status": "success", "bg_url": bg_url, "target": target})
    return jsonify({"error": "Upload failed"}), 400

@app.route('/api/settings/backgrounds/local', methods=['GET', 'DELETE'])
@admin_required
def local_wallpapers():
    if request.method == 'GET':
        files = [f"/wallpapers/{f}" for f in os.listdir(WALLPAPER_FOLDER)] if os.path.exists(WALLPAPER_FOLDER) else []
        return jsonify(files)
    
    bg_url = request.json.get('bg_url')
    if bg_url:
        filename = bg_url.replace('/wallpapers/', '')
        file_path = os.path.join(WALLPAPER_FOLDER, secure_filename(filename))
        if os.path.exists(file_path):
            os.remove(file_path)
            return jsonify({"status": "success"})
    return jsonify({"error": "File not found"}), 404

# ==========================================
# 5. USER MANAGEMENT & MASTER ADMIN
# ==========================================
@app.route('/api/users', methods=['GET'])
@admin_required
def get_users():
    conn = get_db_connection()
    c = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    c.execute("SELECT id, username, role FROM Users ORDER BY id ASC")
    users = c.fetchall()
    
    # Flag the Master Admin (Oldest record / Lowest ID)
    if users:
        master_id = users[0]['id']
        for u in users:
            u['is_master'] = (u['id'] == master_id)
            
    conn.close()
    return jsonify(users)

@app.route('/api/users/add', methods=['POST'])
@admin_required
def add_user():
    data = request.json
    hashed_pw = generate_password_hash(data['password'])
    conn = get_db_connection()
    c = conn.cursor()
    try:
        c.execute("INSERT INTO Users (username, password, role) VALUES (%s, %s, %s)", (data['username'], hashed_pw, data['role']))
        conn.commit()
        return jsonify({"status": "success"})
    except:
        conn.rollback()
        return jsonify({"error": "Username already exists."}), 400
    finally:
        conn.close()

@app.route('/api/users/<int:user_id>', methods=['DELETE'])
@admin_required
def delete_user(user_id):
    if user_id == session.get('user_id'): return jsonify({"error": "Cannot delete active account"}), 400
    
    conn = get_db_connection()
    c = conn.cursor()
    
    # MASTER ADMIN PROTECTION: Prevent deleting the lowest ID
    c.execute("SELECT MIN(id) FROM Users")
    master_id = c.fetchone()[0]
    if user_id == master_id:
        conn.close()
        return jsonify({"error": "Action Denied: You cannot delete the Master Admin account."}), 403

    c.execute("DELETE FROM Users WHERE id = %s", (user_id,))
    conn.commit()
    conn.close()
    return jsonify({"status": "success"})

@app.route('/api/users/master', methods=['POST'])
@admin_required
def update_master_admin():
    data = request.json
    current_user = data.get('current_username')
    current_pass = data.get('current_password')
    new_user = data.get('new_username')
    new_pass = data.get('new_password')
    
    conn = get_db_connection()
    c = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
    
    # Find Master Admin
    c.execute("SELECT id, username, password FROM Users ORDER BY id ASC LIMIT 1")
    master = c.fetchone()
    
    if not master:
        conn.close()
        return jsonify({"error": "Master admin not found"}), 404
        
    # VERIFY current credentials perfectly match Master Admin
    if current_user != master['username'] or not check_password_hash(master['password'], current_pass):
        conn.close()
        return jsonify({"error": "Verification failed. Incorrect current username or password."}), 403
        
    # Apply Updates
    hashed_pw = generate_password_hash(new_pass)
    try:
        c.execute("UPDATE Users SET username = %s, password = %s WHERE id = %s", (new_user, hashed_pw, master['id']))
        conn.commit()
        status = "success"
        err = None
    except psycopg2.IntegrityError:
        conn.rollback()
        status = "error"
        err = "That new username is already taken by another user."
    conn.close()
    
    if status == "success": return jsonify({"status": "success"})
    return jsonify({"error": err}), 400

# ==========================================
# 6. CORE AUTH & ROUTING
# ==========================================
@app.route('/')
def index(): return redirect('/login.html')

@app.route('/api/login', methods=['POST'])
@limiter.limit("10 per minute")
def login():
    data = request.json
    conn = get_db_connection()
    c = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
    c.execute("SELECT id, password, role FROM Users WHERE username=%s", (data.get('username'),))
    user = c.fetchone()
    conn.close()

    if user and check_password_hash(user['password'], data.get('password')):
        session['user_id'] = user['id']
        session['role'] = user['role']
        return jsonify({"status": "success", "role": user['role']})
    return jsonify({"error": "Invalid credentials"}), 401

@app.route('/api/logout', methods=['POST'])
def logout():
    session.clear()
    return jsonify({"status": "success"})

# ==========================================
# 7. RATING AI ENGINE
# ==========================================
@app.route('/api/rate', methods=['POST'])
@login_required
@limiter.limit("20 per minute")
def rate():
    name = request.form.get('name')
    image_files = request.files.getlist('images')
    manual_ratings = request.form.getlist('ratings')

    if not image_files or not name: return jsonify({"error": "Missing data"}), 400

    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT id FROM Persons WHERE name = %s", (name,))
    row = c.fetchone()
    p_id = row[0] if row else None
    if not p_id:
        c.execute("INSERT INTO Persons (name) VALUES (%s) RETURNING id", (name,))
        p_id = c.fetchone()[0]

    c.execute("SELECT value FROM Settings WHERE key = 'rating_mode'")
    mode = c.fetchone()[0]
    results = []

    for i, img_file in enumerate(image_files):
        if img_file.filename == '': continue
        
        filename = secure_filename(img_file.filename)
        file_path = os.path.join(UPLOAD_FOLDER, f"{int(time.time())}_{filename}")
        img_file.save(file_path)
        db_path = file_path.replace('public', '')

        current_rating = 0.0
        comment = "Manually rated."
        emotion = "Neutral"
        age = 0

        if mode == 'ai':
            if FACEPP_API_KEY:
                try:
                    with open(file_path, 'rb') as f:
                        response = requests.post(
                            'https://api-us.faceplusplus.com/facepp/v3/detect',
                            data={'api_key': FACEPP_API_KEY, 'api_secret': FACEPP_API_SECRET, 'return_attributes': 'beauty,emotion,age'},
                            files={'image_file': f}
                        )
                    res_data = response.json()
                    if 'faces' in res_data and len(res_data['faces']) > 0:
                        attrs = res_data['faces'][0]['attributes']
                        age = attrs['age']['value']
                        emotions = attrs['emotion']
                        emotion = max(emotions, key=emotions.get).capitalize()
                        beauty = (attrs['beauty']['male_score'] + attrs['beauty']['female_score']) / 2
                        current_rating = round(beauty / 10, 1)
                        comment = f"Cloud AI detected a {age}-year-old feeling {emotion}."
                    else:
                        comment = "No face detected by Cloud AI."
                except Exception as e:
                    print(f"Face++ Error: {e}")
                    comment = "Cloud AI timeout. Fallback used."
                    current_rating = round(random.uniform(5.5, 8.5), 1)
            else:
                current_rating = round(random.uniform(6.0, 9.0), 1)
                emotion = random.choice(["Happy", "Neutral", "Surprise", "Calm"])
                age = random.randint(18, 45)
                comment = f"[Simulated AI] Detected a {age}-year-old feeling {emotion}."
        else:
            current_rating = float(manual_ratings[i]) if i < len(manual_ratings) else 5.0

        c.execute("INSERT INTO Ratings (person_id, rating, image_path, comment, emotion, age_estimate) VALUES (%s, %s, %s, %s, %s, %s)", 
                  (p_id, current_rating, db_path, comment, emotion, age))
        results.append(current_rating)

    conn.commit()
    conn.close()
    return jsonify({"status": "success", "processed": results})

# ==========================================
# 8. DASHBOARD & EXPORT DATA
# ==========================================
@app.route('/api/data', methods=['GET'])
def get_data():
    conn = get_db_connection()
    c = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        c.execute("""
            SELECT p.id, p.name, ROUND(AVG(r.rating)::numeric, 2) as rating, MAX(r.image_path) as image,
                   MAX(r.emotion) as emotion, MAX(r.age_estimate) as age, MAX(r.comment) as comment
            FROM Persons p LEFT JOIN Ratings r ON p.id = r.person_id GROUP BY p.id, p.name ORDER BY p.id DESC
        """)
        return jsonify(c.fetchall())
    finally:
        conn.close()

@app.route('/api/stats', methods=['GET'])
@admin_required
def get_stats():
    conn = get_db_connection()
    c = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    
    c.execute("SELECT COUNT(*) as total_subjects FROM Persons")
    total_subjects = c.fetchone()['total_subjects']
    
    c.execute("SELECT COUNT(*) as total_ratings, COALESCE(ROUND(AVG(rating)::numeric, 2), 0) as avg_rating FROM Ratings")
    ratings_data = c.fetchone()
    
    c.execute("SELECT COUNT(*) as total_users FROM Users")
    total_users = c.fetchone()['total_users']
    
    conn.close()
    
    return jsonify({
        "total_subjects": total_subjects,
        "total_ratings": ratings_data['total_ratings'],
        "avg_rating": float(ratings_data['avg_rating']),
        "total_users": total_users
    })

@app.route('/api/data/<int:person_id>', methods=['DELETE'])
@admin_required
def delete_subject(person_id):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("DELETE FROM Ratings WHERE person_id = %s", (person_id,))
    c.execute("DELETE FROM Persons WHERE id = %s", (person_id,))
    conn.commit()
    conn.close()
    return jsonify({"status": "success"})

@app.route('/api/export', methods=['GET'])
@admin_required
def export_csv():
    conn = get_db_connection()
    c = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    c.execute("""
        SELECT p.id, p.name, ROUND(CAST(AVG(r.rating) AS numeric), 2) as avg_rating 
        FROM Ratings r JOIN Persons p ON r.person_id = p.id 
        GROUP BY p.id, p.name ORDER BY p.id DESC
    """)
    data = c.fetchall()
    conn.close()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['ID', 'Name', 'Average Rating'])
    for row in data:
         writer.writerow([row['id'], row['name'], row['avg_rating']])

    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-disposition": "attachment; filename=face_analyzer_data.csv"}
    )

# ==========================================
# 9. PUBLIC API ROUTE
# ==========================================
def require_api_key(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        api_key = request.headers.get('X-API-Key')
        if not api_key or api_key != PUBLIC_API_KEY:
            return jsonify({"error": "Invalid API key"}), 401
        return f(*args, **kwargs)
    return decorated

@app.route('/api/public/gallery', methods=['GET'])
@require_api_key
def public_gallery():
    conn = get_db_connection()
    c = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    c.execute("""
        SELECT p.name, r.rating, r.image_path, r.emotion, r.comment
        FROM Persons p LEFT JOIN Ratings r ON p.id = r.person_id
        ORDER BY r.created_at DESC LIMIT 50
    """)
    data = c.fetchall()
    conn.close()
    return jsonify(data)

# ==========================================
# 10. STARTUP
# ==========================================
try:
    init_db()
except Exception as e:
    print(f"DB Init Warning: {e}")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))