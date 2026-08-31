# API Flask Otimizada 2.0
# Otimizações implementadas:
# 1. Cache em memória com write-behind para JSON
# 2. Rotação automática de logs (manter últimos N registros)
# 3. Busca indexada para bans (O(1) em vez de O(n))
# 4. Cache de keys em memória
# 5. Webhooks assíncronos em thread separada
# 6. Novo endpoint /api/admin/dashboard para requisição única
# 7. Paginação em /api/logs
# 8. Produção-ready com gunicorn

from flask import Flask, jsonify, request, Response, send_from_directory
from flask_cors import CORS
import json
import os
import re
import time
from datetime import datetime, timezone, timedelta
import requests
import secrets
import zipfile
import threading
import hashlib
import uuid
from collections import defaultdict
from queue import Queue

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
FRONTEND_DIR = BASE_DIR
app = Flask(__name__)
CORS(app)

# -----------------------
# Config / Arquivos
# -----------------------
LOG_FILE = "logs.json"
BAN_FILE = "bans.json"
USERS_FILE = "users.json"
PRODUTOS_FILE = "produtos.json"
KEYS_FILE = "keys.json"
SCRIPTS_FILE = "scripts.json"
LOADER_DIR = os.path.join(BASE_DIR, "loader")
os.makedirs(LOADER_DIR, exist_ok=True)

# Admin padrão
ADMIN_USER = os.environ.get("ADMIN_USER", "1v99ByRaro")
ADMIN_PASS = os.environ.get("ADMIN_PASS", "199")

# Webhook opcional
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "https://discord.com/api/webhooks/1482954737853927554/WnB9phI4kvI_bB_wJej9U6B9Ob1c6lQhxwW1rA60GIHVdp3colsrbieHQjxeBsdY6MA8")

# Sessões / tokens
TOKENS = {}

# Timezone Brasil
BRASIL = timezone(timedelta(hours=-3))

# -----------------------
# CACHE SYSTEM (OTIMIZAÇÃO 1)
# -----------------------
# Cache para scripts raw processados (watermark + código)
RAW_SCRIPTS_CACHE = {}
SCRIPTS_INDEX = {}

# Estrutura: {filename: {"data": [...], "dirty": bool, "last_write": timestamp}}
CACHE = {}
CACHE_LOCK = threading.Lock()
WRITE_BEHIND_INTERVAL = 5  # segundos
MAX_LOG_ENTRIES = 5000  # rotação de logs

def get_cache(filename, default=None):
    """Obtém dados do cache ou carrega do disco."""
    if default is None:
        default = []
    
    with CACHE_LOCK:
        if filename in CACHE:
            return CACHE[filename]["data"].copy()
    
    # Carrega do disco
    data = carregar_disco(filename, default)
    with CACHE_LOCK:
        CACHE[filename] = {"data": data, "dirty": False, "last_write": time.time()}
    return data

def set_cache(filename, data, mark_dirty=True):
    """Define dados no cache e marca como dirty para escrita posterior."""
    with CACHE_LOCK:
        CACHE[filename] = {"data": data.copy() if isinstance(data, list) else dict(data), "dirty": mark_dirty, "last_write": time.time()}

def carregar_disco(file, default=None):
    """Lê arquivo JSON do disco."""
    if default is None:
        default = []
    if not os.path.exists(file):
        return default
    try:
        with open(file, "r", encoding="utf8") as f:
            return json.load(f)
    except Exception:
        return default

def salvar_disco(file, data):
    """Escreve arquivo JSON no disco."""
    try:
        with open(file, "w", encoding="utf8") as f:
            json.dump(data, f, indent=4, ensure_ascii=False)
    except Exception as e:
        print(f"Erro ao salvar {file}: {e}")

def write_behind_worker():
    """Thread que persiste dados dirty em intervalos regulares."""
    while True:
        time.sleep(WRITE_BEHIND_INTERVAL)
        with CACHE_LOCK:
            for filename, cache_entry in list(CACHE.items()):
                if cache_entry["dirty"]:
                    salvar_disco(filename, cache_entry["data"])
                    cache_entry["dirty"] = False

# Inicia thread de write-behind
threading.Thread(target=write_behind_worker, daemon=True).start()

# -----------------------
# BAN INDEX (OTIMIZAÇÃO 3)
# -----------------------
# Índices para busca O(1)
BAN_INDEX = {"hwid": {}, "ip": {}, "nick": {}}
BAN_INDEX_LOCK = threading.Lock()

def rebuild_ban_index():
    """Reconstrói índices de bans."""
    with BAN_INDEX_LOCK:
        BAN_INDEX["hwid"].clear()
        BAN_INDEX["ip"].clear()
        BAN_INDEX["nick"].clear()
        
        bans = get_cache(BAN_FILE)
        for i, b in enumerate(bans):
            if b.get("hwid"):
                BAN_INDEX["hwid"][b["hwid"]] = i
            if b.get("ip"):
                BAN_INDEX["ip"][b["ip"]] = i
            if b.get("nick"):
                BAN_INDEX["nick"][b["nick"]] = i

def load_bans():
    """Carrega bans com índices."""
    return get_cache(BAN_FILE)

def save_bans(bans):
    """Salva bans e reconstrói índices."""
    set_cache(BAN_FILE, bans)
    rebuild_ban_index()

def is_banned(hwid=None, ip=None, nick=None):
    """Busca O(1) em bans usando índices."""
    with BAN_INDEX_LOCK:
        if hwid and hwid in BAN_INDEX["hwid"]:
            idx = BAN_INDEX["hwid"][hwid]
            bans = get_cache(BAN_FILE)
            if idx < len(bans):
                return True, bans[idx]
        if ip and ip in BAN_INDEX["ip"]:
            idx = BAN_INDEX["ip"][ip]
            bans = get_cache(BAN_FILE)
            if idx < len(bans):
                return True, bans[idx]
        if nick and nick in BAN_INDEX["nick"]:
            idx = BAN_INDEX["nick"][nick]
            bans = get_cache(BAN_FILE)
            if idx < len(bans):
                return True, bans[idx]
    return False, None

