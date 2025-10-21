import os
from datetime import datetime
from flask import Flask, render_template, request, redirect, url_for, session, jsonify
from flask_sqlalchemy import SQLAlchemy
from flask_socketio import SocketIO, join_room, leave_room, emit
from werkzeug.security import generate_password_hash, check_password_hash

# --------------------------
# Config
# --------------------------
app = Flask(__name__)
app.config['SECRET_KEY'] = 'change_this_secret_key'
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///chat.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="eventlet")

# --------------------------
# Models
# --------------------------
class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(64), unique=True, nullable=False)
    password_hash = db.Column(db.String(128), nullable=False)

class Message(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    room = db.Column(db.String(128), index=True, nullable=False)
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

# --------------------------
# DB init
# --------------------------
with app.app_context():
    db.create_all()

# --------------------------
# In-memory tracking
# --------------------------
ACTIVE_USERS = {}
USERNAME_TO_SID = {}

def broadcast_user_list():
    users = sorted(list(USERNAME_TO_SID.keys()))
    socketio.emit("user_list", {"users": users})

# --------------------------
# Routes
# --------------------------
@app.route("/")
def root():
    if "username" in session:
        return redirect(url_for("index"))
    return redirect(url_for("login"))

@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        username = request.form.get("username").strip()
        password = request.form.get("password").strip()
        if not username or not password:
            return render_template("register.html", error="Please fill all fields")
        if User.query.filter_by(username=username).first():
            return render_template("register.html", error="Username already exists")
        hashed = generate_password_hash(password)
        user = User(username=username, password_hash=hashed)
        db.session.add(user)
        db.session.commit()
        session["username"] = username
        return redirect(url_for("index"))
    return render_template("register.html")

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username").strip()
        password = request.form.get("password").strip()
        user = User.query.filter_by(username=username).first()
        if user and check_password_hash(user.password_hash, password):
            session["username"] = username
            return redirect(url_for("index"))
        return render_template("login.html", error="Invalid credentials")
    return render_template("login.html")

@app.route("/logout")
def logout():
    session.pop("username", None)
    return redirect(url_for("login"))

@app.route("/index")
def index():
    if "username" not in session:
        return redirect(url_for("login"))
    rooms = []
    try:
        rows = db.session.query(Message.room).distinct().limit(50).all()
        rooms = [r[0] for r in rows if r[0]]
    except:
        pass
    return render_template("index.html", rooms=rooms, username=session["username"])

@app.route("/chat/<room>")
def chat_room(room):
    if "username" not in session:
        return redirect(url_for("login"))
    username = session["username"]
    messages = Message.query.filter_by(room=room).order_by(Message.timestamp.asc()).all()
    return render_template("chat.html", room=room, username=username, messages=messages)

@app.route("/direct_room", methods=["POST"])
def direct_room():
    if "username" not in session:
        return jsonify({"error": "not authenticated"}), 401
    other = request.form.get("other")
    if not other:
        return jsonify({"error": "missing 'other' username"}), 400
    me = session["username"]
    pair = sorted([me, other])
    room = f"dm_{pair[0]}_{pair[1]}"
    return jsonify({"room": room})

@app.route("/api/messages/<room>")
def api_messages(room):
    limit = int(request.args.get("limit", 50))
    before = request.args.get("before")
    q = Message.query.filter_by(room=room)
    if before:
        try:
            before_dt = datetime.fromisoformat(before)
            q = q.filter(Message.timestamp < before_dt)
        except:
            pass
    msgs = q.order_by(Message.timestamp.desc()).limit(limit).all()
    msgs = list(reversed([m.to_dict() for m in msgs]))
    return jsonify({"messages": msgs})

# --------------------------
# SocketIO Events
# --------------------------
@socketio.on("connect")
def on_connect():
    pass

@socketio.on("join_app")
def on_join_app(data):
    username = data.get("username")
    if not username:
        return
    sid = request.sid
    ACTIVE_USERS[sid] = username
    USERNAME_TO_SID[username] = sid
    broadcast_user_list()

@socketio.on("disconnect")
def on_disconnect():
    sid = request.sid
    username = ACTIVE_USERS.pop(sid, None)
    if username and USERNAME_TO_SID.get(username) == sid:
        USERNAME_TO_SID.pop(username, None)
    broadcast_user_list()

@socketio.on("join")
def on_join(data):
    room = data.get("room")
    username = data.get("username")
    if not room or not username:
        return
    join_room(room)
    emit("system_message", {"msg": f"{username} joined the room."}, room=room)
    broadcast_user_list()

@socketio.on("leave")
def on_leave(data):
    room = data.get("room")
    username = data.get("username")
    if not room or not username:
        return
    leave_room(room)
    emit("system_message", {"msg": f"{username} left the room."}, room=room)
    broadcast_user_list()

@socketio.on("send_message")
def on_send_message(data):
    room = data.get("room")
    text = (data.get("text") or "").strip()
    sender = data.get("sender")
    if not room or not text or not sender:
        return
    msg = Message(room=room, sender=sender, text=text)
    db.session.add(msg)
    db.session.commit()
    emit("new_message", msg.to_dict(), room=room)

@socketio.on("typing")
def on_typing(data):
    room = data.get("room")
    sender = data.get("sender")
    typing = bool(data.get("typing", False))
    if room and sender:
        emit("typing", {"sender": sender, "typing": typing}, room=room, include_self=False)

# --------------------------
# Run app
# --------------------------
if __name__ == "__main__":
    socketio.run(app, host="0.0.0.0", port=5000, debug=True)
