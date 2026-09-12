from flask import Flask, request, jsonify, send_from_directory
import sqlite3
import hashlib
import secrets
from datetime import date, datetime, timedelta
app = Flask(__name__)
@app.route("/app")
def frontend():
    return send_from_directory("frontend", "index.html")
@app.route("/videos/<path:filename>")
def serve_video(filename):
    return send_from_directory("frontend/videos", filename)

@app.route("/api/watch/start", methods=["POST"])
def start_watch():
    user = get_user_from_token()

    if not user:
        return jsonify({
            "ok": False,
            "error": "Authentication required"
        }), 401

    data = request.get_json(silent=True) or {}

    try:
        task_id = int(data.get("task_id"))
    except (TypeError, ValueError):
        return jsonify({
            "ok": False,
            "error": "Invalid task_id"
        }), 400

    conn = get_db()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT id, reward_points
        FROM tasks
        WHERE id = ? AND active = 1
    """, (task_id,))

    task = cursor.fetchone()

    if not task:
        conn.close()
        return jsonify({
            "ok": False,
            "error": "Task not found or inactive"
        }), 404

    today = date.today().isoformat()

    cursor.execute("""
        SELECT id
        FROM task_completions
        WHERE user_id = ?
        AND task_id = ?
        AND completed_date = ?
    """, (user["id"], task_id, today))

    if cursor.fetchone():
        conn.close()
        return jsonify({
            "ok": False,
            "error": "Task already completed today"
        }), 409

    watch_token = secrets.token_urlsafe(32)
    started_at = datetime.utcnow()
    expires_at = started_at + timedelta(minutes=10)

    cursor.execute("""
        INSERT INTO watch_sessions
        (user_id, task_id, token, started_at, expires_at, last_heartbeat)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (
        user["id"],
        task_id,
        watch_token,
        started_at.isoformat(),
        expires_at.isoformat(),
        started_at.isoformat()
    ))

    conn.commit()
    conn.close()

    return jsonify({
        "ok": True,
        "watch_token": watch_token,
        "required_seconds": 30
    })