# -----------------------
# KEYS CACHE (OTIMIZAÇÃO 4)
# -----------------------
def load_keys():
    """Carrega keys do cache."""
    return get_cache(KEYS_FILE, default={})

def save_keys(dct):
    """Salva keys no cache."""
    set_cache(KEYS_FILE, dct)

def create_key(owner: str, roblox: str, duration_raw=None, notes=""):
    keys = load_keys()
    token = secrets.token_hex(24)
    duration_seconds = parse_duration_to_seconds(duration_raw)
    expires_at = make_expiry_from_duration(duration_seconds)
    key_info = {
        "token": token,
        "owner": owner or "",
        "roblox": roblox or "",
        "created": agora_dt().isoformat(),
        "expires_at": expires_at,
        "duration_seconds": duration_seconds,
        "uses": 0,
        "active": True,
        "notes": notes or ""
    }
    keys[token] = key_info
    save_keys(keys)
    return key_info

def edit_key(token: str, owner=None, roblox=None, duration_raw=None, active=None, notes=None):
    keys = load_keys()
    if token not in keys:
        return None
    k = keys[token]
    if owner is not None:
        k["owner"] = owner
    if roblox is not None:
        k["roblox"] = roblox
    if duration_raw is not None:
        dur = parse_duration_to_seconds(duration_raw)
        k["duration_seconds"] = dur
        k["expires_at"] = make_expiry_from_duration(dur)
    if active is not None:
        k["active"] = bool(active)
    if notes is not None:
        k["notes"] = notes
    keys[token] = k
    save_keys(keys)
    return k

def add_time_to_key(token: str, add_raw):
    """Adiciona tempo a uma key."""
    keys = load_keys()
    if token not in keys:
        return None
    k = keys[token]
    add_seconds = parse_duration_to_seconds(add_raw)
    if add_seconds is None:
        k["duration_seconds"] = None
        k["expires_at"] = None
    else:
        if k.get("expires_at") is None:
            k["expires_at"] = timestamp_now() + add_seconds
            k["duration_seconds"] = add_seconds
        else:
            k["expires_at"] = k["expires_at"] + add_seconds
            k["duration_seconds"] = max(0, k["expires_at"] - timestamp_now())
    keys[token] = k
    save_keys(keys)
    return k

def delete_key(token: str):
    keys = load_keys()
    if token not in keys:
        return False
    del keys[token]
    save_keys(keys)
    return True

def is_key_valid(token: str):
    """Valida key sem recarregar do disco (usa cache)."""
    keys = load_keys()
    k = keys.get(token)
    if not k or not k.get("active", True):
        return False
    exp = k.get("expires_at")
    if exp is None:
        return True
    return timestamp_now() <= int(exp)

# -----------------------
# Helpers
# -----------------------
def agora_dt():
    """Retorna datetime com timezone Brasil."""
    return datetime.now(BRASIL)

def agora_str():
    t = agora_dt()
    return t.strftime("%d/%m/%Y"), t.strftime("%H:%M:%S")

def hash_senha(senha: str) -> str:
    return hashlib.sha256(senha.encode()).hexdigest()

def get_ip():
    if request.headers.get("X-Forwarded-For"):
        return request.headers["X-Forwarded-For"].split(",")[0]
    return request.remote_addr

def require_admin_token():
    """Valida token admin."""
    token = request.headers.get("Authorization")
    if not token or token not in TOKENS or TOKENS[token] != "admin":
        return False, (jsonify({"error": "admin apenas"}), 403)
    return True, None

def parse_duration_to_seconds(duration):
    """Parse de duração para segundos."""
    if duration is None:
        return None
    if isinstance(duration, (int, float)):
        if int(duration) <= 0:
            return None
        return int(duration)
    if isinstance(duration, str):
        d = duration.strip().lower()
        if d in ("permanent", "perm", "perma", "0", "infinite", "indef"):
            return None
        if d.isdigit():
            return int(d)
        m = re.match(r"^(\d+)([smhd])$", d)
        if m:
            val = int(m.group(1))
            suf = m.group(2)
            if suf == "s":
                return val
            if suf == "m":
                return val * 60
            if suf == "h":
                return val * 3600
            if suf == "d":
                return val * 86400
    return None

def timestamp_now():
    return int(time.time())

def make_expiry_from_duration(duration_seconds):
    """Cria timestamp de expiração."""
    if duration_seconds is None:
        return None
    return timestamp_now() + int(duration_seconds)

# -----------------------
# ROTAS BÁSICAS
# -----------------------
@app.route("/")
def home():
    """Serve frontend."""
    index_path = os.path.join(os.getcwd(), "index.html")
    if not os.path.isfile(index_path):
        return jsonify({"status": "online", "time": agora_dt().isoformat()})
    with open(index_path, "r", encoding="utf-8") as f:
        html = f.read()
    return Response(html, mimetype="text/html")

@app.route("/api/status")
def api_status():
    return jsonify({"status": "online", "time": agora_dt().isoformat()})

@app.route("/api/stats")
def api_stats():
    """Stats com cache."""
    logs = get_cache(LOG_FILE)
    scripts = []
    try:
        for fname in os.listdir(LOADER_DIR):
            path = os.path.join(LOADER_DIR, fname)
            if os.path.isfile(path):
                scripts.append(fname)
    except:
        pass
    raw_scripts = load_scripts_raw()
    return jsonify({
        "players": len(set(log.get("id") for log in logs if isinstance(log, dict))),
        "execucoes": len(logs),
        "scripts": len(scripts),
        "raw_scripts": len(raw_scripts)
    })

