# app.py
"""
Single-file Flask backend (models + routes + Socket.IO) for a real-time chat app.
It expects the usual Flask folder layout:
  - templates/
    - base.html, login.html, index.html, chat.html
  - static/
    - css/style.css, js/... (or inline scripts in templates)
Adjust templates/static as you like (the assistant previously provided full templates).
"""

import os
from datetime import datetime
from flask import (
    Flask, render_template, render_template_string,
    request, redirect, url_for, session, jsonify, send_from_directory
)
from flask_sqlalchemy import SQLAlchemy
from flask_socketio import SocketIO, emit, join_room, leave_room
from sqlalchemy import desc

# ---------------------------
# Configuration
# ---------------------------
APP_NAME = "FlaskChat"
SECRET_KEY = os.getenv("SECRET_KEY", "change_me_for_prod")
DB_FILENAME = "chat.sqlite"  # stored in instance folder
DEFAULT_PORT = int(os.getenv("PORT", 5000))

# ---------------------------
# App & extensions init
# ---------------------------
app = Flask(__name__, instance_relative_config=True)
app.config["SECRET_KEY"] = SECRET_KEY

# ensure instance folder exists (SQLite will be created there)
os.makedirs(app.instance_path, exist_ok=True)
app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{os.path.join(app.instance_path, DB_FILENAME)}"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db = SQLAlchemy(app)
# Use eventlet if installed; Flask-SocketIO will auto-select if you pass async_mode=None.
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="eventlet")

# ---------------------------
# Models (all here)
# ---------------------------
class Message(db.Model):
    __tablename__ = "messages"
    id = db.Column(db.Integer, primary_key=True)
    room = db.Column(db.String(128), index=True, nullable=False)   # room name (public or dm)
    sender = db.Column(db.String(64), nullable=False)
    text = db.Column(db.Text, nullable=False)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow, index=True)

    def to_dict(self):
        return {
            "id": self.id,
            "room": self.room,
            "sender": self.sender,
            "text": self.text,
            "timestamp": self.timestamp.isoformat()
        }

# ---------------------------
# Create DB tables
# ---------------------------
with app.app_context():
    db.create_all()


# ---------------------------
# In-memory presence tracking
# ---------------------------
# Map session id (sid) -> username
ACTIVE_USERS = {}
# Map username -> sid (last seen)
USERNAME_TO_SID = {}

def broadcast_user_list():
    users = sorted(list(USERNAME_TO_SID.keys()))
    socketio.emit("user_list", {"users": users})

# ---------------------------
# Routes
# ---------------------------
@app.route("/")
def root():
    if "username" in session:
        return redirect(url_for("index"))
    return redirect(url_for("login"))

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        if not username:
            return render_template("login.html", error="Please enter a username.")
        # Simple session login (no password) — replace with Flask-Login for production
        session["username"] = username
        return redirect(url_for("index"))
    return render_template("login.html")

@app.route("/logout")
def logout():
    session.pop("username", None)
    return redirect(url_for("login"))

@app.route("/index")
def index():
    if "username" not in session:
        return redirect(url_for("login"))
    # show recent rooms by distinct room names with latest message timestamp
    # Simple approach: get distinct rooms from Message table (may include DMs)
    rooms = []
    try:
        rows = db.session.query(Message.room).distinct().limit(50).all()
        rooms = [r[0] for r in rows if r[0]]
    except Exception:
        rooms = []
    return render_template("index.html", rooms=rooms, username=session["username"])

@app.route("/chat/<room>")
def chat_room(room):
    if "username" not in session:
        return redirect(url_for("login"))
    username = session["username"]
    # load recent messages for room (limit to last 500)
    messages = Message.query.filter_by(room=room).order_by(Message.timestamp.asc()).limit(500).all()
    return render_template("chat.html", room=room, username=username, messages=messages)

@app.route("/direct_room", methods=["POST"])
def direct_room():
    # returns deterministic DM room name for two usernames (so both can open same DM)
    if "username" not in session:
        return jsonify({"error": "not authenticated"}), 401
    other = None
    if request.is_json:
        other = request.json.get("other")
    else:
        other = request.form.get("other")
    if not other:
        return jsonify({"error": "missing 'other' username"}), 400
    me = session["username"]
    pair = sorted([me, other])
    room = f"dm_{pair[0]}_{pair[1]}"
    return jsonify({"room": room})