@app.route("/api/watch/heartbeat", methods=["POST"])
def watch_heartbeat():
    user = get_user_from_token()

    if not user:
        return jsonify({
            "ok": False,
            "error": "Authentication required"
        }), 401

    data = request.get_json(silent=True) or {}
    watch_token = data.get("watch_token")

    if not watch_token:
        return jsonify({
            "ok": False,
            "error": "Watch token required"
        }), 400

    conn = get_db()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT id, expires_at, verified
        FROM watch_sessions
        WHERE token = ? AND user_id = ?
    """, (watch_token, user["id"]))

    session = cursor.fetchone()

    if not session:
        conn.close()
        return jsonify({
            "ok": False,
            "error": "Invalid watch session"
        }), 404

    if session["verified"]:
        conn.close()
        return jsonify({
            "ok": False,
            "error": "Watch session already verified"
        }), 409

    now = datetime.utcnow()
    expires_at = datetime.fromisoformat(session["expires_at"])

    if now > expires_at:
        conn.close()
        return jsonify({
            "ok": False,
            "error": "Watch session expired"
        }), 410

    cursor.execute("""
        UPDATE watch_sessions
        SET last_heartbeat = ?
        WHERE id = ?
    """, (now.isoformat(), session["id"]))

    conn.commit()
    conn.close()

    return jsonify({
        "ok": True
    })


@app.route("/api/watch/complete", methods=["POST"])
def complete_watch():
    user = get_user_from_token()

    if not user:
        return jsonify({
            "ok": False,
            "error": "Authentication required"
        }), 401

    data = request.get_json(silent=True) or {}
    watch_token = data.get("watch_token")

    if not watch_token:
        return jsonify({
            "ok": False,
            "error": "Watch token required"
        }), 400

    conn = get_db()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT id, task_id, started_at, expires_at, verified, last_heartbeat
        FROM watch_sessions
        WHERE token = ? AND user_id = ?
    """, (watch_token, user["id"]))

    session = cursor.fetchone()

    if not session:
        conn.close()
        return jsonify({
            "ok": False,
            "error": "Invalid watch session"
        }), 404

    if session["verified"]:
        conn.close()
        return jsonify({
            "ok": False,
            "error": "Watch session already verified"
        }), 409

    started_at = datetime.fromisoformat(session["started_at"])
    expires_at = datetime.fromisoformat(session["expires_at"])
    now = datetime.utcnow()

    elapsed = (now - started_at).total_seconds()

    # Require the watch session to be completed through an active
    # client connection. A completed video request received after
    # an expired/disconnected session must not earn a reward.
    if now > expires_at:
        conn.close()
        return jsonify({
            "ok": False,
            "error": "Watch session expired"
        }), 410

    if elapsed < 30:
        remaining = max(1, int(30 - elapsed))

        conn.close()

        return jsonify({
            "ok": False,
            "error": "Video watch time not completed",
            "remaining_seconds": remaining
        }), 400

    # Require a recent heartbeat before awarding the reward.
    # If the client went offline, heartbeats stop and the old
    # session cannot be completed after reconnecting.
    last_heartbeat = session["last_heartbeat"] if "last_heartbeat" in session.keys() else None

    if not last_heartbeat:
        conn.close()
        return jsonify({
            "ok": False,
            "error": "Active watch connection required"
        }), 409

    heartbeat_time = datetime.fromisoformat(last_heartbeat)
    heartbeat_age = (now - heartbeat_time).total_seconds()

    if heartbeat_age > 5:
        conn.close()
        return jsonify({
            "ok": False,
            "error": "Watch connection was interrupted"
        }), 409

    task_id = session["task_id"]
    today = date.today().isoformat()

    cursor.execute("""
        SELECT id
        FROM task_completions
        WHERE user_id = ?
        AND task_id = ?
        AND completed_date = ?
    """, (user["id"], task_id, today))

    if cursor.fetchone():
        conn.close()
        return jsonify({
            "ok": False,
            "error": "Task already completed today"
        }), 409

    cursor.execute("""
        SELECT reward_points
        FROM tasks
        WHERE id = ? AND active = 1
    """, (task_id,))

    task = cursor.fetchone()

    if not task:
        conn.close()
        return jsonify({
            "ok": False,
            "error": "Task not found or inactive"
        }), 404

    reward = task["reward_points"]
    old_balance = user["points"]

    cursor.execute("""
        UPDATE users
        SET points = points + ?
        WHERE id = ?
    """, (reward, user["id"]))

    new_balance = old_balance + reward

    cursor.execute("""
        INSERT INTO point_transactions
        (
            user_id,
            transaction_type,
            amount,
            old_points,
            new_points,
            reference_id,
            description
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (
        user["id"],
        "task_reward",
        reward,
        old_balance,
        new_balance,
        task_id,
        "Task reward"
    ))

    cursor.execute("""
        INSERT INTO task_completions
        (
            user_id,
            task_id,
            completed_date,
            points_earned
        )
        VALUES (?, ?, ?, ?)
    """, (
        user["id"],
        task_id,
        today,
        reward
    ))

    cursor.execute("""
        UPDATE watch_sessions
        SET verified = 1
        WHERE id = ?
    """, (session["id"],))

    conn.commit()

    cursor.execute("""
        SELECT points
        FROM users
        WHERE id = ?
    """, (user["id"],))

    new_balance = cursor.fetchone()["points"]

    conn.close()

    return jsonify({
        "ok": True,
        "message": "Video verified and reward added",
        "reward_points": reward,
        "points": new_balance
    })
@app.route("/watch-video")
def watch_video():
    return """
    <!DOCTYPE html>
    <html>
    <head>
        <title>Watch Video</title>
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <style>
            body{
                font-family:Arial;
                text-align:center;
                padding:20px;
            }
            button{
                padding:15px;
                font-size:18px;
            }
        </style>
    </head>
    <body>

        <h2>🎬 Demo Video Task</h2>

        <video id="video" width="100%" controls playsinline><source src="/videos/demo.mp4" type="video/mp4"></video>

        <p>
            30 second wait karo aur phir button dabao.
        </p>

        <p id="status">▶️ Play video to start watching</p>
        <script>
const token=localStorage.getItem("watchEarnToken");
const taskId=Number(new URLSearchParams(location.search).get("task_id"));
const video=document.getElementById("video");

let watchToken=null,watched=0,last=0,done=false;
let heartbeatTimer=null;
let connectionLost=false;

async function start(){
 if(!token){alert("Please login first");location.href="/app";return false;}

 try{
  const r=await fetch("/api/watch/start",{
   method:"POST",
   headers:{
    "Authorization":"Bearer "+token,
    "Content-Type":"application/json"
   },
   body:JSON.stringify({task_id:taskId}),
   cache:"no-store"
  });

  const d=await r.json();

  if(!d.ok){
   alert("❌ "+d.error);
   return false;
  }

  watchToken=d.watch_token;
  startHeartbeat();
  return true;

 }catch(e){
  alert("❌ Server connection error");
  return false;
 }
}

async function sendHeartbeat(){
 if(!watchToken || done || video.paused) return;

 try{
  const r=await fetch("/api/watch/heartbeat",{
   method:"POST",
   headers:{
    "Authorization":"Bearer "+token,
    "Content-Type":"application/json"
   },
   body:JSON.stringify({watch_token:watchToken}),
   cache:"no-store"
  });

  if(!r.ok){
   connectionLost=true;
   document.getElementById("status").innerText="⚠️ Connection lost. Reconnect and restart video.";
  }else{
   connectionLost=false;
  }

 }catch(e){
  connectionLost=true;
  document.getElementById("status").innerText="⚠️ Internet connection lost. Reward unavailable.";
 }
}

function startHeartbeat(){
 if(heartbeatTimer) return;
 heartbeatTimer=setInterval(sendHeartbeat,2000);
}

function stopHeartbeat(){
 if(heartbeatTimer){
  clearInterval(heartbeatTimer);
  heartbeatTimer=null;
 }
}

video.addEventListener("play",async()=>{
 if(!watchToken && !await start()){
  video.pause();
  return;
 }

 if(connectionLost){
  video.pause();
  alert("❌ Internet connection required. Please reconnect and start the video again.");
  return;
 }

 last=Date.now();
 document.getElementById("status").innerText="▶️ Watching... 0/30 seconds";
});

video.addEventListener("pause",()=>{
 if(last){
  watched+=(Date.now()-last)/1000;
  last=0;
 }
});

setInterval(async()=>{
 if(done||!watchToken||video.paused||connectionLost)return;

 if(last)watched+=(Date.now()-last)/1000;
 last=Date.now();

 document.getElementById("status").innerText=
  "▶️ Watching... "+Math.min(30,Math.floor(watched))+"/30 seconds";

 if(watched>=30){
  done=true;
  video.pause();
  stopHeartbeat();

  document.getElementById("status").innerText="⏳ Verifying reward...";

  if(!navigator.onLine){
   alert("❌ Internet connection required. No reward was added.");
   done=false;
   connectionLost=true;
   return;
  }

  try{
   const r=await fetch("/api/watch/complete",{
    method:"POST",
    headers:{
     "Authorization":"Bearer "+token,
     "Content-Type":"application/json"
    },
    body:JSON.stringify({watch_token:watchToken}),
    cache:"no-store"
   });

   const d=await r.json();

   if(d.ok){
    alert("🎉 Reward mil gaya: "+d.reward_points+" points");
    location.href="/app";
   }else{
    alert("❌ "+d.error);
    done=false;
   }

  }catch(e){
   alert("❌ Internet connection lost. Reward was not added.");
   done=false;
   connectionLost=true;
  }
 }
},1000);

window.addEventListener("offline",()=>{
 connectionLost=true;
 document.getElementById("status").innerText=
  "⚠️ Internet connection lost. Watching stopped.";
});

window.addEventListener("online",()=>{
 if(watchToken && !done){
  document.getElementById("status").innerText=
   "⚠️ Connection restored. Restart the video to begin a new watch session.";
 }
});
        </script>

    </body>
    </html>
    """

DB_NAME = "watch_earn.db"


# =========================
# DATABASE
# =========================

def get_db():
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            salt TEXT NOT NULL,
            points INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            description TEXT,
            reward_points INTEGER NOT NULL,
            active INTEGER DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS watch_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            task_id INTEGER NOT NULL,
            token TEXT UNIQUE NOT NULL,
            started_at TIMESTAMP NOT NULL,
            expires_at TIMESTAMP NOT NULL,
            verified INTEGER DEFAULT 0,
            FOREIGN KEY(user_id) REFERENCES users(id),
            FOREIGN KEY(task_id) REFERENCES tasks(id)
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS task_completions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            task_id INTEGER NOT NULL,
            completed_date TEXT NOT NULL,
            points_earned INTEGER NOT NULL,
            completed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

            UNIQUE(user_id, task_id, completed_date),

            FOREIGN KEY(user_id) REFERENCES users(id),
            FOREIGN KEY(task_id) REFERENCES tasks(id)
        )
    """)

    # Secure login sessions
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            token TEXT UNIQUE NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

            FOREIGN KEY(user_id) REFERENCES users(id)
        )
    """)

    # Create demo tasks only if none exist
    cursor.execute("SELECT COUNT(*) FROM tasks")
    task_count = cursor.fetchone()[0]

    if task_count == 0:
        cursor.execute("""
            INSERT INTO tasks
            (title, description, reward_points)
            VALUES (?, ?, ?)
        """, (
            "Watch Demo Video",
            "Watch the available video and earn points.",
            10
        ))

        cursor.execute("""
            INSERT INTO tasks
            (title, description, reward_points)
            VALUES (?, ?, ?)
        """, (
            "Daily Check-in",
            "Complete your daily check-in.",
            5
        ))

    conn.commit()
    conn.close()


# =========================
# PASSWORD SECURITY
# =========================

def hash_password(password, salt):
    return hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        120000
    ).hex()


# =========================
# AUTHENTICATION
# =========================

def get_user_from_token():

    auth_header = request.headers.get("Authorization", "")

    if not auth_header.startswith("Bearer "):
        return None

    token = auth_header[7:].strip()

    if not token:
        return None

    conn = get_db()

    cursor = conn.cursor()

    cursor.execute("""
        SELECT
            users.id,
            users.username,
            users.points,
            users.is_admin,
            users.is_banned
        FROM sessions
        JOIN users
        ON users.id = sessions.user_id
        WHERE sessions.token = ?
    """, (token,))

    user = cursor.fetchone()

    conn.close()

    if user and user["is_banned"]:
        return None

    return user


# =========================
# HOME
# =========================

@app.route("/")
def home():
    return jsonify({
        "ok": True,
        "app": "Watch & Earn",
        "status": "running"
    })


# =========================
# HEALTH
# =========================

@app.route("/health")
def health():

    try:
        conn = get_db()
        conn.execute("SELECT 1")
        conn.close()

        return jsonify({
            "ok": True,
            "database": "connected"
        })

    except Exception as e:

        return jsonify({
            "ok": False,
            "error": str(e)
        }), 500


# =========================
# REGISTER
# =========================

@app.route("/register", methods=["POST"])
def register():

    data = request.get_json(silent=True) or {}

    username = str(data.get("username", "")).strip()
    password = str(data.get("password", ""))

    if len(username) < 3:
        return jsonify({
            "ok": False,
            "error": "Username must be at least 3 characters"
        }), 400

    if len(password) < 6:
        return jsonify({
            "ok": False,
            "error": "Password must be at least 6 characters"
        }), 400

    salt = secrets.token_bytes(16)

    password_hash = hash_password(
        password,
        salt
    )

    conn = get_db()
    cursor = conn.cursor()

    try:

        cursor.execute("""
            INSERT INTO users
            (username, password_hash, salt)
            VALUES (?, ?, ?)
        """, (
            username,
            password_hash,
            salt.hex()
        ))

        conn.commit()

        user_id = cursor.lastrowid

        return jsonify({
            "ok": True,
            "message": "Account created",
            "user_id": user_id,
            "username": username,
            "points": 0
        })

    except sqlite3.IntegrityError:

        return jsonify({
            "ok": False,
            "error": "Username already exists"
        }), 409

    finally:
        conn.close()


# =========================
# LOGIN
# =========================

@app.route("/login", methods=["POST"])
def login():

    data = request.get_json(silent=True) or {}

    username = str(data.get("username", "")).strip()
    password = str(data.get("password", ""))

    conn = get_db()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT *
        FROM users
        WHERE username = ?
    """, (username,))

    user = cursor.fetchone()

    if not user:
        conn.close()

        return jsonify({
            "ok": False,
            "error": "Invalid username or password"
        }), 401

    salt = bytes.fromhex(user["salt"])

    password_hash = hash_password(
        password,
        salt
    )

    if not secrets.compare_digest(
        password_hash,
        user["password_hash"]
    ):
        conn.close()

        return jsonify({
            "ok": False,
            "error": "Invalid username or password"
        }), 401

    # Block banned users
    if user["is_banned"]:
        conn.close()

        return jsonify({
            "ok": False,
            "error": "Your account has been banned"
        }), 403

    # Create secure random session token
    token = secrets.token_urlsafe(32)

    cursor.execute("""
        INSERT INTO sessions
        (user_id, token)
        VALUES (?, ?)
    """, (
        user["id"],
        token
    ))

    conn.commit()
    conn.close()

    return jsonify({
        "ok": True,
        "message": "Login successful",
        "user_id": user["id"],
        "username": user["username"],
        "points": user["points"],
        "token": token
    })