# -----------------------
# NOVO: Dashboard endpoint (OTIMIZAÇÃO 10)
# -----------------------
@app.route("/api/admin/dashboard", methods=["GET"])
def admin_dashboard():
    """Retorna apenas contadores para dashboard (requisição única)."""
    ok, resp = require_admin_token()
    if not ok:
        return resp
    
    logs = get_cache(LOG_FILE)
    bans = load_bans()
    keys = load_keys()
    produtos = get_cache(PRODUTOS_FILE)
    
    return jsonify({
        "stats": {
            "players": len(set(log.get("id") for log in logs if isinstance(log, dict))),
            "execucoes": len(logs),
            "bans": len(bans),
            "keys": len(keys),
            "produtos": len(produtos)
        },
        "timestamp": agora_dt().isoformat()
    })

# -----------------------
# AUTH (admin)
# -----------------------
@app.route("/api/registro", methods=["POST"])
def api_registro():
    data = request.json or {}
    user = (data.get("user") or "").strip().lower()
    email = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""

    if not user or not password or len(password) < 6:
        return jsonify({"error": "Dados inválidos. Senha deve ter 6+ caracteres"}), 400

    users = get_cache(USERS_FILE, default={})

    if user in users:
        return jsonify({"error": "Usuário já existe"}), 400

    users[user] = {
        "user": user,
        "email": email,
        "password": hash_senha(password),
        "criado": agora_dt().strftime("%d/%m/%Y %H:%M"),
        "vip": False,
        "banido": False
    }
    set_cache(USERS_FILE, users)

    # webhook registro (assíncrono)
    send_webhook_async(f"📥 Novo Registro\nUsuário: `{user}`\nEmail: `{email}")

    token = secrets.token_hex(32)
    TOKENS[token] = user
    return jsonify({"token": token, "user": user})

@app.route("/api/login", methods=["POST"])
def api_login():
    data = request.json or {}
    user = (data.get("user") or "").strip().lower()
    password = data.get("password") or ""

    users = get_cache(USERS_FILE, default={})

    # Admin login (suporta env ADMIN_USER / ADMIN_PASS)
    if user == ADMIN_USER.lower() and password == ADMIN_PASS:
        token = secrets.token_hex(32)
        TOKENS[token] = "admin"
        return jsonify({"token": token, "user": "admin", "admin": True})

    if user not in users:
        return jsonify({"error": "Usuário não encontrado"}), 401

    if users[user].get("banido"):
        return jsonify({"error": "Conta banida"}), 403

    if users[user].get("password") != hash_senha(password):
        return jsonify({"error": "Senha incorreta"}), 401

    token = secrets.token_hex(32)
    TOKENS[token] = user
    return jsonify({"token": token, "user": user, "admin": False})

@app.route("/api/verify", methods=["GET"])
def api_verify_token():
    token = request.headers.get("Authorization")
    if token in TOKENS:
        user = TOKENS[token]
        users = get_cache(USERS_FILE, default={})
        if user == "admin":
            return jsonify({"valid": True, "admin": True})
        if user in users:
            return jsonify({"valid": True, "admin": False, "vip": users[user].get("vip", False)})
    return jsonify({"valid": False}), 401

@app.route("/api/user/me", methods=["GET"])
def api_user_me():
    token = request.headers.get("Authorization")
    if token not in TOKENS:
        return jsonify({"error": "não autorizado"}), 401

    user = TOKENS[token]
    users = get_cache(USERS_FILE, default={})

    if user == "admin":
        return jsonify({"user": "admin", "admin": True, "vip": True})

    if user not in users:
        return jsonify({"error": "usuário não existe"}), 404

    u = users[user]
    return jsonify({
        "user": u.get("user"),
        "email": u.get("email"),
        "criado": u.get("criado"),
        "vip": u.get("vip", False),
        "banido": u.get("banido", False)
    })

@app.route("/api/verify-legacy", methods=["GET"])
def verify_legacy():
    token = request.headers.get("Authorization")
    if token in TOKENS:
        return jsonify({"valid": True})
    return jsonify({"valid": False}), 401
@app.route("/api/admin/login", methods=["POST"])
def admin_login():
    data = request.json
    user = data.get("user", "").strip().lower()
    password = data.get("password", "")
    
    if user == ADMIN_USER.lower() and password == ADMIN_PASS:
        token = secrets.token_hex(32)
        TOKENS[token] = "admin"
        return jsonify({
            "token": token,
            "user": "admin",
            "admin": True,
            "message": "Login admin realizado com sucesso"
        })
    
    return jsonify({
        "error": "Credenciais admin inválidas",
        "admin": False
    }), 401

@app.route("/api/admin/verify", methods=["GET"])
def admin_verify():
    token = request.headers.get("Authorization")
    if not token:
        return jsonify({"admin": False, "error": "Token não fornecido"}), 401
    
    if token in TOKENS and TOKENS[token] == "admin":
        return jsonify({"admin": True, "valid": True})
    
    return jsonify({"admin": False, "valid": False}), 401

@app.route("/api/login-legacy", methods=["POST"])
def login_legacy():
    data = request.json or {}
    user = (data.get("user") or "").strip()
    password = data.get("password") or ""
    if user == ADMIN_USER and password == ADMIN_PASS:
        token = secrets.token_hex(32)
        TOKENS[token] = "admin"
        return jsonify({"token": token})
    return jsonify({"error": "invalid login"}), 401

@app.route("/api/logout", methods=["POST"])
def logout():
    token = request.headers.get("Authorization")
    if token and token in TOKENS:
        del TOKENS[token]
    return jsonify({"success": True})

# -----------------------
# KEYS ENDPOINTS (Admin)
# -----------------------
@app.route("/api/keys", methods=["GET"])
def api_get_keys():
    """Retorna todas as keys (requer admin). Suporta ?active=true|false"""
    ok, resp = require_admin_token()
    if not ok:
        return resp
    keys = load_keys()
    active = request.args.get("active")
    if active is not None:
        active_bool = active.lower() in ("1", "true", "yes")
        keys = {k: v for k, v in keys.items() if v.get("active", True) == active_bool}
    return jsonify(keys)

