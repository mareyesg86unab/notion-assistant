import nest_asyncio
import os
import json
import openai
from notion_client import Client as NotionClient
from dotenv import load_dotenv
from datetime import datetime, timedelta
import dateparser
import asyncio
import logging
from apscheduler.schedulers.asyncio import AsyncIOScheduler
import time
import pytz
from telegram import Bot, Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
import sqlite3
import re
from unidecode import unidecode
import string
from difflib import get_close_matches

# Aplica nest_asyncio para permitir bucles de eventos anidados (necesario para apscheduler y PTB)
nest_asyncio.apply()

# -----------------------------------------------------------------------------
# 1. CONFIGURACIÓN Y CONSTANTES
# -----------------------------------------------------------------------------

# Carga las variables de entorno desde el archivo .env
load_dotenv()

# Configuración del logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# --- Claves de APIs y IDs ---
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
NOTION_API_TOKEN = os.getenv("NOTION_API_TOKEN")
NOTION_DATABASE_ID = os.getenv("NOTION_DATABASE_ID")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_TOKEN")
ASSISTANT_ID = os.getenv("ASSISTANT_ID")
TELEGRAM_CHAT_ID_BRIEFING = os.getenv("TELEGRAM_CHAT_ID") # Para el briefing proactivo
BRIEFING_TIME = os.getenv("BRIEFING_TIME", "08:00") # Hora para el briefing

# --- Verificación de variables de entorno ---
if not all([OPENAI_API_KEY, NOTION_API_TOKEN, NOTION_DATABASE_ID, TELEGRAM_BOT_TOKEN, ASSISTANT_ID]):
    logger.critical("ERROR: Faltan una o más variables de entorno (API keys o Assistant ID). El bot no puede iniciar.")
    exit()

# --- Prompt del Sistema para el Asistente de OpenAI ---
SYSTEM_PROMPT = """
Tu nombre es Olivia, una asistente personal experta en Notion y en gestión del tiempo, diseñada específicamente para ayudar a Mau. Eres proactiva, amigable, y extremadamente organizada. Tu objetivo principal es facilitar la vida de Mau, ayudándole a gestionar sus tareas laborales y personales, y a mantenerse enfocado a pesar de su TDAH.

# Perfil y Contexto del Usuario (Mau)
- **Nombre:** Mau.
- **Ubicación:** Santiago de Chile (Zona horaria: America/Santiago, UTC-4). El Asistente SIEMPRE debe considerar esta zona horaria para cualquier referencia de tiempo, fechas, recordatorios o alarmas. La hora actual debe obtenerse de esta zona horaria.
- **Profesión:** Mau es Ingeniero en Prevención de Riesgos y trabaja en la Asociación Chilena de Seguridad (ACHS).
- **Desafíos personales:** Mau tiene TDAH, lo que le dificulta la organización y la gestión del tiempo. El asistente debe ser proactivo, claro y estructurado para ayudarle.
- **Familia:** Mau tiene dos hijos, Manu y Emi, y una esposa, Camila. Algunas tareas pueden estar relacionadas con ellos.

# Personalidad y Estilo de Comunicación de Olivia
- **Tono:** Amable, profesional pero cercano, y alentador. Usa emojis sutilmente para dar calidez (ej. ✨, ✅, ☕, 🔔).
- **Claridad:** Sé directa y concisa. Resume la información y presenta las tareas en listas claras.
- **Proactividad:** No esperes siempre a que Mau pregunte. Si pide la lista de tareas, pregúntale si quiere hacer algo con alguna de ellas. Si crea una tarea, sugiérele poner una fecha límite o un recordatorio.
- **Empatía:** Reconoce sus desafíos (TDAH) y ofrécele estructura. Por ejemplo: "Sé que tienes muchas cosas en mente, Mau. ¿Qué te parece si nos enfocamos en una cosa a la vez?".
- **Manejo del Lenguaje Natural:** Entiende peticiones informales y con abreviaturas. Si Mau dice "revisar las liquidaciones", sabes que se refiere a la tarea "Revisar liquidaciones de sueldo". Usa la herramienta `find_task_by_title_enhanced` para encontrar la tarea correcta aunque el nombre no sea exacto.

# Interacción con Herramientas (Notion)
- **Confirmación:** Siempre confirma las acciones realizadas. "Listo, he creado la tarea '...' en Notion." o "He actualizado la fecha de '...'."
- **Errores:** Si una herramienta falla, informa a Mau de manera sencilla. "Hubo un problema al conectar con Notion. ¿Podrías intentarlo de nuevo en un momento?".
- **Búsqueda de tareas:** Antes de actualizar o eliminar una tarea, SIEMPRE usa la función `find_task_by_title_enhanced` para asegurarte de que has encontrado la tarea correcta. Esta es tu herramienta principal para localizar tareas.
- **Fechas:** Cuando Mau mencione fechas ("mañana", "próximo martes", "25 de dic"), normalízalas al formato YYYY-MM-DD usando la herramienta `normalize_date` antes de pasarlas a Notion.
- **Recordatorios:** Cuando Mau pida un recordatorio, usa `set_reminder_notion`. Explícale cuándo le recordarás. Ej: "Ok, te recordaré sobre '...' 2 horas antes de su vencimiento."

# Manejo de Conversaciones de Múltiples Pasos
- **Memoria a Corto Plazo:** Si haces una pregunta para obtener información necesaria para una herramienta (como pedir una fecha para `update_task_notion`), DEBES recordar el contexto. Cuando el usuario te responda, utiliza esa respuesta para completar la acción original. No vuelvas a preguntar lo que ya sabes ni intentes realizar una acción diferente.
- **Ejemplo de Flujo Correcto:**
    1. Usuario: "Ponle un recordatorio a la tarea de las vacaciones".
    2. Olivia (tú): (Llamas a `set_reminder_notion`, ves que falla por falta de fecha). "Para poner un recordatorio, la tarea 'Planificar vacaciones' necesita una fecha de vencimiento. ¿Te gustaría agregar una ahora?"
    3. Usuario: "Sí, el 30 de diciembre".
    4. Olivia (tú): (Llamas a `update_task_notion` con `new_due_date='30 de diciembre'`). "Listo, he actualizado la fecha de la tarea. Ahora, ¿cuándo quieres el recordatorio (ej. 1 día antes)?"

Tu objetivo es ser la mejor asistente que Mau podría tener, haciendo su vida más simple y organizada.
"""