# =========================
# LOGOUT
# =========================

@app.route("/logout", methods=["POST"])
def logout():

    auth_header = request.headers.get("Authorization", "")

    if not auth_header.startswith("Bearer "):
        return jsonify({
            "ok": False,
            "error": "Authentication required"
        }), 401

    token = auth_header[7:].strip()

    conn = get_db()

    cursor = conn.cursor()

    cursor.execute("""
        DELETE FROM sessions
        WHERE token = ?
    """, (token,))

    conn.commit()
    deleted = cursor.rowcount

    conn.close()

    return jsonify({
        "ok": True,
        "message": "Logged out",
        "session_removed": deleted > 0
    })


# =========================
# MY PROFILE
# =========================

@app.route("/me", methods=["GET"])
def me():

    user = get_user_from_token()

    if not user:
        return jsonify({
            "ok": False,
            "error": "Authentication required"
        }), 401

    return jsonify({
        "ok": True,
        "user": {
            "id": user["id"],
            "username": user["username"],
            "points": user["points"],
            "is_admin": user["is_admin"]
        }
    })


# =========================
# GET TASKS
# =========================

@app.route("/tasks", methods=["GET"])
def get_tasks():

    user = get_user_from_token()

    if not user:
        return jsonify({
            "ok": False,
            "error": "Authentication required"
        }), 401

    conn = get_db()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT
            id,
            title,
            description,
            reward_points
        FROM tasks
        WHERE active = 1
        ORDER BY id ASC
    """)

    tasks = cursor.fetchall()

    conn.close()

    result = []

    for task in tasks:

        result.append({
            "id": task["id"],
            "title": task["title"],
            "description": task["description"],
            "reward_points": task["reward_points"]
        })

    return jsonify({
        "ok": True,
        "tasks": result
    })



# =========================
# ADMIN GET ALL TASKS
# =========================

@app.route("/admin/tasks", methods=["GET"])
def admin_get_tasks():

    user = get_user_from_token()

    if not user:
        return jsonify({
            "ok": False,
            "error": "Unauthorized"
        }), 401

    if not user["is_admin"]:
        return jsonify({
            "ok": False,
            "error": "Admin access required"
        }), 403

    conn = get_db()

    tasks = conn.execute("""
        SELECT
            id,
            title,
            description,
            reward_points,
            active,
            created_at
        FROM tasks
        ORDER BY id ASC
    """).fetchall()

    conn.close()

    result = []

    for task in tasks:
        result.append({
            "id": task["id"],
            "title": task["title"],
            "description": task["description"] or "",
            "reward_points": task["reward_points"],
            "active": task["active"],
            "created_at": task["created_at"]
        })

    return jsonify({
        "ok": True,
        "tasks": result
    })



# =========================
# ADMIN DELETE TASK
# =========================

@app.route("/admin/tasks/<int:task_id>", methods=["DELETE"])
def admin_delete_task(task_id):

    user = get_user_from_token()

    if not user:
        return jsonify({
            "ok": False,
            "error": "Unauthorized"
        }), 401

    if not user["is_admin"]:
        return jsonify({
            "ok": False,
            "error": "Admin access required"
        }), 403

    conn = get_db()

    task = conn.execute(
        "SELECT id, title FROM tasks WHERE id=?",
        (task_id,)
    ).fetchone()

    if not task:
        conn.close()
        return jsonify({
            "ok": False,
            "error": "Task not found"
        }), 404

    try:
        conn.execute(
            "DELETE FROM task_completions WHERE task_id=?",
            (task_id,)
        )

        conn.execute(
            "DELETE FROM watch_sessions WHERE task_id=?",
            (task_id,)
        )

        conn.execute(
            "DELETE FROM tasks WHERE id=?",
            (task_id,)
        )

        conn.commit()

    except Exception:
        conn.rollback()
        conn.close()
        return jsonify({
            "ok": False,
            "error": "Failed to delete task"
        }), 500

    conn.close()

    return jsonify({
        "ok": True,
        "message": "Task deleted successfully",
        "task_id": task_id,
        "title": task["title"]
    })


# =========================
# COMPLETE TASK
# =========================

@app.route("/tasks/<int:task_id>/complete", methods=["POST"])
def complete_task(task_id):

    # User comes from secure session token
    user = get_user_from_token()

    if not user:
        return jsonify({
            "ok": False,
            "error": "Authentication required"
        }), 401

    user_id = user["id"]

    today = date.today().isoformat()

    conn = get_db()
    cursor = conn.cursor()

    # Check task
    cursor.execute("""
        SELECT
            id,
            title,
            reward_points
        FROM tasks
        WHERE id = ?
        AND active = 1
    """, (task_id,))

    task = cursor.fetchone()

    if not task:

        conn.close()

        return jsonify({
            "ok": False,
            "error": "Task not found or inactive"
        }), 404

    # Check today's completion
    cursor.execute("""
        SELECT id
        FROM task_completions
        WHERE user_id = ?
        AND task_id = ?
        AND completed_date = ?
    """, (
        user_id,
        task_id,
        today
    ))

    already_done = cursor.fetchone()

    if already_done:

        conn.close()

        return jsonify({
            "ok": False,
            "error": "Task already completed today"
        }), 409

    reward = task["reward_points"]

    # Capture balance before reward
    old_balance = user["points"]

    # Add points
    cursor.execute("""
        UPDATE users
        SET points = points + ?
        WHERE id = ?
    """, (
        reward,
        user_id
    ))

    # Record point transaction
    new_balance = old_balance + reward

    cursor.execute("""
        INSERT INTO point_transactions
        (
            user_id,
            transaction_type,
            amount,
            old_points,
            new_points,
            reference_id,
            description
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (
        user_id,
        "task_reward",
        reward,
        old_balance,
        new_balance,
        task_id,
        task["title"]
    ))

    # Record completion
    cursor.execute("""
        INSERT INTO task_completions
        (
            user_id,
            task_id,
            completed_date,
            points_earned
        )
        VALUES (?, ?, ?, ?)
    """, (
        user_id,
        task_id,
        today,
        reward
    ))

    conn.commit()

    # New balance
    cursor.execute("""
        SELECT points
        FROM users
        WHERE id = ?
    """, (user_id,))

    new_balance = cursor.fetchone()["points"]

    conn.close()

    return jsonify({
        "ok": True,
        "message": "Task completed",
        "task_id": task_id,
        "reward_points": reward,
        "points": new_balance
    })


