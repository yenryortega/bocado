"""Bocado — calorie tracker. Flask + PostgreSQL + Claude API (photo analysis)."""
import base64
import datetime as dt
import io
import json
import os
import re
import time
import urllib.request
from functools import wraps
from zoneinfo import ZoneInfo

import anthropic
from flask import Flask, jsonify, request, send_from_directory, session
from flask_sqlalchemy import SQLAlchemy
from PIL import Image, ImageOps
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash

# ---------------------------------------------------------------- config
DEV = os.environ.get("DEV") == "1"
db_url = os.environ.get("DATABASE_URL", "sqlite:///bocado.db")
if db_url.startswith("postgres://"):
    db_url = db_url.replace("postgres://", "postgresql://", 1)

SECRET = os.environ.get("SECRET_KEY")
if not SECRET:
    if not DEV:
        print("WARNING: SECRET_KEY not set — sessions will reset on every restart.")
    SECRET = os.urandom(32)

SIGNUP_CODE = os.environ.get("SIGNUP_CODE", "").strip()
ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "").strip().lower()
DAILY_PHOTO_LIMIT = int(os.environ.get("DAILY_PHOTO_LIMIT", "15"))
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-5-5")
APP_TZ = ZoneInfo(os.environ.get("APP_TZ", "Asia/Tokyo"))

app = Flask(__name__, static_folder="static", static_url_path="/static")
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
app.config.update(
    SQLALCHEMY_DATABASE_URI=db_url,
    SQLALCHEMY_ENGINE_OPTIONS={"pool_pre_ping": True},
    SECRET_KEY=SECRET,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=not DEV,
    PERMANENT_SESSION_LIFETIME=dt.timedelta(days=60),
    MAX_CONTENT_LENGTH=15 * 1024 * 1024,
)
db = SQLAlchemy(app)


# ---------------------------------------------------------------- models
class User(db.Model):
    __tablename__ = "users"
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), unique=True, nullable=False, index=True)
    name = db.Column(db.String(80), nullable=False, default="")
    pw = db.Column(db.String(255), nullable=False)
    lang = db.Column(db.String(5), nullable=False, default="en")
    profile = db.Column(db.JSON)
    is_admin = db.Column(db.Boolean, nullable=False, default=False)
    created = db.Column(db.DateTime, default=dt.datetime.utcnow)


class Meal(db.Model):
    __tablename__ = "meals"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    day = db.Column(db.Date, nullable=False, index=True)
    tipo = db.Column(db.String(12), nullable=False)
    hora = db.Column(db.String(5), nullable=False)
    items = db.Column(db.JSON, nullable=False)
    thumb = db.Column(db.Text)  # small JPEG data URL of the dish photo
    created = db.Column(db.DateTime, default=dt.datetime.utcnow)

    def out(self):
        return {"id": self.id, "day": self.day.isoformat(), "tipo": self.tipo, "hora": self.hora,
                "items": self.items, "thumb": self.thumb}


class Favorite(db.Model):
    __tablename__ = "favorites"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    name = db.Column(db.String(80), nullable=False)
    items = db.Column(db.JSON, nullable=False)
    created = db.Column(db.DateTime, default=dt.datetime.utcnow)

    def out(self):
        return {"id": self.id, "name": self.name, "items": self.items}


class Analysis(db.Model):
    __tablename__ = "analyses"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    day = db.Column(db.Date, nullable=False, index=True)
    created = db.Column(db.DateTime, default=dt.datetime.utcnow)


with app.app_context():
    db.create_all()
    # lightweight migration: add columns introduced after the first deploy
    if "thumb" not in {c["name"] for c in db.inspect(db.engine).get_columns("meals")}:
        with db.engine.begin() as conn:
            conn.execute(db.text("ALTER TABLE meals ADD COLUMN thumb TEXT"))


# ---------------------------------------------------------------- helpers
def err(code, status=400):
    return jsonify({"error": code}), status


def today():
    return dt.datetime.now(APP_TZ).date()


def parse_day(s):
    try:
        return dt.date.fromisoformat(str(s))
    except ValueError:
        return None


def current_user():
    uid = session.get("uid")
    return db.session.get(User, uid) if uid else None


