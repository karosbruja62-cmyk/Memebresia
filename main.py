"""
Bot de Telegram para controlar membresías de un canal o grupo (v2).

- Registra solo a quien entra al canal (30 días por defecto).
- Envía recordatorios antes de vencer (una sola vez cada uno).
- Expulsa automáticamente al vencer (sin bloquear: puede volver si renueva).
- Usa Postgres si existe DATABASE_URL (Railway); si no, usa SQLite local.
- Varios canales: cada canal tiene un #alias; membresías, llaves y enlaces son por canal.
- Llaves de un solo uso (/llave, /canjear): el admin crea una llave con la
  duración que quiera; al canjearla empieza a correr el tiempo y al vencer
  se expulsa automáticamente. La llave queda inutilizable.

Variables de entorno:
    BOT_TOKEN         token de @BotFather                      (obligatoria)
    GROUP_ID          canal inicial (alias #principal)         (opcional)
                      Los demás canales se registran solos al agregar el bot como admin.
    ADMIN_IDS         tus IDs separados por coma               (obligatoria)
    DATABASE_URL      cadena de Postgres (Railway la da)       (opcional)
    DIAS_MEMBRESIA    días por defecto                         (def. 30)
    REMINDER_DAYS     recordatorios, días antes de vencer      (def. 5,2,1)
    REVISION_SEGUNDOS cada cuánto revisa vencimientos          (def. 60)
    TZ_OFFSET         diferencia horaria para mostrar fechas   (def. -5)
    AUTO_REGISTRO     1 registra a quien entra, 0 no           (def. 1)
    TELEGRAM_API_PROXY URL de un proxy hacia api.telegram.org  (opcional)
                      (usa 0 si vendes el acceso solo con llaves)
"""

import logging
import math
import os
import re
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone

from telegram import (
    BotCommand,
    BotCommandScopeChat,
    BotCommandScopeDefault,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ChatMemberHandler,
    CommandHandler,
    ContextTypes,
)

logging.basicConfig(
    format="%(asctime)s %(levelname)s: %(message)s", level=logging.INFO
)
# Evita que el token aparezca en los logs (httpx imprime la URL completa)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("membresias")

# ---------- Configuración ----------
TOKEN = os.environ["BOT_TOKEN"]
_g = os.environ.get("GROUP_ID", "").strip()
GROUP_ID = int(_g) if _g else None  # opcional: canal inicial (alias "principal")
ADMIN_IDS = {int(x) for x in os.environ["ADMIN_IDS"].split(",") if x.strip()}
DATABASE_URL = os.environ.get("DATABASE_URL")
DB_PATH = os.environ.get("DB_PATH", "membresias_v2.db")
DIAS_MEMBRESIA = int(os.environ.get("DIAS_MEMBRESIA", "30"))
REMINDER_DAYS = sorted(
    {int(x) for x in os.environ.get("REMINDER_DAYS", "5,2,1").split(",") if x.strip()},
    reverse=True,
)
REVISION_SEGUNDOS = int(os.environ.get("REVISION_SEGUNDOS", "60"))
LOCAL_TZ = timezone(timedelta(hours=float(os.environ.get("TZ_OFFSET", "-5"))))
AUTO_REGISTRO = os.environ.get("AUTO_REGISTRO", "1") == "1"
# Si el hosting no puede llegar a api.telegram.org, pasa por tu proxy (ej. Cloudflare Worker)
TELEGRAM_API_PROXY = os.environ.get("TELEGRAM_API_PROXY", "").strip().rstrip("/")
ALFABETO = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # sin 0/O/1/I para no confundir
UNIDADES = {"m": 60, "h": 3600, "d": 86400}
VIGENCIA_ENLACE = 600  # el enlace de invitación vence en 10 minutos

# ---------- Tiempo ----------
FMT = "%Y-%m-%dT%H:%M:%S"


def ahora():
    return datetime.now(timezone.utc)


