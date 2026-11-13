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

# Telegram cambio las operaciones de temas de channels a messages.
# Esta compatibilidad funciona con esquemas antiguos y recientes de Telethon.
CrearTemaRequest = getattr(mensajes_tl, "CreateForumTopicRequest", None)
ObtenerTemasRequest = getattr(mensajes_tl, "GetForumTopicsRequest", None)
TEMAS_EN_MESSAGES = CrearTemaRequest is not None and ObtenerTemasRequest is not None
if not TEMAS_EN_MESSAGES:
    CrearTemaRequest = getattr(canales_tl, "CreateForumTopicRequest", None)
    ObtenerTemasRequest = getattr(canales_tl, "GetForumTopicsRequest", None)
if CrearTemaRequest is None or ObtenerTemasRequest is None:
    raise RuntimeError(
        "Tu version de Telethon no admite los temas de Telegram. "
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
# Solo evita ráfagas inmediatas. Una nueva consulta después puede recibir el QR.
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
    # Bot de registro del cliente para usuarios que no existen en la API.
    # Se acepta @Usuario o https://t.me/Usuario.
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
            raise RuntimeError(
                f"No puedo leer {ruta.name}. No iniciaré para evitar reactivar "
                f"chats que estaban en OFF. Corrige o restaura el archivo: {exc}"
            ) from exc
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

    if (not config["api_id"] or not config["api_hash"]) and permitir_preguntar:
        if not sys.stdin.isatty():
            raise RuntimeError("Configura api_id/api_hash en config.json antes de usar VPS.")
        print("Configuración inicial de Telegram (solo una vez).")
        if not config["api_id"]:
            config["api_id"] = int(input("API ID de Telegram: ").strip())
        if not config["api_hash"]:
            config["api_hash"] = getpass.getpass("API HASH de Telegram: ").strip()
        if not config["api_hash"]:
            raise RuntimeError("No se proporcionó API HASH")
        escribir_json_atomico(CONFIG_FILE, config)
        print("Configuración guardada en config.json.")

    if config["api_id"] <= 0 or not config["api_hash"]:
        raise RuntimeError(
            "Faltan credenciales de Telegram. Ejecuta una vez: "
            "python bot_banca_mejorado.py --login"
        )
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
    """Evita interpretar un número o una conversación normal como compra."""
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


# Activa SOLO por una orden deliberada. Una frase conversacional sobre activación
# no debe sumar días por accidente. Se admiten acentos, /activate y respuestas.
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


def es_imagen(event):
    mensaje = event.message
    if getattr(mensaje, "photo", None):
        return True
    archivo = getattr(mensaje, "file", None)
    return bool(archivo and (getattr(archivo, "mime_type", "") or "").startswith("image/"))


class ApiRechazo(Exception):
    pass


class ApiNoRegistrado(ApiRechazo):
    """La API confirmó que el cliente no tiene cuenta de Telegram registrada."""


class ApiIncierta(Exception):
    pass


def es_cliente_no_registrado(respuesta):
    """Reconoce respuestas conocidas de usuario inexistente sin confundir fallos de red.

    Se revisa el contenido y no el HTTP 404 por sí solo: un 404 también puede
    indicar que el endpoint de la API ha cambiado o no existe.
    """
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
    """Valida el bot proporcionado, sin construir enlaces no configurados."""
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
    # No se confirma una licencia con una respuesta vacía o ambigua.
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
        """Registra avisos exclusivamente en el grupo de ventas (nunca en 'me').

        La cola se guarda en disco, por lo que un grupo caído no bloquea la API.
        """
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
        """Conserva un historial de licencia separado del producto de compra.

        Un pedido marcado como VOUCHERS no debe impedir activar una licencia
        ni convertirse de manera ficticia en una venta de BOT DE BANCA.
        """
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
        logger.info("Estado actualizado para chat %s", uid)

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
            self.cambiar_estado_venta(anterior["id"], "cancelado", "Pedido reemplazado por uno nuevo")
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
        if len(self.ventas["salida"]) > 2000:
            logger.error("Cola de Telegram excede 2000 eventos; revisar conexión del grupo")

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
                "Comandos (solo propietario): /pagado ID, /entregado ID, /cancelar ID, /estado ID")

    def cambiar_estado_venta(self, venta_id, nuevo, detalle="", *, pago_confirmado=False, precio_confirmado=False):
        venta = self.ventas["pedidos"].get(str(venta_id).upper())
        if not venta:
            return None
        if venta["estado"] == "cancelado" and nuevo != "cancelado":
            raise ValueError("El pedido está cancelado; crea otro en vez de reabrirlo")
        if venta["estado"] == "entregado" and nuevo in ("revision", "pendiente"):
            raise ValueError("No se puede volver a pendiente un pedido ya entregado")
        cambiado = (venta["estado"] != nuevo or
                    (pago_confirmado and not venta.get("pago_confirmado")) or
                    (precio_confirmado and not venta.get("precio_confirmado")))
        if not cambiado:
            return venta
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
            except (ValueError, TypeError):
                # Telethon no siempre conoce el access_hash del grupo por su ID.
                # Carga los diálogos en la tarea de ventas, sin bloquear privados.
                try:
                    await self.client.get_dialogs(limit=None)
                    self.grupo_ventas = await self.client.get_entity(int(gid))
                except Exception as exc:
                    raise RuntimeError(
                        f"No puedo abrir el grupo de ventas {gid}. "
                        "Comprueba que la cuenta de Telegram pertenece al grupo "
                        "y que sus temas están activados."
                    ) from exc
            self.grupo_ventas_id = utils.get_peer_id(self.grupo_ventas)
        elif self.config.get("crear_grupo_ventas_si_falta", True):
            resultado = await self.client(CreateChannelRequest(
                title=self.config["nombre_grupo_ventas"],
                about="Registro interno de pedidos, pagos y activaciones. Acceso privado.",
                megagroup=True, forum=True,
            ))
            self.grupo_ventas = next((x for x in getattr(resultado, "chats", [])
                                     if getattr(x, "megagroup", False)), None)
            if self.grupo_ventas is None:
                raise RuntimeError("Telegram no devolvió el grupo creado")
            self.grupo_ventas_id = utils.get_peer_id(self.grupo_ventas)
            escribir_json_atomico(GRUPO_FILE, {"grupo_ventas_id": self.grupo_ventas_id})
            await self.avisar(f"📒 Grupo privado de ventas creado: ID {self.grupo_ventas_id}")
        else:
            logger.warning("Registro de ventas sin grupo: configura grupo_ventas_id")
            return
        if not getattr(self.grupo_ventas, "forum", False):
            logger.warning("El grupo no usa Temas; los eventos irán al chat principal")
            self.temas_ids.clear()
            return
        # Evita recrear temas existentes al reiniciar o si Telegram tarda en indexarlos.
        try:
            encontrados = await self.listar_temas()
        except Exception as exc:
            logger.warning("No se pudieron leer temas (%s); se usará el grupo principal", exc)
            self.temas_ids.clear()
            return
        self.temas_ids.clear()
        for clave, titulo in TEMAS_VENTAS.items():
            tema = encontrados.get(titulo)
            if tema is None:
                parametros = {"peer" if TEMAS_EN_MESSAGES else "channel": self.grupo_ventas,
                              "title": titulo}
                try:
                    resultado_creacion = await self.client(CrearTemaRequest(**parametros))
                except Exception as exc:
                    logger.warning("No se pudo crear tema %s: %s. Se usará grupo principal", titulo, exc)
                    self.temas_ids.clear()
                    return
                # Telegram incluye el ID del mensaje de servicio que crea el tema.
                tema = self.id_tema_creado(resultado_creacion)
                if tema is None:
                    for intento in range(4):
                        await asyncio.sleep(1 + intento)
                        encontrados = await self.listar_temas()
                        tema = encontrados.get(titulo)
                        if tema is not None:
                            break
                if tema is None:
                    logger.warning("El tema %s no devolvió ID. Se usará el grupo principal", titulo)
                    self.temas_ids.clear()
                    return
                encontrados[titulo] = int(tema)
            self.temas_ids[clave] = int(tema)
        logger.info("Grupo de ventas %s listo, %s temas", self.grupo_ventas_id, len(self.temas_ids))

    @staticmethod
    def id_tema_creado(resultado):
        """Obtiene el ID del tema creado desde las actualizaciones de Telegram."""
        for update in getattr(resultado, "updates", ()):
            mensaje = getattr(update, "message", None)
            accion = getattr(mensaje, "action", None)
            if accion is not None and type(accion).__name__ == "MessageActionTopicCreate":
                return getattr(mensaje, "id", None)
        return None

    async def listar_temas(self):
        """Lista todos los temas, sin filtro por emoji o palabras parciales."""
        encontrados = {}
        offset_date = 0
        offset_id = 0
        offset_topic = 0
        vistos = set()
        for _ in range(12):  # hasta 1200 temas, con protección contra bucles
            parametros = {
                "peer" if TEMAS_EN_MESSAGES else "channel": self.grupo_ventas,
                "q": "", "offset_date": offset_date, "offset_id": offset_id,
                "offset_topic": offset_topic, "limit": 100,
            }
            resultado = await self.client(ObtenerTemasRequest(**parametros))
            temas = list(getattr(resultado, "topics", ()) or ())
            for tema in temas:
                titulo = getattr(tema, "title", None)
                tid = getattr(tema, "id", None)
                if titulo and isinstance(tid, int):
                    encontrados[titulo] = tid
            if len(temas) < 100:
                break
            ultimo = temas[-1]
            ultimo_id = getattr(ultimo, "id", 0)
            if ultimo_id in vistos or not ultimo_id:
                break
            vistos.add(ultimo_id)
            offset_topic = ultimo_id
            offset_id = getattr(ultimo, "top_message", 0) or 0
            fecha = getattr(ultimo, "date", None)
            offset_date = fecha if isinstance(fecha, datetime) else 0
        return encontrados

    async def buscar_tema(self, nombre):
        return (await self.listar_temas()).get(nombre)

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
                except FloodWaitError:
                    raise
                except Exception:
                    logger.exception("No se pudo notificar al tema %s", aviso["tema"])
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
        # El registro se vacía en loop_registro_ventas, no bloquea el mensaje al cliente.

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
                        logger.warning("Foto no recuperable para pedido %s", item["pedido"])
                        if venta:
                            self.encolar_evento(venta, "pagos", "⚠️ La imagen original ya no está disponible")
                        cola.pop(0)
                        self.guardar_ventas()
                        continue
                    cliente = (venta.get("cliente") or "Sin nombre") if venta else "Sin nombre"
                    producto = (venta.get("producto") or "por identificar") if venta else "por identificar"
                    importe = (venta.get("importe") or "por confirmar") if venta else "por confirmar"
                    cantidad = (f" ({venta['cantidad']} créditos)" if venta and venta.get("cantidad") else "")
                    caption = (
                        "📸 COMPROBANTE RECIBIDO — SIN VERIFICAR\n"
                        f"🧾 Pedido: {item['pedido']}\n"
                        f"👤 Cliente: {cliente}\n"
                        f"🆔 Telegram: {item['cliente_id']}\n"
                        f"📦 Producto: {producto}{cantidad}\n"
                        f"💰 Importe indicado: S/ {importe}\n"
                        "🟠 Estado: Pendiente de revisión\n\n"
                        f"Confirma el pago en este grupo: /pagado {item['pedido']}\n"
                        f"Al entregar el producto: /entregado {item['pedido']}"
                    )
                    kwargs = {"caption": caption, "parse_mode": None}
                    if tema is not None:
                        kwargs["reply_to"] = tema
                    await self.client.send_file(
                        self.grupo_ventas, mensaje.media, **kwargs,
                    )
                except FloodWaitError:
                    raise
                except Exception:
                    logger.exception("No se pudo subir el comprobante %s al grupo", item["clave"])
                    return
                cola.pop(0)
                self.guardar_ventas()

    async def conectar_registro_ventas(self):
        if self.grupo_ventas is None:
            await self.crear_o_abrir_grupo_ventas()
        await self.drenar_notificaciones()
        await self.drenar_comprobantes()

    async def loop_registro_ventas(self):
        # Nunca detiene la atención de mensajes privados por un grupo sin permisos.
        fallos = 0
        while True:
            try:
                await self.conectar_registro_ventas()
                fallos = 0
            except asyncio.CancelledError:
                raise
            except FloodWaitError as exc:
                logger.warning("Límite temporal de Telegram en grupo: %s s", exc.seconds)
                await asyncio.sleep(min(3600, int(exc.seconds)))
            except Exception as exc:
                fallos += 1
                if fallos == 1 or fallos % 20 == 0:
                    logger.exception("Registro de ventas no disponible; los privados siguen activos: %s", exc)
            # Se drena pronto si hubo ventas y el grupo está operativo.
            # Con fallos se espera para no insistir contra Telegram cada instante.
            if fallos:
                self.ventas_pendientes_evento.clear()
                await asyncio.sleep(45)
            else:
                self.ventas_pendientes_evento.clear()
                try:
                    await asyncio.wait_for(self.ventas_pendientes_evento.wait(), timeout=45)
                except asyncio.TimeoutError:
                    pass

    async def comando_grupo_ventas(self, event):
        if not self.mi_id or not self.grupo_ventas_id:
            return
        if event.chat_id != self.grupo_ventas_id or event.sender_id != self.mi_id:
            return
        texto = (event.raw_text or "").strip()
        if texto.lower() in ("/prueba", "/pruebagrupo"):
            # Prueba inofensiva del canal de auditoría; NO toca la API ni clientes.
            self.registrar_resultado_activacion(
                self.mi_id, "simulacion",
                detalle="EJEMPLO: activar +30 días -> ACTIVA; retirar 30 -> INACTIVA. "
                        "Solo simulación; no se cambió la base de datos de licencias.")
            await event.reply(
                "🧪 Prueba registrada en Activaciones e Historial. "
                "No se activó ni se quitó ningún día a usuarios reales.")
            return
        match = re.fullmatch(r"/(pagado|revision|entregado|vendido|cancelar|estado)\s+(V-\d{6,})", texto, re.I)
        if match:
            accion, pedido_id = match.group(1).lower(), match.group(2).upper()
            venta = self.ventas["pedidos"].get(pedido_id)
            if not venta:
                await event.reply(f"❌ No encuentro el pedido {pedido_id}.")
                return
            if accion == "estado":
                await event.reply(self.formatear_venta(venta, "Consulta de estado"))
                return
            try:
                tipo = {"cancelar": "cancelado", "revision": "revision",
                        "vendido": "entregado"}.get(accion, accion)
                if tipo == "pagado":
                    estado_efectivo = "entregado" if venta["estado"] == "entregado" else "pagado"
                    venta = self.cambiar_estado_venta(pedido_id, estado_efectivo, "Pago confirmado MANUALMENTE por el propietario",
                                                     pago_confirmado=True, precio_confirmado=True)
                else:
                    venta = self.cambiar_estado_venta(pedido_id, tipo, "Actualizado por el propietario")
                if tipo in ("cancelado", "entregado"):
                    self.guardar_estado_cliente(venta["cliente_id"], estado=tipo, recordatorio_pendiente=False)
                if tipo == "entregado":
                    self.actualizar_chat(venta["cliente_id"], activo=False, motivo="entrega_manual")
            except ValueError as exc:
                await event.reply(f"⚠️ {exc}")
                return
            await event.reply(f"✅ {pedido_id}: {ESTADOS_SEMAFORO.get(tipo, tipo)}")
            await self.drenar_notificaciones()
            return
        if texto.lower() == "/resumen":
            conteos = {v: 0 for v in ESTADOS_SEMAFORO}
            for v in self.ventas["pedidos"].values():
                if v["estado"] in conteos:
                    conteos[v["estado"]] += 1
            resumen = "📊 RESUMEN DE VENTAS (histórico)\n" + "\n".join(
                f"{ESTADOS_SEMAFORO[k]}: {valor}" for k, valor in conteos.items()
            )
            await event.reply(resumen)
        elif texto.lower() == "/ayudaventas":
            await event.reply("📒 CONTROL DE VENTAS\n"
                              "/resumen — total por estado\n"
                              "/estado V-000001 — consultar\n"
                              "/revision V-000001 — comprobante recibido\n"
                              "/pagado V-000001 — marcar pago verificado\n"
                              "/entregado V-000001 — voucher o créditos enviados\n"
                              "/vendido V-000001 — igual que /entregado\n"
                              "/cancelar V-000001 — cancelar\n\n"
                              "En el privado del cliente: actívate, /prueba, /desactivar.\n"
                              "/prueba solo SIMULA en el grupo, no modifica licencias.\n"
                              "/desactivar no quita días hasta integrar una API de revocación.\n"
                              "/prueba en el grupo: verifica las notificaciones SIN tocar la API.")

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
        except Exception as exc:
            logger.warning("No se pudo borrar comando administrativo: %s", exc)

    async def mensaje_saliente(self, event):
        if not event.is_private or event.chat_id is None:
            return
        uid = str(event.chat_id)
        if self.mi_id and uid == str(self.mi_id):
            return
        texto = normalizar(event.raw_text or "")
        if (uid, event.id) in self.mensajes_propios:
            return
        # Dar prioridad a una orden humana de activación aunque coincida con
        # el envío de un mensaje del bot al mismo cliente.
        if self.enviando.get(uid, 0) and not parece_orden_activar(texto):
            return

        if texto in ("/on", "/off", "/estado", "/status", "/ayuda", "/help"):
            await self.eliminar_comando(event)
            if texto == "/on":
                self.actualizar_chat(uid, activo=True, motivo="comando_on")
                await self.avisar(f"✅ Respuestas ON para {uid}.")
            elif texto == "/off":
                self.actualizar_chat(uid, activo=False, motivo="comando_off")
                if self.estado_cliente(uid).get("recordatorio_pendiente"):
                    self.guardar_estado_cliente(uid, recordatorio_pendiente=False)
                await self.avisar(f"🛑 Respuestas OFF para {uid}.")
            elif texto in ("/estado", "/status"):
                info = self.datos_chat(uid)
                op = info.get("operacion", {})
                await self.avisar(
                    f"📋 Chat {uid}\n"
                    f"Auto: {'ON' if self.esta_on(uid) else 'OFF'}\n"
                    f"Última activación: {info.get('ultima_activacion', {}).get('fecha', 'ninguna')}\n"
                    f"API: {op.get('estado', 'sin operación')}"
                )
            else:
                await self.avisar(
                    "Comandos para ese chat: /on, /off, /activate, /estado.\n"
                    "Solo si la API quedó incierta, revisa el panel y luego usa /desbloquear."
                )
            return

        if texto == "/desbloquear":
            await self.eliminar_comando(event)
            op = self.datos_chat(uid).get("operacion", {})
            if op.get("estado") in ("incierta", "en_curso"):
                self.actualizar_chat(uid, operacion={"estado": "desbloqueada", "fecha": ahora_iso()})
                await self.avisar(
                    f"⚠️ Desbloqueado ID {uid}. Asegúrate de revisar primero "
                    "en el panel si ya se agregaron los 30 días."
                )
            else:
                await self.avisar(f"ID {uid} no tiene activación incierta.")
            return

        if texto in ("/prueba", "/prueba licencia", "/testlicencia"):
            await self.eliminar_comando(event)
            detalle = ("Prueba de flujo: ejemplo +30 días => ACTIVA; "
                       "ejemplo -30 días => DESACTIVADA. "
                       "No se llamó a la API, no se alteró ningún saldo ni se avisó al cliente.")
            self.registrar_resultado_activacion(uid, "simulacion", detalle=detalle)
            return

        if texto in ("/desactivar", "/quitar", "/quitar 30"):
            await self.eliminar_comando(event)
            await self.avisar(
                f"⚠️ DESACTIVACIÓN NO EJECUTADA para ID {uid}: la API configurada "
                "solo tiene /api/add_days. Falta un endpoint confirmado para "
                "quitar/revocar días; no se modificó la licencia.", tema="activaciones"
            )
            return

        if es_orden_activar(texto):
            # En respuesta, valida que el mensaje citado pertenezca al cliente;
            # nunca activamos al autor equivocado o al responder al propio bot.
            respuesta_id = getattr(event.message, "reply_to_msg_id", None)
            if respuesta_id is not None:
                try:
                    citado = await event.get_reply_message()
                except Exception:
                    logger.exception("No se pudo verificar la respuesta citada en %s", uid)
                    await self.avisar(f"⚠️ No se activó a {uid}: no pude verificar el mensaje respondido.", tema="activaciones")
                    return
                if citado is None or int(getattr(citado, "sender_id", 0) or 0) != int(uid):
                    await self.avisar(f"⚠️ No se activó a {uid}: responde al mensaje ORIGINAL del cliente.", tema="activaciones")
                    return
            await self.eliminar_comando(event)
            await self.activar_cliente(event, uid, responder_a=respuesta_id)
            return
        if parece_orden_activar(texto):
            await self.eliminar_comando(event)
            await self.avisar("⚠️ Para activar usa: actívate, activar, /activate o /activate 30. Solo 30 días.")
            return

        if texto in ("/entregado", "/vendido", "/voucher", "/entregavoucher"):
            await self.eliminar_comando(event)
            venta = self.venta_actual(uid)
            if not venta or venta.get("estado") == "cancelado":
                await self.avisar(f"⚠️ No hay pedido vigente para registrar entrega al usuario {uid}.")
            elif venta["producto"] == "banca":
                await self.avisar("⚠️ Para la activación bancaria usa /activate: verifica la API.")
            else:
                self.cambiar_estado_venta(venta["id"], "entregado", "Producto entregado manualmente en el chat privado")
                self.guardar_estado_cliente(uid, estado="entregado", recordatorio_pendiente=False)
                self.actualizar_chat(uid, activo=False, motivo="entrega_manual")
                await self.avisar(f"✅ Entrega anotada para {venta['id']} (cliente {uid}).")
            return

        if texto == "/pagado":
            await self.eliminar_comando(event)
            venta = self.venta_actual(uid)
            if venta and venta["estado"] != "cancelado":
                estado_efectivo = "entregado" if venta["estado"] == "entregado" else "pagado"
                self.cambiar_estado_venta(venta["id"], estado_efectivo, "Pago confirmado manualmente por el propietario",
                                          pago_confirmado=True, precio_confirmado=True)
                await self.avisar(f"✅ Pago de {venta['id']} confirmado manualmente.")
            else:
                await self.avisar("⚠️ Este cliente no tiene pedido vigente.")
            return

        # Una respuesta humana ya NO apaga el bot indefinidamente.
        # Solo /off y la activación confirmada lo dejan inactivo.
        if self.estado_cliente(uid).get("recordatorio_pendiente"):
            self.guardar_estado_cliente(uid, recordatorio_pendiente=False)

    async def activar_cliente(self, event, uid, *, responder_a=None):
        lock = self.locks_activacion.setdefault(uid, asyncio.Lock())
        async with lock:
            # La licencia se vincula al ID de Telegram, no a la última categoría
            # comercial. Un pedido de VOUCHERS o CRÉDITOS no bloquea /activate.
            # Solo la API decide si la cuenta puede recibir los 30 días.
            producto = self.estado_cliente(uid).get("producto", "sin pedido")
            datos = self.datos_chat(uid)
            operacion = datos.get("operacion", {})
            if operacion.get("estado") in ("en_curso", "incierta"):
                await self.avisar(
                    f"⚠️ ID {uid}: hay una activación pendiente o incierta. "
                    "Verifica el panel antes de usar /desbloquear."
                )
                return
            ultima = datos.get("ultima_activacion", {})
            try:
                marca_ultima = float(ultima.get("timestamp", 0)) if isinstance(ultima, dict) else 0
            except (TypeError, ValueError):
                marca_ultima = 0
            if time.time() - marca_ultima < COOLDOWN_ACTIVACION:
                await self.avisar(f"⚠️ ID {uid}: activación reciente. No se sumaron días otra vez.")
                return

            try:
                self.actualizar_chat(uid, operacion={"estado": "en_curso", "fecha": ahora_iso()})
            except OSError as exc:
                await self.avisar(f"❌ No se pudo guardar el estado de {uid}: {exc}. API no llamada.")
                return

            try:
                await asyncio.to_thread(agregar_dias_api, self.config, uid)
            except ApiNoRegistrado as exc:
                # La API confirma ausencia de cuenta: no marcar pagado, no apagar
                # el chat ni crear una activación. El cliente recibe instrucciones.
                try:
                    self.actualizar_chat(uid, operacion={
                        "estado": "no_registrado", "fecha": ahora_iso()
                    })
                except OSError:
                    logger.exception("No se pudo guardar el rechazo por falta de registro de %s", uid)
                try:
                    await self.enviar_automatico(
                        event, mensaje_registro_pendiente(self.config, uid),
                        forzar=True, responder_a=responder_a,
                    )
                except Exception:
                    logger.exception("Fallo aviso de registro al cliente %s", uid)
                    await self.avisar(f"⚠️ ID {uid} NO registrado; no pude notificarle.")
                self.registrar_resultado_activacion(
                    uid, "no_registrado", detalle=str(exc)[:250])
                return
            except ApiRechazo as exc:
                self.actualizar_chat(uid, operacion={"estado": "rechazada", "fecha": ahora_iso()})
                self.registrar_resultado_activacion(
                    uid, "rechazada", detalle=str(exc)[:350])
                try:
                    await self.enviar_automatico(
                        event,
                        "❌ LICENCIA NO ACTIVADA\n\n"
                        "No fue posible completar la activación. Un asesor revisará el problema. "
                        "No se confirmó la activación de tu cuenta.",
                        forzar=True, responder_a=responder_a,
                    )
                except Exception:
                    logger.exception("No se pudo avisar al cliente %s del rechazo", uid)
                return
            except Exception as exc:
                logger.warning("Activación incierta %s: %s", uid, exc)
                self.actualizar_chat(uid, operacion={"estado": "incierta", "fecha": ahora_iso()})
                self.registrar_resultado_activacion(
                    uid, "incierta", detalle=(f"{str(exc)[:250]}. "
                         "Revisar panel antes de /desbloquear y repetir."))
                try:
                    await self.enviar_automatico(
                        event,
                        "⚠️ ACTIVACIÓN PENDIENTE DE VERIFICACIÓN\n\n"
                        "No pudimos confirmar la respuesta del sistema. "
                        "Un asesor verificará el estado de tu licencia antes de reintentar.",
                        forzar=True, responder_a=responder_a,
                    )
                except Exception:
                    logger.exception("No se pudo avisar al cliente %s de la respuesta incierta", uid)
                return

            try:
                self.actualizar_chat(
                    uid,
                    activo=False,
                    motivo="activado_30_dias",
                    operacion={"estado": "confirmada", "fecha": ahora_iso()},
                    ultima_activacion={"dias": DIAS, "fecha": ahora_iso(), "timestamp": time.time()},
                )
            except OSError as exc:
                # API confirmó; aunque falle el disco, avisamos al cliente.
                # El registro en memoria conserva en_curso para no activar dos veces.
                logger.exception("API confirmó pero no pude persistir la activación de %s", uid)
                await self.avisar(
                    f"🚨 API ACTIVÓ ID {uid}, pero falló el guardado local: {exc}. "
                    "NO repitas la activación; revisa el historial."
                )
            # Registrar el éxito de licencia sin fabricar una venta bancaria ni
            # cancelar el pedido VOUCHERS existente.
            try:
                venta = self.venta_actual(uid)
                if venta and venta.get("producto") == "banca" and venta.get("estado") != "cancelado":
                    self.cambiar_estado_venta(
                        venta["id"], "entregado", "Activación de 30 días confirmada por la API"
                    )
                self.registrar_resultado_activacion(
                    uid, "confirmada", dias=DIAS,
                    detalle="La API /api/add_days confirmó +30 días de licencia")
            except Exception:
                logger.exception("API confirmó a %s, pero falló el historial local", uid)
                await self.avisar(
                    f"⚠️ API confirmó a {uid}, pero falló el registro de ventas. No repetir la activación.",
                    tema="activaciones")
            try:
                self.guardar_estado_cliente(uid, estado="entregado", recordatorio_pendiente=False)
            except OSError:
                logger.exception("No se pudo actualizar el estado del cliente %s", uid)
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
                logger.exception("API activó, pero falló la confirmación a %s", uid)
                await self.avisar(f"⚠️ ID {uid} activado en API, pero el mensaje al cliente falló.")
            logger.info("API confirmó %s días para ID %s; registro enviado al grupo", DIAS, uid)

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
            if not datos or len(datos) >= 6_000_000:
                raise ValueError("Imagen del QR vacía o demasiado grande")
            QR_FILE.write_bytes(datos)
        try:
            await asyncio.to_thread(descargar)
        except Exception as exc:
            logger.warning("No se pudo descargar el QR: %s", exc)

    async def mandar_qr(self, event):
        uid = str(event.chat_id)
        if QR_FILE.is_file() and QR_FILE.stat().st_size:
            try:
                await self.enviar_automatico(event, archivo=QR_FILE)
                return
            except FloodWaitError:
                raise
            except Exception as exc:
                logger.warning("No se pudo mandar foto QR a %s: %s", uid, exc)
        url = self.config.get("qr_url", "")
        if url:
            await self.enviar_automatico(event, "📲 QR de pago:\n" + url)

    async def mandar_pago(self, event, uid, *, forzar=False, producto=None, pedido_creditos=None,
                          registrar_pedido=True):
        estado = self.estado_cliente(uid)
        producto = producto or estado.get("producto") or "banca"
        if producto not in PRODUCTOS_COMPRA:
            producto = "banca"
        if producto == "creditos" and pedido_creditos is None and estado.get("producto") == "creditos":
            pedido_creditos = estado.get("pedido_creditos")
        if (not forzar and estado and estado.get("producto", "banca") == producto
                and (producto != "creditos" or estado.get("pedido_creditos") == pedido_creditos)
                and time.time() - float(estado.get("ultimo_pago") or 0) < REPETIR_PAGO_SEGUNDOS):
            return
        datos = PRODUCTOS_COMPRA[producto]
        nombre = datos["nombre"]
        if producto == "creditos":
            if pedido_creditos:
                nombre = f"{pedido_creditos['cantidad']} CRÉDITOS"
            precio = pedido_creditos.get("importe") if pedido_creditos else None
            if precio:
                precio_texto = (
                    f"💰 Importe indicado en el mensaje: <b>S/ {precio}</b>\n"
                    "⚠️ <b>Precio sujeto a confirmación por un asesor.</b>\n\n"
                )
            else:
                precio_texto = "💰 Precio: <b>pendiente de cotización</b>\n\n"
            instrucciones = (
                "⚠️ <b>Antes de pagar, espera que un asesor confirme el precio.</b>\n"
                "📸 Si ya pagaste, envía tu comprobante para revisión."
            )
        elif datos["precio"] is None:
            precio_texto = "💰 Precio: <b>por confirmar</b>\n\n"
            instrucciones = "Un asesor identificará el producto y confirmará el importe."
        else:
            precio_texto = f"💰 Precio: <b>S/ {datos['precio']}</b>\n\n"
            instrucciones = (
                "✅ Verifica el banco y el titular antes de transferir.\n"
                "📸 Después del pago, envía una imagen del comprobante. "
                "Un asesor revisará el pago antes de entregar o activar el producto."
            )
        duracion = f"📅 Duración: <b>{datos['duracion']}</b>\n" if datos["duracion"] else ""
        await self.mandar_qr(event)
        texto = (
            "🤖 <b>LOS CHUCKYS — CONFIRMACIÓN DE PEDIDO</b>\n\n"
            f"📦 Producto: <b>{nombre}</b>\n"
            f"{duracion}"
            f"{precio_texto}"
            "🏦 <b>TRANSFERENCIA BANCARIA</b>\n"
            "Número de cuenta:\n"
            f"<code>{self.config['numero_cuenta']}</code>\n\n"
            f"{instrucciones}"
        )
        enviado = await self.enviar_automatico(event, texto, html=True)
        if enviado is not None:
            emisor = await event.get_sender()
            nombre_cliente = " ".join(filter(None, [getattr(emisor, "first_name", ""),
                                                    getattr(emisor, "last_name", "")])).strip()
            venta = None
            if registrar_pedido:
                venta, _ = self.nueva_venta(uid, producto, pedido_creditos, cliente_nombre=nombre_cliente)
            # Repetir QR/precio nunca degrada el pedido tras recibir comprobante
            # ni crea un nuevo aviso de 2 minutos por una simple consulta.
            venta_previa = venta or self.venta_actual(uid)
            estado_previo = (venta_previa or {}).get("estado") or estado.get("estado")
            cambios = {"ultimo_pago": time.time()}
            if registrar_pedido and estado_previo in ("pendiente", None):
                cambios.update(
                    venta_id=venta["id"] if venta else estado.get("venta_id"),
                    estado="pendiente", producto=producto,
                    pedido_creditos=pedido_creditos if producto == "creditos" else None,
                    recordatorio_pendiente=producto != "creditos" and not estado.get("recordatorio_enviado", False),
                    recordatorio_enviado=estado.get("recordatorio_enviado", False),
                )
            elif not estado:
                cambios.update(producto=producto, estado="informacion", recordatorio_pendiente=False)
            self.guardar_estado_cliente(uid, **cambios)

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
            permitir_respuestas = self.esta_on(uid)
            try:
                emisor = await event.get_sender()
                if getattr(emisor, "bot", False):
                    return
                texto = normalizar(event.raw_text or "")
                estado = self.estado_cliente(uid)

                if es_imagen(event):
                    venta = self.venta_actual(uid)
                    vigente = bool(venta and venta.get("estado") in
                                   ("pendiente", "revision", "pagado"))
                    posible_pago = bool(re.search(
                        r"\b(comprobante|pago|pague|transferencia|deposito|voucher de pago)\b",
                        texto))
                    en_proceso = estado.get("estado") in ("pendiente", "revision")
                    if not vigente and not en_proceso and not posible_pago:
                        if permitir_respuestas:
                            await self.respuesta_corta(
                                event, "📸 Recibí tu imagen. Para comprar escribe /bot.")
                        return

                    if not vigente:
                        producto = (estado.get("producto") if en_proceso else None) or "otro"
                        if producto not in PRODUCTOS_COMPRA:
                            producto = "otro"
                        nombre_cliente = " ".join(filter(None, [
                            getattr(emisor, "first_name", ""),
                            getattr(emisor, "last_name", "")])).strip()
                        venta, _ = self.nueva_venta(
                            uid, producto, estado.get("pedido_creditos") if en_proceso else None,
                            cliente_nombre=nombre_cliente)
                    if venta.get("estado") in ("pendiente", "revision"):
                        self.cambiar_estado_venta(
                            venta["id"], "revision", "Captura recibida; pago sin verificar")
                    self.guardar_estado_cliente(
                        uid, estado="revision" if venta.get("estado") != "pagado" else "pagado",
                        venta_id=venta["id"], recordatorio_pendiente=False)
                    await self.copiar_comprobante_en_grupo(event, uid)
                    if permitir_respuestas:
                        await self.enviar_automatico(
                            event,
                            "✅ Recibí tu comprobante. Un asesor revisará el pago. "
                            "El pago todavía no está confirmado.")
                    return

                if not permitir_respuestas:
                    return

                if texto in ("/qr", "qr", "pagar", "reenviar"):
                    if time.time() - self.ultimo_qr.get(uid, 0) >= REPETIR_QR_SEGUNDOS:
                        await self.mandar_pago(event, uid, forzar=True)
                        self.ultimo_qr[uid] = time.time()
                    return

                pedido_especial = detectar_pedido_especial(texto)
                if pedido_especial:
                    producto, pedido_creditos = pedido_especial
                    await self.mandar_pago(event, uid, producto=producto, pedido_creditos=pedido_creditos)
                    return

                if quiere_menu(texto):
                    # Cada consulta explícita de precio envía QR + importe nuevamente,
                    # aunque el usuario reenvíe el mismo texto varios minutos después.
                    if quiere_precio(texto):
                        producto = "vouchers" if re.search(r"\bvouchers?\b", texto) else "banca"
                        await self.mandar_pago(event, uid, producto=producto)
                    else:
                        await self.enviar_automatico(
                            event,
                            "🤖 LOS CHUCKYS — PRECIOS\n\n"
                            "📦 BOT DE BANCA: 30 días — S/ 5\n"
                            "🎟️ VOUCHERS: 30 días — S/ 5\n"
                            "🪙 CRÉDITOS — importe a confirmar con asesor\n\n"
                            "Para recibir QR + precio, escribe: precio del bot o precio vouchers."
                        )
                    return

                if quiere_comprar(texto):
                    producto = "vouchers" if re.search(r"\bvouchers?\b", texto) else "banca"
                    await self.mandar_pago(event, uid, producto=producto)
                    return

                if estado.get("estado") == "revision":
                    await self.respuesta_corta(event, "✅ Tu comprobante sigue en revisión por un asesor.")
                    return
                if estado.get("estado") == "pendiente":
                    # No repetir QR ni molestar con cada saludo o mensaje casual.
                    return
                if estado.get("estado") in ("entregado", "pagado"):
                    return
                await self.respuesta_corta(
                    event, "👋 ¡Hola! Soy el asistente de Los Chuckys. "
                           "Escribe /bot para ver los planes o «precio del bot» para recibir el QR."
                )
            except FloodWaitError as exc:
                logger.warning("Telegram pidió esperar %s segundos", exc.seconds)
                await asyncio.sleep(min(int(exc.seconds), 3600))
            except Exception:
                logger.exception("No se pudo responder al chat %s", uid)

    async def enviar_recordatorio(self, uid):
        uid = str(uid)
        if not self.esta_on(uid):
            return
        self.enviando[uid] = self.enviando.get(uid, 0) + 1
        try:
            mensaje = await self.client.send_message(
                int(uid),
                "⏰ Han pasado 2 minutos desde que te envié el QR de pago.\n\n"
                "📸 Si ya realizaste el pago, envía la captura del comprobante "
                "para que un asesor pueda revisarlo.\n\n"
                "Si decidiste no comprar, no necesitas responder. Gracias."
            )
            self.recordar_envio(uid, mensaje)
        finally:
            restantes = self.enviando[uid] - 1
            if restantes:
                self.enviando[uid] = restantes
            else:
                self.enviando.pop(uid, None)

    async def revisar_recordatorios(self):
        while True:
            try:
                for uid in list(self.estados):
                    if not self.esta_on(uid):
                        continue
                    estado = self.estado_cliente(uid)
                    if (estado.get("estado") != "pendiente"
                            or not estado.get("recordatorio_pendiente")
                            or estado.get("recordatorio_enviado")):
                        continue
                    try:
                        transcurrido = time.time() - float(estado.get("ultimo_pago", 0))
                    except (TypeError, ValueError):
                        continue
                    if transcurrido < ESPERA_COMPROBANTE_SEGUNDOS:
                        continue
                    if transcurrido > 24 * 60 * 60:
                        self.guardar_estado_cliente(uid, recordatorio_pendiente=False)
                        continue
                    lock = self.locks_entrada.setdefault(uid, asyncio.Lock())
                    async with lock:
                        estado = self.estado_cliente(uid)
                        if (not self.esta_on(uid) or estado.get("estado") != "pendiente"
                                or not estado.get("recordatorio_pendiente")
                                or estado.get("recordatorio_enviado")):
                            continue
                        self.guardar_estado_cliente(
                            uid, recordatorio_pendiente=False,
                            recordatorio_enviado=True,
                            fecha_recordatorio=ahora_iso(),
                        )
                        try:
                            await self.enviar_recordatorio(uid)
                            logger.info("Recordatorio único después de 2 minutos para %s", uid)
                        except Exception:
                            logger.exception("No fue posible enviar el recordatorio a %s", uid)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Error revisando recordatorios")
            await asyncio.sleep(INTERVALO_REVISION_SEGUNDOS)

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
                logger.info("BOT BANCA activo con %s | S/ 5, licencia 30 días | AVISOS AL GRUPO", cuenta.first_name)
                self.tarea_recordatorios = asyncio.create_task(self.revisar_recordatorios())
                self.tarea_grupo = asyncio.create_task(self.loop_registro_ventas())
                await self.client.run_until_disconnected()
                logger.warning("Telegram desconectado; reconectando")
            except (asyncio.CancelledError, KeyboardInterrupt):
                raise
            except Exception:
                logger.exception("Error de conexión (se reintentará)")
            finally:
                if self.tarea_grupo is not None:
                    self.tarea_grupo.cancel()
                    try:
                        await self.tarea_grupo
                    except asyncio.CancelledError:
                        pass
                    self.tarea_grupo = None
                if self.tarea_recordatorios is not None:
                    self.tarea_recordatorios.cancel()
                    try:
                        await self.tarea_recordatorios
                    except asyncio.CancelledError:
                        pass
                    self.tarea_recordatorios = None
                try:
                    await self.client.disconnect()
                except Exception:
                    logger.exception("No se pudo cerrar conexión")
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
        logger.info("Programa detenido manualmente")
    except Exception as exc:
        logger.exception("No se pudo iniciar: %s", exc)
        print(f"ERROR: {exc}")
        raise SystemExit(1)
