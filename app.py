import os
import io
import csv
import re
import uuid
import hmac
import base64
import secrets
import time
import logging
from pathlib import Path
from html import escape
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from functools import wraps

import httpx
import qrcode
from flask import (
    Flask, render_template, request, redirect,
    url_for, session, flash, jsonify, Response
)
from dotenv import load_dotenv
from supabase import create_client, Client

ENV_PATH = Path(__file__).resolve().with_name(".env")
load_dotenv(ENV_PATH, override=True)

# ==========================================
# KONFIGURASI
# ==========================================


def clean_env(key, default=""):
    return os.environ.get(key, default).strip().strip('"').strip("'")


MAX_UPLOAD_MB = 3
MAX_UPLOAD_BYTES = MAX_UPLOAD_MB * 1024 * 1024

ADMIN_SESSION_IDLE_HOURS = 12
ADMIN_SESSION_MAX_HOURS = 24

# Total Kuota: 10 + 15 + 75 = 100
KUOTA_DEFAULT = 100

SUPABASE_URL = clean_env("SUPABASE_URL", "")
SUPABASE_SERVICE_ROLE_KEY = clean_env("SUPABASE_SERVICE_ROLE_KEY", "")
SUPABASE_BUCKET = clean_env("SUPABASE_BUCKET", "bukti-transfer")
ADMIN_PASSWORD = clean_env("ADMIN_PASSWORD", "")
WHATSAPP_GROUP_LINK = clean_env("WHATSAPP_GROUP_LINK", "")
FLASK_SECRET_KEY = clean_env("FLASK_SECRET_KEY", "")

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)


def load_secret_key() -> str:
    env_key = os.environ.get("FLASK_SECRET_KEY", "").strip()
    if env_key:
        return env_key
    key_file = ENV_PATH.with_name(".flask_secret_key")
    try:
        if key_file.exists():
            saved = key_file.read_text(encoding="utf-8").strip()
            if saved:
                return saved
        new_key = secrets.token_hex(32)
        key_file.write_text(new_key, encoding="utf-8")
        return new_key
    except OSError:
        return secrets.token_hex(32)


app.secret_key = load_secret_key()

app.config.update(
    PERMANENT_SESSION_LIFETIME=timedelta(hours=ADMIN_SESSION_IDLE_HOURS),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=bool(os.environ.get(
        "VERCEL")) or os.environ.get("COOKIE_SECURE") == "1",
    MAX_CONTENT_LENGTH=MAX_UPLOAD_BYTES + 200 * 1024
)

supabase: Client = (
    create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)
    if SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY else None
)

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# ==========================================
# HELPER
# ==========================================


def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        is_api = request.path.startswith("/api/")
        if not session.get("admin_logged_in"):
            if is_api:
                return jsonify({"success": False, "message": "Sesi admin berakhir."}), 401
            return redirect(url_for("login"))
        if time.time() - session.get("login_at", 0) > ADMIN_SESSION_MAX_HOURS * 3600:
            session.clear()
            if is_api:
                return jsonify({"success": False, "message": "Sesi berakhir."}), 401
            flash("Sesi berakhir, silakan login kembali.", "error")
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated_function


def get_wib_now():
    return datetime.now(ZoneInfo("Asia/Jakarta")).strftime("%d-%m-%Y %H:%M:%S WIB")


def generate_qr_base64(ticket_code: str) -> str:
    qr = qrcode.QRCode(
        version=1, error_correction=qrcode.constants.ERROR_CORRECT_H, box_size=10, border=4)
    qr.add_data(ticket_code)
    qr.make(fit=True)
    # Disesuaikan ke warna cokelat gelap
    img = qr.make_image(fill_color="#3e2110", back_color="white")
    buffer = io.BytesIO()
    img.save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode("utf-8")


def detect_image_type(data: bytes):
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg", ".jpg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png", ".png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp", ".webp"
    return None, None


def storage_path_from_url(url: str):
    if not url or f"/{SUPABASE_BUCKET}/" not in url:
        return None
    return url.split(f"/{SUPABASE_BUCKET}/", 1)[1].split("?")[0] or None


def remove_bukti_files(paths):
    paths = [p for p in paths if p]
    if not (supabase and paths):
        return
    try:
        supabase.storage.from_(SUPABASE_BUCKET).remove(paths)
    except Exception:
        app.logger.exception("Gagal menghapus file bukti")


def csv_safe(value):
    if value is None:
        return ""
    s = str(value)
    return "'" + s if s[:1] in ("=", "+", "-", "@", "\t", "\r") else s


def run_with_retry(fn, tries=3):
    for attempt in range(tries):
        try:
            return fn()
        except httpx.TransportError:
            if attempt == tries - 1:
                raise
            time.sleep(0.4)


def get_kuota_maksimal() -> int:
    if not supabase:
        return KUOTA_DEFAULT
    try:
        res = supabase.table("pengaturan").select(
            "kuota_maksimal").eq("id", 1).execute()
        if res.data:
            return res.data[0]["kuota_maksimal"]
    except Exception:
        pass
    return KUOTA_DEFAULT