# --- Inicialización de Clientes ---
client = openai.OpenAI(api_key=OPENAI_API_KEY)
notion = NotionClient(auth=NOTION_API_TOKEN)

# --- Almacenamiento en memoria para Threads de Conversación ---
USER_THREADS = {}  # {chat_id: thread_id}

# --- Constantes de la Base de Datos ---
DB_FILE = "reminders.db"

# -----------------------------------------------------------------------------
# 2. FUNCIONES DE BASE DE DATOS Y UTILIDADES
# -----------------------------------------------------------------------------

def init_db():
    """Inicializa la base de datos para recordatorios."""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS reminders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id INTEGER NOT NULL,
        task_title TEXT NOT NULL,
        remind_time TIMESTAMP NOT NULL,
        status TEXT DEFAULT 'pending'
    )
    """)
    conn.commit()
    conn.close()
    logger.info("Base de datos de recordatorios inicializada.")

def normalize_title(title: str) -> str:
    """Normaliza un título para búsqueda: minúsculas, sin tildes, sin puntuación."""
    if not title: return ""
    title = unidecode(title.lower())
    title = title.translate(str.maketrans('', '', string.punctuation))
    return " ".join(title.split())

def find_task_by_title_enhanced(title_to_find: str) -> tuple[str | None, str | None]:
    """Busca una tarea en Notion por relevancia. Devuelve (task_id, real_title)."""
    try:
        norm_title_to_find = normalize_title(title_to_find)
        if not norm_title_to_find: return None, None

        response = notion.databases.query(database_id=NOTION_DATABASE_ID)
        
        best_match = {"id": None, "title": None, "score": 0}
        search_words = set(norm_title_to_find.split())

        for page in response.get("results", []):
            title_prop = page.get("properties", {}).get("Nombre de tarea", {}).get("title", [])
            if not (title_prop and title_prop[0].get("plain_text")): continue

            real_title = title_prop[0]["plain_text"]
            norm_title = normalize_title(real_title)
            
            title_words = set(norm_title.split())
            common_words = search_words.intersection(title_words)
            keyword_score = len(common_words)
            
            similarity_score = 1 if get_close_matches(norm_title_to_find, [norm_title], n=1, cutoff=0.6) else 0
            
            total_score = (keyword_score * 2) + similarity_score

            if total_score > best_match["score"]:
                best_match = {"id": page["id"], "title": real_title, "score": total_score}

        return best_match["id"], best_match["title"]
    except Exception as e:
        logger.error(f"Error en find_task_by_title_enhanced: {e}")
        return None, None

def set_reminder_db(chat_id: int, task_title: str, due_date_str: str, reminder_str: str) -> str:
    """Parsea la petición de recordatorio y la guarda en la BD."""
    match = re.search(r"(\d+)\s*(minuto|hora|d[ií]a)s?", reminder_str, re.IGNORECASE)
    if not match:
        return "No entendí el formato del recordatorio. Prueba con '30 minutos antes', '1 hora antes', etc."

    value, unit = int(match.group(1)), match.group(2).lower().replace('í', 'i')
    delta_map = {"minuto": "minutes", "hora": "hours", "dia": "days"}
    delta = timedelta(**{delta_map[unit]: value})

    try:
        # La fecha que viene de Notion debería estar en formato ISO 8601.
        # dateparser la convertirá en un objeto datetime con zona horaria.
        due_datetime = dateparser.parse(due_date_str)
        if not due_datetime:
            raise ValueError("No se pudo interpretar la fecha de vencimiento desde Notion.")

        # Como fallback, si la fecha es "naive" (sin zona horaria), se asume que es de Santiago.
        # Esto ocurre si la tarea en Notion solo tiene una fecha pero no una hora.
        if due_datetime.tzinfo is None:
            target_timezone = pytz.timezone('America/Santiago')
            due_datetime = target_timezone.localize(due_datetime)
            # Si solo era una fecha, el recordatorio se basará en el final de ese día.
            if due_datetime.hour == 0 and due_datetime.minute == 0:
                due_datetime = due_datetime.replace(hour=23, minute=59, second=59)

        remind_time = due_datetime - delta
        # Para el mensaje al usuario, mostrar la hora en la zona local.
        local_remind_time = remind_time.astimezone(pytz.timezone('America/Santiago'))

    except (ValueError, TypeError) as e:
        logger.error(f"Error parseando fecha para recordatorio: {e}", exc_info=True)
        return "La fecha de la tarea no es válida o tiene un formato incorrecto para crear un recordatorio."

    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    # Guardamos la hora del recordatorio en UTC para que el scheduler funcione correctamente
    cursor.execute("INSERT INTO reminders (chat_id, task_title, remind_time) VALUES (?, ?, ?)",
                   (chat_id, task_title, remind_time.astimezone(pytz.utc)))
    conn.commit()
    conn.close()
    
    return f"OK. Te recordaré sobre '{task_title}' el {local_remind_time.strftime('%d de %b a las %H:%M')}."

# -----------------------------------------------------------------------------
# 3. FUNCIONES DE "HERRAMIENTAS" PARA EL ASISTENTE DE OPENAI
# -----------------------------------------------------------------------------

def normalize_date(date_str: str) -> str | None:
    """
    Normaliza una cadena de texto a una fecha (YYYY-MM-DD) o
    fecha y hora (formato ISO 8601) si se especifica una hora.
    """
    if not date_str:
        return None

    # Configuración para que dateparser entienda español y prefiera fechas futuras
    settings = {
        'PREFER_DATES_FROM': 'future',
        'DATE_ORDER': 'DMY',
        'TIMEZONE': 'America/Santiago',
        'RETURN_AS_TIMEZONE_AWARE': True
    }
    
    dt = dateparser.parse(date_str, languages=["es"], settings=settings)
    
    if not dt:
        return None

    # Heurística para ver si el usuario especificó una hora.
    time_indicators = ['a las', ':', 'am', 'pm', 'h', 'hora']
    has_time_specifier = any(indicator in date_str.lower() for indicator in time_indicators)

    # Si no se especifica hora y el resultado es medianoche, devolver solo la fecha.
    if not has_time_specifier and dt.hour == 0 and dt.minute == 0 and dt.second == 0:
        return dt.strftime("%Y-%m-%d")
    else:
        # Devolver en formato ISO 8601, que Notion entiende para fecha y hora.
        return dt.isoformat()

def create_task_notion(title: str, category: str = None, due_date: str = None, description: str = None):
    logger.info(f"Tool Call: create_task_notion('{title}')")
    props = {"Nombre de tarea": {"title": [{"text": {"content": title}}]}, "Estado": {"status": {"name": "Por hacer"}}}
    if category: props["Etiquetas"] = {"multi_select": [{"name": category}]}
    if due_date: 
        norm_date = normalize_date(due_date)
        if norm_date: props["Fecha límite"] = {"date": {"start": norm_date}}
    if description: props["Descripción"] = {"rich_text": [{"text": {"content": description}}]}
    try:
        notion.pages.create(parent={"database_id": NOTION_DATABASE_ID}, properties=props)
        return json.dumps({"status": "success", "message": f"Tarea '{title}' creada con éxito."})
    except Exception as e:
        logger.error(f"Error creando tarea en Notion: {e}", exc_info=True)
        return json.dumps({"status": "error", "message": f"Hubo un error al crear la tarea: {e}"})

def list_tasks_notion(category: str = None, status: str = None, due_date: str = None):
    logger.info("Tool Call: list_tasks_notion")
    filters = []
    if category: filters.append({"property": "Etiquetas", "multi_select": {"contains": category}})
    if status: filters.append({"property": "Estado", "status": {"equals": status}})
    if due_date:
        norm_date = normalize_date(due_date)
        if norm_date: filters.append({"property": "Fecha límite", "date": {"equals": norm_date}})
    query = {"database_id": NOTION_DATABASE_ID, "filter": {"and": filters}} if filters else {"database_id": NOTION_DATABASE_ID}
    try:
        response = notion.databases.query(**query)
        tasks = [{"title": p.get("properties", {}).get("Nombre de tarea", {}).get("title", [{}])[0].get("plain_text", "(Sin título)"),
                  "status": p.get("properties", {}).get("Estado", {}).get("status", {}).get("name", "N/A"),
                  "due_date": p.get("properties", {}).get("Fecha límite", {}).get("date", {}).get("start", "N/A")}
                 for p in response.get("results", [])]
        return json.dumps({"status": "success", "data": tasks or "No se encontraron tareas con esos criterios."})
    except Exception as e:
        logger.error(f"Error listando tareas de Notion: {e}", exc_info=True)
        return json.dumps({"status": "error", "message": f"Hubo un error al listar las tareas: {e}"})

def update_task_notion(title_to_find: str, new_title: str = None, new_status: str = None, new_due_date: str = None, new_category: str = None):
    logger.info(f"Tool Call: update_task_notion('{title_to_find}')")
    task_id, real_title = find_task_by_title_enhanced(title_to_find)
    if not task_id: return json.dumps({"status": "error", "message": f"No encontré una tarea que coincida con '{title_to_find}'."})
    props = {}
    if new_title: props["Nombre de tarea"] = {"title": [{"text": {"content": new_title}}]}
    if new_status: props["Estado"] = {"status": {"name": new_status}}
    if new_due_date:
        norm_date = normalize_date(new_due_date)
        if norm_date: props["Fecha límite"] = {"date": {"start": norm_date}}
    if new_category: props["Etiquetas"] = {"multi_select": [{"name": new_category}]}
    if not props: return json.dumps({"status": "error", "message": "No se proporcionaron nuevos datos para actualizar."})
    try:
        notion.pages.update(page_id=task_id, properties=props)
        return json.dumps({"status": "success", "message": f"Tarea '{real_title}' actualizada correctamente."})
    except Exception as e:
        logger.error(f"Error actualizando tarea en Notion: {e}", exc_info=True)
        return json.dumps({"status": "error", "message": f"Error al actualizar la tarea: {e}"})

def delete_task_notion(title_to_find: str):
    logger.info(f"Tool Call: delete_task_notion('{title_to_find}')")
    task_id, real_title = find_task_by_title_enhanced(title_to_find)
    if not task_id: return json.dumps({"status": "error", "message": f"No encontré una tarea que coincida con '{title_to_find}' para eliminar."})
    try:
        notion.pages.update(page_id=task_id, archived=True)
        return json.dumps({"status": "success", "message": f"Tarea '{real_title}' archivada correctamente."})
    except Exception as e:
        logger.error(f"Error archivando tarea en Notion: {e}", exc_info=True)
        return json.dumps({"status": "error", "message": f"Error al archivar la tarea: {e}"})

def set_reminder_notion(title_to_find: str, reminder_str: str, chat_id: int):
    logger.info(f"Tool Call: set_reminder_notion('{title_to_find}')")
    task_id, real_title = find_task_by_title_enhanced(title_to_find)
    if not task_id: return json.dumps({"status": "error", "message": f"No encontré la tarea '{title_to_find}'."})
    try:
        page = notion.pages.retrieve(page_id=task_id)
        due_date_prop = page.get("properties", {}).get("Fecha límite", {}).get("date")
        if not (due_date_prop and due_date_prop.get("start")):
            return json.dumps({"status": "error", "message": f"La tarea '{real_title}' no tiene fecha límite."})
        result_message = set_reminder_db(chat_id, real_title, due_date_prop["start"], reminder_str)
        return json.dumps({"status": "success", "message": result_message})
    except Exception as e:
        logger.error(f"Error en set_reminder_notion: {e}", exc_info=True)
        return json.dumps({"status": "error", "message": f"Hubo un error al procesar el recordatorio: {e}"})

# -----------------------------------------------------------------------------
# 4. LÓGICA PRINCIPAL DEL ASISTENTE Y TELEGRAM
# -----------------------------------------------------------------------------

async def get_or_create_thread(chat_id):
    if chat_id not in USER_THREADS:
        logger.info(f"Creando nuevo thread para el chat_id: {chat_id}")
        try:
            thread = await asyncio.to_thread(client.beta.threads.create)
            USER_THREADS[chat_id] = thread.id
            return thread.id
        except Exception as e:
            logger.error(f"Error creando thread: {e}")
            return None
    return USER_THREADS[chat_id]

async def execute_tool_call(tool_call, chat_id: int):
    """Ejecuta una función de herramienta y devuelve el resultado."""
    func_name = tool_call.function.name
    arguments = json.loads(tool_call.function.arguments)
    tool_functions = {"create_task_notion": create_task_notion, "list_tasks_notion": list_tasks_notion, 
                      "update_task_notion": update_task_notion, "delete_task_notion": delete_task_notion, 
                      "set_reminder_notion": set_reminder_notion}
    
    if func_name in tool_functions:
        if func_name == 'set_reminder_notion': arguments['chat_id'] = chat_id
        output = await asyncio.to_thread(tool_functions[func_name], **arguments)
        return {"tool_call_id": tool_call.id, "output": output}
    return {"tool_call_id": tool_call.id, "output": json.dumps({"status": "error", "message": f"Herramienta '{func_name}' desconocida."})}

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id, user_message = update.message.chat_id, update.message.text
    await context.bot.send_chat_action(chat_id=chat_id, action='typing')

    thread_id = await get_or_create_thread(chat_id)
    if not thread_id:
        await update.message.reply_text("Lo siento, no pude iniciar una conversación. Inténtalo más tarde.")
        return

    try:
        await asyncio.to_thread(client.beta.threads.messages.create, thread_id=thread_id, role="user", content=user_message)
        run = await asyncio.to_thread(client.beta.threads.runs.create, thread_id=thread_id, assistant_id=ASSISTANT_ID)

        # Bucle principal para gestionar el ciclo de vida del "run"
        while True:
            await asyncio.sleep(1.5) # Dar tiempo para que el estado cambie
            run = await asyncio.to_thread(client.beta.threads.runs.retrieve, thread_id=thread_id, run_id=run.id)

            if run.status == "completed":
                messages = await asyncio.to_thread(client.beta.threads.messages.list, thread_id=thread_id, limit=1)
                response_text = messages.data[0].content[0].text.value
                await update.message.reply_text(response_text, parse_mode=ParseMode.MARKDOWN)
                break  # Salir del bucle

            if run.status in ["queued", "in_progress"]:
                continue  # Continuar esperando

            if run.status == "requires_action":
                tool_calls = run.required_action.submit_tool_outputs.tool_calls
                tool_outputs = await asyncio.gather(*[execute_tool_call(tc, chat_id) for tc in tool_calls])
                
                # Enviar los resultados y volver al inicio del bucle para esperar el siguiente estado
                run = await asyncio.to_thread(
                    client.beta.threads.runs.submit_tool_outputs,
                    thread_id=thread_id,
                    run_id=run.id,
                    tool_outputs=tool_outputs
                )
                continue # Volver a esperar

            # Si el estado es fallido, cancelado o expirado, informar y salir.
            if run.status in ["failed", "cancelled", "expired"]:
                logger.error(f"Run {run.id} terminó con estado: {run.status}. Razón: {run.last_error}")
                error_message = run.last_error.message if run.last_error else "sin detalles"
                await update.message.reply_text(f"Lo siento, la operación falló ({error_message}).")
                break # Salir del bucle
            
    except Exception as e:
        logger.error(f"Error en handle_message para chat {chat_id}: {e}", exc_info=True)
        await update.message.reply_text("Hubo un problema inesperado al procesar tu mensaje. Inténtalo de nuevo.")

# -----------------------------------------------------------------------------
# 5. COMANDOS, SCHEDULING Y EJECUCIÓN DEL BOT
# -----------------------------------------------------------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Comando /start. Saluda al usuario."""
    await update.message.reply_text("¡Hola, Mau! Soy Olivia, tu asistente personal. ¿En qué puedo ayudarte hoy?")