# =========================
# START SERVER
# =========================

@app.route("/withdraw", methods=["POST"])
def withdraw():

    user = get_user_from_token()

    if not user:
        return jsonify({
            "ok": False,
            "error": "Unauthorized"
        }), 401

    data = request.get_json(silent=True) or {}

    try:
        amount = int(data.get("amount", 0))
    except (TypeError, ValueError):
        return jsonify({
            "ok": False,
            "error": "Invalid withdrawal amount"
        }), 400

    account_type = str(data.get("account_type", "")).strip()
    account_number = str(data.get("account_number", "")).strip()

    ALLOWED_WITHDRAWAL_METHODS = {
        "Easypaisa",
        "JazzCash",
        "USDT (TRC20)"
    }

    MIN_WITHDRAW = 10

    if amount < MIN_WITHDRAW:
        return jsonify({
            "ok": False,
            "error": f"Minimum withdrawal is {MIN_WITHDRAW} points"
        }), 400

    if not account_type or not account_number:
        return jsonify({
            "ok": False,
            "error": "Account details required"
        }), 400

    if account_type not in ALLOWED_WITHDRAWAL_METHODS:
        return jsonify({
            "ok": False,
            "error": "Invalid withdrawal method"
        }), 400

    if account_type == "USDT (TRC20)":
        import re

        if not re.fullmatch(r"T[1-9A-HJ-NP-Za-km-z]{33}", account_number):
            return jsonify({
                "ok": False,
                "error": "Invalid USDT TRC20 wallet address"
            }), 400

    conn = get_db()

    try:
        conn.execute("BEGIN IMMEDIATE")

        current = conn.execute(
            "SELECT points FROM users WHERE id=?",
            (user["id"],)
        ).fetchone()

        if not current:
            conn.rollback()
            conn.close()
            return jsonify({
                "ok": False,
                "error": "User not found"
            }), 404

        balance = current["points"]

        if balance < amount:
            conn.rollback()
            conn.close()
            return jsonify({
                "ok": False,
                "error": "Insufficient balance"
            }), 400

        cursor = conn.execute("""
            UPDATE users
            SET points = points - ?
            WHERE id = ? AND points >= ?
        """, (
            amount,
            user["id"],
            amount
        ))

        if cursor.rowcount != 1:
            conn.rollback()
            conn.close()
            return jsonify({
                "ok": False,
                "error": "Insufficient balance"
            }), 400

        withdrawal_cursor = conn.execute("""
            INSERT INTO withdrawals
            (
                user_id,
                amount,
                account_type,
                account_number
            )
            VALUES (?, ?, ?, ?)
        """, (
            user["id"],
            amount,
            account_type,
            account_number
        ))

        withdrawal_id = withdrawal_cursor.lastrowid
        new_balance = balance - amount

        conn.execute("""
            INSERT INTO point_transactions
            (
                user_id,
                transaction_type,
                amount,
                old_points,
                new_points,
                reference_id,
                description
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            user["id"],
            "withdrawal",
            -amount,
            balance,
            new_balance,
            withdrawal_id,
            "Withdrawal request"
        ))

        conn.commit()

    except Exception:
        conn.rollback()
        conn.close()
        return jsonify({
            "ok": False,
            "error": "Withdrawal failed"
        }), 500

    conn.close()

    return jsonify({
        "ok": True,
        "message": "Withdrawal request submitted",
        "withdrawal_id": withdrawal_id,
        "remaining_points": new_balance
    })

@app.route("/withdrawals", methods=["GET"])
def withdrawals():
    user = get_user_from_token()

    if not user:
        return jsonify({
            "ok": False,
            "error": "Unauthorized"
        }), 401

    conn = get_db()

    rows = conn.execute("""
        SELECT
            id,
            amount,
            account_type,
            account_number,
            status,
            created_at,
            processed_at,
            admin_note
        FROM withdrawals
        WHERE user_id=?
        ORDER BY id DESC
    """, (user["id"],)).fetchall()

    conn.close()

    history = []

    for row in rows:
        history.append({
            "id": row["id"],
            "amount": row["amount"],
            "account_type": row["account_type"],
            "account_number": row["account_number"],
            "status": row["status"],
            "created_at": row["created_at"],
            "processed_at": row["processed_at"],
            "admin_note": row["admin_note"]
        })

    return jsonify({
        "ok": True,
        "withdrawals": history
    })


@app.route("/transactions", methods=["GET"])
def transactions():
    user = get_user_from_token()

    if not user:
        return jsonify({
            "ok": False,
            "error": "Unauthorized"
        }), 401

    conn = get_db()

    rows = conn.execute("""
        SELECT
            id,
            transaction_type,
            amount,
            old_points,
            new_points,
            reference_id,
            description,
            created_at
        FROM point_transactions
        WHERE user_id=?
        ORDER BY id DESC
        LIMIT 100
    """, (user["id"],)).fetchall()

    conn.close()

    history = []

    for row in rows:
        history.append({
            "id": row["id"],
            "transaction_type": row["transaction_type"],
            "amount": row["amount"],
            "old_points": row["old_points"],
            "new_points": row["new_points"],
            "reference_id": row["reference_id"],
            "description": row["description"],
            "created_at": row["created_at"]
        })

    return jsonify({
        "ok": True,
        "transactions": history
    })



@app.route("/deposits", methods=["POST"])
def create_deposit():
    user = get_user_from_token()

    if not user:
        return jsonify({
            "ok": False,
            "error": "Unauthorized"
        }), 401

    data = request.get_json(silent=True) or {}

    try:
        package_id = int(data.get("package_id", 0))
    except (TypeError, ValueError):
        return jsonify({
            "ok": False,
            "error": "Invalid package"
        }), 400

    payment_method = str(data.get("payment_method", "")).strip()
    payment_reference = str(data.get("payment_reference", "")).strip()

    ALLOWED_DEPOSIT_METHODS = {
        "Easypaisa",
        "JazzCash",
        "USDT (TRC20)"
    }

    if package_id <= 0:
        return jsonify({
            "ok": False,
            "error": "Invalid package"
        }), 400

    if payment_method not in ALLOWED_DEPOSIT_METHODS:
        return jsonify({
            "ok": False,
            "error": "Invalid payment method"
        }), 400

    if not payment_reference:
        return jsonify({
            "ok": False,
            "error": "Payment reference required"
        }), 400

    if len(payment_reference) > 200:
        return jsonify({
            "ok": False,
            "error": "Payment reference is too long"
        }), 400

    conn = get_db()

    try:
        package = conn.execute("""
            SELECT id, name, points, price, currency
            FROM packages
            WHERE id=? AND active=1
        """, (package_id,)).fetchone()

        if not package:
            conn.close()
            return jsonify({
                "ok": False,
                "error": "Package not found or inactive"
            }), 404

        cursor = conn.execute("""
            INSERT INTO deposits
            (
                user_id,
                package_id,
                amount,
                points,
                payment_method,
                payment_reference
            )
            VALUES (?, ?, ?, ?, ?, ?)
        """, (
            user["id"],
            package["id"],
            package["price"],
            package["points"],
            payment_method,
            payment_reference
        ))

        deposit_id = cursor.lastrowid
        conn.commit()

    except Exception:
        conn.rollback()
        conn.close()
        return jsonify({
            "ok": False,
            "error": "Deposit request failed"
        }), 500

    conn.close()

    return jsonify({
        "ok": True,
        "message": "Deposit request submitted",
        "deposit_id": deposit_id,
        "package": package["name"],
        "points": package["points"],
        "amount": package["price"],
        "currency": package["currency"],
        "status": "pending"
    }), 201


@app.route("/deposits", methods=["GET"])
def get_deposits():
    user = get_user_from_token()

    if not user:
        return jsonify({
            "ok": False,
            "error": "Unauthorized"
        }), 401

    conn = get_db()

    rows = conn.execute("""
        SELECT
            d.id,
            d.amount,
            d.points,
            d.payment_method,
            d.payment_reference,
            d.status,
            d.created_at,
            d.processed_at,
            d.admin_note,
            p.name AS package_name
        FROM deposits d
        JOIN packages p ON p.id = d.package_id
        WHERE d.user_id=?
        ORDER BY d.id DESC
    """, (user["id"],)).fetchall()

    conn.close()

    deposits = []

    for row in rows:
        deposits.append({
            "id": row["id"],
            "package": row["package_name"],
            "amount": row["amount"],
            "points": row["points"],
            "payment_method": row["payment_method"],
            "payment_reference": row["payment_reference"],
            "status": row["status"],
            "created_at": row["created_at"],
            "processed_at": row["processed_at"],
            "admin_note": row["admin_note"]
        })

    return jsonify({
        "ok": True,
        "deposits": deposits
    })


@app.route("/admin/tasks", methods=["POST"])
def admin_add_task():
    user = get_user_from_token()

    if not user:
        return jsonify({"ok": False, "error": "Unauthorized"}), 401

    if not user["is_admin"]:
        return jsonify({"ok": False, "error": "Admin access required"}), 403

    data = request.get_json(silent=True) or {}

    title = str(data.get("title", "")).strip()
    description = str(data.get("description", "")).strip()

    try:
        reward_points = int(data.get("reward_points", 0))
    except (TypeError, ValueError):
        reward_points = 0

    if not title:
        return jsonify({"ok": False, "error": "Task title is required"}), 400

    if reward_points <= 0:
        return jsonify({"ok": False, "error": "Reward points must be greater than 0"}), 400

    conn = get_db()

    cursor = conn.execute(
        """
        INSERT INTO tasks (title, description, reward_points, active)
        VALUES (?, ?, ?, 1)
        """,
        (title, description, reward_points)
    )

    task_id = cursor.lastrowid
    conn.commit()
    conn.close()

    return jsonify({
        "ok": True,
        "message": "Task created successfully",
        "task": {
            "id": task_id,
            "title": title,
            "description": description,
            "reward_points": reward_points,
            "active": 1
        }
    }), 201


@app.route("/admin/tasks/<int:task_id>", methods=["PUT"])
def admin_edit_task(task_id):
    user = get_user_from_token()

    if not user:
        return jsonify({"ok": False, "error": "Unauthorized"}), 401

    if not user["is_admin"]:
        return jsonify({"ok": False, "error": "Admin access required"}), 403

    data = request.get_json(silent=True) or {}

    conn = get_db()

    task = conn.execute(
        "SELECT id, title, description, reward_points, active FROM tasks WHERE id=?",
        (task_id,)
    ).fetchone()

    if not task:
        conn.close()
        return jsonify({"ok": False, "error": "Task not found"}), 404

    title = str(data.get("title", task["title"])).strip()
    description = str(data.get("description", task["description"] or "")).strip()

    try:
        reward_points = int(data.get("reward_points", task["reward_points"]))
    except (TypeError, ValueError):
        conn.close()
        return jsonify({"ok": False, "error": "Invalid reward points"}), 400

    active = data.get("active", task["active"])

    if not title:
        conn.close()
        return jsonify({"ok": False, "error": "Task title is required"}), 400

    if reward_points <= 0:
        conn.close()
        return jsonify({"ok": False, "error": "Reward points must be greater than 0"}), 400

    try:
        active = int(active)
    except (TypeError, ValueError):
        active = task["active"]

    active = 1 if active else 0

    conn.execute(
        """
        UPDATE tasks
        SET title=?, description=?, reward_points=?, active=?
        WHERE id=?
        """,
        (title, description, reward_points, active, task_id)
    )

    conn.commit()
    conn.close()

    return jsonify({
        "ok": True,
        "message": "Task updated successfully",
        "task": {
            "id": task_id,
            "title": title,
            "description": description,
            "reward_points": reward_points,
            "active": active
        }
    })


@app.route("/admin/tasks/<int:task_id>/toggle", methods=["POST"])
def admin_toggle_task(task_id):
    user = get_user_from_token()

    if not user:
        return jsonify({"ok": False, "error": "Unauthorized"}), 401

    if not user["is_admin"]:
        return jsonify({"ok": False, "error": "Admin access required"}), 403

    conn = get_db()

    task = conn.execute(
        "SELECT id, title, active FROM tasks WHERE id=?",
        (task_id,)
    ).fetchone()

    if not task:
        conn.close()
        return jsonify({"ok": False, "error": "Task not found"}), 404

    new_active = 0 if task["active"] else 1

    conn.execute(
        "UPDATE tasks SET active=? WHERE id=?",
        (new_active, task_id)
    )

    conn.commit()
    conn.close()

    return jsonify({
        "ok": True,
        "message": "Task enabled" if new_active else "Task disabled",
        "task": {
            "id": task_id,
            "title": task["title"],
            "active": new_active
        }
    })


@app.route("/admin/stats", methods=["GET"])
def admin_stats():

    user = get_user_from_token()

    if not user:
        return jsonify({
            "ok": False,
            "error": "Unauthorized"
        }), 401

    if not user["is_admin"]:
        return jsonify({
            "ok": False,
            "error": "Admin access required"
        }), 403

    conn = get_db()

    total_users = conn.execute(
        "SELECT COUNT(*) AS count FROM users"
    ).fetchone()["count"]

    pending_amount = conn.execute(
        "SELECT COALESCE(SUM(amount), 0) AS total FROM withdrawals WHERE status='pending'"
    ).fetchone()["total"]

    approved_amount = conn.execute(
        "SELECT COALESCE(SUM(amount), 0) AS total FROM withdrawals WHERE status='approved'"
    ).fetchone()["total"]

    rejected_amount = conn.execute(
        "SELECT COALESCE(SUM(amount), 0) AS total FROM withdrawals WHERE status='rejected'"
    ).fetchone()["total"]

    conn.close()

    return jsonify({
        "ok": True,
        "total_users": total_users,
        "pending_amount": pending_amount,
        "approved_amount": approved_amount,
        "rejected_amount": rejected_amount
    })


@app.route("/admin/users", methods=["GET"])
def admin_users():

    user = get_user_from_token()

    if not user:
        return jsonify({
            "ok": False,
            "error": "Unauthorized"
        }), 401

    if not user["is_admin"]:
        return jsonify({
            "ok": False,
            "error": "Admin access required"
        }), 403

    # Optional search parameter
    search = request.args.get("search", "").strip()

    conn = get_db()

    if search:
        rows = conn.execute("""
            SELECT id, username, points, is_admin, is_banned, created_at
            FROM users
            WHERE username LIKE ? OR CAST(id AS TEXT) LIKE ?
            ORDER BY id DESC
        """, (
            f"%{search}%",
            f"%{search}%"
        )).fetchall()
    else:
        rows = conn.execute("""
            SELECT id, username, points, is_admin, is_banned, created_at
            FROM users
            ORDER BY id DESC
        """).fetchall()

    conn.close()

    users = []

    for row in rows:
        users.append({
            "id": row["id"],
            "username": row["username"],
            "points": row["points"],
            "is_admin": row["is_admin"],
            "is_banned": row["is_banned"],
            "created_at": row["created_at"]
        })

    return jsonify({
        "ok": True,
        "users": users
    })




@app.route("/admin/users/<int:user_id>/points", methods=["POST"])
def admin_update_user_points(user_id):

    admin = get_user_from_token()

    if not admin:
        return jsonify({
            "ok": False,
            "error": "Unauthorized"
        }), 401

    if not admin["is_admin"]:
        return jsonify({
            "ok": False,
            "error": "Admin access required"
        }), 403

    data = request.get_json(silent=True) or {}

    try:
        amount = int(data.get("amount", 0))
    except (TypeError, ValueError):
        return jsonify({
            "ok": False,
            "error": "Amount must be a valid integer"
        }), 400

    if amount == 0:
        return jsonify({
            "ok": False,
            "error": "Amount cannot be zero"
        }), 400

    reason = str(data.get("reason", "")).strip()

    conn = get_db()

    try:
        conn.execute("BEGIN IMMEDIATE")

        target = conn.execute("""
            SELECT id, username, points
            FROM users
            WHERE id = ?
        """, (user_id,)).fetchone()

        if not target:
            conn.rollback()
            conn.close()
            return jsonify({
                "ok": False,
                "error": "User not found"
            }), 404

        old_points = target["points"]
        new_points = old_points + amount

        if new_points < 0:
            conn.rollback()
            conn.close()
            return jsonify({
                "ok": False,
                "error": "User points cannot go below zero"
            }), 400

        conn.execute("""
            UPDATE users
            SET points = ?
            WHERE id = ?
        """, (new_points, user_id))

        conn.execute("""
            INSERT INTO admin_point_logs
            (admin_user_id, target_user_id, amount, old_points, new_points, reason)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (
            admin["id"],
            user_id,
            amount,
            old_points,
            new_points,
            reason
        ))

        transaction_type = "admin_add" if amount > 0 else "admin_remove"

        conn.execute("""
            INSERT INTO point_transactions
            (
                user_id,
                transaction_type,
                amount,
                old_points,
                new_points,
                reference_id,
                description
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            user_id,
            transaction_type,
            amount,
            old_points,
            new_points,
            admin["id"],
            reason if reason else "Admin point adjustment"
        ))

        conn.commit()

    except Exception:
        conn.rollback()
        conn.close()
        return jsonify({
            "ok": False,
            "error": "Failed to update points"
        }), 500

    conn.close()

    return jsonify({
        "ok": True,
        "message": "Points updated successfully",
        "user": {
            "id": target["id"],
            "username": target["username"],
            "points": new_points,
            "change": amount
        }
    })


@app.route("/admin/point-history", methods=["GET"])
def admin_point_history():

    admin = get_user_from_token()

    if not admin:
        return jsonify({
            "ok": False,
            "error": "Unauthorized"
        }), 401

    if not admin["is_admin"]:
        return jsonify({
            "ok": False,
            "error": "Admin access required"
        }), 403

    search = request.args.get("search", "").strip()

    conn = get_db()

    if search:
        logs = conn.execute("""
            SELECT
                l.id,
                l.admin_user_id,
                admin.username AS admin_username,
                l.target_user_id,
                target.username AS target_username,
                l.amount,
                l.old_points,
                l.new_points,
                l.reason,
                l.created_at
            FROM admin_point_logs l
            JOIN users admin ON admin.id = l.admin_user_id
            JOIN users target ON target.id = l.target_user_id
            WHERE target.username LIKE ?
               OR CAST(target.id AS TEXT) LIKE ?
            ORDER BY l.id DESC
        """, (f"%{search}%", f"%{search}%")).fetchall()
    else:
        logs = conn.execute("""
            SELECT
                l.id,
                l.admin_user_id,
                admin.username AS admin_username,
                l.target_user_id,
                target.username AS target_username,
                l.amount,
                l.old_points,
                l.new_points,
                l.reason,
                l.created_at
            FROM admin_point_logs l
            JOIN users admin ON admin.id = l.admin_user_id
            JOIN users target ON target.id = l.target_user_id
            ORDER BY l.id DESC
        """).fetchall()

    conn.close()

    return jsonify({
        "ok": True,
        "history": [dict(row) for row in logs]
    })


@app.route("/admin/users/<int:user_id>/ban", methods=["POST"])
def admin_toggle_user_ban(user_id):

    admin = get_user_from_token()

    if not admin:
        return jsonify({
            "ok": False,
            "error": "Unauthorized"
        }), 401

    if not admin["is_admin"]:
        return jsonify({
            "ok": False,
            "error": "Admin access required"
        }), 403

    data = request.get_json(silent=True) or {}

    banned = data.get("banned")

    if not isinstance(banned, bool):
        return jsonify({
            "ok": False,
            "error": "banned must be true or false"
        }), 400

    conn = get_db()

    target = conn.execute("""
        SELECT id, username, is_banned
        FROM users
        WHERE id = ?
    """, (user_id,)).fetchone()

    if not target:
        conn.close()
        return jsonify({
            "ok": False,
            "error": "User not found"
        }), 404

    # Prevent admin from accidentally banning/unbanning self
    if target["id"] == admin["id"]:
        conn.close()
        return jsonify({
            "ok": False,
            "error": "You cannot change your own ban status"
        }), 400

    conn.execute("""
        UPDATE users
        SET is_banned = ?
        WHERE id = ?
    """, (1 if banned else 0, user_id))

    # Immediately invalidate all existing sessions when banning
    if banned:
        conn.execute("""
            DELETE FROM sessions
            WHERE user_id = ?
        """, (user_id,))

    conn.commit()
    conn.close()

    return jsonify({
        "ok": True,
        "message": "User banned successfully" if banned else "User unbanned successfully",
        "user": {
            "id": target["id"],
            "username": target["username"],
            "is_banned": banned
        }
    })


@app.route("/admin/withdrawals", methods=["GET"])
def admin_withdrawals():

    user = get_user_from_token()

    if not user:
        return jsonify({
            "ok": False,
            "error": "Unauthorized"
        }), 401

    if not user["is_admin"]:
        return jsonify({
            "ok": False,
            "error": "Admin access required"
        }), 403

    conn = get_db()

    rows = conn.execute("""
        SELECT
            w.id,
            w.user_id,
            u.username,
            w.amount,
            w.account_type,
            w.account_number,
            w.status,
            w.created_at,
            w.processed_at,
            w.admin_note
        FROM withdrawals w
        JOIN users u ON u.id = w.user_id
        ORDER BY w.id DESC
    """).fetchall()

    conn.close()

    withdrawals = []

    for row in rows:
        withdrawals.append({
            "id": row["id"],
            "user_id": row["user_id"],
            "username": row["username"],
            "amount": row["amount"],
            "account_type": row["account_type"],
            "account_number": row["account_number"],
            "status": row["status"],
            "created_at": row["created_at"],
            "processed_at": row["processed_at"],
            "admin_note": row["admin_note"]
        })

    return jsonify({
        "ok": True,
        "withdrawals": withdrawals
    })



@app.route("/admin/withdrawals/<int:withdrawal_id>/approve", methods=["POST"])
def approve_withdrawal(withdrawal_id):

    user = get_user_from_token()

    if not user:
        return jsonify({
            "ok": False,
            "error": "Unauthorized"
        }), 401

    if not user["is_admin"]:
        return jsonify({
            "ok": False,
            "error": "Admin access required"
        }), 403

    conn = get_db()

    try:
        conn.execute("BEGIN IMMEDIATE")

        withdrawal = conn.execute("""
            SELECT id, amount, status
            FROM withdrawals
            WHERE id=?
        """, (withdrawal_id,)).fetchone()

        if not withdrawal:
            conn.rollback()
            return jsonify({
                "ok": False,
                "error": "Withdrawal not found"
            }), 404

        if withdrawal["status"] != "pending":
            conn.rollback()
            return jsonify({
                "ok": False,
                "error": "Withdrawal is already processed"
            }), 400

        updated = conn.execute("""
            UPDATE withdrawals
            SET status='approved',
                processed_at=CURRENT_TIMESTAMP
            WHERE id=? AND status='pending'
        """, (withdrawal_id,))

        if updated.rowcount != 1:
            conn.rollback()
            return jsonify({
                "ok": False,
                "error": "Withdrawal is already processed"
            }), 400

        conn.commit()

        return jsonify({
            "ok": True,
            "message": "Withdrawal approved",
            "withdrawal_id": withdrawal_id
        })

    except Exception:
        conn.rollback()
        return jsonify({
            "ok": False,
            "error": "Failed to approve withdrawal"
        }), 500

    finally:
        conn.close()



@app.route("/admin/withdrawals/<int:withdrawal_id>/reject", methods=["POST"])
def reject_withdrawal(withdrawal_id):

    user = get_user_from_token()

    if not user:
        return jsonify({
            "ok": False,
            "error": "Unauthorized"
        }), 401

    if not user["is_admin"]:
        return jsonify({
            "ok": False,
            "error": "Admin access required"
        }), 403

    data = request.get_json(silent=True) or {}
    admin_note = str(data.get("admin_note", "")).strip()

    conn = get_db()

    try:
        conn.execute("BEGIN IMMEDIATE")

        withdrawal = conn.execute("""
            SELECT id, user_id, amount, status
            FROM withdrawals
            WHERE id=?
        """, (withdrawal_id,)).fetchone()

        if not withdrawal:
            conn.rollback()
            return jsonify({
                "ok": False,
                "error": "Withdrawal not found"
            }), 404

        if withdrawal["status"] != "pending":
            conn.rollback()
            return jsonify({
                "ok": False,
                "error": "Withdrawal is already processed"
            }), 400

        balance_row = conn.execute("""
            SELECT points
            FROM users
            WHERE id=?
        """, (withdrawal["user_id"],)).fetchone()

        if not balance_row:
            conn.rollback()
            return jsonify({
                "ok": False,
                "error": "User not found"
            }), 404

        old_balance = balance_row["points"]
        new_balance = old_balance + withdrawal["amount"]

        conn.execute("""
            UPDATE users
            SET points = points + ?
            WHERE id=?
        """, (
            withdrawal["amount"],
            withdrawal["user_id"]
        ))

        updated = conn.execute("""
            UPDATE withdrawals
            SET status='rejected',
                processed_at=CURRENT_TIMESTAMP,
                admin_note=?
            WHERE id=? AND status='pending'
        """, (
            admin_note,
            withdrawal_id
        ))

        if updated.rowcount != 1:
            conn.rollback()
            return jsonify({
                "ok": False,
                "error": "Withdrawal is already processed"
            }), 400

        conn.execute("""
            INSERT INTO point_transactions
            (
                user_id,
                transaction_type,
                amount,
                old_points,
                new_points,
                reference_id,
                description
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            withdrawal["user_id"],
            "withdrawal_refund",
            withdrawal["amount"],
            old_balance,
            new_balance,
            withdrawal_id,
            "Withdrawal rejected - points refunded"
        ))

        conn.commit()

        return jsonify({
            "ok": True,
            "message": "Withdrawal rejected and points refunded",
            "withdrawal_id": withdrawal_id,
            "refunded_points": withdrawal["amount"],
            "user_balance": new_balance
        })

    except Exception:
        conn.rollback()
        return jsonify({
            "ok": False,
            "error": "Failed to reject withdrawal"
        }), 500

    finally:
        conn.close()