def get_total_terdaftar() -> int:
    if not supabase:
        return 0

    res = run_with_retry(lambda: supabase.table(
        "peserta").select("id", count="exact").execute())
    return res.count or 0


def kuota_penuh() -> bool:
    if not supabase:
        return False
    try:
        return get_total_terdaftar() >= get_kuota_maksimal()
    except Exception:
        return False


def hitung_harga_dinamis(total_terdaftar: int) -> tuple[int, str]:
    """Mengembalikan (Harga, Nama Paket) berdasarkan urutan pendaftar."""
    if total_terdaftar < 10:
        return 59000, "Early Bird (1-10)"
    elif total_terdaftar < 25:
        return 69000, "Presale (11-25)"
    else:
        return 79000, "Normal (26+)"

# ==========================================
# PUBLIC ROUTES
# ==========================================


@app.route("/")
def index():
    # Mengirim data harga saat ini ke halaman depan agar informatif
    total = get_total_terdaftar()
    harga_saat_ini, nama_paket = hitung_harga_dinamis(total)
    sisa_kuota_paket = 0

    if total < 10:
        sisa_kuota_paket = 10 - total
    elif total < 25:
        sisa_kuota_paket = 25 - total

    return render_template("index.html", harga=harga_saat_ini, paket=nama_paket, sisa_paket=sisa_kuota_paket)


@app.route("/about")
def about():
    return render_template("about.html")


def render_daftar(status=200):
    total = get_total_terdaftar()
    harga_saat_ini, _ = hitung_harga_dinamis(total)
    return render_template("daftar.html", form=request.form, max_mb=MAX_UPLOAD_MB, harga_aktif=harga_saat_ini), status


@app.errorhandler(413)
def file_too_large(_e):
    flash(f"Ukuran file terlalu besar. Maksimal {MAX_UPLOAD_MB} MB.", "error")
    return render_daftar(413)


@app.route("/habis")
def habis():
    if not kuota_penuh():
        return redirect(url_for("daftar"))
    return render_template("habis.html")


@app.route("/daftar", methods=["GET", "POST"])
def daftar():
    if request.method == "GET":
        if kuota_penuh():
            return redirect(url_for("habis"))
        total = get_total_terdaftar()
        harga_saat_ini, _ = hitung_harga_dinamis(total)
        return render_template("daftar.html", form={}, max_mb=MAX_UPLOAD_MB, harga_aktif=harga_saat_ini)

    def fail(message, status=400):
        flash(message, "error")
        return render_daftar(status)

    if not supabase:
        return fail("Sistem pendaftaran belum terhubung ke database.", 503)

    nama = request.form.get("nama", "").strip()
    email = request.form.get("email", "").strip().lower()
    no_telp = re.sub(r"[\s\-]", "", request.form.get("no_telp", ""))
    domisili = request.form.get("domisili", "").strip()
    file_bukti = request.files.get("bukti_tf")

    try:
        umur = int(request.form.get("umur", ""))
    except ValueError:
        return fail("Umur harus berupa angka.")

    if not (2 <= len(nama) <= 100):
        return fail("Nama lengkap harus 2–100 karakter.")
    if len(email) > 254 or not EMAIL_RE.match(email):
        return fail("Format email tidak valid.")
    if not re.fullmatch(r"\+?\d{8,15}", no_telp):
        return fail("Nomor telepon tidak valid (8–15 digit).")
    if not (2 <= len(domisili) <= 100):
        return fail("Domisili harus 2–100 karakter.")
    if not (10 <= umur <= 99):
        return fail("Umur harus antara 10 dan 99 tahun.")

    if not file_bukti or not file_bukti.filename:
        return fail("Bukti transfer wajib diunggah.")
    file_bytes = file_bukti.read(MAX_UPLOAD_BYTES + 1)
    if len(file_bytes) > MAX_UPLOAD_BYTES:
        return fail(f"Ukuran bukti transfer maksimal {MAX_UPLOAD_MB} MB.")
    content_type, ext = detect_image_type(file_bytes)
    if not content_type:
        return fail("Bukti transfer harus berupa gambar JPG, JPEG, PNG, atau WEBP.")

    try:
        total_pendaftar = get_total_terdaftar()
        if total_pendaftar >= get_kuota_maksimal():
            return redirect(url_for("habis"))

        sama_email = run_with_retry(lambda: supabase.table(
            "peserta").select("id,nama").eq("email", email).execute())
        nama_baru = " ".join(nama.lower().split())
        if any(" ".join((r.get("nama") or "").lower().split()) == nama_baru for r in (sama_email.data or [])):
            return fail("Nama ini sudah terdaftar dengan email tersebut.")

        # Tentukan harga secara dinamis tepat saat submit diterima
        total_bayar, paket_dinamis = hitung_harga_dinamis(total_pendaftar)

    except Exception as e:
        app.logger.exception("Gagal memeriksa data")
        return fail("Terjadi gangguan pada server. Silakan coba lagi.", 500)

    ticket_code = f"HW-{uuid.uuid4().hex[:6].upper()}"
    storage_filename = f"tf_{ticket_code}_{uuid.uuid4().hex[:4]}{ext}"
    uploaded = False

    try:
        qr_base64 = generate_qr_base64(ticket_code)

        supabase.storage.from_(SUPABASE_BUCKET).upload(
            path=storage_filename, file=file_bytes, file_options={
                "content-type": content_type}
        )
        uploaded = True
        bukti_tf_url = supabase.storage.from_(
            SUPABASE_BUCKET).get_public_url(storage_filename)

        supabase.table("peserta").insert({
            "nama": nama,
            "email": email,
            "no_telp": no_telp,
            "umur": umur,
            "domisili": domisili,
            "paket": paket_dinamis,  # Disimpan sebagai Early Bird / Presale / Normal
            "total_bayar": total_bayar,
            "bukti_tf_url": bukti_tf_url,
            "qr_code": ticket_code,
            "qr_base64": qr_base64,
            # Diabaikan karena email dihapus, tapi kolom tetap ada agar db tidak error
            "email_sent": False,
            "status_hadir": False,
            "waktu_scan": None,
        }).execute()
    except Exception as e:
        app.logger.exception("Gagal menyimpan pendaftaran")
        if uploaded:
            remove_bukti_files([storage_filename])
        return fail("Pendaftaran gagal diproses. Silakan coba lagi.", 500)

    return redirect(url_for("sukses"))


