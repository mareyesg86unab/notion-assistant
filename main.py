import nest_asyncio
import os
import json
import openai
from notion_client import Client as NotionClient
from dotenv import load_dotenv
from datetime import datetime
import dateparser
import asyncio
import logging
from apscheduler.schedulers.asyncio import AsyncIOScheduler
import time

# Importar funciones de utils.py
from utils import (
    find_task_by_title_enhanced,
    set_reminder_db,
    init_db,
    check_reminders
)

nest_asyncio.apply()

# Configuración de logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Telegram imports
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

# -----------------------------------------------------------------------------
# 1. CONFIGURACIÓN Y CONSTANTES
# -----------------------------------------------------------------------------
load_dotenv()

# --- Claves de APIs y IDs ---
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
NOTION_API_TOKEN = os.getenv("NOTION_API_TOKEN")
NOTION_DATABASE_ID = os.getenv("NOTION_DATABASE_ID")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
ASSISTANT_ID = os.getenv("ASSISTANT_ID")

# --- Configuración de Briefing Diario ---
BRIEFING_TIME = os.getenv("BRIEFING_TIME", "08:00")

# --- Verificación de variables de entorno ---
if not all([OPENAI_API_KEY, NOTION_API_TOKEN, NOTION_DATABASE_ID, TELEGRAM_TOKEN, ASSISTANT_ID]):
    logger.critical("ERROR: Faltan una o más variables de entorno (API keys o Assistant ID). El bot no puede iniciar.")
    exit()
if not TELEGRAM_CHAT_ID:
    logger.warning("ADVERTENCIA: No se ha configurado TELEGRAM_CHAT_ID. El briefing diario no funcionará.")

# --- Inicialización de Clientes ---
client = openai.OpenAI(api_key=OPENAI_API_KEY)
notion = NotionClient(auth=NOTION_API_TOKEN)

# --- Almacenamiento en memoria ---
USER_THREADS = {} # Almacena el thread_id de cada usuario {chat_id: thread_id}

# -----------------------------------------------------------------------------
# 2. FUNCIONES DE "HERRAMIENTAS" PARA EL ASISTENTE
# -----------------------------------------------------------------------------

def normalize_date(date_str: str) -> str | None:
    """Normaliza una cadena de texto a una fecha en formato YYYY-MM-DD."""
    if not date_str: return None
    settings = {'PREFER_DATES_FROM': 'future', 'DATE_ORDER': 'DMY'}
    dt = dateparser.parse(date_str, languages=["es"], settings=settings)
    return dt.strftime("%Y-%m-%d") if dt else None

def create_task_notion(title: str, category: str = None, due_date: str = None, description: str = None):
    """Crea una tarea en Notion con título, categoría, fecha y descripción."""
    logger.info(f"Tool Call: create_task_notion(title='{title}', category='{category}', due_date='{due_date}')")
    
    normalized_due_date = normalize_date(due_date)
    
    props_to_create = {
        "Nombre de tarea": {"title": [{"text": {"content": title}}]},
        "Estado": {"status": {"name": "Por hacer"}}
    }
    if category:
        props_to_create["Etiquetas"] = {"multi_select": [{"name": category}]}
    if normalized_due_date:
        props_to_create["Fecha límite"] = {"date": {"start": normalized_due_date}}
    if description:
        props_to_create["Descripción"] = {"rich_text": [{"text": {"content": description}}]}

    try:
        notion.pages.create(
            parent={"database_id": NOTION_DATABASE_ID},
            properties=props_to_create
        )
        return json.dumps({"status": "success", "message": f"Tarea '{title}' creada con éxito."})
    except Exception as e:
        logger.error(f"Error al crear la tarea en Notion: {e}")
        return json.dumps({"status": "error", "message": f"Hubo un error al crear la tarea en Notion: {e}"})