@app.route("/api/keys", methods=["POST"])
def api_create_key():
    """Cria uma nova key (requer admin)."""
    ok, resp = require_admin_token()
    if not ok:
        return resp
    data = request.json or {}
    key_info = create_key(
        owner=data.get("owner"),
        roblox=data.get("roblox"),
        duration_raw=data.get("duration"),
        notes=data.get("notes", "")
    )
    return jsonify({"status": "criada", "key": key_info})

@app.route("/api/keys/<key_token>", methods=["GET"])
def api_get_key_by_token(key_token):
    """Retorna informações de uma key específica (requer admin)."""
    ok, resp = require_admin_token()
    if not ok:
        return resp
    keys = load_keys()
    k = keys.get(key_token)
    if not k:
        return jsonify({"error": "key não encontrada"}), 404
    return jsonify(k)

@app.route("/api/keys/<key_token>", methods=["PUT"])
def api_edit_key(key_token):
    """Edita uma key existente (requer admin)."""
    ok, resp = require_admin_token()
    if not ok:
        return resp
    data = request.json or {}
    k = edit_key(
        key_token,
        owner=data.get("owner"),
        roblox=data.get("roblox"),
        duration_raw=data.get("duration"),
        active=data.get("active"),
        notes=data.get("notes")
    )
    if not k:
        return jsonify({"error": "key não encontrada"}), 404
    return jsonify({"status": "atualizada", "key": k})

@app.route("/api/keys/<key_token>/addtime", methods=["POST"])
def api_add_time_key(key_token):
    """Adiciona tempo a uma key (requer admin)."""
    ok, resp = require_admin_token()
    if not ok:
        return resp
    data = request.json or {}
    add_raw = data.get("add")
    if add_raw is None:
        return jsonify({"error": "missing add value"}), 400
    k = add_time_to_key(key_token, add_raw)
    if not k:
        return jsonify({"error": "não encontrado"}), 404
    return jsonify({"status": "atualizado", "key": k})

@app.route("/api/keys/<key_token>", methods=["DELETE"])
def api_delete_key(key_token):
    """Deleta uma key (requer admin)."""
    ok, resp = require_admin_token()
    if not ok:
        return resp
    if not delete_key(key_token):
        return jsonify({"error": "key não encontrada"}), 404
    return jsonify({"status": "deletada"})

# -----------------------
# KEYS ENDPOINTS (Admin Only - Novas rotas para dashboard)
# -----------------------
# -----------------------
@app.route("/api/admin/keys", methods=["GET"])
def admin_keys_list():
    ok, resp = require_admin_token()
    if not ok:
        return resp
    keys = load_keys()
    safe = [{
        "token": k["token"],
        "owner": k.get("owner"),
        "roblox": k.get("roblox"),
        "created": k.get("created"),
        "expires_at": k.get("expires_at"),
        "uses": k.get("uses", 0),
        "active": k.get("active", True)
    } for k in keys.values()]
    return jsonify(safe)

@app.route("/api/admin/keys", methods=["POST"])
def admin_create_key():
    ok, resp = require_admin_token()
    if not ok:
        return resp
    data = request.json or {}
    key_info = create_key(
        owner=data.get("owner"),
        roblox=data.get("roblox"),
        duration_raw=data.get("duration"),
        notes=data.get("notes", "")
    )
    return jsonify({"status": "criada", "key": key_info})

@app.route("/api/admin/keys/<key_token>", methods=["PUT"])
def admin_edit_key(key_token):
    ok, resp = require_admin_token()
    if not ok:
        return resp
    data = request.json or {}
    k = edit_key(
        key_token,
        owner=data.get("owner"),
        roblox=data.get("roblox"),
        duration_raw=data.get("duration"),
        active=data.get("active"),
        notes=data.get("notes")
    )
    if not k:
        return jsonify({"error": "key não encontrada"}), 404
    return jsonify({"status": "atualizada", "key": k})

@app.route("/api/admin/keys/<key_token>", methods=["DELETE"])
def admin_delete_key(key_token):
    ok, resp = require_admin_token()
    if not ok:
        return resp
    if not delete_key(key_token):
        return jsonify({"error": "key não encontrada"}), 404
    return jsonify({"status": "deletada"})

@app.route("/api/admin/keys/<key_token>/addtime", methods=["POST"])
def admin_add_time_key(key_token):
    ok, resp = require_admin_token()
    if not ok:
        return resp
    data = request.json or {}
    k = add_time_to_key(key_token, data.get("duration"))
    if not k:
        return jsonify({"error": "key não encontrada"}), 404
    return jsonify({"status": "tempo adicionado", "key": k})

@app.route("/api/key/validate/<key_token>", methods=["GET"])
def public_validate_key(key_token):
    """Valida key sem recarregar do disco (cache)."""
    k = load_keys().get(key_token)
    if not k:
        return jsonify({"valid": False}), 404
    valid = is_key_valid(key_token)
    if valid:
        k["uses"] = k.get("uses", 0) + 1
        keys = load_keys()
        keys[key_token] = k
        save_keys(keys)
        return jsonify({"valid": True, "key": k})
    return jsonify({"valid": False, "reason": "expired_or_inactive"}), 403

# -----------------------
# LOGS (com rotação e paginação - OTIMIZAÇÕES 2 e 9)
# -----------------------
def rotate_logs_if_needed():
    """Rotaciona logs se exceder MAX_LOG_ENTRIES."""
    logs = get_cache(LOG_FILE)
    if len(logs) > MAX_LOG_ENTRIES:
        logs = logs[-MAX_LOG_ENTRIES:]
        set_cache(LOG_FILE, logs)