def login_required(f):
    @wraps(f)
    def wrapper(*a, **kw):
        u = current_user()
        if not u:
            return err("auth", 401)
        return f(u, *a, **kw)
    return wrapper


def photos_today(u):
    return Analysis.query.filter_by(user_id=u.id, day=today()).count()


def ai_enabled():
    return bool(os.environ.get("ANTHROPIC_API_KEY", "").strip())


def me_json(u):
    return {"ai": ai_enabled(),
        "email": u.email, "name": u.name, "lang": u.lang, "profile": u.profile,
        "is_admin": u.is_admin, "photos_today": photos_today(u),
        "photo_limit": None if u.is_admin else DAILY_PHOTO_LIMIT,
    }


EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_fails = {}  # simple in-memory login throttle: key -> [timestamps]


def throttled(key):
    now = time.time()
    _fails[key] = [t for t in _fails.get(key, []) if now - t < 900]
    return len(_fails[key]) >= 5


def num(v, lo=0, hi=100000):
    try:
        return max(lo, min(hi, float(v)))
    except (TypeError, ValueError):
        return 0


def clean_items(items):
    if not isinstance(items, list) or not 0 < len(items) <= 50:
        return None
    out = []
    for i in items:
        if not isinstance(i, dict):
            return None
        n = str(i.get("n", "")).strip()[:80]
        if not n:
            continue
        out.append({"n": n, "g": round(num(i.get("g"), 0, 5000)), "k": num(i.get("k"), 0, 1000),
                    "p": num(i.get("p"), 0, 100), "c": num(i.get("c"), 0, 100), "f": num(i.get("f"), 0, 100)})
    return out or None


def clean_profile(p):
    if not isinstance(p, dict):
        return None
    try:
        out = {"sexo": p["sexo"], "edad": int(p["edad"]), "peso": float(p["peso"]), "alt": float(p["alt"]),
               "act": float(p["act"]), "obj": int(p["obj"])}
    except (KeyError, TypeError, ValueError):
        return None
    ok = (out["sexo"] in ("h", "m") and 14 <= out["edad"] <= 100 and 30 <= out["peso"] <= 300
          and 120 <= out["alt"] <= 230 and out["act"] in (1.2, 1.375, 1.55, 1.725, 1.9)
          and out["obj"] in (-500, -250, 0, 300))
    return out if ok else None


# ---------------------------------------------------------------- pages
@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.get("/apple-touch-icon.png")
@app.get("/apple-touch-icon-precomposed.png")
@app.get("/apple-touch-icon-180x180.png")
def touch_icon():
    return send_from_directory(app.static_folder, "icon-180.png", max_age=86400)


@app.get("/favicon.ico")
def favicon():
    return send_from_directory(app.static_folder, "icon-192.png", mimetype="image/png", max_age=86400)


@app.get("/healthz")
def health():
    return "ok"


# ---------------------------------------------------------------- auth
@app.get("/api/config")
def config():
    first = User.query.count() == 0
    return jsonify({"signup_open": first or bool(SIGNUP_CODE), "needs_code": not first and bool(SIGNUP_CODE), "ai": ai_enabled()})


@app.post("/api/register")
def register():
    d = request.get_json(silent=True) or {}
    email = str(d.get("email", "")).strip().lower()
    pw = str(d.get("password", ""))
    name = str(d.get("name", "")).strip()[:80]
    first = User.query.count() == 0
    if not first:
        if not SIGNUP_CODE:
            return err("closed", 403)
        if str(d.get("code", "")).strip() != SIGNUP_CODE:
            return err("bad_code", 403)
    if not EMAIL_RE.match(email):
        return err("bad_email")
    if len(pw) < 8:
        return err("weak")
    if User.query.filter_by(email=email).first():
        return err("exists", 409)
    lang = d.get("lang") if d.get("lang") in ("es", "en", "ja") else "en"
    u = User(email=email, name=name or email.split("@")[0], pw=generate_password_hash(pw), lang=lang,
             is_admin=bool(first or (ADMIN_EMAIL and email == ADMIN_EMAIL)))
    db.session.add(u)
    db.session.commit()
    session.clear()
    session.permanent = True
    session["uid"] = u.id
    return jsonify(me_json(u))