def iso(dt):
    return dt.astimezone(timezone.utc).strftime(FMT)


def parse(s):
    return datetime.strptime(s, FMT).replace(tzinfo=timezone.utc)


def dias_restantes(vence_iso):
    seg = (parse(vence_iso) - ahora()).total_seconds()
    return max(0, math.ceil(seg / 86400))


def fecha_local(vence_iso):
    return parse(vence_iso).astimezone(LOCAL_TZ).strftime("%d/%m/%Y %H:%M")


def fmt_seg(seg):
    """Segundos a texto corto: 2d 3h 10m."""
    seg = max(int(seg), 0)
    d, r = divmod(seg, 86400)
    h, r = divmod(r, 3600)
    m = r // 60
    partes = []
    if d:
        partes.append(f"{d}d")
    if h:
        partes.append(f"{h}h")
    if m or not partes:
        partes.append(f"{m}m")
    return " ".join(partes)


def tiempo_restante(vence_iso):
    return fmt_seg((parse(vence_iso) - ahora()).total_seconds())


# ---------- Base de datos (Postgres o SQLite) ----------
def run(sql, params=(), fetch=None):
    """Ejecuta SQL escrito con '?'. fetch: None, 'one' o 'all'."""
    if DATABASE_URL:
        import psycopg2
        import psycopg2.extras

        conn = psycopg2.connect(DATABASE_URL)
        try:
            with conn:
                with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                    cur.execute(sql.replace("?", "%s"), params)
                    if fetch == "all":
                        return [dict(r) for r in cur.fetchall()] 
                    if fetch == "one":
                        r = cur.fetchone()
                        return dict(r) if r else None
        finally:
            conn.close()
    else:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                cur = conn.execute(sql, params)
                if fetch == "all":
                    return [dict(r) for r in cur.fetchall()]
                if fetch == "one":
                    r = conn.fetchone()
                    return dict(r) if r else None
        finally:
            conn.close()


def init_db():
    run("""
        CREATE TABLE IF NOT EXISTS miembros (
            user_id BIGINT PRIMARY KEY,
            nombre TEXT,
            ultimo_pago TEXT,
            vence TEXT NOT NULL,
            recordatorios TEXT NOT NULL DEFAULT '',
            estado TEXT NOT NULL DEFAULT 'activo'
        )
    """)
    for col in ("renovaciones INTEGER NOT NULL DEFAULT 0", "expulsado_en TEXT"):
        try:
            run(f"ALTER TABLE miembros ADD COLUMN {col}")
        except Exception:
            pass


def obtener(uid, chat_id):
    return run("SELECT * FROM membresias WHERE user_id = ? AND chat_id = ?", (uid, chat_id), "one")


def registrar_td(uid, chat_id, nombre, delta, sumar=True):
    ahora_ = ahora()
    fila = obtener(uid, chat_id)
    base = ahora_
    if sumar and fila and fila["estado"] == "activo" and parse(fila["vence"]) > base:
        base = parse(fila["vence"])
    vence = base + delta
    run("""
        INSERT INTO membresias (user_id, chat_id, nombre, ultimo_pago, vence, recordatorios, estado)
        VALUES (?, ?, ?, ?, ?, '', 'activo')
        ON CONFLICT (user_id, chat_id) DO UPDATE SET
            nombre = COALESCE(EXCLUDED.nombre, membresias.nombre),
            ultimo_pago = EXCLUDED.ultimo_pago,
            vence = EXCLUDED.vence,
            recordatorios = '',
            estado = 'activo',
            renovaciones = membresias.renovaciones + ?,
            expulsado_en = NULL
    """, (uid, chat_id, nombre, iso(ahora_), iso(vence), 1 if fila else 0))
    return iso(vence)


def registrar(uid, chat_id, nombre, dias, sumar=True):
    return registrar_td(uid, chat_id, nombre, timedelta(days=dias), sumar)
