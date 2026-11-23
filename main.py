"""Bot de Telegram para controlar membresías de canales y grupos."""

import logging
import math
import os
import re
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone

from telegram import BotCommand, BotCommandScopeChat, BotCommandScopeDefault, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import TelegramError
from telegram.ext import Application, CallbackQueryHandler, ChatMemberHandler, CommandHandler, ContextTypes

logging.basicConfig(format="%(asctime)s %(levelname)s: %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("membresias")

TOKEN = os.environ["BOT_TOKEN"]
GROUP_ID = int(os.environ["GROUP_ID"]) if os.environ.get("GROUP_ID", "").strip() else None
ADMIN_IDS = {int(x) for x in os.environ["ADMIN_IDS"].split(",") if x.strip()}
DATABASE_URL = os.environ.get("DATABASE_URL")
DB_PATH = os.environ.get("DB_PATH", "membresias.db")
DIAS_MEMBRESIA = int(os.environ.get("DIAS_MEMBRESIA", "30"))
REMINDER_DAYS = sorted({int(x) for x in os.environ.get("REMINDER_DAYS", "5,2,1").split(",") if x.strip()}, reverse=True)
REVISION_SEGUNDOS = int(os.environ.get("REVISION_SEGUNDOS", "60"))
LOCAL_TZ = timezone(timedelta(hours=float(os.environ.get("TZ_OFFSET", "-5"))))
AUTO_REGISTRO = os.environ.get("AUTO_REGISTRO", "1") == "1"
TELEGRAM_API_PROXY = os.environ.get("TELEGRAM_API_PROXY", "").strip().rstrip("/")
ALFABETO = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
UNIDADES = {"m": 60, "h": 3600, "d": 86400}
VIGENCIA_ENLACE = 600
FMT = "%Y-%m-%dT%H:%M:%S"


def ahora():
    return datetime.now(timezone.utc)


def iso(dt):
    return dt.astimezone(timezone.utc).strftime(FMT)


def parse(valor):
    return datetime.strptime(valor, FMT).replace(tzinfo=timezone.utc)


def fecha_local(valor):
    return parse(valor).astimezone(LOCAL_TZ).strftime("%d/%m/%Y %H:%M")


def fmt_seg(segundos):
    segundos = max(int(segundos), 0)
    dias, resto = divmod(segundos, 86400)
    horas, resto = divmod(resto, 3600)
    minutos = resto // 60
    partes = []
    if dias:
        partes.append(f"{dias}d")
    if horas:
        partes.append(f"{horas}h")
    if minutos or not partes:
        partes.append(f"{minutos}m")
    return " ".join(partes)


def tiempo_restante(valor):
    return fmt_seg((parse(valor) - ahora()).total_seconds())


def run(sql, params=(), fetch=None):
    if DATABASE_URL:
        import psycopg2
        import psycopg2.extras
        conexion = psycopg2.connect(DATABASE_URL)
        try:
            with conexion:
                with conexion.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cursor:
                    cursor.execute(sql.replace("?", "%s"), params)
                    if fetch == "all":
                        return [dict(row) for row in cursor.fetchall()] if cursor.description else []
                    if fetch == "one":
                        row = cursor.fetchone()
                        return dict(row) if row else None
        finally:
            conexion.close()
    else:
        conexion = sqlite3.connect(DB_PATH)
        conexion.row_factory = sqlite3.Row
        try:
            with conexion:
                cursor = conexion.execute(sql, params)
                if fetch == "all":
                    return [dict(row) for row in cursor.fetchall()]
                if fetch == "one":
                    row = cursor.fetchone()
                    return dict(row) if row else None
        finally:
            conexion.close()


def init_db():
    run("""CREATE TABLE IF NOT EXISTS canales (
        chat_id BIGINT PRIMARY KEY, alias TEXT UNIQUE NOT NULL,
        titulo TEXT, activo INTEGER NOT NULL DEFAULT 1)""")
    run("""CREATE TABLE IF NOT EXISTS membresias (
        user_id BIGINT NOT NULL, chat_id BIGINT NOT NULL, nombre TEXT,
        ultimo_pago TEXT, vence TEXT NOT NULL,
        recordatorios TEXT NOT NULL DEFAULT '', estado TEXT NOT NULL DEFAULT 'activo',
        renovaciones INTEGER NOT NULL DEFAULT 0, expulsado_en TEXT,
        PRIMARY KEY (user_id, chat_id))""")
    run("""CREATE TABLE IF NOT EXISTS llaves (
        codigo TEXT PRIMARY KEY, segundos BIGINT NOT NULL, creada TEXT NOT NULL,
        estado TEXT NOT NULL DEFAULT 'disponible', usada_por BIGINT,
        usada_en TEXT, chat_id BIGINT)""")
    if GROUP_ID is not None:
        run("INSERT INTO canales (chat_id, alias, activo) VALUES (?, 'principal', 1) ON CONFLICT (chat_id) DO NOTHING", (GROUP_ID,))
    log.info("Base de datos: %s", "Postgres" if DATABASE_URL else f"SQLite ({DB_PATH})")


SQL_MEMB = "SELECT m.*, c.titulo, c.alias FROM membresias m LEFT JOIN canales c ON c.chat_id = m.chat_id "


def canales_activos():
    return run("SELECT * FROM canales WHERE activo = 1 ORDER BY alias", (), "all")


def registrar_canal(chat_id, titulo, alias=None):
    fila = run("SELECT alias FROM canales WHERE chat_id = ?", (chat_id,), "one")
    if fila:
        run("UPDATE canales SET titulo = ?, activo = 1 WHERE chat_id = ?", (titulo, chat_id))
        return fila["alias"]
    if alias:
        alias = re.sub(r"[^a-z0-9]", "", alias.lower())[:12]
    else:
        base = re.sub(r"[^a-z0-9]", "", (titulo or "canal").lower())[:12] or "canal"
        alias = base
        numero = 1
        while run("SELECT 1 AS x FROM canales WHERE alias = ?", (alias,), "one"):
            numero += 1
            alias = f"{base}{numero}"
    run("INSERT INTO canales (chat_id, alias, titulo, activo) VALUES (?, ?, ?, 1)", (chat_id, alias, titulo))
    return alias


def canal_activo(chat_id):
    return run("SELECT * FROM canales WHERE chat_id = ? AND activo = 1", (chat_id,), "one")


def separar_alias(args):
    alias = None
    resto = []
    for arg in args or []:
        if arg.startswith("#") and len(arg) > 1 and alias is None:
            alias = arg[1:].lower()
        else:
            resto.append(arg)
    return alias, resto


def texto_canales():
    return "\n".join(f"#{c['alias']} - {c['titulo'] or c['chat_id']}" for c in canales_activos()) or "—"


def elegir_canal(alias):
    activos = canales_activos()
    if alias:
        for canal in activos:
            if canal["alias"] == alias:
                return canal, None
        return None, f"No existe el canal #{alias}.\n{texto_canales()}"
    if len(activos) == 1:
        return activos[0], None
    if not activos:
        return None, "No hay canales registrados. Agrega el bot como administrador de un canal."
    return None, "Hay varios canales, indica cuál con #alias:\n" + texto_canales()


def obtener(uid, chat_id):
    return run("SELECT * FROM membresias WHERE user_id = ? AND chat_id = ?", (uid, chat_id), "one")


def registrar_td(uid, chat_id, nombre, delta, sumar=True):
    base = ahora()
    fila = obtener(uid, chat_id)
    if sumar and fila and fila["estado"] == "activo" and parse(fila["vence"]) > base:
        base = parse(fila["vence"])
    vence = base + delta
    run("""INSERT INTO membresias
        (user_id, chat_id, nombre, ultimo_pago, vence, recordatorios, estado)
        VALUES (?, ?, ?, ?, ?, '', 'activo')
        ON CONFLICT (user_id, chat_id) DO UPDATE SET
        nombre = COALESCE(EXCLUDED.nombre, membresias.nombre),
        ultimo_pago = EXCLUDED.ultimo_pago, vence = EXCLUDED.vence,
        recordatorios = '', estado = 'activo',
        renovaciones = membresias.renovaciones + ?, expulsado_en = NULL""",
        (uid, chat_id, nombre, iso(ahora()), iso(vence), 1 if fila else 0))
    return iso(vence)


def registrar(uid, chat_id, nombre, dias, sumar=True):
    return registrar_td(uid, chat_id, nombre, timedelta(days=dias), sumar)


def etiqueta(fila):
    return fila.get("titulo") or fila.get("alias") or str(fila.get("chat_id"))


def solo_admin(funcion):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not update.effective_user or update.effective_user.id not in ADMIN_IDS:
            await update.message.reply_text("No tienes permiso para usar este comando.")
            return
        return await funcion(update, context)
    return wrapper


async def avisar_admins(context, texto):
    for admin in ADMIN_IDS:
        try:
            await context.bot.send_message(admin, texto)
        except TelegramError as exc:
            log.warning("No pude avisar al admin %s: %s", admin, exc)


async def sacar_del_canal(context, chat_id, uid):
    try:
        await context.bot.ban_chat_member(chat_id, uid)
        await context.bot.unban_chat_member(chat_id, uid, only_if_banned=True)
        return True
    except TelegramError as exc:
        log.error("No pude expulsar a %s de %s: %s", uid, chat_id, exc)
        return False


def uid_arg(args):
    return int(args[0]) if args and args[0].isdigit() else None


def parse_duracion(texto):
    coincidencia = re.fullmatch(r"(\d+)([mhd])", texto.strip().lower())
    if not coincidencia or int(coincidencia.group(1)) <= 0:
        return None
    return int(coincidencia.group(1)) * UNIDADES[coincidencia.group(2)]


def nuevo_codigo():
    return "-".join("".join(secrets.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(4)) for _ in range(3))


async def crear_enlace(context, chat_id):
    enlace = await context.bot.create_chat_invite_link(chat_id, member_limit=1, expire_date=ahora() + timedelta(seconds=VIGENCIA_ENLACE))
    return enlace.invite_link


COMANDOS_USUARIO = [
    ("start", "Cómo funciona el bot"), ("tiempo", "Ver cuánto te queda"),
    ("canjear", "Canjear una llave"), ("enlace", "Pedir un enlace"), ("miid", "Ver tu ID"),
]
COMANDOS_ADMIN = COMANDOS_USUARIO + [
    ("ayuda", "Comandos de administrador"), ("canales", "Ver canales"),
    ("addcanal", "Agregar canal"), ("quitarcanal", "Desactivar canal"),
    ("llave", "Crear llave"), ("llaves", "Ver llaves"), ("revocar", "Revocar llave"),
    ("pago", "Renovar por los días predeterminados"), ("agregar", "Agregar días"),
    ("estado", "Estado de usuario"), ("lista", "Miembros activos"),
    ("usuarios", "Usuarios activos y vencidos"), ("vencer", "Hacer vencer ahora"),
    ("quitar", "Eliminar y expulsar"),
]


async def post_init(app):
    try:
        await app.bot.set_my_commands([BotCommand(c, d[:256]) for c, d in COMANDOS_USUARIO], scope=BotCommandScopeDefault())
        for admin in ADMIN_IDS:
            await app.bot.set_my_commands([BotCommand(c, d[:256]) for c, d in COMANDOS_ADMIN], scope=BotCommandScopeChat(admin))
    except TelegramError as exc:
        log.warning("No pude publicar el menú: %s", exc)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Hola. Usa /canjear TU-LLAVE para activarte, /tiempo para consultar tu membresía y /enlace para pedir acceso.")


async def miid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"Tu ID de Telegram es: {update.effective_user.id}")


