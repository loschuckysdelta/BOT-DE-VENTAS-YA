import asyncio
import getpass
import json
import logging
import os
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.request
from collections import deque
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path

try:
    from telethon import TelegramClient, events, utils
    from telethon.tl.functions.channels import CreateChannelRequest
    from telethon.tl.functions import channels as canales_tl, messages as mensajes_tl
    from telethon.errors import FloodWaitError
except ImportError as exc:
    print(f"Error al importar Telethon: {exc}")
    print("Instala las dependencias con: python -m pip install -r requirements.txt")
    raise SystemExit(1) from exc

CrearTemaRequest = getattr(mensajes_tl, "CreateForumTopicRequest", None)
ObtenerTemasRequest = getattr(mensajes_tl, "GetForumTopicsRequest", None)
TEMAS_EN_MESSAGES = CrearTemaRequest is not None and ObtenerTemasRequest is not None
if not TEMAS_EN_MESSAGES:
    CrearTemaRequest = getattr(canales_tl, "CreateForumTopicRequest", None)
    ObtenerTemasRequest = getattr(canales_tl, "GetForumTopicsRequest", None)
if CrearTemaRequest is None or ObtenerTemasRequest is None:
    raise RuntimeError(
        "Tu versión de Telethon no admite los temas de Telegram. "
        "Actualiza con: python -m pip install --upgrade telethon"
    )

BASE = Path(__file__).resolve().parent
CONFIG_FILE = BASE / "config.json"
CONTROL_FILE = BASE / "control_chats.json"
ESTADO_FILE = BASE / "estado_bot_banca.json"
SESION = BASE / "loschuckys_sesion"
QR_FILE = BASE / "qr_pago.png"
IMAGEN_ASESOR = BASE / "asesor.png"
VENTAS_FILE = BASE / "historial_ventas.json"
GRUPO_FILE = BASE / "grupo_ventas.json"

TEMAS_VENTAS = {
    "pendientes": "🟡 Pendientes",
    "pagos": "💳 Pagos y comprobantes",
    "activaciones": "✅ Activaciones",
    "entregas": "🎟️ Entregas de vouchers y créditos",
    "historial": "📒 Historial de ventas",
    "cancelados": "🔴 Cancelados",
}

ESTADOS_SEMAFORO = {
    "pendiente": "🟡 PEDIDO PENDIENTE",
    "revision": "🟠 COMPROBANTE EN REVISIÓN",
    "pagado": "🟢 PAGO CONFIRMADO",
    "entregado": "🟢 PRODUCTO ENTREGADO",
    "cancelado": "🔴 PEDIDO CANCELADO",
}

DIAS = 30
PRECIO = 5
PRODUCTOS_COMPRA = {
    "banca": {"nombre": "BOT DE BANCA", "duracion": "30 días", "precio": PRECIO},
    "vouchers": {"nombre": "VOUCHERS", "duracion": "30 días", "precio": 5},
    "creditos": {"nombre": "CRÉDITOS", "duracion": None, "precio": None},
    "otro": {"nombre": "PRODUCTO POR IDENTIFICAR", "duracion": None, "precio": None},
}
ESTADO_VIGENCIA = 7 * 24 * 60 * 60
ESPERA_COMPROBANTE_SEGUNDOS = 2 * 60
REPETIR_PAGO_SEGUNDOS = 3
REPETIR_RESPUESTA_SEGUNDOS = 8
REPETIR_QR_SEGUNDOS = 3
INTERVALO_REVISION_SEGUNDOS = 5
COOLDOWN_ACTIVACION = 90
RECONEXION_SEGUNDOS = 10

CONFIG_POR_DEFECTO = {
    "api_id": 39958326,
    "api_hash": "9c8b9c90d362cda9dd87cd3605f05cbf",
    "qr_url": "https://i.postimg.cc/sfccGwZ0/image.png",
    "numero_cuenta": "09400141002543052450",
    "api_activar_url": "https://activaciones.vercel.app/api/add_days",
    "api_desactivar_url": "",
    "bot_registro_username": "@YapeXCenter_bot",
    "token_activacion": "",
    "timeout_api_segundos": 15,
    "grupo_ventas_id": -1004489972169,
    "crear_grupo_ventas_si_falta": False,
    "nombre_grupo_ventas": "LOS CHUCKYS | CONTROL DE VENTAS",
}

logger = logging.getLogger("loschuckys.banca")
logger.setLevel(logging.INFO)
if not logger.handlers:
    fmt = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")
    consola = logging.StreamHandler(sys.stdout)
    consola.setFormatter(fmt)
    archivo = RotatingFileHandler(
        BASE / "bot_banca.log", maxBytes=1_500_000, backupCount=3, encoding="utf-8"
    )
    archivo.setFormatter(fmt)
    logger.addHandler(consola)
    logger.addHandler(archivo)


def ahora_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def escribir_json_atomico(ruta, datos):
    temporal = ruta.with_name(ruta.name + ".tmp")
    try:
        with temporal.open("w", encoding="utf-8") as f:
            json.dump(datos, f, ensure_ascii=False, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporal, ruta)
        if os.name != "nt":
            os.chmod(ruta, 0o600)
    finally:
        if temporal.exists():
            temporal.unlink()


def leer_json(ruta, *, estricto=False):
    if not ruta.exists():
        return {}
    try:
        with ruta.open(encoding="utf-8") as f:
            datos = json.load(f)
        if not isinstance(datos, dict):
            raise ValueError("La raíz debe ser un objeto JSON")
        return datos
    except (OSError, ValueError) as exc:
        if estricto:
            raise RuntimeError(f"No puedo leer {ruta.name}: {exc}") from exc
        logger.warning("No se pudo cargar %s: %s", ruta.name, exc)
        return {}


