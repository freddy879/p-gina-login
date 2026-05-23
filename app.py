from flask import Flask, request, jsonify, send_from_directory, send_file
from flask_cors import CORS
from pymongo import MongoClient
from pymongo.errors import ConnectionFailure
from bson import ObjectId
import os
import uuid
import json
import mimetypes
import io
from datetime import datetime

app = Flask(__name__)
CORS(app)

# =========================================
# MONGODB ATLAS CONNECTION
# =========================================

MONGO_URI = "mongodb+srv://nexus_dri:freddy123@cluster0.np3mr9e.mongodb.net/?retryWrites=true&w=majority"
DB_NAME = "nexus_drive"

client = MongoClient(MONGO_URI)
db = client[DB_NAME]

# Collections
files_col    = db["files"]       # metadatos de archivos
folders_col  = db["folders"]     # carpetas (incluso vacías)
users_col    = db["users"]       # usuarios
chunks_col   = db["file_chunks"] # contenido binario de archivos (base64 chunks)

# Índices para performance
files_col.create_index("folder")
files_col.create_index("uploaded_at")
files_col.create_index("share_id", sparse=True)
folders_col.create_index("name", unique=True)
users_col.create_index("username", unique=True)
chunks_col.create_index("file_id")

# =========================================
# SEED USUARIOS POR DEFECTO
# =========================================

def seed_users():
    defaults = [
        {"username": "admin",  "password": "admin123",  "role": "Administrador", "department": "IT",       "avatar": "AD"},
        {"username": "maria",  "password": "maria123",  "role": "Gerente",        "department": "Ventas",   "avatar": "MR"},
        {"username": "carlos", "password": "carlos123", "role": "Analista",       "department": "Finanzas", "avatar": "CL"},
    ]
    for u in defaults:
        users_col.update_one({"username": u["username"]}, {"$setOnInsert": u}, upsert=True)

seed_users()

# =========================================
# HELPERS
# =========================================

def format_size(bytes_size):
    for unit in ["B", "KB", "MB", "GB"]:
        if bytes_size < 1024:
            return f"{bytes_size:.1f} {unit}"
        bytes_size /= 1024
    return f"{bytes_size:.1f} TB"

def get_file_icon(filename):
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    icons = {
        "pdf": "pdf", "doc": "word", "docx": "word",
        "xls": "excel", "xlsx": "excel", "csv": "excel",
        "ppt": "ppt", "pptx": "ppt",
        "jpg": "image", "jpeg": "image", "png": "image", "gif": "image", "webp": "image",
        "mp4": "video", "avi": "video", "mov": "video", "webm": "video",
        "mp3": "audio", "wav": "audio", "flac": "audio", "m4a": "audio",
        "zip": "archive", "rar": "archive", "7z": "archive",
        "py": "code", "js": "code", "ts": "code", "html": "code",
        "css": "code", "json": "code", "sh": "code",
        "txt": "text", "md": "text", "log": "text",
    }
    return icons.get(ext, "file")

def serialize(doc):
    """Convierte ObjectId a string para JSON."""
    if doc is None:
        return None
    doc = dict(doc)
    if "_id" in doc:
        doc["_id"] = str(doc["_id"])
    return doc

# =========================================
# AUTH
# =========================================

@app.route("/login", methods=["POST"])
def login():
    data = request.json or {}
    username = data.get("username", "").strip()
    password = data.get("password", "")
    user = users_col.find_one({"username": username, "password": password})
    if user:
        return jsonify({
            "success": True,
            "user": {
                "username": user["username"],
                "role":     user["role"],
                "department": user["department"],
                "avatar":   user.get("avatar", username[:2].upper())
            }
        })
    return jsonify({"success": False, "error": "Credenciales incorrectas"}), 401

# =========================================
# HOME / STATUS
# =========================================

@app.route("/")
def home():
    total_files = files_col.count_documents({})
    pipeline = [{"$group": {"_id": None, "total": {"$sum": "$size"}}}]
    size_result = list(files_col.aggregate(pipeline))
    total_size = size_result[0]["total"] if size_result else 0
    return jsonify({
        "status": "NEXUS DRIVE ENTERPRISE — MongoDB Atlas",
        "version": "4.0",
        "db": DB_NAME,
        "stats": {
            "total_files": total_files,
            "total_size": format_size(total_size),
            "total_folders": folders_col.count_documents({})
        }
    })