async def check_reminders(bot: Bot):
    """Revisa y envía recordatorios pendientes desde la BD."""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    # Comparar con la hora actual en UTC, ya que así se guardan en la BD
    now_utc = datetime.now(pytz.utc)
    
    try:
        cursor.execute("SELECT id, chat_id, task_title FROM reminders WHERE remind_time <= ? AND status = 'pending'", (now_utc,))
        reminders = cursor.fetchall()
        for r_id, chat_id, task_title in reminders:
            message = f"🔔 **Recordatorio** 🔔\n\nNo te olvides de tu tarea: **{task_title}**"
            await bot.send_message(chat_id=chat_id, text=message, parse_mode=ParseMode.MARKDOWN)
            cursor.execute("UPDATE reminders SET status = 'sent' WHERE id = ?", (r_id,))
            conn.commit()
            logger.info(f"Recordatorio enviado para '{task_title}' al chat {chat_id}")
    except Exception as e:
        logger.error(f"Error en check_reminders: {e}", exc_info=True)
    finally:
        conn.close()

async def generate_and_send_briefing(bot: Bot, chat_id: int):
    """Genera y envía el briefing diario de tareas para hoy."""
    logger.info(f"Generando briefing diario para el chat_id: {chat_id}")
    tasks_json = list_tasks_notion(due_date="hoy")
    tasks_data = json.loads(tasks_json).get("data", [])
    message = "☕ *¡Buenos días! Tu briefing diario de Notion está listo.*\n\n"
    if isinstance(tasks_data, list) and tasks_data:
        message += "*Tareas para hoy:*\n"
        for task in tasks_data:
            message += f"- *{task['title']}* (Estado: {task['status']})\n"
    else:
        message += "✨ No tienes tareas programadas para hoy. ¡Que tengas un día productivo!\n"
    await bot.send_message(chat_id=chat_id, text=message, parse_mode=ParseMode.MARKDOWN)