def leer_configuracion(*, permitir_preguntar=False):
    config = dict(CONFIG_POR_DEFECTO)
    datos = leer_json(CONFIG_FILE, estricto=True)
    config.update({k: v for k, v in datos.items() if k in config})
    if os.getenv("TELEGRAM_API_ID"):
        config["api_id"] = os.environ["TELEGRAM_API_ID"]
    if os.getenv("TELEGRAM_API_HASH"):
        config["api_hash"] = os.environ["TELEGRAM_API_HASH"]
    if os.getenv("ACTIVACIONES_TOKEN"):
        config["token_activacion"] = os.environ["ACTIVACIONES_TOKEN"]
    if os.getenv("BOT_REGISTRO_USERNAME"):
        config["bot_registro_username"] = os.environ["BOT_REGISTRO_USERNAME"]

    try:
        config["api_id"] = int(config["api_id"])
    except (ValueError, TypeError) as exc:
        raise RuntimeError("api_id inválido en config.json") from exc
    config["api_hash"] = str(config.get("api_hash") or "").strip()
    return config


def normalizar(texto):
    texto = unicodedata.normalize("NFD", (texto or "").lower())
    texto = "".join(c for c in texto if unicodedata.category(c) != "Mn")
    return re.sub(r"\s+", " ", texto).strip()


def extraer_pedido_creditos(texto):
    t = normalizar(texto)
    if not re.search(r"\b(quiero|comprar|adquirir|deseo|solicito|solicitar|oferta)\b", t):
        return None
    cantidad = re.search(r"\b(\d{1,7})\s*creditos?\b", t)
    if not cantidad:
        cantidad = re.search(r"\bcreditos?\s+(\d{1,7})\b", t)
    if not cantidad:
        return None
    unidades = int(cantidad.group(1))
    if unidades < 1:
        return None
    importe = None
    precio = re.search(r"\bs\s*/\s*(\d{1,7}(?:[.,]\d{1,2})?)\b", t)
    if not precio:
        precio = re.search(r"\b(?:por|a)\s+(\d{1,7}(?:[.,]\d{1,2})?)\s*soles?\b", t)
    if precio:
        from decimal import Decimal, InvalidOperation
        try:
            valor = Decimal(precio.group(1).replace(",", "."))
            if valor > 0:
                importe = f"{valor:.2f}"
                if importe.endswith(".00"):
                    importe = importe[:-3]
        except InvalidOperation:
            pass
    return {"cantidad": unidades, "importe": importe}


def detectar_pedido_especial(texto):
    pedido_creditos = extraer_pedido_creditos(texto)
    if pedido_creditos is not None:
        return "creditos", pedido_creditos
    t = normalizar(texto)
    intencion = bool(re.search(r"\b(quiero|comprar|adquirir|deseo|contratar|solicito|oferta)\b", t))
    if intencion and re.search(r"\bvouchers?\b", t) and re.search(r"\b30\s*dias\b", t):
        return "vouchers", None
    return None


def quiere_comprar(texto):
    t = normalizar(texto)
    if t == "1":
        return True
    producto = bool(re.search(r"\b(bot\s+(?:de\s+)?(?:banca|bancario|yape)|licencia|vouchers?)\b", t))
    intencion = bool(re.search(r"\b(quiero|comprar|adquirir|necesito|deseo|contratar|pagar|pedido|activar)\b", t))
    return producto and intencion


def quiere_menu(texto):
    t = normalizar(texto)
    return bool(
        re.search(r"(^|\s)(/bot|/banca|/start|plan|planes|precio|precios|tarifa|tarifas|menu)(\s|$)", t)
        or any(p in t for p in ("cuanto cuesta", "que precio", "cuanto vale", "cuanto sale"))
    )


def quiere_precio(texto):
    t = normalizar(texto)
    return bool(re.search(r"\b(precio|precios|cuesta|vale|coste|costo|costaria|tarifa|tarifas)\b", t)
                or "cuanto sale" in t)


ORDEN_ACTIVAR = re.compile(
    r"^(?:/(?:activate|activar)|activate|activar|activa|activate|activate licencia|"
    r"activate la licencia|activa licencia|activa la licencia|activar licencia|"
    r"activar la licencia|activale|activale licencia|activate ya|activa ya|"
    r"activa 30 dias|activar 30 dias)"
    r"(?:\s+(?:30|30 dias|por 30 dias))?[.!]?$"
)


def es_orden_activar(texto):
    return bool(ORDEN_ACTIVAR.fullmatch(normalizar(texto)))


def parece_orden_activar(texto):
    return bool(re.match(r"^(?:/(?:activate|activar)\b|(?:activar|activa|activate|activale)\b)",
                         normalizar(texto)))


ORDEN_DESACTIVAR = re.compile(
    r"^(?:/(?:desactivar|desactivate|quitar|revocar)|desactivar|desactiva|"
    r"desactivate|desactivale|quitar|revocar)"
    r"(?:\s+(?:licencia|la licencia|30|30 dias|30 dias de licencia))?[.!]?$"
)


def es_orden_desactivar(texto):
    return bool(ORDEN_DESACTIVAR.fullmatch(normalizar(texto)))


def parece_orden_desactivar(texto):
    return bool(re.match(r"^(?:/(?:desactivar|desactivate|quitar|revocar)\b|"
                         r"(?:desactivar|desactiva|desactivate|desactivale|quitar|revocar)\b)",
                         normalizar(texto)))