@app.post("/api/login")
def login():
    d = request.get_json(silent=True) or {}
    email = str(d.get("email", "")).strip().lower()
    key = f"{request.remote_addr}|{email}"
    if throttled(key):
        return err("too_many", 429)
    u = User.query.filter_by(email=email).first()
    if not u or not check_password_hash(u.pw, str(d.get("password", ""))):
        _fails.setdefault(key, []).append(time.time())
        return err("invalid", 401)
    _fails.pop(key, None)
    session.clear()
    session.permanent = True
    session["uid"] = u.id
    return jsonify(me_json(u))


@app.post("/api/logout")
def logout():
    session.clear()
    return jsonify({"ok": True})


# ---------------------------------------------------------------- account
@app.get("/api/me")
@login_required
def me(u):
    return jsonify(me_json(u))


@app.put("/api/me")
@login_required
def update_me(u):
    d = request.get_json(silent=True) or {}
    if "lang" in d and d["lang"] in ("es", "en", "ja"):
        u.lang = d["lang"]
    if "name" in d:
        u.name = str(d["name"]).strip()[:80] or u.name
    if "profile" in d:
        p = clean_profile(d["profile"])
        if not p:
            return err("bad_profile")
        u.profile = p
    db.session.commit()
    return jsonify(me_json(u))


@app.put("/api/me/password")
@login_required
def change_password(u):
    d = request.get_json(silent=True) or {}
    if not check_password_hash(u.pw, str(d.get("current", ""))):
        return err("wrong_pw", 403)
    new = str(d.get("new", ""))
    if len(new) < 8:
        return err("weak")
    u.pw = generate_password_hash(new)
    db.session.commit()
    return jsonify({"ok": True})


@app.delete("/api/me/data")
@login_required
def delete_data(u):
    Meal.query.filter_by(user_id=u.id).delete()
    Favorite.query.filter_by(user_id=u.id).delete()
    u.profile = None
    db.session.commit()
    return jsonify(me_json(u))


@app.delete("/api/me")
@login_required
def delete_account(u):
    d = request.get_json(silent=True) or {}
    if not check_password_hash(u.pw, str(d.get("password", ""))):
        return err("wrong_pw", 403)
    Meal.query.filter_by(user_id=u.id).delete()
    Favorite.query.filter_by(user_id=u.id).delete()
    Analysis.query.filter_by(user_id=u.id).delete()
    db.session.delete(u)
    db.session.commit()
    session.clear()
    return jsonify({"ok": True})


# ---------------------------------------------------------------- meals
@app.get("/api/days")
@login_required
def days(u):
    start, end = parse_day(request.args.get("start")), parse_day(request.args.get("end"))
    if not start or not end or (end - start).days > 400:
        return err("bad_range")
    rows = (Meal.query.filter(Meal.user_id == u.id, Meal.day >= start, Meal.day <= end)
            .order_by(Meal.day, Meal.hora, Meal.id).all())
    out = {}
    for m in rows:
        out.setdefault(m.day.isoformat(), []).append(m.out())
    return jsonify(out)


@app.post("/api/meals")
@login_required
def add_meal(u):
    d = request.get_json(silent=True) or {}
    day = parse_day(d.get("day"))
    items = clean_items(d.get("items"))
    tipo = str(d.get("tipo", ""))[:12]
    hora = str(d.get("hora", ""))[:5]
    if not day or not items or not tipo or not re.match(r"^\d\d:\d\d$", hora):
        return err("bad_meal")
    thumb = d.get("thumb")
    if not (isinstance(thumb, str) and thumb.startswith("data:image/jpeg;base64,") and len(thumb) < 80000):
        thumb = None
    m = Meal(user_id=u.id, day=day, tipo=tipo, hora=hora, items=items, thumb=thumb)
    db.session.add(m)
    db.session.commit()
    return jsonify(m.out())


@app.get("/api/streak")
@login_required
def streak(u):
    """Consecutive days with at least one meal, ending today (or yesterday if today is still empty)."""
    since = today() - dt.timedelta(days=366)
    days = {r[0] for r in db.session.query(Meal.day).filter(Meal.user_id == u.id, Meal.day >= since).distinct()}
    d = today() if today() in days else today() - dt.timedelta(days=1)
    n = 0
    while d in days:
        n += 1
        d -= dt.timedelta(days=1)
    return jsonify({"streak": n, "today": today() in days})