# API endpoint to fetch older messages (simple pagination)
@app.route("/api/messages/<room>")
def api_messages(room):
    # query params: before=<iso timestamp> & limit
    limit = int(request.args.get("limit", 50))
    before = request.args.get("before")
    q = Message.query.filter_by(room=room)
    if before:
        try:
            before_dt = datetime.fromisoformat(before)
            q = q.filter(Message.timestamp < before_dt)
        except Exception:
            pass
    msgs = q.order_by(Message.timestamp.desc()).limit(limit).all()
    msgs = list(reversed([m.to_dict() for m in msgs]))  # return ascending
    return jsonify({"messages": msgs})

# ---------------------------
# Socket.IO events
# ---------------------------

@socketio.on("connect")
def on_connect():
    sid = request.sid
    # note: client should emit 'join_app' on connect with username to register presence
    app.logger.debug(f"Client connected: {sid}")

@socketio.on("join_app")
def on_join_app(data):
    """
    Called by client immediately after connecting to associate the socket session with username.
    Payload: { username: "<name>" }
    """
    username = data.get("username")
    if not username:
        return
    sid = request.sid
    ACTIVE_USERS[sid] = username
    USERNAME_TO_SID[username] = sid
    # send updated presence to all
    broadcast_user_list()
    app.logger.debug(f"{username} joined app (sid={sid})")

@socketio.on("disconnect")
def on_disconnect():
    sid = request.sid
    username = ACTIVE_USERS.pop(sid, None)
    if username and USERNAME_TO_SID.get(username) == sid:
        USERNAME_TO_SID.pop(username, None)
    broadcast_user_list()
    app.logger.debug(f"Client disconnected: {sid} ({username})")

@socketio.on("join")
def on_join(data):
    """
    Client joins a room (room name string).
    Payload: { room: "<room>", username: "<user>" }
    """
    room = data.get("room")
    username = data.get("username")
    if not room or not username:
        return
    join_room(room)
    # notify room
    socketio.emit("system_message", {"msg": f"{username} joined the room."}, room=room)
    broadcast_user_list()

@socketio.on("leave")
def on_leave(data):
    """
    Client leaves a room.
    Payload: { room: "<room>", username: "<user>" }
    """
    room = data.get("room")
    username = data.get("username")
    if not room or not username:
        return
    leave_room(room)
    socketio.emit("system_message", {"msg": f"{username} left the room."}, room=room)
    broadcast_user_list()

@socketio.on("send_message")
def on_send_message(data):
    """
    Send a chat message to a room. Saves to DB and emits 'new_message'
    Payload: { room: "<room>", text: "<text>", sender: "<username>" }
    """
    room = data.get("room")
    text = (data.get("text") or "").strip()
    sender = data.get("sender")
    if not room or not text or not sender:
        return
    # Save message
    try:
        msg = Message(room=room, sender=sender, text=text, timestamp=datetime.utcnow())
        db.session.add(msg)
        db.session.commit()
    except Exception as e:
        app.logger.exception("Failed saving message")
        db.session.rollback()
        return
    payload = {
        "id": msg.id,
        "room": msg.room,
        "sender": msg.sender,
        "text": msg.text,
        "timestamp": msg.timestamp.isoformat()
    }
    # Broadcast to room
    socketio.emit("new_message", payload, room=room)

@socketio.on("typing")
def on_typing(data):
    """
    Typing indicator broadcast to room (excluding the typing client).
    Payload: { room: "<room>", sender: "<username>", typing: true/false }
    """
    room = data.get("room")
    sender = data.get("sender")
    is_typing = bool(data.get("typing", False))
    if room and sender:
        emit("typing", {"sender": sender, "typing": is_typing}, room=room, include_self=False)

# ---------------------------
# Static file serving helper (optional)
# ---------------------------
# If you want to serve additional static paths, uncomment / adjust.
# Flask already serves /static/<path:...> via app.static_folder.

# ---------------------------
# Utilities & CLI
# ---------------------------
def run():
    # If eventlet is not installed, SocketIO will fallback and warn.
    # It's recommended to install eventlet for production dev server.
    socketio.run(app, host="0.0.0.0", port=DEFAULT_PORT, debug=True)

if __name__ == "__main__":
    run()