def es_imagen(event):
    mensaje = event.message
    if getattr(mensaje, "photo", None):
        return True
    archivo = getattr(mensaje, "file", None)
    return bool(archivo and (getattr(archivo, "mime_type", "") or "").startswith("image/"))


class ApiRechazo(Exception):
    pass


class ApiNoRegistrado(ApiRechazo):
    pass


class ApiIncierta(Exception):
    pass


def es_cliente_no_registrado(respuesta):
    if isinstance(respuesta, dict):
        indicadores = ("registered", "registrado", "isRegistered", "is_registered",
                       "userExists", "user_exists", "accountExists", "account_exists")
        if any(respuesta.get(k) is False for k in indicadores):
            return True
        campos = ("error", "message", "mensaje", "detail", "details",
                  "description", "code", "status", "reason", "data", "user", "usuario", "account", "result")
        return any(es_cliente_no_registrado(respuesta[k]) for k in campos
                   if k in respuesta and respuesta[k] is not None)
    if isinstance(respuesta, (tuple, list)):
        return any(es_cliente_no_registrado(item) for item in respuesta[:8])
    if not isinstance(respuesta, str):
        return False
    texto = normalizar(respuesta).replace("_", " ").replace("-", " ")
    if not texto or texto.lstrip().startswith("<"):
        return False
    frases = (
        r"\b(?:usuario|user|cliente|client|cuenta|account)\b.{0,75}\b(?:no existe|no encontrado|no encontrada|no registrado|no registrada|no esta registrado|no esta registrada|no se encuentra|not found|not registered|does not exist|inexistente|sin registrar)\b",
        r"\b(?:no existe|no se encontro|no encontramos|no se encuentra|no esta registrado|no user found|sin registro|not found)\b.{0,45}\b(?:usuario|user|cuenta|account|cliente|client|telegram)\b",
        r"\b(?:telegram\s*(?:id)?|telegramid)\b.{0,55}\b(?:no registrado|no esta registrado|no encontrado|no se encuentra|not found|not registered|no existe)\b",
        r"\b(?:user not found|user not registered|account not found|usuario no registrado|usuario no encontrado|registro requerido|registration required|register first|registrate primero|debes registrarte)\b",
    )
    return any(re.search(patron, texto) for patron in frases)


def obtener_usuario_bot_registro(config):
    dato = str(config.get("bot_registro_username") or "").strip()
    dato = re.sub(r"^(?:https?://)?(?:t\.me|telegram\.me)/", "", dato, flags=re.I)
    dato = dato.strip("@/ ").split("?", 1)[0]
    return dato if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{4,31}", dato) else ""


def mensaje_registro_pendiente(config, uid):
    usuario_bot = obtener_usuario_bot_registro(config)
    instruccion = (f"🤖 Regístrate en: https://t.me/{usuario_bot}\n"
                   if usuario_bot else
                   "🤖 Pide al asesor el enlace del bot de registro.\n")
    return (
        "❌ LICENCIA NO ACTIVADA\n\n"
        "🔎 Tu cuenta de Telegram no aparece registrada en el sistema de licencias.\n"
        f"🆔 Tu ID de Telegram: {uid}\n\n"
        "📝 Para activar tu licencia primero necesitas crear tu cuenta.\n"
        f"{instruccion}\n"
        "✅ Cuando termines de registrarte, avisa al asesor para que vuelva a intentar la activación.\n\n"
        "⚠️ No se agregaron días a tu licencia."
    )