@app.route("/api/log", methods=["POST"])
def log_route():
    data = request.json or {}
    
    ip = get_ip()
    hwid = data.get("hwid")
    nick = data.get("nick")
    
    # Verifica ban
    banned, reason = is_banned(hwid=hwid, ip=ip, nick=nick)
    if banned:
        return jsonify({"status": "banido"}), 403
    
    # Valida key se fornecida
    provided_key = get_key_from_request()
    if provided_key:
        if not is_key_valid(provided_key):
            return jsonify({"error": "key inválida ou expirada"}), 403
    
    data_log, hora_log = agora_str()
    novo = {
        "jogo": data.get("jogo"),
        "game_id": data.get("game_id"),
        "displaynick": data.get("displaynick"),
        "nick": nick,
        "id": data.get("id"),
        "executor": data.get("executor", "Desconhecido"),
        "place_id": data.get("place_id"),
        "job_id": data.get("job_id"),
        "version": data.get("version", "1.0.0"),
        "hwid": hwid,
        "ip": ip,
        "data": data_log,
        "hora": hora_log,
        "meta": {
            "user_agent": request.headers.get("User-Agent"),
            "key": provided_key is not None
        }
    }
    
    logs = get_cache(LOG_FILE)
    logs.append(novo)
    set_cache(LOG_FILE, logs)
    rotate_logs_if_needed()
    
    return jsonify({"status": "registrado"})

@app.route("/api/logs", methods=["GET"])
def logs_route():
    """Logs com paginação."""
    page = request.args.get("page", 1, type=int)
    limit = request.args.get("limit", 50, type=int)
    
    if page < 1:
        page = 1
    if limit < 1 or limit > 500:
        limit = 50
    
    logs = get_cache(LOG_FILE)
    total = len(logs)
    start = (page - 1) * limit
    end = start + limit
    
    paginated = logs[start:end]
    
    return jsonify({
        "logs": paginated,
        "pagination": {
            "page": page,
            "limit": limit,
            "total": total,
            "pages": (total + limit - 1) // limit
        }
    })

# -----------------------
# BAN endpoints
# -----------------------
@app.route("/api/admin/ban", methods=["POST"])
def admin_ban():
    ok, resp = require_admin_token()
    if not ok:
        return resp
    
    data = request.json or {}
    hwid = data.get("hwid")
    ip = data.get("ip")
    nick = data.get("nick")
    motivo = data.get("motivo", "sem motivo especificado")
    data_log, hora_log = agora_str()
    
    entry = {
        "hwid": hwid,
        "ip": ip,
        "nick": nick,
        "motivo": motivo,
        "data": data_log,
        "hora": hora_log
    }
    
    bans = load_bans()
    exists = False
    for b in bans:
        if (hwid and b.get("hwid") == hwid) or (ip and b.get("ip") == ip) or (nick and b.get("nick") == nick):
            exists = True
            break
    
    if not exists:
        bans.append(entry)
        save_bans(bans)
    
    return jsonify({"status": "banido", "entry": entry})

@app.route("/api/admin/unban", methods=["POST"])
def admin_unban():
    ok, resp = require_admin_token()
    if not ok:
        return resp
    
    data = request.json or {}
    hwid = data.get("hwid")
    ip = data.get("ip")
    nick = data.get("nick")
    
    bans = load_bans()
    novos = [b for b in bans if not ((hwid and b.get("hwid") == hwid) or (ip and b.get("ip") == ip) or (nick and b.get("nick") == nick))]
    save_bans(novos)
    return jsonify({"status": "desbanido"})

@app.route("/api/admin/banlist", methods=["GET"])
def admin_banlist():
    ok, resp = require_admin_token()
    if not ok:
        return resp
    return jsonify(load_bans())

@app.route("/api/banlist", methods=["GET"])
def public_banlist():
    """Banlist pública (sem motivos)."""
    bans = load_bans()
    short = [{"hwid": b.get("hwid"), "ip": b.get("ip"), "nick": b.get("nick"), "data": b.get("data")} for b in bans]
    return jsonify(short)

# -----------------------
# LOADER helpers
# -----------------------
def get_key_from_request():
    """Obtém key da requisição."""
    key = request.headers.get("X-Loader-Key")
    if not key:
        auth = request.headers.get("Authorization", "")
        if auth and not auth.startswith("Bearer "):
            key = auth
    if not key:
        key = request.args.get("key")
    return key

def check_loader_allowed():
    """Verifica permissão de loader."""
    if request.headers.get("X-Panel") == "1":
        return True
    
    token = request.headers.get("Authorization")
    if token and token in TOKENS and TOKENS[token] == "admin":
        return True
    
    ua = (request.headers.get("User-Agent") or "").lower()
    if "roblox" in ua:
        return True
    if request.headers.get("X-Roblox-UserId") or request.headers.get("X-Roblox-PlaceId"):
        return True
    
    key = get_key_from_request()
    if key and is_key_valid(key):
        return True
    
    return False

# -----------------------
# LOADER endpoints
# -----------------------
@app.route("/api/load/<path:route_id>", methods=["GET"])
def load_route(route_id):
    if not re.match(r"^[A-Za-z0-9_\-\.]+$", route_id):
        return ("Not found", 404)
    
    if not check_loader_allowed():
        return (jsonify({"error": "forbidden"}), 403)
    
    candidates = [
        os.path.join(LOADER_DIR, route_id),
        os.path.join(LOADER_DIR, route_id + ".lua"),
        os.path.join(LOADER_DIR, route_id + ".txt")
    ]
    for c in candidates:
        real = os.path.realpath(c)
        if os.path.isfile(real) and real.startswith(os.path.realpath(LOADER_DIR)):
            try:
                with open(real, "r", encoding="utf-8", errors="ignore") as f:
                    return Response(f.read(), mimetype="text/plain")
            except Exception as e:
                return jsonify({"error": "read error", "detail": str(e)}), 500
    
    universal_candidates = [
        os.path.join(LOADER_DIR, "universal"),
        os.path.join(LOADER_DIR, "universal.lua"),
        os.path.join(LOADER_DIR, "universal.txt")
    ]
    for u in universal_candidates:
        if os.path.isfile(u):
            with open(u, "r", encoding="utf-8", errors="ignore") as f:
                return Response(f.read(), mimetype="text/plain")
    
    return ("Not found", 404)