@app.route("/sukses")
def sukses():
    return render_template("sukses.html", wa_link=WHATSAPP_GROUP_LINK)

# ==========================================
# AUTH & ADMIN ROUTES (Sisanya tidak diubah, kecuali API Send QR dihapus)
# ==========================================


@app.route("/login", methods=["GET", "POST"])
def login():
    if session.get("admin_logged_in"):
        return redirect(url_for("admin_dashboard"))
    if request.method == "POST":
        password = request.form.get("password", "")
        if ADMIN_PASSWORD and hmac.compare_digest(password.encode(), ADMIN_PASSWORD.encode()):
            session.clear()
            session["admin_logged_in"] = True
            session["login_at"] = time.time()
            session.permanent = True
            return redirect(url_for("admin_dashboard"))
        time.sleep(1)
        flash("Password admin salah.", "error")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.pop("admin_logged_in", None)
    return redirect(url_for("index"))


@app.route("/admin")
@login_required
def admin_dashboard():
    peserta_list = []
    if supabase:
        res = supabase.table("peserta").select(
            "*").order("created_at", desc=True).execute()
        peserta_list = res.data or []
    total = len(peserta_list)
    sudah_hadir = sum(1 for p in peserta_list if p.get("status_hadir"))
    km = get_kuota_maksimal()
    return render_template("admin.html", peserta_list=peserta_list, total_terdaftar=total,
                           sudah_hadir=sudah_hadir, belum_hadir=total - sudah_hadir,
                           kuota_maksimal=km, sisa_kuota=max(0, km - total))


@app.route("/admin/kuota", methods=["POST"])
@login_required
def update_kuota():
    try:
        kb = int(request.form.get("kuota", KUOTA_DEFAULT))
        if kb < 0:
            raise ValueError
    except ValueError:
        flash("Kuota harus berupa angka 0 atau lebih.", "error")
        return redirect(url_for("admin_dashboard"))
    if supabase:
        supabase.table("pengaturan").upsert(
            {"id": 1, "kuota_maksimal": kb}).execute()
        flash(f"Kuota berhasil diperbarui menjadi {kb}.", "success")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/delete/<int:peserta_id>", methods=["POST"])
@login_required
def delete_peserta(peserta_id):
    if supabase:
        res = supabase.table("peserta").select(
            "bukti_tf_url").eq("id", peserta_id).execute()
        supabase.table("peserta").delete().eq("id", peserta_id).execute()
        if res.data:
            remove_bukti_files(
                [storage_path_from_url(res.data[0].get("bukti_tf_url"))])
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/export")
@login_required
def export_csv():
    peserta_list = []
    if supabase:
        res = supabase.table("peserta").select(
            "*").order("id", desc=False).execute()
        peserta_list = res.data or []

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["No", "Nama Lengkap", "Email", "No Telp", "Umur",
                    "Domisili", "Paket/Tier", "Total Bayar", "Waktu Daftar"])

    for idx, p in enumerate(peserta_list, start=1):
        writer.writerow([
            idx, csv_safe(p.get("nama")), csv_safe(
                p.get("email")), csv_safe(p.get("no_telp")),
            p.get("umur"), csv_safe(p.get("domisili")), p.get(
                "paket"), p.get("total_bayar"), p.get("created_at")
        ])

    return Response(output.getvalue(), mimetype="text/csv", headers={"Content-Disposition": "attachment;filename=peserta_her_wisdom.csv"})


if __name__ == "__main__":
    app.run(debug=True, port=5000)