def list_tasks_notion(category: str = None, status: str = None, due_date: str = None):
    """Lista tareas de Notion, filtrando opcionalmente por categoría, estado o fecha límite."""
    logger.info(f"Tool Call: list_tasks_notion(category='{category}', status='{status}', due_date='{due_date}')")
    filters = []
    
    if category:
        filters.append({"property": "Etiquetas", "multi_select": {"contains": category}})
    if status:
        filters.append({"property": "Estado", "status": {"equals": status}})
    if due_date:
        normalized_date = normalize_date(due_date)
        if normalized_date:
            filters.append({"property": "Fecha límite", "date": {"equals": normalized_date}})

    query = {"database_id": NOTION_DATABASE_ID}
    if filters:
        query["filter"] = {"and": filters}

    try:
        response = notion.databases.query(**query)
        tasks = []
        for p in response.get("results", []):
            props = p.get("properties", {})
            title = props.get("Nombre de tarea", {}).get("title", [{}])[0].get("plain_text", "(Sin título)")
            task_status = props.get("Estado", {}).get("status", {}).get("name", "N/A")
            task_due = props.get("Fecha límite", {}).get("date", {}).get("start", "N/A")
            tasks.append({"title": title, "status": task_status, "due_date": task_due})
        
        if not tasks:
            return json.dumps({"status": "success", "data": "No se encontraron tareas con esos criterios."})
        return json.dumps({"status": "success", "data": tasks})
    except Exception as e:
        logger.error(f"Error al listar tareas de Notion: {e}")
        return json.dumps({"status": "error", "message": f"Hubo un error al listar las tareas: {e}"})

def update_task_notion(title_to_find: str, new_title: str = None, new_status: str = None, new_due_date: str = None, new_category: str = None):
    """Busca una tarea por su título y actualiza sus propiedades."""
    logger.info(f"Tool Call: update_task_notion(title_to_find='{title_to_find}', ...)")
    
    task_id, real_title, _ = find_task_by_title_enhanced(notion, NOTION_DATABASE_ID, title_to_find)
    
    if not task_id:
        return json.dumps({"status": "error", "message": f"No encontré una tarea que coincida con '{title_to_find}'."})

    props_to_update = {}
    if new_title:
        props_to_update["Nombre de tarea"] = {"title": [{"text": {"content": new_title}}]}
    if new_status:
        props_to_update["Estado"] = {"status": {"name": new_status}}
    if new_due_date:
        normalized_date = normalize_date(new_due_date)
        if normalized_date:
            props_to_update["Fecha límite"] = {"date": {"start": normalized_date}}
    if new_category:
        props_to_update["Etiquetas"] = {"multi_select": [{"name": new_category}]}

    if not props_to_update:
        return json.dumps({"status": "error", "message": "No se proporcionaron nuevos datos para actualizar."})

    try:
        notion.pages.update(page_id=task_id, properties=props_to_update)
        return json.dumps({"status": "success", "message": f"Tarea '{real_title}' actualizada correctamente."})
    except Exception as e:
        logger.error(f"Error al actualizar la tarea '{real_title}': {e}")
        return json.dumps({"status": "error", "message": f"Error al actualizar la tarea: {e}"})

def delete_task_notion(title_to_find: str):
    """Busca una tarea por su título y la archiva (elimina)."""
    logger.info(f"Tool Call: delete_task_notion(title_to_find='{title_to_find}')")
    
    task_id, real_title, _ = find_task_by_title_enhanced(notion, NOTION_DATABASE_ID, title_to_find)
    
    if not task_id:
        return json.dumps({"status": "error", "message": f"No encontré una tarea que coincida con '{title_to_find}' para eliminar."})

    try:
        notion.pages.update(page_id=task_id, archived=True)
        return json.dumps({"status": "success", "message": f"Tarea '{real_title}' archivada correctamente."})
    except Exception as e:
        logger.error(f"Error al archivar la tarea '{real_title}': {e}")
        return json.dumps({"status": "error", "message": f"Error al archivar la tarea: {e}"})

def set_reminder_notion(title_to_find: str, reminder_str: str, chat_id: int):
    """Busca una tarea, obtiene su fecha límite y establece un recordatorio."""
    logger.info(f"Tool Call: set_reminder_notion(title_to_find='{title_to_find}', reminder_str='{reminder_str}') for chat {chat_id}")
    
    task_id, real_title, _ = find_task_by_title_enhanced(notion, NOTION_DATABASE_ID, title_to_find)
    
    if not task_id:
        return json.dumps({"status": "error", "message": f"No encontré la tarea '{title_to_find}' para establecer un recordatorio."})

    try:
        page = notion.pages.retrieve(page_id=task_id)
        due_date_prop = page.get("properties", {}).get("Fecha límite", {}).get("date")
        if not due_date_prop or not due_date_prop.get("start"):
            return json.dumps({"status": "error", "message": f"La tarea '{real_title}' no tiene una fecha límite para poder crear un recordatorio."})
        
        due_date = due_date_prop["start"]
        result_message = set_reminder_db(chat_id, real_title, due_date, reminder_str)
        return json.dumps({"status": "success", "message": result_message})

    except Exception as e:
        logger.error(f"Error al configurar recordatorio para '{real_title}': {e}")
        return json.dumps({"status": "error", "message": f"Hubo un error al procesar el recordatorio: {e}"})