@app.route("/api/loader/list", methods=["GET"])
def loader_list():
    if not check_loader_allowed():
        return jsonify({"error": "forbidden"}), 403
    
    files = []
    try:
        for fname in os.listdir(LOADER_DIR):
            path = os.path.join(LOADER_DIR, fname)
            if os.path.isfile(path):
                stat = os.stat(path)
                files.append({
                    "id": fname,
                    "size": stat.st_size,
                    "modified": datetime.fromtimestamp(stat.st_mtime).strftime("%d/%m/%Y %H:%M")
                })
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    
    return jsonify(files)

@app.route("/api/loader/save", methods=["POST"])
def loader_save():
    if not check_loader_allowed():
        return jsonify({"error": "forbidden"}), 403
    
    data = request.get_json()
    if not data:
        return jsonify({"error": "invalid json"}), 400
    
    file_id = data.get("id")
    content = data.get("content")
    if not file_id or content is None:
        return jsonify({"error": "missing id or content"}), 400
    
    if not re.match(r"^[A-Za-z0-9_\-\.]+$", file_id):
        return jsonify({"error": "invalid id (use only letters, numbers, _, -, .)"}), 400
    
    file_path = os.path.join(LOADER_DIR, file_id)
    real_path = os.path.realpath(file_path)
    if not real_path.startswith(os.path.realpath(LOADER_DIR)):
        return jsonify({"error": "invalid path"}), 400
    
    try:
        with open(real_path, "w", encoding="utf-8") as f:
            f.write(content)
        return jsonify({"status": "ok", "path": file_id})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/loader/delete", methods=["POST"])
def loader_delete():
    if not check_loader_allowed():
        return jsonify({"error": "forbidden"}), 403
    
    data = request.get_json()
    if not data:
        return jsonify({"error": "invalid json"}), 400
    
    file_id = data.get("id")
    if not file_id:
        return jsonify({"error": "missing id"}), 400
    
    if not re.match(r"^[A-Za-z0-9_\-\.]+$", file_id):
        return jsonify({"error": "invalid id"}), 400
    
    file_path = os.path.join(LOADER_DIR, file_id)
    real_path = os.path.realpath(file_path)
    if not real_path.startswith(os.path.realpath(LOADER_DIR)):
        return jsonify({"error": "invalid path"}), 400
    
    try:
        if os.path.isfile(real_path):
            os.remove(real_path)
            return jsonify({"status": "deletado"})
        return jsonify({"error": "arquivo não encontrado"}), 404
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# -----------------------
# PRODUTOS
# -----------------------
def load_produtos():
    return get_cache(PRODUTOS_FILE, default=[])

def save_produtos(produtos):
    set_cache(PRODUTOS_FILE, produtos)

@app.route("/api/produtos", methods=["GET"])
def api_get_produtos():
    """GET /api/produtos com filtros: ?categoria=, ?ativo=, ?q="""
    produtos = load_produtos()
    categoria = request.args.get("categoria")
    ativo = request.args.get("ativo")
    q = request.args.get("q")
    res = produtos
    if categoria:
        res = [p for p in res if p.get("categoria") == categoria]
    if ativo is not None:
        ativo_bool = ativo.lower() in ("1", "true", "yes")
        res = [p for p in res if bool(p.get("ativo", True)) == ativo_bool]
    if q:
        ql = q.lower()
        res = [p for p in res if ql in (p.get("nome","").lower() + " " + p.get("descricao","").lower())]
    return jsonify(res)

@app.route("/api/produtos/<int:prod_id>", methods=["GET"])
def api_get_produto(prod_id):
    produtos = load_produtos()
    for p in produtos:
        if p.get("id") == prod_id:
            return jsonify(p)
    return jsonify({"error": "não encontrado"}), 404

@app.route("/api/produtos", methods=["POST"])
def api_criar_produto():
    ok, resp = require_admin_token()
    if not ok:
        return resp
    
    data = request.json or {}
    produtos = load_produtos()
    novo_id = max((p.get("id", 0) for p in produtos), default=0) + 1
    
    novo = {
        "id": novo_id,
        "nome": data.get("nome", ""),
        "descricao": data.get("descricao", ""),
        "preco": float(data.get("preco", 0)),
        "preco_antigo": float(data.get("preco_antigo", 0)),
        "categoria": data.get("categoria", "outros"),
        "imagem": data.get("imagem", ""),
        "desconto": data.get("desconto", 0),
        "auto": bool(data.get("auto", False)),
        "link_discord": data.get("link_discord", ""),
        "ativo": bool(data.get("ativo", True)),
        "criado": agora_dt().strftime("%d/%m/%Y %H:%M")
    }
    produtos.append(novo)
    save_produtos(produtos)
    
    # Webhook assíncrono (OTIMIZAÇÃO 5)
    send_webhook_async(f"🆕 Produto criado: `{novo['nome']}` (id: {novo_id})")
    
    return jsonify({"status": "criado", "produto": novo})

@app.route("/api/produtos/<int:prod_id>", methods=["PUT"])
def api_editar_produto(prod_id):
    ok, resp = require_admin_token()
    if not ok:
        return resp
    
    data = request.json or {}
    produtos = load_produtos()
    for i, p in enumerate(produtos):
        if p.get("id") == prod_id:
            produtos[i].update({
                "nome": data.get("nome", p.get("nome")),
                "descricao": data.get("descricao", p.get("descricao")),
                "preco": float(data.get("preco", p.get("preco", 0))),
                "preco_antigo": float(data.get("preco_antigo", p.get("preco_antigo", 0))),
                "categoria": data.get("categoria", p.get("categoria", "outros")),
                "imagem": data.get("imagem", p.get("imagem", "")),
                "desconto": data.get("desconto", p.get("desconto", 0)),
                "auto": bool(data.get("auto", p.get("auto", False))),
                "link_discord": data.get("link_discord", p.get("link_discord", "")),
                "ativo": bool(data.get("ativo", p.get("ativo", True)))
            })
            save_produtos(produtos)
            
            send_webhook_async(f"✏️ Produto atualizado: `{produtos[i]['nome']}` (id: {prod_id})")
            
            return jsonify({"status": "atualizado", "produto": produtos[i]})
    return jsonify({"error": "não encontrado"}), 404