# =========================================
# SUBIR ARCHIVO  (almacena binario en MongoDB)
# =========================================

@app.route("/upload", methods=["POST"])
def upload():
    if "file" not in request.files:
        return jsonify({"error": "No file"}), 400
    file        = request.files["file"]
    folder      = request.form.get("folder", "root")
    uploaded_by = request.form.get("user", "unknown")
    department  = request.form.get("department", "General")
    tags_raw    = request.form.get("tags", "")

    if file.filename == "":
        return jsonify({"error": "empty file"}), 400

    file_id     = str(uuid.uuid4())
    original_name = file.filename
    ext = original_name.rsplit(".", 1)[-1].lower() if "." in original_name else ""
    mime = mimetypes.guess_type(original_name)[0] or "application/octet-stream"

    # Leer contenido completo en memoria
    content = file.read()
    size    = len(content)

    # Guardar binario en MongoDB en chunks de 4MB para evitar límite de 16MB por doc
    CHUNK_SIZE = 4 * 1024 * 1024
    chunk_ids = []
    for i in range(0, len(content), CHUNK_SIZE):
        chunk = content[i:i + CHUNK_SIZE]
        res = chunks_col.insert_one({
            "file_id":  file_id,
            "index":    len(chunk_ids),
            "data":     chunk   # guardado como bytes (Binary BSON)
        })
        chunk_ids.append(str(res.inserted_id))

    share_id = str(uuid.uuid4())[:12]
    tags = [t.strip() for t in tags_raw.split(",") if t.strip()]

    doc = {
        "id":            file_id,
        "original_name": original_name,
        "ext":           ext,
        "folder":        folder,
        "size":          size,
        "size_display":  format_size(size),
        "mime":          mime,
        "icon":          get_file_icon(original_name),
        "uploaded_by":   uploaded_by,
        "department":    department,
        "tags":          tags,
        "uploaded_at":   datetime.now().isoformat(),
        "starred":       False,
        "is_public":     False,
        "share_id":      share_id,
        "description":   "",
        "chunk_ids":     chunk_ids,
        "chunk_count":   len(chunk_ids)
    }
    files_col.insert_one(doc)

    # Asegurar que la carpeta exista en folders_col
    folders_col.update_one(
        {"name": folder},
        {"$setOnInsert": {"name": folder, "created_at": datetime.now().isoformat()}},
        upsert=True
    )

    return jsonify({"message": "uploaded", "file_id": file_id, "file": serialize(doc)})

# =========================================
# LISTAR ARCHIVOS
# =========================================

@app.route("/files", methods=["GET"])
def list_files():
    folder      = request.args.get("folder", "")
    search      = request.args.get("search", "")
    sort_by     = request.args.get("sort", "date")
    filter_type = request.args.get("type", "")
    starred     = request.args.get("starred", "")

    query = {}
    if folder and folder != "all":
        query["folder"] = folder
    if search:
        query["$or"] = [
            {"original_name": {"$regex": search, "$options": "i"}},
            {"tags": {"$regex": search, "$options": "i"}},
            {"description": {"$regex": search, "$options": "i"}}
        ]
    if filter_type:
        query["icon"] = filter_type
    if starred == "true":
        query["starred"] = True

    sort_field = {"date": "uploaded_at", "name": "original_name", "size": "size"}.get(sort_by, "uploaded_at")
    sort_dir   = -1 if sort_by in ("date", "size") else 1

    docs = list(files_col.find(query, {"chunk_ids": 0}).sort(sort_field, sort_dir))
    return jsonify([serialize(d) for d in docs])

# =========================================
# LISTAR CARPETAS (incluye vacías)
# =========================================

@app.route("/folders", methods=["GET"])
def list_folders():
    # Carpetas registradas explícitamente
    registered = {f["name"]: f for f in folders_col.find({}, {"_id": 0})}

    # Agregar conteos desde archivos
    pipeline = [
        {"$group": {"_id": "$folder", "count": {"$sum": 1}, "size": {"$sum": "$size"}}}
    ]
    counts = {r["_id"]: {"count": r["count"], "size": r["size"]} for r in files_col.aggregate(pipeline)}

    all_names = set(registered.keys()) | set(counts.keys())
    result = []
    for name in sorted(all_names):
        c = counts.get(name, {"count": 0, "size": 0})
        result.append({
            "name":         name,
            "count":        c["count"],
            "size":         c["size"],
            "size_display": format_size(c["size"])
        })
    return jsonify(result)