def agregar_dias_api(config, uid):
    payload = json.dumps({"telegramId": str(uid), "amount": DIAS}).encode("utf-8")
    cabeceras = {"Content-Type": "application/json", "Accept": "application/json"}
    if config.get("token_activacion"):
        cabeceras["Authorization"] = "Bearer " + str(config["token_activacion"])
    solicitud = urllib.request.Request(
        config["api_activar_url"], data=payload, headers=cabeceras, method="POST"
    )
    try:
        with urllib.request.urlopen(solicitud, timeout=float(config["timeout_api_segundos"])) as res:
            tipo = res.headers.get("Content-Type", "").lower()
            cuerpo = res.read(32768).decode("utf-8", errors="replace").strip()
    except urllib.error.HTTPError as exc:
        detalle = exc.read(4096).decode("utf-8", errors="replace").strip()
        try:
            contenido_error = json.loads(detalle)
        except ValueError:
            contenido_error = detalle
        if es_cliente_no_registrado(contenido_error):
            raise ApiNoRegistrado(f"HTTP {exc.code}: {detalle[:350]}") from exc
        if 400 <= exc.code < 500:
            raise ApiRechazo(f"HTTP {exc.code}: {detalle[:350]}") from exc
        raise ApiIncierta(f"HTTP {exc.code}: {detalle[:350]}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ApiIncierta(str(exc)) from exc

    if "text/html" in tipo or cuerpo.lower().startswith("<!doctype html"):
        raise ApiIncierta("El endpoint devolvió HTML en lugar de una respuesta de API")
    if not cuerpo:
        raise ApiIncierta("La API no devolvió confirmación verificable")
    try:
        respuesta = json.loads(cuerpo)
    except ValueError:
        if cuerpo.lower() in ("ok", "success", "true"):
            return True
        if es_cliente_no_registrado(cuerpo):
            raise ApiNoRegistrado(cuerpo[:350])
        raise ApiIncierta("Respuesta no reconocida de la API")
    if respuesta is True:
        return True
    if respuesta is False:
        raise ApiRechazo("La API devolvió false")
    if not isinstance(respuesta, dict):
        raise ApiIncierta("La API no devolvió un resultado válido")
    if es_cliente_no_registrado(respuesta):
        raise ApiNoRegistrado(str(respuesta.get("error") or respuesta.get("message")
                                  or "El usuario de Telegram no está registrado")[:350])
    if respuesta.get("success") is False or respuesta.get("ok") is False or respuesta.get("error"):
        raise ApiRechazo(str(respuesta.get("error") or respuesta.get("message") or "La API rechazó la operación")[:350])
    estado = str(respuesta.get("status", "")).lower()
    if (respuesta.get("success") is True or respuesta.get("ok") is True
            or estado in ("ok", "success", "completed", "activated")):
        return True
    raise ApiIncierta("La API respondió sin success:true, ok:true o status de éxito")


def quitar_dias_api(config, uid):
    from urllib.parse import urlparse
    destino = str(config.get("api_desactivar_url") or "").strip()
    if (not destino or not destino.lower().startswith("https://")
            or destino.rstrip("/") == str(config.get("api_activar_url", "")).rstrip("/")):
        raise ApiRechazo("No hay un endpoint HTTPS de revocación configurado")
    if not urlparse(destino).netloc:
        raise ApiRechazo("La URL de revocación es inválida")
    payload = json.dumps({"telegramId": str(uid), "amount": DIAS}).encode("utf-8")
    cabeceras = {"Content-Type": "application/json", "Accept": "application/json"}
    if config.get("token_activacion"):
        cabeceras["Authorization"] = "Bearer " + str(config["token_activacion"])
    req = urllib.request.Request(destino, data=payload, headers=cabeceras, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=float(config["timeout_api_segundos"])) as res:
            tipo = res.headers.get("Content-Type", "").lower()
            cuerpo = res.read(32768).decode("utf-8", errors="replace").strip()
    except urllib.error.HTTPError as exc:
        cuerpo = exc.read(4096).decode("utf-8", errors="replace").strip()
        try:
            respuesta = json.loads(cuerpo)
        except ValueError:
            respuesta = cuerpo
        if es_cliente_no_registrado(respuesta):
            raise ApiNoRegistrado("Usuario no registrado") from exc
        if 400 <= exc.code < 500:
            raise ApiRechazo(f"HTTP {exc.code}: {cuerpo[:250]}") from exc
        raise ApiIncierta(f"HTTP {exc.code}: {cuerpo[:250]}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ApiIncierta(str(exc)) from exc
    if "text/html" in tipo or cuerpo.lower().startswith("<!doctype html") or not cuerpo:
        raise ApiIncierta("La API de revocación no devolvió una confirmación válida")
    try:
        respuesta = json.loads(cuerpo)
    except ValueError:
        if cuerpo.lower() in ("ok", "success", "true"):
            return True
        raise ApiIncierta("Respuesta de revocación no reconocida")
    if respuesta is True:
        return True
    if not isinstance(respuesta, dict):
        raise ApiIncierta("Respuesta de revocación inválida")
    if es_cliente_no_registrado(respuesta):
        raise ApiNoRegistrado("Usuario no registrado")
    if respuesta.get("success") is False or respuesta.get("ok") is False or respuesta.get("error"):
        raise ApiRechazo(str(respuesta.get("error") or respuesta.get("message") or "La API rechazó la revocación")[:350])
    estado = str(respuesta.get("status", "")).lower()
    if respuesta.get("success") is True or respuesta.get("ok") is True or estado in ("ok", "success", "completed", "deactivated", "revoked"):
        return True
    raise ApiIncierta("El servidor no confirmó la desactivación")


class BotBanca:
    def __init__(self, config):
        self.config = config
        self.client = TelegramClient(
            str(SESION), config["api_id"], config["api_hash"],
            auto_reconnect=True, connection_retries=10,
            retry_delay=5, request_retries=5, timeout=20,
        )
        self.mi_id = None
        self.control = leer_json(CONTROL_FILE, estricto=True)
        self.estados = leer_json(ESTADO_FILE)
        self.locks_entrada = {}
        self.locks_activacion = {}
        self.enviando = {}
        self.mensajes_propios = set()
        self.historial_propios = deque(maxlen=4000)
        self.ultima_respuesta = {}
        self.ultimo_qr = {}
        self.ventas_pendientes_evento = asyncio.Event()
        self.tarea_recordatorios = None
        self.tarea_grupo = None
        self.grupo_ventas = None
        self.grupo_ventas_id = None
        self.temas_ids = {}
        self.notificaciones_lock = asyncio.Lock()
        self.ventas = leer_json(VENTAS_FILE, estricto=True)
        if not isinstance(self.ventas.get("pedidos"), dict):
            self.ventas["pedidos"] = {}
        if not isinstance(self.ventas.get("salida"), list):
            self.ventas["salida"] = []
        if not isinstance(self.ventas.get("comprobantes_pendientes"), list):
            self.ventas["comprobantes_pendientes"] = []
        self.ventas.setdefault("siguiente", 1)

        self.client.add_event_handler(self.mensaje_saliente, events.NewMessage(outgoing=True))
        self.client.add_event_handler(self.mensaje_entrante, events.NewMessage(incoming=True))
        self.client.add_event_handler(self.comando_grupo_ventas, events.NewMessage())

    async def avisar(self, mensaje, *, tema="historial"):
        texto = str(mensaje)
        logger.info("AVISO GRUPO [%s]: %s", tema, texto.replace("\n", " | "))
        if tema == "historial" and any(x in texto.lower() for x in (
            "activ", "licencia", "desbloque", "se agregaron", "no se sumaron")):
            tema = "activaciones"
        if tema not in TEMAS_VENTAS:
            tema = "historial"
        self.ventas["salida"].append({"tema": tema, "mensaje": texto})
        self.guardar_ventas()
        return None

    def registrar_resultado_activacion(self, uid, estado, *, dias=0, detalle=""):
        producto = self.estado_cliente(uid).get("producto") or "sin pedido"
        registro = {
            "fecha": ahora_iso(), "cliente_id": str(uid), "estado": estado,
            "dias": dias, "producto_solicitado": producto, "detalle": detalle[:500],
        }
        self.ventas.setdefault("activaciones", []).append(registro)
        etiqueta = {
            "confirmada": "✅ LICENCIA ACTIVADA — API CONFIRMÓ",
            "no_registrado": "❌ LICENCIA NO ACTIVADA — SIN REGISTRO",
            "rechazada": "❌ LICENCIA NO ACTIVADA — API RECHAZÓ",
            "incierta": "⚠️ ACTIVACIÓN SIN CONFIRMAR",
            "simulacion": "🧪 SIMULACIÓN — NO MODIFICA LA API",
            "desactivada": "🚫 LICENCIA DESACTIVADA — API CONFIRMÓ",
            "desactivacion_rechazada": "❌ DESACTIVACIÓN RECHAZADA POR API",
            "desactivacion_incierta": "⚠️ DESACTIVACIÓN SIN CONFIRMAR",
            "desactivacion_sin_api": "⚠️ DESACTIVACIÓN NO EJECUTADA — FALTA API",
        }.get(estado, estado.upper())
        aviso = (f"{etiqueta}\n"
                 f"🆔 Telegram: {uid}\n"
                 f"📅 Días confirmados: {dias}\n"
                 f"📦 Pedido anterior: {producto}\n"
                 f"🕒 {registro['fecha']}\n"
                 f"📝 {detalle or 'Sin observaciones'}")
        self.ventas["salida"].append({"tema": "activaciones", "mensaje": aviso})
        self.ventas["salida"].append({"tema": "historial", "mensaje": aviso})
        self.guardar_ventas()
        return registro

    def datos_chat(self, uid):
        dato = self.control.get(str(uid), {})
        return dato if isinstance(dato, dict) else {}

    def esta_on(self, uid):
        return self.datos_chat(uid).get("activo", True) is not False

    def actualizar_chat(self, uid, **cambios):
        uid = str(uid)
        dato = dict(self.datos_chat(uid))
        dato.update(cambios)
        dato["actualizado"] = ahora_iso()
        self.control[uid] = dato
        escribir_json_atomico(CONTROL_FILE, self.control)

    def estado_cliente(self, uid):
        uid = str(uid)
        dato = self.estados.get(uid)
        if not isinstance(dato, dict):
            return {}
        try:
            fecha = float(dato.get("fecha", 0))
        except (ValueError, TypeError):
            return {}
        if time.time() - fecha >= ESTADO_VIGENCIA:
            self.estados.pop(uid, None)
            escribir_json_atomico(ESTADO_FILE, self.estados)
            return {}
        return dato

    def guardar_estado_cliente(self, uid, **datos):
        anterior = dict(self.estado_cliente(uid))
        anterior.update(datos)
        anterior["fecha"] = time.time()
        self.estados[str(uid)] = anterior
        escribir_json_atomico(ESTADO_FILE, self.estados)

    def guardar_ventas(self):
        escribir_json_atomico(VENTAS_FILE, self.ventas)
        self.ventas_pendientes_evento.set()

    def venta_actual(self, uid):
        claves = [v for v in self.ventas["pedidos"].values()
                  if str(v.get("cliente_id")) == str(uid)]
        return max(claves, key=lambda v: int(v.get("numero", 0))) if claves else None

    def nueva_venta(self, uid, producto, pedido_creditos=None, cliente_nombre=""):
        anterior = self.venta_actual(uid)
        cantidad = (pedido_creditos or {}).get("cantidad") if producto == "creditos" else None
        fijo = PRODUCTOS_COMPRA[producto]["precio"]
        precio = ((pedido_creditos or {}).get("importe") if producto == "creditos"
                  else str(fijo) if fijo is not None else None)
        if (anterior and anterior.get("estado") not in ("cancelado", "entregado")
                and anterior.get("producto") == producto
                and anterior.get("cantidad") == cantidad
                and anterior.get("importe") == precio):
            return anterior, False
        if anterior and anterior.get("estado") in ("pendiente", "revision"):
            self.cambiar_estado_venta(anterior["id"], "cancelado", "Pedido reemplazado")
        numero = int(self.ventas.get("siguiente", 1))
        self.ventas["siguiente"] = numero + 1
        vid = f"V-{numero:06d}"
        venta = {
            "id": vid, "numero": numero, "cliente_id": str(uid),
            "cliente": cliente_nombre[:100], "producto": producto,
            "cantidad": cantidad, "importe": precio,
            "precio_confirmado": producto in ("banca", "vouchers"), "pago_confirmado": False,
            "estado": "pendiente", "creada": ahora_iso(), "actualizada": ahora_iso(),
            "eventos": [{"fecha": ahora_iso(), "estado": "pendiente", "detalle": "Solicitud recibida"}],
        }
        self.ventas["pedidos"][vid] = venta
        self.encolar_evento(venta, "pendientes", "Solicitud de compra recibida")
        self.guardar_ventas()
        return venta, True

    def encolar_evento(self, venta, tema, descripcion):
        bloque = self.formatear_venta(venta, descripcion)
        for destino in dict.fromkeys([tema, "historial"]):
            self.ventas["salida"].append({"tema": destino, "mensaje": bloque})

    def formatear_venta(self, venta, descripcion=""):
        nombres = {"banca": "Bot de banca (30 días)",
                   "vouchers": "Vouchers (30 días)", "creditos": "Créditos",
                   "otro": "Producto pendiente de identificar"}
        status = ESTADOS_SEMAFORO.get(venta.get("estado"), venta.get("estado", ""))
        dato = f"\n🪙 Cantidad: {venta['cantidad']}" if venta.get("cantidad") else ""
        importe = venta.get("importe")
        precio = (f"S/ {importe} (por confirmar)" if not venta.get("precio_confirmado")
                  else f"S/ {importe}") if importe else "Por cotizar"
        cliente = venta.get("cliente") or "Sin nombre"
        return (f"{status}\n🧾 Pedido: {venta['id']}\n"
                f"👤 Cliente: {cliente}\n🆔 Telegram: {venta['cliente_id']}\n"
                f"📦 Producto: {nombres.get(venta['producto'], venta['producto'])}{dato}\n"
                f"💰 Importe: {precio}\n"
                f"💳 Pago verificado: {'SÍ' if venta.get('pago_confirmado') else 'NO'}\n"
                f"🕒 Actualizado: {venta['actualizada']}\n"
                f"📝 {descripcion}\n\n"
                "Comandos: /pagado ID, /entregado ID, /cancelar ID, /estado ID")

    def cambiar_estado_venta(self, venta_id, nuevo, detalle="", *, pago_confirmado=False, precio_confirmado=False):
        venta = self.ventas["pedidos"].get(str(venta_id).upper())
        if not venta:
            return None
        venta["estado"] = nuevo
        if pago_confirmado:
            venta["pago_confirmado"] = True
        if precio_confirmado:
            venta["precio_confirmado"] = True
        venta["actualizada"] = ahora_iso()
        venta.setdefault("eventos", []).append({"fecha": ahora_iso(), "estado": nuevo, "detalle": detalle})
        tema = {"pendiente": "pendientes", "revision": "pagos", "pagado": "pagos",
                "entregado": "activaciones" if venta["producto"] == "banca" else "entregas",
                "cancelado": "cancelados"}.get(nuevo, "historial")
        self.encolar_evento(venta, tema, detalle)
        self.guardar_ventas()
        return venta

    async def crear_o_abrir_grupo_ventas(self):
        config_id = self.config.get("grupo_ventas_id")
        guardado = leer_json(GRUPO_FILE, estricto=True)
        gid = config_id or guardado.get("grupo_ventas_id")
        if gid:
            try:
                self.grupo_ventas = await self.client.get_entity(int(gid))
            except Exception:
                try:
                    await self.client.get_dialogs(limit=None)
                    self.grupo_ventas = await self.client.get_entity(int(gid))
                except Exception as exc:
                    raise RuntimeError(f"No puedo abrir el grupo de ventas {gid}: {exc}") from exc
            self.grupo_ventas_id = utils.get_peer_id(self.grupo_ventas)

    async def drenar_notificaciones(self):
        if not self.grupo_ventas:
            return
        async with self.notificaciones_lock:
            for _ in range(min(50, len(self.ventas["salida"]))):
                aviso = self.ventas["salida"][0]
                tema = self.temas_ids.get(aviso.get("tema"))
                try:
                    parametros = {"link_preview": False, "parse_mode": None}
                    if tema is not None:
                        parametros["reply_to"] = tema
                    await self.client.send_message(
                        self.grupo_ventas, aviso["mensaje"], **parametros,
                    )
                except Exception:
                    return
                self.ventas["salida"].pop(0)
                self.guardar_ventas()

    async def copiar_comprobante_en_grupo(self, event, uid):
        venta = self.venta_actual(uid)
        if not venta:
            return
        mensaje_id = int(event.message.id)
        clave = f"{uid}:{mensaje_id}"
        cola = self.ventas["comprobantes_pendientes"]
        if not any(item.get("clave") == clave for item in cola):
            cola.append({"clave": clave, "cliente_id": str(uid),
                         "mensaje_id": mensaje_id, "pedido": venta["id"],
                         "fecha": ahora_iso()})
            self.guardar_ventas()

    async def drenar_comprobantes(self):
        tema = self.temas_ids.get("pagos")
        if not self.grupo_ventas:
            return
        async with self.notificaciones_lock:
            cola = self.ventas["comprobantes_pendientes"]
            for _ in range(min(30, len(cola))):
                item = cola[0]
                venta = self.ventas["pedidos"].get(item["pedido"])
                try:
                    mensaje = await self.client.get_messages(
                        int(item["cliente_id"]), ids=int(item["mensaje_id"]))
                    if not mensaje or not getattr(mensaje, "media", None):
                        cola.pop(0)
                        self.guardar_ventas()
                        continue
                    cliente = (venta.get("cliente") or "Sin nombre") if venta else "Sin nombre"
                    caption = (
                        "📸 COMPROBANTE RECIBIDO\n"
                        f"🧾 Pedido: {item['pedido']}\n"
                        f"👤 Cliente: {cliente}\n"
                        f"🆔 Telegram: {item['cliente_id']}\n"
                        f"Confirma el pago: /pagado {item['pedido']}"
                    )
                    kwargs = {"caption": caption, "parse_mode": None}
                    if tema is not None:
                        kwargs["reply_to"] = tema
                    await self.client.send_file(self.grupo_ventas, mensaje.media, **kwargs)
                except Exception:
                    return
                cola.pop(0)
                self.guardar_ventas()

    async def loop_registro_ventas(self):
        while True:
            try:
                if self.grupo_ventas is None:
                    await self.crear_o_abrir_grupo_ventas()
                await self.drenar_notificaciones()
                await self.drenar_comprobantes()
            except Exception:
                pass
            self.ventas_pendientes_evento.clear()
            try:
                await asyncio.wait_for(self.ventas_pendientes_evento.wait(), timeout=45)
            except asyncio.TimeoutError:
                pass

    async def comando_grupo_ventas(self, event):
        pass

    def recordar_envio(self, uid, mensajes):
        if not isinstance(mensajes, (tuple, list)):
            mensajes = [mensajes]
        for mensaje in mensajes:
            mid = getattr(mensaje, "id", None)
            if mid is None:
                continue
            clave = (uid, mid)
            if len(self.historial_propios) == self.historial_propios.maxlen:
                self.mensajes_propios.discard(self.historial_propios[0])
            self.historial_propios.append(clave)
            self.mensajes_propios.add(clave)

    async def enviar_automatico(self, event, texto=None, *, archivo=None, forzar=False,
                                html=False, responder_a=None):
        uid = str(event.chat_id)
        if not forzar and not self.esta_on(uid):
            return None
        self.enviando[uid] = self.enviando.get(uid, 0) + 1
        try:
            kwargs = {"file": str(archivo)} if archivo else {}
            if html:
                kwargs["parse_mode"] = "html"
            if responder_a is not None:
                kwargs["reply_to"] = responder_a
            resultado = await event.respond(texto, **kwargs) if texto is not None else await event.respond(**kwargs)
            self.recordar_envio(uid, resultado)
            return resultado
        finally:
            restantes = self.enviando[uid] - 1
            if restantes:
                self.enviando[uid] = restantes
            else:
                self.enviando.pop(uid, None)

    async def eliminar_comando(self, event):
        try:
            await event.delete()
        except Exception:
            pass

    async def mensaje_saliente(self, event):
        if not event.is_private or event.chat_id is None:
            return
        uid = str(event.chat_id)
        if self.mi_id and uid == str(self.mi_id):
            return
        texto = normalizar(event.raw_text or "")
        if (uid, event.id) in self.mensajes_propios:
            return

        if texto in ("/on", "/off"):
            await self.eliminar_comando(event)
            if texto == "/on":
                self.actualizar_chat(uid, activo=True, motivo="comando_on")
            elif texto == "/off":
                self.actualizar_chat(uid, activo=False, motivo="comando_off")
            return

        if es_orden_desactivar(texto):
            respuesta_id = await self.validar_respuesta_cliente(event, uid)
            if respuesta_id is False:
                return
            await self.eliminar_comando(event)
            await self.desactivar_cliente(event, uid, responder_a=respuesta_id)
            return

        if es_orden_activar(texto):
            respuesta_id = await self.validar_respuesta_cliente(event, uid)
            if respuesta_id is False:
                return
            await self.eliminar_comando(event)
            # CORRECCIÓN CLAVE: Se permite activar la licencia directamente
            # sin importar si el pedido anterior figuraba como Vouchers u otro.
            await self.activar_cliente(event, uid, responder_a=respuesta_id)
            return

    async def validar_respuesta_cliente(self, event, uid):
        respuesta_id = getattr(event.message, "reply_to_msg_id", None)
        if respuesta_id is None:
            return None
        try:
            citado = await event.get_reply_message()
            if citado is None and self.client is not None:
                citado = await self.client.get_messages(int(uid), ids=int(respuesta_id))
        except Exception:
            return False
        if citado is None or int(getattr(citado, "sender_id", 0) or 0) != int(uid):
            return False
        return int(respuesta_id)

    async def desactivar_cliente(self, event, uid, *, responder_a=None):
        lock = self.locks_activacion.setdefault(uid, asyncio.Lock())
        async with lock:
            try:
                await asyncio.to_thread(quitar_dias_api, self.config, uid)
                await self.enviar_automatico(event, "🚫 Licencia desactivada correctamente.", forzar=True, responder_a=responder_a)
            except Exception as exc:
                await self.enviar_automatico(event, f"❌ Error al desactivar: {exc}", forzar=True, responder_a=responder_a)

    async def activar_cliente(self, event, uid, *, responder_a=None):
        lock = self.locks_activacion.setdefault(uid, asyncio.Lock())
        async with lock:
            try:
                # LLAMADA A LA API DE ACTIVACIÓN (consume los 30 días)
                await asyncio.to_thread(agregar_dias_api, self.config, uid)
            except ApiNoRegistrado as exc:
                try:
                    await self.enviar_automatico(
                        event, mensaje_registro_pendiente(self.config, uid),
                        forzar=True, responder_a=responder_a,
                    )
                except Exception:
                    pass
                self.registrar_resultado_activacion(uid, "no_registrado", detalle=str(exc)[:250])
                return
            except ApiRechazo as exc:
                self.registrar_resultado_activacion(uid, "rechazada", detalle=str(exc)[:350])
                try:
                    await self.enviar_automatico(
                        event, "❌ LICENCIA NO ACTIVADA: La API rechazó la solicitud.",
                        forzar=True, responder_a=responder_a,
                    )
                except Exception:
                    pass
                return
            except Exception as exc:
                try:
                    await self.enviar_automatico(
                        event, f"⚠️ Error de conexión con la API: {exc}",
                        forzar=True, responder_a=responder_a,
                    )
                except Exception:
                    pass
                return

            try:
                self.actualizar_chat(
                    uid,
                    activo=False,
                    motivo="activado_30_dias",
                    operacion={"estado": "confirmada", "fecha": ahora_iso()},
                    ultima_activacion={"dias": DIAS, "fecha": ahora_iso(), "timestamp": time.time()},
                )
            except Exception:
                pass

            try:
                venta = self.venta_actual(uid)
                if venta and venta.get("estado") != "cancelado":
                    self.cambiar_estado_venta(
                        venta["id"], "entregado", "Activación de 30 días confirmada por la API"
                    )
                self.registrar_resultado_activacion(
                    uid, "confirmada", dias=DIAS,
                    detalle="La API confirmó +30 días de licencia")
            except Exception:
                pass

            try:
                await self.enviar_automatico(
                    event,
                    "✅ <b>LICENCIA ACTIVADA CORRECTAMENTE</b>\n\n"
                    f"📅 Tiempo activado: <b>{DIAS} días</b>\n"
                    "🟢 Estado: <b>ACTIVA</b>\n"
                    "💜 Gracias por tu compra. — Los Chuckys",
                    forzar=True, html=True, responder_a=responder_a,
                )
            except Exception:
                pass

    async def preparar_qr(self):
        if QR_FILE.is_file() and QR_FILE.stat().st_size > 0:
            return
        url = self.config.get("qr_url", "")
        if not url:
            return
        def descargar():
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=15) as res:
                datos = res.read(6_000_000)
            QR_FILE.write_bytes(datos)
        try:
            await asyncio.to_thread(descargar)
        except Exception:
            pass

    async def mandar_qr(self, event):
        if QR_FILE.is_file() and QR_FILE.stat().st_size:
            try:
                await self.enviar_automatico(event, archivo=QR_FILE)
                return
            except Exception:
                pass

    async def mandar_pago(self, event, uid, *, forzar=False, producto=None, pedido_creditos=None,
                          registrar_pedido=True):
        await self.mandar_qr(event)
        texto = (
            "🤖 <b>LOS CHUCKYS — CONFIRMACIÓN DE PEDIDO</b>\n\n"
            f"🏦 Cuenta: <code>{self.config['numero_cuenta']}</code>\n"
            "Envía tu comprobante después de transferir."
        )
        await self.enviar_automatico(event, texto, html=True)
        if registrar_pedido:
            self.nueva_venta(uid, producto or "banca", pedido_creditos)

    async def respuesta_corta(self, event, texto):
        uid = str(event.chat_id)
        ahora = time.time()
        if ahora - self.ultima_respuesta.get(uid, 0) < REPETIR_RESPUESTA_SEGUNDOS:
            return
        enviado = await self.enviar_automatico(event, texto)
        if enviado is not None:
            self.ultima_respuesta[uid] = ahora

    async def mensaje_entrante(self, event):
        if not event.is_private or event.sender_id is None or event.out:
            return
        uid = str(event.sender_id)
        lock = self.locks_entrada.setdefault(uid, asyncio.Lock())
        async with lock:
            if not self.esta_on(uid):
                return
            try:
                emisor = await event.get_sender()
                if getattr(emisor, "bot", False):
                    return
                texto = normalizar(event.raw_text or "")

                if es_imagen(event):
                    venta = self.venta_actual(uid)
                    if not venta:
                        venta, _ = self.nueva_venta(uid, "banca")
                    self.cambiar_estado_venta(venta["id"], "revision", "Captura recibida")
                    await self.copiar_comprobante_en_grupo(event, uid)
                    await self.enviar_automatico(event, "✅ Recibí tu comprobante. Un asesor validará tu pago.")
                    return

                if quiere_menu(texto) or quiere_comprar(texto):
                    await self.mandar_pago(event, uid, producto="banca")
                    return
            except Exception:
                pass

    async def iniciar(self, *, login=False):
        if login:
            await self.client.start()
            cuenta = await self.client.get_me()
            print(f"Sesión iniciada para {cuenta.first_name} ({cuenta.id}).")
            await self.client.disconnect()
            return
        await self.preparar_qr()
        while True:
            try:
                await self.client.connect()
                if not await self.client.is_user_authorized():
                    logger.error("Inicia sesión: python bot_banca_mejorado.py --login")
                    return
                cuenta = await self.client.get_me()
                self.mi_id = cuenta.id
                logger.info("BOT ACTIVO para %s", cuenta.first_name)
                self.tarea_grupo = asyncio.create_task(self.loop_registro_ventas())
                await self.client.run_until_disconnected()
            except (asyncio.CancelledError, KeyboardInterrupt):
                raise
            except Exception:
                pass
            finally:
                if self.tarea_grupo is not None:
                    self.tarea_grupo.cancel()
                try:
                    await self.client.disconnect()
                except Exception:
                    pass
            await asyncio.sleep(RECONEXION_SEGUNDOS)


async def principal():
    login = "--login" in sys.argv
    config = leer_configuracion(permitir_preguntar=login)
    bot = BotBanca(config)
    await bot.iniciar(login=login)


if __name__ == "__main__":
    try:
        asyncio.run(principal())
    except KeyboardInterrupt:
        logger.info("Detenido")
    except Exception as exc:
        print(f"ERROR: {exc}")
        raise SystemExit(1)