@app.route("/packages", methods=["GET"])
def get_packages():
    user = get_user_from_token()

    if not user:
        return jsonify({
            "ok": False,
            "error": "Unauthorized"
        }), 401

    conn = get_db()

    rows = conn.execute("""
        SELECT id, name, points, price, currency
        FROM packages
        WHERE active=1
        ORDER BY points ASC
    """).fetchall()

    conn.close()

    packages = []

    for row in rows:
        packages.append({
            "id": row["id"],
            "name": row["name"],
            "points": row["points"],
            "price": row["price"],
            "currency": row["currency"]
        })

    return jsonify({
        "ok": True,
        "packages": packages
    })


# =========================
# ADMIN DEPOSIT MANAGEMENT
# =========================

@app.route("/admin/deposits", methods=["GET"])
def admin_get_deposits():

    user = get_user_from_token()

    if not user:
        return jsonify({
            "ok": False,
            "error": "Unauthorized"
        }), 401

    if not user["is_admin"]:
        return jsonify({
            "ok": False,
            "error": "Admin access required"
        }), 403

    conn = get_db()

    rows = conn.execute("""
        SELECT
            d.id,
            d.user_id,
            u.username,
            d.package_id,
            p.name AS package_name,
            d.amount,
            d.points,
            d.payment_method,
            d.payment_reference,
            d.status,
            d.created_at,
            d.processed_at,
            d.admin_note
        FROM deposits d
        JOIN users u ON u.id = d.user_id
        JOIN packages p ON p.id = d.package_id
        ORDER BY d.id DESC
    """).fetchall()

    conn.close()

    deposits = []

    for row in rows:
        deposits.append({
            "id": row["id"],
            "user_id": row["user_id"],
            "username": row["username"],
            "package_id": row["package_id"],
            "package_name": row["package_name"],
            "amount": row["amount"],
            "points": row["points"],
            "payment_method": row["payment_method"],
            "payment_reference": row["payment_reference"],
            "status": row["status"],
            "created_at": row["created_at"],
            "processed_at": row["processed_at"],
            "admin_note": row["admin_note"]
        })

    return jsonify({
        "ok": True,
        "deposits": deposits
    })