@app.route("/api/produtos/<int:prod_id>", methods=["DELETE"])
def api_excluir_produto(prod_id):
    ok, resp = require_admin_token()
    if not ok:
        return resp
    
    produtos = load_produtos()
    encontrados = [p for p in produtos if p.get("id") == prod_id]
    if not encontrados:
        return jsonify({"error": "não encontrado"}), 404
    
    produtos = [p for p in produtos if p.get("id") != prod_id]
    save_produtos(produtos)
    
    send_webhook_async(f"🗑️ Produto excluído: id {prod_id}")
    
    return jsonify({"status": "excluído"})

@app.route("/api/produtos/categorias", methods=["GET"])
def api_get_categorias():
    produtos = load_produtos()
    cats = {}
    for p in produtos:
        if p.get("ativo", True):
            cat = p.get("categoria", "outros")
            cats[cat] = cats.get(cat, 0) + 1
    return jsonify(cats)

# -----------------------
# WEBHOOKS ASSÍNCRONOS (OTIMIZAÇÃO 5)
# -----------------------
WEBHOOK_QUEUE = Queue()

def send_webhook_async(message):
    """Enfileira webhook para envio assíncrono."""
    if WEBHOOK_URL and WEBHOOK_URL.startswith("https://"):
        WEBHOOK_QUEUE.put({"content": message})

def webhook_worker():
    """Thread que processa webhooks enfileirados."""
    while True:
        try:
            payload = WEBHOOK_QUEUE.get(timeout=1)
            try:
                requests.post(WEBHOOK_URL, json=payload, timeout=5)
            except:
                pass
        except:
            pass

threading.Thread(target=webhook_worker, daemon=True).start()

# -----------------------
# SCRIPTS RAW
# -----------------------
WATERMARK = '''local Maker = "34hz"
print("by 34hz")
'''
def rebuild_scripts_index():
    """Reconstrói o índice de scripts para busca O(1)."""
    global SCRIPTS_INDEX
    scripts = get_cache(SCRIPTS_FILE, default=[])
    with CACHE_LOCK:
        SCRIPTS_INDEX = {s["id"]: s for s in scripts}
        # Limpa cache de raw processado ao reconstruir índice
        RAW_SCRIPTS_CACHE.clear()

def load_scripts_raw():
    return get_cache(SCRIPTS_FILE, default=[])

def save_scripts_raw(lst):
    set_cache(SCRIPTS_FILE, lst)
    rebuild_scripts_index()

# Inicializa o índice na primeira carga
rebuild_scripts_index()

def is_roblox_request():
    """Detecta requisição Roblox."""
    ua = (request.headers.get("User-Agent") or "").lower()
    if "roblox" in ua:
        return True
    if request.headers.get("X-Roblox-UserId") or request.headers.get("X-Roblox-PlaceId"):
        return True
    return False

@app.route("/api/scripts", methods=["GET"])
def api_scripts_list():
    ok, resp = require_admin_token()
    if not ok:
        return resp
    scripts = load_scripts_raw()
    safe = [{"id": s["id"], "titulo": s.get("titulo",""), "criado": s.get("criado",""), "atualizado": s.get("atualizado","")} for s in scripts]
    return jsonify(safe)

@app.route("/api/scripts", methods=["POST"])
def api_scripts_create():
    ok, resp = require_admin_token()
    if not ok:
        return resp
    data = request.json or {}
    titulo = (data.get("titulo") or "").strip()
    codigo = data.get("codigo") or ""
    if not titulo:
        return jsonify({"error": "titulo obrigatório"}), 400
    if not codigo:
        return jsonify({"error": "codigo obrigatório"}), 400
    scripts = load_scripts_raw()
    novo = {
        "id": uuid.uuid4().hex,
        "titulo": titulo,
        "codigo": codigo,
        "criado": agora_dt().isoformat(),
        "atualizado": agora_dt().isoformat()
    }
    scripts.append(novo)
    save_scripts_raw(scripts)
    rebuild_scripts_index()
    return jsonify({"status": "criado", "script": {"id": novo["id"], "titulo": novo["titulo"], "criado": novo["criado"]}})

@app.route("/api/scripts/<string:script_id>", methods=["GET"])
def api_scripts_get(script_id):
    ok, resp = require_admin_token()
    if not ok:
        return resp
    scripts = load_scripts_raw()
    for s in scripts:
        if s["id"] == script_id:
            return jsonify(s)
    return jsonify({"error": "não encontrado"}), 404

@app.route("/api/scripts/<string:script_id>", methods=["PUT"])
def api_scripts_edit(script_id):
    ok, resp = require_admin_token()
    if not ok:
        return resp
    data = request.json or {}
    scripts = load_scripts_raw()
    for i, s in enumerate(scripts):
        if s["id"] == script_id:
            if "titulo" in data and data["titulo"].strip():
                scripts[i]["titulo"] = data["titulo"].strip()
            if "codigo" in data:
                scripts[i]["codigo"] = data["codigo"]
            scripts[i]["atualizado"] = agora_dt().isoformat()
            save_scripts_raw(scripts)
            rebuild_scripts_index()
            return jsonify({"status": "atualizado", "script": {"id": scripts[i]["id"], "titulo": scripts[i]["titulo"]}})
    return jsonify({"error": "não encontrado"}), 404

@app.route("/api/scripts/<string:script_id>", methods=["DELETE"])
def api_scripts_delete(script_id):
    ok, resp = require_admin_token()
    if not ok:
        return resp
    scripts = load_scripts_raw()
    novos = [s for s in scripts if s["id"] != script_id]
    if len(novos) == len(scripts):
        return jsonify({"error": "não encontrado"}), 404
    save_scripts_raw(novos)
    rebuild_scripts_index()
    return jsonify({"status": "deletado"})