# =========================================
# CREAR CARPETA
# =========================================

@app.route("/folders/create", methods=["POST"])
def create_folder():
    data = request.json or {}
    name = data.get("name", "").strip()
    if not name:
        return jsonify({"error": "Nombre requerido"}), 400
    folders_col.update_one(
        {"name": name},
        {"$setOnInsert": {"name": name, "created_at": datetime.now().isoformat()}},
        upsert=True
    )
    return jsonify({"message": "Carpeta creada", "name": name})

# =========================================
# RENOMBRAR CARPETA
# =========================================

@app.route("/folders/rename", methods=["PATCH"])
def rename_folder():
    data     = request.json or {}
    old_name = data.get("old_name", "").strip()
    new_name = data.get("new_name", "").strip()
    if not old_name or not new_name or old_name == new_name:
        return jsonify({"error": "Nombres inválidos"}), 400

    # Renombrar en folders_col
    folders_col.update_one({"name": old_name}, {"$set": {"name": new_name}})

    # Mover todos los archivos a la nueva carpeta
    files_col.update_many({"folder": old_name}, {"$set": {"folder": new_name}})

    return jsonify({"message": "Carpeta renombrada", "old": old_name, "new": new_name})

# =========================================
# SERVIR ARCHIVO (descarga)
# =========================================

def reconstruct_file(file_id):
    """Reconstruye el binario desde los chunks en MongoDB."""
    chunks = list(chunks_col.find({"file_id": file_id}).sort("index", 1))
    if not chunks:
        return None
    content = b"".join(bytes(c["data"]) for c in chunks)
    return content

@app.route("/download/<file_id>")
def download(file_id):
    doc = files_col.find_one({"id": file_id})
    if not doc:
        return jsonify({"error": "not found"}), 404
    content = reconstruct_file(file_id)
    if content is None:
        return jsonify({"error": "file data not found"}), 404
    return send_file(
        io.BytesIO(content),
        as_attachment=True,
        download_name=doc["original_name"],
        mimetype=doc.get("mime", "application/octet-stream")
    )

# =========================================
# PREVISUALIZAR ARCHIVO (inline)
# =========================================

@app.route("/preview/<file_id>")
def preview_file(file_id):
    doc = files_col.find_one({"id": file_id})
    if not doc:
        return jsonify({"error": "not found"}), 404
    content = reconstruct_file(file_id)
    if content is None:
        return jsonify({"error": "file data not found"}), 404
    return send_file(
        io.BytesIO(content),
        as_attachment=False,
        download_name=doc["original_name"],
        mimetype=doc.get("mime", "application/octet-stream")
    )

# =========================================
# VER ARCHIVO POR SHARE LINK (público)
# =========================================

@app.route("/view/<share_id>")
def view_file(share_id):
    doc = files_col.find_one({"share_id": share_id})
    if not doc:
        return jsonify({"error": "Archivo no encontrado"}), 404
    if not doc.get("is_public"):
        return jsonify({"error": "Acceso denegado"}), 403
    content = reconstruct_file(doc["id"])
    if content is None:
        return jsonify({"error": "file data not found"}), 404
    return send_file(
        io.BytesIO(content),
        as_attachment=False,
        download_name=doc["original_name"],
        mimetype=doc.get("mime", "application/octet-stream")
    )

# =========================================
# COMPARTIR / REVOCAR
# =========================================

@app.route("/share/<file_id>", methods=["POST"])
def share_file(file_id):
    doc = files_col.find_one_and_update(
        {"id": file_id}, {"$set": {"is_public": True}}, return_document=True
    )
    if not doc:
        return jsonify({"error": "not found"}), 404
    return jsonify({
        "shared": True,
        "share_id": doc["share_id"],
        "full_url": f"http://127.0.0.1:5000/view/{doc['share_id']}"
    })

@app.route("/unshare/<file_id>", methods=["POST"])
def unshare_file(file_id):
    result = files_col.update_one({"id": file_id}, {"$set": {"is_public": False}})
    if result.matched_count == 0:
        return jsonify({"error": "not found"}), 404
    return jsonify({"shared": False})