# =========================
# ADMIN APPROVE DEPOSIT
# =========================

@app.route("/admin/deposits/<int:deposit_id>/approve", methods=["POST"])
def admin_approve_deposit(deposit_id):

    user = get_user_from_token()

    if not user:
        return jsonify({
            "ok": False,
            "error": "Unauthorized"
        }), 401

    if not user["is_admin"]:
        return jsonify({
            "ok": False,
            "error": "Admin access required"
        }), 403

    conn = get_db()

    try:
        conn.execute("BEGIN IMMEDIATE")

        deposit = conn.execute("""
            SELECT
                d.id,
                d.user_id,
                d.points,
                d.status,
                u.points AS user_points
            FROM deposits d
            JOIN users u ON u.id = d.user_id
            WHERE d.id = ?
        """, (deposit_id,)).fetchone()

        if not deposit:
            conn.rollback()

            return jsonify({
                "ok": False,
                "error": "Deposit not found"
            }), 404

        if deposit["status"] != "pending":
            conn.rollback()

            return jsonify({
                "ok": False,
                "error": "Deposit is not pending"
            }), 409

        old_points = deposit["user_points"]
        new_points = old_points + deposit["points"]

        cursor = conn.execute("""
            UPDATE deposits
            SET status = 'approved',
                processed_at = CURRENT_TIMESTAMP
            WHERE id = ?
              AND status = 'pending'
        """, (deposit_id,))

        if cursor.rowcount != 1:
            conn.rollback()

            return jsonify({
                "ok": False,
                "error": "Deposit was already processed"
            }), 409

        conn.execute("""
            UPDATE users
            SET points = points + ?
            WHERE id = ?
        """, (
            deposit["points"],
            deposit["user_id"]
        ))

        conn.execute("""
            INSERT INTO point_transactions
            (
                user_id,
                transaction_type,
                amount,
                old_points,
                new_points,
                reference_id,
                description
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            deposit["user_id"],
            "deposit",
            deposit["points"],
            old_points,
            new_points,
            deposit_id,
            "Deposit approved"
        ))

        conn.commit()

        return jsonify({
            "ok": True,
            "message": "Deposit approved",
            "deposit_id": deposit_id,
            "credited_points": deposit["points"],
            "user_balance": new_points
        })

    except Exception as e:
        conn.rollback()

        return jsonify({
            "ok": False,
            "error": "Failed to approve deposit"
        }), 500

    finally:
        conn.close()

@app.route("/admin/deposits/<int:deposit_id>/reject", methods=["POST"])
def admin_reject_deposit(deposit_id):
    user = get_user_from_token()
    if not user:
        return jsonify({"ok": False, "error": "Authentication required"}), 401

    if not user["is_admin"]:
        return jsonify({"ok": False, "error": "Admin access required"}), 403

    conn = get_db()

    try:
        conn.execute("BEGIN IMMEDIATE")

        deposit = conn.execute("""
            SELECT id, user_id, points, status
            FROM deposits
            WHERE id = ?
        """, (deposit_id,)).fetchone()

        if not deposit:
            conn.rollback()
            return jsonify({
                "ok": False,
                "error": "Deposit not found"
            }), 404

        if deposit["status"] != "pending":
            conn.rollback()
            return jsonify({
                "ok": False,
                "error": "Deposit is not pending"
            }), 409

        cur = conn.execute("""
            UPDATE deposits
            SET status = 'rejected',
                processed_at = CURRENT_TIMESTAMP
            WHERE id = ?
              AND status = 'pending'
        """, (deposit_id,))

        if cur.rowcount != 1:
            conn.rollback()
            return jsonify({
                "ok": False,
                "error": "Deposit is not pending"
            }), 409

        conn.commit()

        return jsonify({
            "ok": True,
            "message": "Deposit rejected",
            "deposit_id": deposit_id,
            "refunded_points": 0
        }), 200

    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass

        return jsonify({
            "ok": False,
            "error": "Internal server error"
        }), 500

    finally:
        conn.close()


if __name__ == "__main__":

    init_db()

    print("================================")
    print("     WATCH & EARN BACKEND")
    print("================================")
    print("Database: READY")
    print("Authentication: SECURE")
    print("Server: STARTING")
    print("================================")

    app.run(
        host="0.0.0.0",
        port=5000,
        debug=False
    )