@solo_admin
async def ayuda(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Comandos de administrador:\n" + "\n".join(f"/{c} - {d}" for c, d in COMANDOS_ADMIN))


@solo_admin
async def canales(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Canales activos:\n" + texto_canales())


@solo_admin
async def addcanal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args or not re.fullmatch(r"-?\d+", context.args[0]):
        await update.message.reply_text("Uso: /addcanal CHAT_ID [alias]")
        return
    chat_id = int(context.args[0])
    alias = context.args[1] if len(context.args) > 1 else None
    if alias:
        usado = run("SELECT 1 AS x FROM canales WHERE alias = ? AND chat_id <> ?", (alias.lower(), chat_id), "one")
        if usado:
            await update.message.reply_text("Ese alias ya está en uso.")
            return
    try:
        chat = await context.bot.get_chat(chat_id)
    except TelegramError as exc:
        await update.message.reply_text(f"No pude acceder al canal: {exc}")
        return
    alias = registrar_canal(chat_id, chat.title, alias)
    await update.message.reply_text(f"Canal registrado: #{alias} - {chat.title}")


@solo_admin
async def quitarcanal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    alias, _ = separar_alias(context.args)
    if not alias:
        await update.message.reply_text("Uso: /quitarcanal #alias")
        return
    canal, error = elegir_canal(alias)
    if error:
        await update.message.reply_text(error)
        return
    run("UPDATE canales SET activo = 0 WHERE chat_id = ?", (canal["chat_id"],))
    await update.message.reply_text(f"#{alias} desactivado.")


async def canal_cambio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    miembro = update.my_chat_member
    if miembro is None or miembro.chat.type == "private":
        return
    estado = miembro.new_chat_member.status
    if estado == "administrator":
        alias = registrar_canal(miembro.chat.id, miembro.chat.title)
        await avisar_admins(context, f"Canal registrado: #{alias} - {miembro.chat.title} ({miembro.chat.id}).")
    elif estado in ("left", "kicked"):
        run("UPDATE canales SET activo = 0 WHERE chat_id = ?", (miembro.chat.id,))
        await avisar_admins(context, f"El bot salió de {miembro.chat.title}. Canal desactivado.")


@solo_admin
async def agregar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    alias, args = separar_alias(context.args)
    if len(args) < 2 or not args[0].isdigit() or not args[1].isdigit():
        await update.message.reply_text("Uso: /agregar ID DIAS [Nombre] [#alias]")
        return
    canal, error = elegir_canal(alias)
    if error:
        await update.message.reply_text(error)
        return
    uid, dias = int(args[0]), int(args[1])
    nombre = " ".join(args[2:]) or None
    vence = registrar(uid, canal["chat_id"], nombre, dias)
    await update.message.reply_text(f"Membresía de {nombre or uid} activa hasta {fecha_local(vence)} en #{canal['alias']}.")


@solo_admin
async def pago(update: Update, context: ContextTypes.DEFAULT_TYPE):
    alias, args = separar_alias(context.args)
    uid = uid_arg(args)
    if uid is None:
        await update.message.reply_text("Uso: /pago ID [Nombre] [#alias]")
        return
    canal, error = elegir_canal(alias)
    if error:
        await update.message.reply_text(error)
        return
    vence = registrar(uid, canal["chat_id"], " ".join(args[1:]) or None, DIAS_MEMBRESIA)
    await update.message.reply_text(f"Pago registrado en #{canal['alias']}. Activa hasta {fecha_local(vence)}.")


@solo_admin
async def estado(update: Update, context: ContextTypes.DEFAULT_TYPE):
    alias, args = separar_alias(context.args)
    uid = uid_arg(args)
    if uid is None:
        await update.message.reply_text("Uso: /estado ID [#alias]")
        return
    filas = run(SQL_MEMB + "WHERE m.user_id = ? ORDER BY c.alias", (uid,), "all")
    if alias:
        filas = [fila for fila in filas if fila["alias"] == alias]
    if not filas:
        await update.message.reply_text("Ese usuario no está registrado.")
        return
    lineas = []
    for fila in filas:
        if fila["estado"] != "activo" or parse(fila["vence"]) <= ahora():
            lineas.append(f"#{fila['alias']}: vencido.")
        else:
            lineas.append(f"#{fila['alias']}: quedan {tiempo_restante(fila['vence'])}, vence {fecha_local(fila['vence'])}.")
    await update.message.reply_text(f"{filas[0]['nombre'] or uid}\n" + "\n".join(lineas))


async def tiempo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    filas = run(SQL_MEMB + "WHERE m.user_id = ? ORDER BY c.alias", (update.effective_user.id,), "all")
    if not filas:
        await update.message.reply_text("No tienes una membresía registrada. Usa /canjear TU-LLAVE.")
        return
    lineas = []
    for fila in filas:
        if fila["estado"] != "activo" or parse(fila["vence"]) <= ahora():
            lineas.append(f"{etiqueta(fila)}: venció.")
        else:
            lineas.append(f"{etiqueta(fila)}: te quedan {tiempo_restante(fila['vence'])}, vence {fecha_local(fila['vence'])}.")
    await update.message.reply_text("\n".join(lineas))


async def enviar_bloques(update, lineas):
    bloque = ""
    for linea in lineas:
        if len(bloque) + len(linea) > 3500:
            await update.message.reply_text(bloque)
            bloque = ""
        bloque += linea + "\n"
    if bloque:
        await update.message.reply_text(bloque)


@solo_admin
async def lista(update: Update, context: ContextTypes.DEFAULT_TYPE):
    alias, _ = separar_alias(context.args)
    filas = run(SQL_MEMB + "WHERE m.estado = 'activo' ORDER BY c.alias, m.vence", (), "all")
    if alias:
        filas = [fila for fila in filas if fila["alias"] == alias]
    if not filas:
        await update.message.reply_text("No hay miembros activos.")
        return
    await enviar_bloques(update, [f"Activos: {len(filas)}"] + [f"#{f['alias']} {f['nombre'] or '-'} ({f['user_id']}): {tiempo_restante(f['vence'])}" for f in filas])


@solo_admin
async def usuarios(update: Update, context: ContextTypes.DEFAULT_TYPE):
    alias, _ = separar_alias(context.args)
    filas = run(SQL_MEMB + "ORDER BY c.alias, m.vence", (), "all")
    if alias:
        filas = [fila for fila in filas if fila["alias"] == alias]
    activos = [fila for fila in filas if fila["estado"] == "activo"]
    vencidos = [fila for fila in filas if fila["estado"] != "activo"]
    lineas = [f"ACTIVOS ({len(activos)})"]
    lineas += [f"#{f['alias']} {f['nombre'] or '-'} ({f['user_id']}): {tiempo_restante(f['vence'])}, renovaciones {f['renovaciones']}" for f in activos]
    lineas += ["", f"VENCIDOS ({len(vencidos)})"]
    lineas += [f"#{f['alias']} {f['nombre'] or '-'} ({f['user_id']}): venció {fecha_local(f['vence'])}" for f in vencidos]
    await enviar_bloques(update, lineas)


@solo_admin
async def vencer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    alias, args = separar_alias(context.args)
    uid = uid_arg(args)
    if uid is None:
        await update.message.reply_text("Uso: /vencer ID [#alias]")
        return
    canal, error = elegir_canal(alias)
    if error:
        await update.message.reply_text(error)
        return
    if not obtener(uid, canal["chat_id"]):
        await update.message.reply_text("Ese usuario no está registrado en ese canal.")
        return
    run("UPDATE membresias SET vence = ?, estado = 'activo' WHERE user_id = ? AND chat_id = ?", (iso(ahora() - timedelta(minutes=1)), uid, canal["chat_id"]))
    await update.message.reply_text(f"Marcado como vencido en #{canal['alias']}.")


@solo_admin
async def quitar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    alias, args = separar_alias(context.args)
    uid = uid_arg(args)
    if uid is None:
        await update.message.reply_text("Uso: /quitar ID [#alias]")
        return
    canal, error = elegir_canal(alias)
    if error:
        await update.message.reply_text(error)
        return
    run("DELETE FROM membresias WHERE user_id = ? AND chat_id = ?", (uid, canal["chat_id"]))
    ok = await sacar_del_canal(context, canal["chat_id"], uid)
    await update.message.reply_text(f"{uid} eliminado y expulsado de #{canal['alias']}." if ok else f"{uid} eliminado de la base, pero no pude expulsarlo de #{canal['alias']}.")


def crear_llave(segundos, canal):
    codigo = "-".join("".join(secrets.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(4)) for _ in range(3))
    run("INSERT INTO llaves (codigo, segundos, creada, estado, chat_id) VALUES (?, ?, ?, 'disponible', ?)", (codigo, segundos, iso(ahora()), canal["chat_id"]))
    return f"Llave creada para #{canal['alias']} ({canal['titulo'] or canal['chat_id']}):\n<code>{codigo}</code>\nDuración: {fmt_seg(segundos)}. Solo abre ese canal."


@solo_admin
async def llave(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        await update.message.reply_text("Crea las llaves por privado con el bot.")
        return
    alias, args = separar_alias(context.args)
    segundos = parse_duracion(args[0]) if args else None
    if not segundos:
        await update.message.reply_text("Uso: /llave 30d [#alias]\n(m = minutos, h = horas, d = días)")
        return
    if not alias and len(canales_activos()) > 1:
        teclado = [[InlineKeyboardButton(c["titulo"] or c["alias"], callback_data=f"llave:{segundos}:{c['chat_id']}")] for c in canales_activos()]
        await update.message.reply_text(f"¿Para qué canal es la llave de {fmt_seg(segundos)}?", reply_markup=InlineKeyboardMarkup(teclado))
        return
    canal, error = elegir_canal(alias)
    if error:
        await update.message.reply_text(error)
        return
    await update.message.reply_html(crear_llave(segundos, canal))


async def llave_boton(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query.from_user.id not in ADMIN_IDS:
        await query.answer("Sin permiso.", show_alert=True)
        return
    _, segundos, chat_id = query.data.split(":")
    canal = canal_activo(int(chat_id))
    await query.answer()
    if not canal:
        await query.edit_message_text("Ese canal ya no está activo.")
        return
    await query.edit_message_text(crear_llave(int(segundos), canal), parse_mode="HTML")


@solo_admin
async def llaves(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        return
    canales = {c["chat_id"]: c["alias"] for c in run("SELECT * FROM canales", (), "all")}
    disponibles = run("SELECT * FROM llaves WHERE estado = 'disponible' ORDER BY creada", (), "all")
    usadas = run("SELECT * FROM llaves WHERE estado = 'usada' ORDER BY usada_en DESC LIMIT 10", (), "all")
    texto = f"<b>Disponibles ({len(disponibles)})</b>\n"
    texto += "\n".join(f"<code>{f['codigo']}</code> · {fmt_seg(f['segundos'])} · #{canales.get(f['chat_id'], '?')}" for f in disponibles[:30]) or "—"
    texto += "\n\n<b>Usadas</b>\n"
    texto += "\n".join(f"<code>{f['codigo']}</code> · #{canales.get(f['chat_id'], '?')} · {f['usada_por']}" for f in usadas) or "—"
    await update.message.reply_html(texto)


@solo_admin
async def revocar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Uso: /revocar CODIGO")
        return
    codigo = context.args[0].strip().upper()
    fila = run("SELECT codigo FROM llaves WHERE codigo = ? AND estado = 'disponible'", (codigo,), "one")
    if fila:
        run("UPDATE llaves SET estado = 'revocada' WHERE codigo = ?", (codigo,))
        await update.message.reply_text("Llave revocada.")
    else:
        await update.message.reply_text("No existe o ya fue usada.")


async def canjear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        await update.message.reply_text("Escríbeme por privado para canjear tu llave.")
        return
    if not context.args:
        await update.message.reply_text("Uso: /canjear TU-LLAVE")
        return
    user = update.effective_user
    codigo = context.args[0].strip().upper()
    fila = run("SELECT segundos, chat_id FROM llaves WHERE codigo = ? AND estado = 'disponible'", (codigo,), "one")
    if not fila:
        await update.message.reply_text("Llave inválida o ya utilizada.")
        return
    run("UPDATE llaves SET estado = 'usada', usada_por = ?, usada_en = ? WHERE codigo = ?", (user.id, iso(ahora()), codigo))

    def devolver_llave():
        run("UPDATE llaves SET estado = 'disponible', usada_por = NULL, usada_en = NULL WHERE codigo = ?", (codigo,))

    canal = canal_activo(fila["chat_id"] or GROUP_ID)
    if not canal:
        devolver_llave()
        await update.message.reply_text("El canal de esta llave no está disponible.")
        return
    try:
        vence = registrar_td(user.id, canal["chat_id"], user.full_name, timedelta(seconds=int(fila["segundos"])))
    except Exception:
        devolver_llave()
        await update.message.reply_text("Hubo un error. Tu llave sigue disponible, intenta de nuevo.")
        return
    await avisar_admins(context, f"Llave {codigo} canjeada por {user.full_name} ({user.id}) en #{canal['alias']}. Vence {fecha_local(vence)}.")
    texto = f"Llave activada para {canal['titulo'] or canal['alias']} ({fmt_seg(int(fila['segundos']))}). Tu membresía vence el {fecha_local(vence)}."
    try:
        miembro = await context.bot.get_chat_member(canal["chat_id"], user.id)
        ya_dentro = miembro.status in ("member", "administrator", "creator")
    except TelegramError:
        ya_dentro = False
    if ya_dentro:
        await update.message.reply_text(texto + "\nYa estás dentro del canal.")
        return
    try:
        url = await crear_enlace(context, canal["chat_id"])
    except TelegramError:
        await update.message.reply_text(texto + "\nNo pude generar el enlace. Usa /enlace.")
        return
    await update.message.reply_text(texto + f"\n\nEntra aquí (una sola vez, vence en {VIGENCIA_ENLACE // 60} min):\n{url}")


def membresias_vigentes(uid):
    filas = run(SQL_MEMB + "WHERE m.user_id = ? AND m.estado = 'activo' AND c.activo = 1 ORDER BY c.alias", (uid,), "all")
    return [fila for fila in filas if parse(fila["vence"]) > ahora()]


async def enlace(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        return
    vigentes = membresias_vigentes(update.effective_user.id)
    if not vigentes:
        await update.message.reply_text("No tienes una membresía activa.")
        return
    if len(vigentes) > 1:
        teclado = [[InlineKeyboardButton(etiqueta(fila), callback_data=f"enl:{fila['chat_id']}")] for fila in vigentes]
        await update.message.reply_text("¿De qué canal quieres el enlace?", reply_markup=InlineKeyboardMarkup(teclado))
        return
    await entregar_enlace(context, update.message.reply_text, vigentes[0])


async def entregar_enlace(context, responder, fila):
    try:
        url = await crear_enlace(context, fila["chat_id"])
    except TelegramError:
        await responder("No pude generar el enlace, intenta de nuevo.")
        return
    await responder(f"{etiqueta(fila)}: te quedan {tiempo_restante(fila['vence'])}.\nEnlace (una sola vez):\n{url}")


async def enlace_boton(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = int(query.data.split(":")[1])
    for fila in membresias_vigentes(query.from_user.id):
        if fila["chat_id"] == chat_id:
            await entregar_enlace(context, query.message.reply_text, fila)
            return
    await query.message.reply_text("No tienes una membresía activa en ese canal.")


async def nuevo_miembro(update: Update, context: ContextTypes.DEFAULT_TYPE):
    miembro = update.chat_member
    if miembro is None:
        return
    canal = canal_activo(miembro.chat.id)
    if not canal:
        return
    before = miembro.old_chat_member.status
    after = miembro.new_chat_member.status
    user = miembro.new_chat_member.user
    if user.is_bot or user.id in ADMIN_IDS:
        return
    if not (before in ("left", "kicked") and after == "member"):
        return
    fila = obtener(user.id, miembro.chat.id)
    if fila and fila["estado"] == "expulsado":
        await sacar_del_canal(context, miembro.chat.id, user.id)
        try:
            await context.bot.send_message(user.id, "Tu membresía venció. Contacta al administrador para renovar.")
        except TelegramError:
            pass
        return
    if fila and fila["estado"] == "activo" and parse(fila["vence"]) > ahora():
        return
    if not AUTO_REGISTRO:
        await avisar_admins(context, f"Entró sin membresía registrada a #{canal['alias']}: {user.full_name} ({user.id}).")
        return
    vence = registrar(user.id, miembro.chat.id, user.full_name, DIAS_MEMBRESIA, sumar=False)
    await avisar_admins(context, f"Nuevo miembro registrado en #{canal['alias']}: {user.full_name} ({user.id}). Vence {fecha_local(vence)}.")


async def revisar(context: ContextTypes.DEFAULT_TYPE):
    filas = run(SQL_MEMB.replace("LEFT JOIN", "JOIN") + "WHERE m.estado = 'activo' AND c.activo = 1", (), "all")
    instante = ahora()
    for fila in filas:
        uid = fila["user_id"]
        restante = (parse(fila["vence"]) - instante).total_seconds()
        canal = etiqueta(fila)
        if restante <= 0:
            if await sacar_del_canal(context, fila["chat_id"], uid):
                run("UPDATE membresias SET estado = 'expulsado', expulsado_en = ? WHERE user_id = ? AND chat_id = ?", (iso(instante), uid, fila["chat_id"]))
                try:
                    await context.bot.send_message(uid, f"Tu membresía de {canal} venció y fuiste retirado.")
                except TelegramError:
                    pass
                await avisar_admins(context, f"Expulsado por vencimiento de {canal}: {fila['nombre'] or uid} ({uid}).")
            continue
        enviados = {x for x in fila["recordatorios"].split(",") if x}
        alcanzados = [dias for dias in REMINDER_DAYS if restante <= dias * 86400 and str(dias) not in enviados]
        if not alcanzados:
            continue
        enviados.update(str(dias) for dias in alcanzados)
        run("UPDATE membresias SET recordatorios = ? WHERE user_id = ? AND chat_id = ?", (",".join(sorted(enviados)), uid, fila["chat_id"]))
        try:
            await context.bot.send_message(uid, f"Tu membresía de {canal} vence en {tiempo_restante(fila['vence'])} ({fecha_local(fila['vence'])}).")
        except TelegramError as exc:
            log.warning("No pude enviar recordatorio a %s: %s", uid, exc)


async def error_handler(update, context: ContextTypes.DEFAULT_TYPE):
    log.error("Error: %s", context.error)


def main():
    init_db()
    builder = Application.builder().token(TOKEN).post_init(post_init)
    if TELEGRAM_API_PROXY:
        builder = builder.base_url(TELEGRAM_API_PROXY + "/bot")
    app = builder.build()
    for nombre, funcion in [
        ("start", start), ("miid", miid), ("tiempo", tiempo), ("ayuda", ayuda),
        ("canales", canales), ("addcanal", addcanal), ("quitarcanal", quitarcanal),
        ("agregar", agregar), ("pago", pago), ("estado", estado), ("lista", lista),
        ("usuarios", usuarios), ("vencer", vencer), ("quitar", quitar),
        ("llave", llave), ("llaves", llaves), ("revocar", revocar),
        ("canjear", canjear), ("enlace", enlace),
    ]:
        app.add_handler(CommandHandler(nombre, funcion))
    app.add_handler(CallbackQueryHandler(llave_boton, pattern=r"^llave:"))
    app.add_handler(CallbackQueryHandler(enlace_boton, pattern=r"^enl:"))
    app.add_handler(ChatMemberHandler(nuevo_miembro, ChatMemberHandler.CHAT_MEMBER))
    app.add_handler(ChatMemberHandler(canal_cambio, ChatMemberHandler.MY_CHAT_MEMBER))
    app.add_error_handler(error_handler)
    app.job_queue.run_repeating(revisar, interval=REVISION_SEGUNDOS, first=10)
    log.info("Bot en marcha...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