# =========================================
# ELIMINAR ARCHIVO
# =========================================

@app.route("/delete/<file_id>", methods=["DELETE"])
def delete(file_id):
    doc = files_col.find_one({"id": file_id})
    if not doc:
        return jsonify({"error": "not found"}), 404
    files_col.delete_one({"id": file_id})
    chunks_col.delete_many({"file_id": file_id})
    return jsonify({"message": "deleted"})

# =========================================
# MARCAR COMO FAVORITO
# =========================================

@app.route("/star/<file_id>", methods=["POST"])
def star(file_id):
    doc = files_col.find_one({"id": file_id})
    if not doc:
        return jsonify({"error": "not found"}), 404
    new_val = not doc.get("starred", False)
    files_col.update_one({"id": file_id}, {"$set": {"starred": new_val}})
    return jsonify({"starred": new_val})

# =========================================
# RENOMBRAR ARCHIVO
# =========================================

@app.route("/rename/<file_id>", methods=["PATCH"])
def rename_file(file_id):
    data = request.json or {}
    new_name = data.get("name", "").strip()
    if not new_name:
        return jsonify({"error": "Nombre requerido"}), 400
    ext = new_name.rsplit(".", 1)[-1].lower() if "." in new_name else ""
    files_col.update_one({"id": file_id}, {"$set": {
        "original_name": new_name,
        "ext": ext,
        "icon": get_file_icon(new_name),
        "mime": mimetypes.guess_type(new_name)[0] or "application/octet-stream"
    }})
    doc = files_col.find_one({"id": file_id}, {"chunk_ids": 0})
    return jsonify(serialize(doc))

# =========================================
# ACTUALIZAR DESCRIPCIÓN / TAGS
# =========================================

@app.route("/update/<file_id>", methods=["PATCH"])
def update_file(file_id):
    data = request.json or {}
    updates = {}
    if "description" in data:
        updates["description"] = data["description"]
    if "tags" in data:
        updates["tags"] = data["tags"]
    if not updates:
        return jsonify({"error": "nothing to update"}), 400
    files_col.update_one({"id": file_id}, {"$set": updates})
    doc = files_col.find_one({"id": file_id}, {"chunk_ids": 0})
    return jsonify(serialize(doc))

# =========================================
# ESTADÍSTICAS
# =========================================

@app.route("/stats", methods=["GET"])
def stats():
    total_files = files_col.count_documents({})
    size_pipeline = [{"$group": {"_id": None, "total": {"$sum": "$size"}}}]
    size_result   = list(files_col.aggregate(size_pipeline))
    total_size    = size_result[0]["total"] if size_result else 0

    starred_count = files_col.count_documents({"starred": True})

    by_type_pipeline = [{"$group": {"_id": "$icon", "count": {"$sum": 1}}}]
    by_type = {r["_id"]: r["count"] for r in files_col.aggregate(by_type_pipeline)}

    by_dept_pipeline = [{"$group": {"_id": "$department", "size": {"$sum": "$size"}}}]
    by_dept = {r["_id"]: format_size(r["size"]) for r in files_col.aggregate(by_dept_pipeline)}

    by_user_pipeline = [{"$group": {"_id": "$uploaded_by", "count": {"$sum": 1}}}]
    by_user = {r["_id"]: r["count"] for r in files_col.aggregate(by_user_pipeline)}

    recent = [serialize(d) for d in files_col.find({}, {"chunk_ids": 0}).sort("uploaded_at", -1).limit(5)]

    return jsonify({
        "total_files":     total_files,
        "total_size":      format_size(total_size),
        "total_size_bytes": total_size,
        "starred":         starred_count,
        "by_type":         by_type,
        "by_department":   by_dept,
        "by_user":         by_user,
        "recent":          recent
    })

# =========================================
# INICIAR SERVIDOR
# =========================================

if __name__ == "__main__":
    try:
        client.admin.command("ping")
        print("✅ Conectado a MongoDB Atlas — Nexus Drive Enterprise 4.0")
    except ConnectionFailure as e:
        print(f"❌ Error de conexión a MongoDB: {e}")
    app.run(host="0.0.0.0", port=5000, debug=True)