@app.put("/api/meals/<int:mid>")
@login_required
def update_meal(u, mid):
    m = Meal.query.filter_by(id=mid, user_id=u.id).first()
    if not m:
        return err("not_found", 404)
    d = request.get_json(silent=True) or {}
    if "items" in d:
        items = clean_items(d.get("items"))
        if not items:
            return err("bad_meal")
        m.items = items
    if "tipo" in d:
        tipo = str(d.get("tipo", ""))[:12]
        if tipo:
            m.tipo = tipo
    db.session.commit()
    return jsonify(m.out())


@app.delete("/api/meals/<int:mid>")
@login_required
def delete_meal(u, mid):
    Meal.query.filter_by(id=mid, user_id=u.id).delete()
    db.session.commit()
    return jsonify({"ok": True})


@app.delete("/api/days/<day>")
@login_required
def clear_day(u, day):
    d = parse_day(day)
    if not d:
        return err("bad_day")
    Meal.query.filter_by(user_id=u.id, day=d).delete()
    db.session.commit()
    return jsonify({"ok": True})


# ---------------------------------------------------------------- favorites, recents, barcode
@app.get("/api/favorites")
@login_required
def favorites(u):
    rows = Favorite.query.filter_by(user_id=u.id).order_by(Favorite.created.desc()).limit(100).all()
    return jsonify([f.out() for f in rows])


@app.post("/api/favorites")
@login_required
def add_favorite(u):
    d = request.get_json(silent=True) or {}
    items = clean_items(d.get("items"))
    name = str(d.get("name", "")).strip()[:80]
    if not items or not name:
        return err("bad_fav")
    if Favorite.query.filter_by(user_id=u.id).count() >= 100:
        return err("too_many_favs")
    f = Favorite(user_id=u.id, name=name, items=items)
    db.session.add(f)
    db.session.commit()
    return jsonify(f.out())


@app.delete("/api/favorites/<int:fid>")
@login_required
def delete_favorite(u, fid):
    Favorite.query.filter_by(id=fid, user_id=u.id).delete()
    db.session.commit()
    return jsonify({"ok": True})


@app.get("/api/recents")
@login_required
def recents(u):
    rows = Meal.query.filter_by(user_id=u.id).order_by(Meal.day.desc(), Meal.id.desc()).limit(60).all()
    seen, out = set(), []
    for m in rows:
        key = "|".join(sorted(i.get("n", "").lower() for i in m.items))
        if key in seen:
            continue
        seen.add(key)
        out.append({"name": ", ".join(i.get("n", "") for i in m.items)[:80], "items": m.items, "tipo": m.tipo})
        if len(out) >= 10:
            break
    return jsonify(out)


@app.get("/api/barcode/<code>")
@login_required
def barcode(u, code):
    if not re.fullmatch(r"\d{6,14}", code):
        return err("bad_code")
    url = (f"https://world.openfoodfacts.org/api/v2/product/{code}.json"
           "?fields=product_name,product_name_ja,product_name_es,product_name_en,brands,nutriments,serving_quantity")
    req = urllib.request.Request(url, headers={"User-Agent": "Bocado/1.0 (calorie tracker; contact via app owner)"})
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            data = json.loads(r.read().decode())
    except Exception as e:
        app.logger.warning("OFF lookup failed: %s", e)
        return err("lookup_failed", 502)
    p = data.get("product") if data.get("status") == 1 else None
    if not p:
        return err("not_found", 404)
    n = p.get("nutriments") or {}
    kcal = n.get("energy-kcal_100g")
    if kcal is None and n.get("energy_100g") is not None:
        kcal = float(n["energy_100g"]) / 4.184
    if kcal is None:
        return err("no_nutrition", 404)
    lang = request.args.get("lang", u.lang)
    name = p.get(f"product_name_{lang}") or p.get("product_name") or p.get("product_name_en") or code
    if p.get("brands"):
        name = f"{name} ({p['brands'].split(',')[0].strip()})"
    try:
        serving = float(p.get("serving_quantity") or 0)
    except (TypeError, ValueError):
        serving = 0
    return jsonify({"n": name[:60], "g": round(serving) if 0 < serving < 2000 else 100, "k": round(num(kcal, 0, 1000), 1),
                    "p": round(num(n.get("proteins_100g"), 0, 100), 1), "c": round(num(n.get("carbohydrates_100g"), 0, 100), 1),
                    "f": round(num(n.get("fat_100g"), 0, 100), 1)})