async def scheduled_briefing(bot: Bot):
    """Función llamada por el scheduler para el briefing."""
    if TELEGRAM_CHAT_ID_BRIEFING:
        await generate_and_send_briefing(bot, int(TELEGRAM_CHAT_ID_BRIEFING))

async def briefing_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Comando /briefing. Envía el briefing a demanda."""
    await update.message.reply_text("Generando tu briefing, un momento...")
    await generate_and_send_briefing(context.bot, update.message.chat_id)

async def main():
    """Función principal que configura y ejecuta el bot."""
    init_db()
    
    bot = Bot(token=TELEGRAM_BOT_TOKEN)
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    # --- Handlers ---
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("briefing", briefing_command))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # --- Scheduler ---
    scheduler = AsyncIOScheduler(timezone='UTC') # El scheduler en UTC para comparar con fechas UTC de la BD
    scheduler.add_job(check_reminders, 'interval', minutes=1, args=[bot])
    if BRIEFING_TIME and TELEGRAM_CHAT_ID_BRIEFING:
        try:
            hour, minute = map(int, BRIEFING_TIME.split(':'))
            # Programar en la zona horaria local
            local_tz = pytz.timezone('America/Santiago')
            scheduler.add_job(scheduled_briefing, 'cron', hour=hour, minute=minute, timezone=local_tz, args=[bot])
            logger.info(f"Briefing diario programado a las {BRIEFING_TIME} (local) para el chat {TELEGRAM_CHAT_ID_BRIEFING}.")
        except ValueError:
            logger.error(f"Formato de BRIEFING_TIME ('{BRIEFING_TIME}') incorrecto. Usar HH:MM.")
    scheduler.start()

    logger.info("Bot y planificador iniciados. ¡Listo para recibir mensajes!")
    try:
        await application.run_polling(allowed_updates=Update.ALL_TYPES)
    finally:
        scheduler.shutdown()
        logger.info("Bot y planificador detenidos.")

if __name__ == "__main__":
    asyncio.run(main())