# -----------------------------------------------------------------------------
# 3. LÓGICA PRINCIPAL DEL ASISTENTE Y TELEGRAM
# -----------------------------------------------------------------------------

async def get_or_create_thread(chat_id):
    """Obtiene o crea un thread_id para un chat_id de usuario."""
    if chat_id not in USER_THREADS:
        logger.info(f"Creando nuevo thread para el chat_id: {chat_id}")
        try:
            thread = await asyncio.to_thread(client.beta.threads.create)
            USER_THREADS[chat_id] = thread.id
            return thread.id
        except Exception as e:
            logger.error(f"Error creando thread para {chat_id}: {e}")
            return None
    return USER_THREADS[chat_id]

async def execute_tool_call(tool_call, chat_id: int):
    """Ejecuta la función correspondiente a un tool_call del asistente."""
    func_name = tool_call.function.name
    arguments = json.loads(tool_call.function.arguments)
    
    tool_functions = {
        "create_task_notion": create_task_notion,
        "list_tasks_notion": list_tasks_notion,
        "update_task_notion": update_task_notion,
        "delete_task_notion": delete_task_notion,
        "set_reminder_notion": set_reminder_notion,
    }
    
    if func_name in tool_functions:
        function_to_call = tool_functions[func_name]
        try:
            if func_name == 'set_reminder_notion':
                arguments['chat_id'] = chat_id
                
            output = await asyncio.to_thread(function_to_call, **arguments)
            return {"tool_call_id": tool_call.id, "output": output}
        except Exception as e:
            logger.error(f"Error ejecutando la herramienta '{func_name}': {e}")
            return {"tool_call_id": tool_call.id, "output": json.dumps({"status": "error", "message": str(e)})}
    
    return {"tool_call_id": tool_call.id, "output": json.dumps({"status": "error", "message": f"Herramienta '{func_name}' desconocida."})}


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Gestiona los mensajes de texto de los usuarios usando la API de Asistentes."""
    chat_id = update.message.chat_id
    user_message = update.message.text
    
    await context.bot.send_chat_action(chat_id=chat_id, action='typing')

    thread_id = await get_or_create_thread(chat_id)
    if not thread_id:
        await update.message.reply_text("Lo siento, no pude iniciar una conversación en este momento. Inténtalo de nuevo más tarde.")
        return

    try:
        await asyncio.to_thread(
            client.beta.threads.messages.create,
            thread_id=thread_id,
            role="user",
            content=user_message
        )
    except Exception as e:
        logger.error(f"Error al añadir mensaje al thread {thread_id}: {e}")
        await update.message.reply_text("Hubo un problema al procesar tu mensaje. Por favor, inténtalo de nuevo.")
        return

    try:
        run = await asyncio.to_thread(
            client.beta.threads.runs.create,
            thread_id=thread_id,
            assistant_id=ASSISTANT_ID
        )
    except Exception as e:
        logger.error(f"Error al crear el run para el thread {thread_id}: {e}")
        await update.message.reply_text("No pude procesar tu solicitud en este momento. Inténtalo de nuevo.")
        return

    while run.status in ["queued", "in_progress"]:
        await asyncio.sleep(1)
        run = await asyncio.to_thread(client.beta.threads.runs.retrieve, thread_id=thread_id, run_id=run.id)

    if run.status == "requires_action":
        tool_calls = run.required_action.submit_tool_outputs.tool_calls
        tool_outputs = await asyncio.gather(*[execute_tool_call(tc, chat_id=chat_id) for tc in tool_calls])

        try:
            run = await asyncio.to_thread(
                client.beta.threads.runs.submit_tool_outputs,
                thread_id=thread_id,
                run_id=run.id,
                tool_outputs=tool_outputs
            )
            while run.status in ["queued", "in_progress"]:
                await asyncio.sleep(1)
                run = await asyncio.to_thread(client.beta.threads.runs.retrieve, thread_id=thread_id, run_id=run.id)
        except Exception as e:
            logger.error(f"Error al enviar tool outputs para el run {run.id}: {e}")
            await update.message.reply_text("Tuve problemas para usar mis herramientas. Inténtalo de nuevo.")
            return

    if run.status == "completed":
        try:
            messages = await asyncio.to_thread(client.beta.threads.messages.list, thread_id=thread_id)
            assistant_messages = [m for m in messages.data if m.run_id == run.id and m.role == "assistant"]
            if assistant_messages:
                response_text = assistant_messages[0].content[0].text.value
                await update.message.reply_text(response_text)
            else:
                logger.warning(f"Run {run.id} completado pero no se encontraron mensajes del asistente.")
                await update.message.reply_text("Procesé tu solicitud, pero no generé una respuesta de texto.")
        except Exception as e:
            logger.error(f"Error al obtener la respuesta del asistente para el thread {thread_id}: {e}")
            await update.message.reply_text("No pude recuperar la respuesta final. Por favor, revisa Notion para ver si tu acción se completó.")
    
    elif run.status in ["failed", "cancelled", "expired"]:
        logger.error(f"Run {run.id} falló con estado: {run.status}. Razón: {run.last_error}")
        error_message = run.last_error.message if run.last_error else "sin detalles"
        await update.message.reply_text(f"Lo siento, la operación falló ({error_message}). Por favor, intenta de nuevo.")

# -----------------------------------------------------------------------------
# 4. COMANDOS Y SCHEDULING
# -----------------------------------------------------------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Envía un mensaje de bienvenida cuando el usuario usa /start."""
    await update.message.reply_text(
        "¡Hola! Soy tu asistente de Notion. Puedes pedirme que cree, liste, actualice o elimine tareas. "
        "También puedo establecer recordatorios. ¿En qué te puedo ayudar hoy?"
    )