# ---------------------------------------------------------------- photo analysis
LANG_NAMES = {"es": "Spanish", "en": "English", "ja": "Japanese"}


def build_prompt(desc, has_img, lang):
    ln = LANG_NAMES.get(lang, "English")
    return f"""You are a nutritionist. {'Look at the photo of the dish.' if has_img else 'There is no photo: use only the description.'}
{('User details: "' + desc[:500] + '"') if desc else ''}
Identify each food separately and estimate the portion in grams using visual references (plate size, cutlery, hands). List oil, butter, sauces or dressings as separate items when they are likely, even if barely visible. Use standard food-composition values (USDA FoodData Central or the Japanese 食品成分表) for the food as cooked.
Write "plato", every "nombre", "confianza" and "nota" in {ln}. Each "nombre" must be SHORT: 1-4 words, no brackets, no ingredient lists.
Reply with ONLY JSON in this shape:
{{"plato":"short dish name","items":[{{"nombre":"cooked white rice","gramos":150,"kcal_100g":130,"prot_100g":2.7,"carb_100g":28.2,"grasa_100g":0.3}}],"confianza":"high|medium|low","nota":"one sentence on the most uncertain part"}}
If it is not food, return "items":[] and explain in "nota"."""


def parse_json(text):
    try:
        return json.loads(text)
    except ValueError:
        m = re.search(r"\{.*\}", text, re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except ValueError:
                pass
    return None


def prep_image(f):
    img = Image.open(f.stream)
    img = ImageOps.exif_transpose(img).convert("RGB")
    img.thumbnail((1568, 1568))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=85)
    return base64.b64encode(buf.getvalue()).decode()


@app.post("/api/analyze")
@login_required
def analyze(u):
    if not ai_enabled():
        return err("ai_off", 503)
    if not u.is_admin and photos_today(u) >= DAILY_PHOTO_LIMIT:
        return err("limit", 429)
    desc = str(request.form.get("desc", "")).strip()
    lang = request.form.get("lang") or u.lang
    f = request.files.get("image")
    if not f and not desc:
        return err("need_input")
    content = []
    if f:
        try:
            content.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": prep_image(f)}})
        except Exception:
            return err("bad_image")
    content.append({"type": "text", "text": build_prompt(desc, bool(f), lang)})
    try:
        client = anthropic.Anthropic()
        msg = client.messages.create(model=CLAUDE_MODEL, max_tokens=1500, messages=[{"role": "user", "content": content}])
    except anthropic.RateLimitError:
        return err("rate_limited", 429)
    except Exception as e:  # network / auth / overload
        app.logger.error("Claude API error: %s", e)
        return err("ai_error", 502)
    data = parse_json("".join(b.text for b in msg.content if b.type == "text"))
    if not isinstance(data, dict):
        return err("ai_format", 502)
    items = []
    for i in data.get("items") or []:
        if isinstance(i, dict):
            items.append({"n": str(i.get("nombre", "—"))[:60], "g": round(num(i.get("gramos"), 0, 5000)),
                          "k": num(i.get("kcal_100g"), 0, 1000), "p": num(i.get("prot_100g"), 0, 100),
                          "c": num(i.get("carb_100g"), 0, 100), "f": num(i.get("grasa_100g"), 0, 100)})
    db.session.add(Analysis(user_id=u.id, day=today()))
    db.session.commit()
    return jsonify({"plato": str(data.get("plato", ""))[:80], "items": items,
                    "confianza": str(data.get("confianza", ""))[:20], "nota": str(data.get("nota", ""))[:300],
                    "photos_today": photos_today(u)})


@app.errorhandler(413)
def too_big(_):
    return err("too_big", 413)


if __name__ == "__main__":
    app.run(debug=DEV, port=int(os.environ.get("PORT", 5000)))