@app.route("/api/raw/<string:script_id>", methods=["GET"])
def api_raw_deliver(script_id):
    if not is_roblox_request():
        return Response("403 Forbidden", status=403, mimetype="text/plain")
    
    # Busca O(1) no índice
    if script_id in SCRIPTS_INDEX:
        # Cache do código final processado
        if script_id in RAW_SCRIPTS_CACHE:
            return Response(RAW_SCRIPTS_CACHE[script_id], status=200, mimetype="text/plain")
        
        s = SCRIPTS_INDEX[script_id]
        codigo_final = WATERMARK + "\n" + s.get("codigo", "")
        RAW_SCRIPTS_CACHE[script_id] = codigo_final
        return Response(codigo_final, status=200, mimetype="text/plain")
        
    return Response("-- not found", status=404, mimetype="text/plain")

# -----------------------
# BACKUPS (OTIMIZAÇÃO 6)
# -----------------------
def enviar_webhook_arquivo(titulo, arquivo):
    """Envia arquivo ao webhook (assíncrono)."""
    try:
        if not WEBHOOK_URL or not WEBHOOK_URL.startswith("https://"):
            return
        data, hora = agora_str()
        tamanho = os.path.getsize(arquivo) if os.path.exists(arquivo) else 0
        tamanho_mb = round(tamanho / 1024 / 1024, 2)
        msg = f"""# {titulo}

> Data: {data}
> Hora: {hora}
> Tamanho: {tamanho_mb} MB
"""
        with open(arquivo, "rb") as f:
            files = {"file": (os.path.basename(arquivo), f)}
            payload = {"content": msg}
            requests.post(WEBHOOK_URL, data=payload, files=files, timeout=10)
    except Exception:
        pass

def backup_source():
    """Cria backup do source."""
    try:
        nome = f"backup_source_{int(time.time())}.zip"
        with zipfile.ZipFile(nome, "w", zipfile.ZIP_DEFLATED) as zipf:
            for candidate in ("app.py", "requirements.txt", "index.html", "logs.json", "bans.json", "users.json", "produtos.json", "keys.json", "scripts.json", "package.json"):
                if os.path.exists(candidate):
                    try:
                        zipf.write(candidate)
                    except Exception:
                        pass
            for root, dirs, files in os.walk(LOADER_DIR):
                for file in files:
                    path = os.path.join(root, file)
                    try:
                        zipf.write(path, arcname=os.path.relpath(path, start=os.getcwd()))
                    except Exception:
                        pass
        enviar_webhook_arquivo("Backup Source", nome)
    finally:
        try:
            if os.path.exists(nome):
                os.remove(nome)
        except Exception:
            pass

def backup_dados():
    """Cria backup dos dados."""
    arquivos = [LOG_FILE, BAN_FILE, USERS_FILE, PRODUTOS_FILE, KEYS_FILE]
    for arq in arquivos:
        try:
            if not os.path.exists(arq):
                continue
            nome_zip = f"backup_{os.path.splitext(os.path.basename(arq))[0]}_{int(time.time())}.zip"
            with zipfile.ZipFile(nome_zip, "w", zipfile.ZIP_DEFLATED) as zipf:
                try:
                    zipf.write(arq)
                except Exception:
                    pass
            enviar_webhook_arquivo("Backup Dados", nome_zip)
        finally:
            try:
                if os.path.exists(nome_zip):
                    os.remove(nome_zip)
            except Exception:
                pass

def sistema_backup(loop_seconds=21600):
    """Thread de backups periódicos."""
    while True:
        try:
            backup_source()
            backup_dados()
        except Exception as e:
            try:
                print("Erro backup:", e)
            except:
                pass
        time.sleep(loop_seconds)

# -----------------------
# PRUNE DE KEYS
# -----------------------
def prune_expired_keys():
    """Remove keys expiradas."""
    keys = load_keys()
    now = timestamp_now()
    removed = []
    for token, info in list(keys.items()):
        exp = info.get("expires_at")
        if exp is not None:
            try:
                if int(exp) < now:
                    removed.append(token)
                    del keys[token]
            except Exception:
                continue
    if removed:
        save_keys(keys)
    return len(removed)

def prune_worker(interval_seconds=3600):
    """Worker de prune periódico."""
    while True:
        try:
            removed = prune_expired_keys()
            if removed:
                try:
                    print(f"Prune expirados: removidas {removed} keys")
                except:
                    pass
        except Exception as e:
            try:
                print("Erro prune keys:", e)
            except:
                pass
        time.sleep(interval_seconds)

@app.route("/api/admin/prune_keys", methods=["POST"])
def admin_prune_keys():
    ok, resp = require_admin_token()
    if not ok:
        return resp
    removed = prune_expired_keys()
    return jsonify({"status": "prune_executado", "removed": removed})

# -----------------------
# INICIALIZAÇÃO
# -----------------------
if __name__ == "__main__":
    # Reconstrói índices de bans
    rebuild_ban_index()
    rebuild_scripts_index()
    
    # Inicia thread de backup
    try:
        threading.Thread(target=sistema_backup, kwargs={"loop_seconds": 21600}, daemon=True).start()
    except Exception as e:
        print("Não foi possível iniciar thread de backup:", e)
    
    # Inicia thread de prune
    try:
        threading.Thread(target=prune_worker, kwargs={"interval_seconds": 3600}, daemon=True).start()
    except Exception as e:
        print("Não foi possível iniciar thread de prune:", e)
    
    # Configuráveis via env
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", 80))
    
    print(f"Starting optimized API on {host}:{port} ...")
    print("Use gunicorn for production: gunicorn -w 4 -b 0.0.0.0:80 app:app")
    app.run(host=host, port=port, threaded=True)