async def generate_and_send_briefing(application: Application, chat_id: int):
    """Genera y envía el briefing diario de tareas."""
    logger.info(f"Generando briefing diario para el chat_id: {chat_id}")
    
    tasks_today_json = list_tasks_notion(due_date="hoy")
    tasks_today_data = json.loads(tasks_today_json)
    
    message = "☕ *¡Buenos días! Tu briefing diario de Notion está listo.*\n\n"
    
    if tasks_today_data.get("status") == "success" and isinstance(tasks_today_data.get("data"), list):
        tasks = tasks_today_data["data"]
        if tasks:
            message += "*Tareas para hoy:*\n"
            for task in tasks:
                message += f"- *{task['title']}* (Estado: {task['status']})\n"
        else:
            message += "✨ No tienes tareas programadas para hoy. ¡Que tengas un día productivo!\n"
    else:
        message += "No pude recuperar las tareas de hoy desde Notion.\n"
        
    await application.bot.send_message(chat_id=chat_id, text=message, parse_mode=ParseMode.MARKDOWN)

async def scheduled_briefing(application: Application):
    """Función llamada por el scheduler para enviar el briefing."""
    if TELEGRAM_CHAT_ID:
        await generate_and_send_briefing(application, int(TELEGRAM_CHAT_ID))
    else:
        logger.warning("Scheduled briefing se ejecutó, pero no hay TELEGRAM_CHAT_ID configurado.")

async def briefing_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Envía el briefing diario a demanda."""
    chat_id = update.message.chat_id
    await context.bot.send_message(chat_id=chat_id, text="Generando tu briefing, un momento...")
    await generate_and_send_briefing(context.application, chat_id)

async def run_telegram_bot():
    """Inicializa y corre el bot de Telegram."""
    init_db()
    application = Application.builder().token(TELEGRAM_TOKEN).build()
    scheduler = AsyncIOScheduler()
    
    if BRIEFING_TIME and TELEGRAM_CHAT_ID:
        try:
            hour, minute = map(int, BRIEFING_TIME.split(':'))
            scheduler.add_job(scheduled_briefing, 'cron', hour=hour, minute=minute, args=[application])
            logger.info(f"Briefing diario programado para las {BRIEFING_TIME} en el chat {TELEGRAM_CHAT_ID}.")
        except ValueError:
            logger.error(f"El formato de BRIEFING_TIME ('{BRIEFING_TIME}') es incorrecto. Debe ser HH:MM.")

    scheduler.add_job(lambda: check_reminders(application), 'interval', minutes=1)
    scheduler.start()
    
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("briefing", briefing_command))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    logger.info("Bot iniciado. Esperando mensajes...")
    await application.run_polling()

if __name__ == "__main__":
    try:
        asyncio.run(run_telegram_bot())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot detenido manualmente.")
    except Exception as e:
        logger.critical(f"Error fatal al ejecutar el bot: {e}", exc_info=True)
