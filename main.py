from apscheduler.schedulers.asyncio import AsyncIOScheduler
import utils
import os
import json
import openai
from notion_client import Client as NotionClient
from dotenv import load_dotenv
from datetime import datetime
import dateparser
import asyncio
from difflib import get_close_matches
import nest_asyncio
import logging
from unidecode import unidecode
import string
import re

# Configuración de logging
logging.basicConfig(level=logging.INFO)

# Telegram imports (AQUÍ ESTÁ LA CORRECCIÓN)
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

# -----------------------------------------------------------------------------
# 1. CONFIGURACIÓN Y CONSTANTES
# (El resto del código es idéntico y correcto)
# -----------------------------------------------------------------------------

# Carga variables de entorno desde un archivo .env (para desarrollo local)
load_dotenv()

# Prompt mejorado para el asistente
SYSTEM_PROMPT = (
    "Eres Olivia, una asistente virtual que ayuda a los usuarios a gestionar tareas en Notion. "
    "Tu objetivo es facilitar la vida del usuario, guiándolo paso a paso y usando un lenguaje sencillo. "
    "Solo puedes usar las siguientes categorías: Estudios, Laboral, Domésticas. "
    "Si el usuario menciona una categoría no reconocida, sugiere la más cercana o pídele que elija una válida. "
    "Acepta fechas en cualquier formato (ej: 'mañana', '21-06-2025', 'el viernes') y conviértelas a formato ISO 8601 (YYYY-MM-DD). "
    "Si falta información, pregunta solo lo necesario. "
    "Antes de crear, editar o borrar una tarea, confirma con el usuario si la instrucción no es explícita. "
    "Nunca inventes etiquetas nuevas. "
    "Si el usuario comete errores de tipeo, intenta adivinar la intención y sugiere correcciones. "
    "Nunca crees una tarea nueva a menos que el usuario lo solicite claramente. Si el nombre de la tarea no coincide exactamente, sugiere la tarea más parecida y pide confirmación antes de crear una nueva."
)

# Inicialización de clientes de APIs
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
NOTION_API_TOKEN = os.getenv("NOTION_API_TOKEN")
NOTION_DATABASE_ID = os.getenv("NOTION_DATABASE_ID")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")

if not all([OPENAI_API_KEY, NOTION_API_TOKEN, NOTION_DATABASE_ID, TELEGRAM_TOKEN]):
    print("ERROR: Faltan una o más variables de entorno (API keys).")

client = openai.OpenAI(api_key=OPENAI_API_KEY)
notion = NotionClient(auth=NOTION_API_TOKEN)

# Mapeo y normalización de categorías
CATEGORY_MAP = {
    "estudio": "Estudios", "estudios": "Estudios", "academico": "Estudios",
    "académico": "Estudios", "universidad": "Estudios",
    "trabajo": "Laboral", "laboral": "Laboral", "laborales": "Laboral",
    "empleo": "Laboral", "oficio": "Laboral", "profesional": "Laboral",
    "domestica": "Domésticas", "doméstica": "Domésticas", "domesticas": "Domésticas",
    "casa": "Domésticas", "hogar": "Domésticas", "limpieza": "Domésticas",
}
VALID_CATEGORIES = sorted(list(set(CATEGORY_MAP.values())))

ORDINAL_MAP = {
    "primera": 1, "primer": 1, "1ra": 1, "1era": 1, "1er": 1, "uno": 1, "1": 1,
    "segunda": 2, "segundo": 2, "2da": 2, "2do": 2, "dos": 2, "2": 2,
    "tercera": 3, "tercer": 3, "3ra": 3, "3er": 3, "tres": 3, "3": 3,
    "cuarta": 4, "cuarto": 4, "4ta": 4, "4to": 4, "cuatro": 4, "4": 4,
    "quinta": 5, "quinto": 5, "5ta": 5, "5to": 5, "cinco": 5, "5": 5,
    "sexta": 6, "sexto": 6, "6ta": 6, "6to": 6, "seis": 6, "6": 6,
    "septima": 7, "septimo": 7, "7ma": 7, "7mo": 7, "siete": 7, "7": 7,
    "octava": 8, "octavo": 8, "8va": 8, "8vo": 8, "ocho": 8, "8": 8,
    "novena": 9, "noveno": 9, "9na": 9, "9no": 9, "nueve": 9, "9": 9,
    "decima": 10, "decimo": 10, "10ma": 10, "10mo": 10, "diez": 10, "10": 10
}

# -----------------------------------------------------------------------------
# 2. FUNCIONES AUXILIARES (Helpers)
# -----------------------------------------------------------------------------

def suggest_category(cat: str) -> str | None:
    matches = get_close_matches(cat.lower(), CATEGORY_MAP.keys(), n=1, cutoff=0.6)
    return CATEGORY_MAP[matches[0]] if matches else None

def normalize_category(cat: str) -> str | None:
    if not cat: return None
    key = cat.strip().lower()
    return CATEGORY_MAP.get(key) or suggest_category(key)

def normalize_date(date_str: str) -> str | None:
    if not date_str: return None
    settings = {'PREFER_DATES_FROM': 'future'}
    dt = dateparser.parse(date_str, languages=["es"], settings=settings)
    return dt.strftime("%Y-%m-%d") if dt else None

def normalize_title(title: str) -> str:
    if not title:
        return ""
    # Quitar tildes, minúsculas, quitar puntuación y espacios extra
    title = unidecode(title.lower())
    title = title.translate(str.maketrans('', '', string.punctuation))
    title = " ".join(title.split())
    return title

def find_task_id_by_title(title: str) -> tuple[str | None, str | None]:
        """Wrapper para la función de búsqueda mejorada."""
        return utils.find_task_by_title_enhanced(notion, NOTION_DATABASE_ID, title)
   
    try:
        norm_title = normalize_title(title)
        # Buscar coincidencia exacta (normalizada)
        all_tasks = notion.databases.query(database_id=NOTION_DATABASE_ID).get("results", [])
        task_titles = [p["properties"]["Nombre de tarea"]["title"][0]["plain_text"] for p in all_tasks if p["properties"]["Nombre de tarea"]["title"]]
        norm_task_titles = [normalize_title(t) for t in task_titles]
        if norm_title in norm_task_titles:
            idx = norm_task_titles.index(norm_title)
            return all_tasks[idx]["id"], task_titles[idx]
        # Si no hay coincidencia exacta, buscar aproximada
        matches = get_close_matches(norm_title, norm_task_titles, n=1, cutoff=0.6)
        if matches:
            idx = norm_task_titles.index(matches[0])
            return all_tasks[idx]["id"], task_titles[idx]
        return None, None
    except Exception as e:
        logging.error(f"Error en find_task_id_by_title: {e}")
        return None, None

def extract_task_index(user_input: str) -> int | None:
    """Detecta si el usuario se refiere a una tarea por posición (primera, tarea 1, etc.) y devuelve el índice (base 0)."""
    user_input = user_input.lower()
    # Buscar patrones como 'primera tarea', 'tarea 2', 'segunda tarea', etc.
    for word, idx in ORDINAL_MAP.items():
        if re.search(rf"\b{word}\b.*tarea|tarea.*\b{word}\b", user_input):
            return idx - 1  # base 0
    # Buscar 'tarea N'
    m = re.search(r"tarea\s*(\d+)", user_input)
    if m:
        return int(m.group(1)) - 1
    return None

# -----------------------------------------------------------------------------
# 3. LÓGICA DE INTERACCIÓN CON NOTION
# -----------------------------------------------------------------------------

def create_task_notion(**kwargs):
    title = kwargs.get("title")
    description = kwargs.get("description", "")
    raw_cat = kwargs.get("category", "")
    category = normalize_category(raw_cat)
    due_date = normalize_date(kwargs.get("due_date"))

    if not category:
        return {"status": "error", "message": f"La categoría '{raw_cat}' no es válida. Usa una de estas: {', '.join(VALID_CATEGORIES)}"}
    if not due_date:
        return {"status": "error", "message": f"La fecha '{kwargs.get('due_date')}' no es válida. Intenta con 'mañana', 'próximo viernes' o 'DD-MM-YYYY'."}

    # Antes de crear, verifica si existe una tarea similar
    _, similar_title = find_task_id_by_title(title)
    if similar_title:
        return {
            "status": "confirm",
            "message": f"Ya existe una tarea similar llamada '{similar_title}'. ¿Seguro que quieres crear una nueva tarea llamada '{title}'? Responde 'Sí, crear' para confirmar."
        }

    try:
        notion.pages.create(
            parent={"database_id": NOTION_DATABASE_ID},
            properties={
                "Nombre de tarea": {"title": [{"text": {"content": title}}]},
                "Etiquetas": {"multi_select": [{"name": category}]},
                "Fecha límite": {"date": {"start": due_date}},
                "Descripción": {"rich_text": [{"text": {"content": description}}]},
                "Estado": {"status": {"name": "Por hacer"}}
            }
        )
        return {"status": "success", "action": "create_task", "title": title, "category": category, "due_date": due_date}
    except Exception as e:
        logging.error(f"Error al crear la tarea en Notion: {e}")
        return {"status": "error", "message": f"Error al crear la tarea en Notion: {e}"}

def list_tasks_notion(category=None, status=None):
    filters = []
    if category:
        cat = normalize_category(category)
        if cat:
            filters.append({"property": "Etiquetas", "multi_select": {"contains": cat}})
    if status:
        filters.append({"property": "Estado", "status": {"equals": status}})

    query = {"database_id": NOTION_DATABASE_ID}
    if filters:
        query["filter"] = {"and": filters}

    try:
        response = notion.databases.query(**query)
        if not response or not isinstance(response, dict):
            return {"status": "error", "message": "No se pudo obtener respuesta válida de Notion. Verifica tu API key, permisos y el ID de la base de datos."}
        results = response.get("results", [])
        tasks = []
        for p in results:
            props = p.get("properties", {})
            title_list = props.get("Nombre de tarea", {}).get("title", [])
            title = title_list[0]["plain_text"] if title_list else "(Sin título)"
            due = props.get("Fecha límite", {}).get("date", {}).get("start", "N/A")
            status_val = props.get("Estado", {}).get("status", {}).get("name", "N/A")
            tasks.append({
                "id": p.get("id", ""),
                "title": title,
                "due": due,
                "status": status_val,
            })
        return tasks
    except Exception as e:
        return {"status": "error", "message": f"Error al listar tareas: {e}"}

def update_task_notion(task_id: str = None, title: str = None, status: str = None):
    if not task_id and title:
        task_id, real_title = find_task_id_by_title(title)
        if not task_id:
            return {"status": "error", "message": f"Tarea '{title}' no encontrada. ¿Quizás quisiste decir '{real_title}'?"} if real_title else {"status": "error", "message": f"Tarea '{title}' no encontrada."}
        title = real_title  # Usar el nombre real para feedback
    if not task_id:
        return {"status": "error", "message": "Se necesita el título o ID de la tarea para actualizarla."}
    try:
        notion.pages.update(page_id=task_id, properties={"Estado": {"status": {"name": status}}})
        return {"status": "success", "action": "update_task", "title": title or f"ID {task_id}", "new_status": status}
    except Exception as e:
        return {"status": "error", "message": f"Error al actualizar la tarea: {e}"}

def delete_task_notion(task_id: str = None, title: str = None):
    if not task_id and title:
        task_id = find_task_id_by_title(title)
        if not task_id:
            return {"status": "error", "message": f"Tarea '{title}' no encontrada."}

    if not task_id:
        return {"status": "error", "message": "Se necesita el título o ID de la tarea para eliminarla."}

    try:
        notion.pages.update(page_id=task_id, archived=True)
        return {"status": "success", "action": "delete_task", "title": title or f"ID {task_id}"}
    except Exception as e:
        return {"status": "error", "message": f"Error al eliminar la tarea: {e}"}

# -----------------------------------------------------------------------------
# 4. DEFINICIÓN DE FUNCIONES PARA OPENAI
# -----------------------------------------------------------------------------

functions = [
    {
        "name": "create_task",
        "description": "Crea una tarea nueva en Notion.",
        "parameters": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "El título de la tarea."},
                "description": {"type": "string", "description": "Una descripción opcional para la tarea."},
                "category": {"type": "string", "description": f"La categoría de la tarea. Debe ser una de: {', '.join(VALID_CATEGORIES)}"},
                "due_date": {"type": "string", "description": "La fecha de entrega, ej. 'mañana', '31 de diciembre', '25/12/2024'."}
            },
            "required": ["title", "category", "due_date"]
        }
    },
    {
        "name": "list_tasks",
        "description": "Recupera una lista de tareas, opcionalmente filtradas por categoría o estado.",
        "parameters": {
            "type": "object",
            "properties": {
                "category": {"type": "string", "description": f"Filtrar por categoría. Opciones: {', '.join(VALID_CATEGORIES)}"},
                "status": {"type": "string", "enum": ["Por hacer", "En progreso", "Hecho"], "description": "Filtrar por estado."}
            }
        }
    },
    {
        "name": "update_task",
        "description": "Actualiza el estado de una tarea existente, identificada por su título o ID.",
        "parameters": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "El ID de la tarea a actualizar."},
                "title": {"type": "string", "description": "El título de la tarea a actualizar."},
                "status": {"type": "string", "enum": ["Por hacer", "En progreso", "Hecho"], "description": "El nuevo estado de la tarea."}
            },
            "required": ["status"]
        }
    },
    {
        "name": "delete_task",
        "description": "Elimina (archiva) una tarea existente, identificada por su título o ID.",
        "parameters": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "El ID de la tarea a eliminar."},
                "title": {"type": "string", "description": "El título de la tarea a eliminar."}
            }
        }
     },
    {
        "name": "set_reminder",
        "description": "Configura un recordatorio para una tarea existente. El usuario debe especificar cuánto tiempo antes de la fecha límite.",
        "parameters": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "El título de la tarea para la cual se configura el recordatorio."},
                "reminder_str": {"type": "string", "description": "Descripción del tiempo para el recordatorio, ej: '30 minutos antes', '1 hora antes', '2 dias antes'."}
            },
            "required": ["title", "reminder_str"]
        }
    }
]

# -----------------------------------------------------------------------------
# 5. HANDLERS Y LÓGICA DEL BOT DE TELEGRAM
# -----------------------------------------------------------------------------

def add_to_history(history, role, content):
    history.append({"role": role, "content": content})
    if len(history) > 21:
        del history[1:3]

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["history"] = [{"role": "system", "content": SYSTEM_PROMPT}]
    await update.message.reply_text("¡Hola! Soy Olivia 🤖. Estoy lista para ayudarte a gestionar tus tareas en Notion. ¿Qué necesitas hacer?")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if "history" not in context.user_data:
        context.user_data["history"] = [{"role": "system", "content": SYSTEM_PROMPT}]
    
    history = context.user_data["history"]
    user_input = update.message.text
    add_to_history(history, "user", user_input)

    # Manejo de confirmación para crear tarea
    if context.user_data.get("pending_create_task"):
        pending = context.user_data.pop("pending_create_task")
        if user_input.strip().lower() in ["sí, crear", "si, crear", "crear", "sí", "si"]:
            result = create_task_notion(**pending)
            if result["status"] == "success":
                await update.message.reply_text(f"✅ ¡Tarea creada! \n<b>Título:</b> {result['title']}\n<b>Categoría:</b> {result['category']}\n<b>Fecha:</b> {result['due_date']}", parse_mode=ParseMode.HTML)
            else:
                await update.message.reply_text(f"❌ Error: {result['message']}")
        else:
            await update.message.reply_text("Operación cancelada. No se creó la tarea. Si necesitas ayuda, puedes escribir /help o ver tareas similares con '¿Qué tareas tengo?'")
        return

    # Detectar referencia a tarea por posición
    idx = extract_task_index(user_input)
    if idx is not None:
        # Obtener lista de tareas actuales
        tasks = list_tasks_notion()
        if isinstance(tasks, dict) and tasks.get("status") == "error":
            await update.message.reply_text(f"❌ Error: {tasks['message']}")
            return
        if 0 <= idx < len(tasks):
            # Reemplazar en el user_input el texto de referencia por el título real
            real_title = tasks[idx]["title"]
            # Reemplazar 'primera tarea', 'tarea 1', etc. por el título real
            user_input = re.sub(r"(primera|primer|1ra|1era|1er|uno|1|segunda|segundo|2da|2do|dos|2|tercera|tercer|3ra|3er|tres|3|cuarta|cuarto|4ta|4to|cuatro|4|quinta|quinto|5ta|5to|cinco|5|sexta|sexto|6ta|6to|seis|6|septima|septimo|7ma|7mo|siete|7|octava|octavo|8va|8vo|ocho|8|novena|noveno|9na|9no|nueve|9|decima|decimo|10ma|10mo|diez|10)\s*tarea|tarea\s*(\d+)", real_title, user_input, flags=re.IGNORECASE)
            # Actualizar el historial con el nuevo input
            history[-1]["content"] = user_input
        else:
            await update.message.reply_text(f"No hay una tarea en la posición indicada. Actualmente tienes {len(tasks)} tareas.")
            return

    try:
        response = client.chat.completions.create(
            model="gpt-4",
            messages=history,
            functions=functions,
            function_call="auto",
            temperature=0.3  # Más precisión, menos inventos
        )
        msg = response.choices[0].message

        if msg.function_call:
            add_to_history(history, "assistant", f"Ejecutando función: {msg.function_call.name}")
            fn_name = msg.function_call.name
            args = json.loads(msg.function_call.arguments)
            
            result = None
            reply_text = "Algo salió mal."

            if fn_name == "create_task":
                result = create_task_notion(**args)
                if result["status"] == "success":
                    reply_text = f"✅ ¡Tarea creada! \n<b>Título:</b> {result['title']}\n<b>Categoría:</b> {result['category']}\n<b>Fecha:</b> {result['due_date']}"
                elif result["status"] == "confirm":
                    # Guardar los datos pendientes y pedir confirmación
                    context.user_data["pending_create_task"] = args
                    reply_text = result["message"]
                else:
                    reply_text = f"❌ Error: {result['message']}"

            elif fn_name == "list_tasks":
                result = list_tasks_notion(**args)
                if isinstance(result, dict) and result.get("status") == "error":
                    reply_text = f"❌ Error: {result['message']}"
                elif not result:
                    reply_text = "No encontré tareas con esos criterios."
                else:
                    task_list_str = "Aquí están tus tareas:\n\n"
                    for task in result:
                        task_list_str += f"🔹 <b>{task['title']}</b>\n   - Estado: {task['status']}\n   - Fecha: {task['due']}\n"
                    reply_text = task_list_str

            elif fn_name == "update_task":
                result = update_task_notion(**args)
                if result["status"] == "success":
                    reply_text = f"✅ ¡Tarea actualizada! '{result['title']}' ahora está '{result['new_status']}'."
                else:
                    reply_text = f"❌ Error: {result['message']}"

            elif fn_name == "delete_task":
                result = delete_task_notion(**args)
                if result["status"] == "success":
                    reply_text = f"🗑️ ¡Tarea '{result['title']}' eliminada correctamente!"
                else:
                    reply_text = f"❌ Error: {result['message']}"
            
            else:
                reply_text = f"🤔 Función desconocida: {fn_name}"

            await update.message.reply_text(reply_text, parse_mode=ParseMode.HTML)

        else:
            add_to_history(history, "assistant", msg.content)
            await update.message.reply_text(msg.content)

    except Exception as e:
        logging.error(f"Error en handle_message: {e}")
        await update.message.reply_text("Lo siento, ocurrió un error inesperado al procesar tu solicitud.")

# -----------------------------------------------------------------------------
# 6. PUNTO DE ENTRADA DE LA APLICACIÓN
# -----------------------------------------------------------------------------

async def run_telegram_bot():
    print("Iniciando el bot de Telegram...")
    application = Application.builder().token(TELEGRAM_TOKEN).build()
    
    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    # Obtener el puerto de la variable de entorno o usar 8080 por defecto
    port = int(os.getenv("PORT", 8080))
    
    # Configurar el webhook
    webhook_url = os.getenv("WEBHOOK_URL")
    if webhook_url:
        print(f"Configurando webhook en: {webhook_url}")
        await application.bot.set_webhook(url=webhook_url)
        await application.run_webhook(
            listen="0.0.0.0",
            port=port,
            webhook_url=webhook_url
        )
    else:
        print("No se encontró WEBHOOK_URL, usando polling como fallback")
        await application.run_polling(allowed_updates=Update.ALL_TYPES)

def run_cli():
    print("🟣 Olivia iniciada en modo CLI. Escribe 'salir' para terminar.\n")
    cli_history = [{"role": "system", "content": SYSTEM_PROMPT}]
    while True:
        user_input = input("Tú: ")
        if user_input.lower().strip() in ("salir", "exit", "quit"):
            break
        print("Olivia (CLI): Lógica de CLI no implementada en esta versión.")


if __name__ == "__main__":
    mode = os.getenv("MODE", "cli").lower()
    
    if mode == "telegram":
        import asyncio
        nest_asyncio.apply()
        loop = asyncio.get_event_loop()
        loop.run_until_complete(run_telegram_bot())
    elif mode == "cli":
        run_cli()
    else:
        print(f"Modo '{mode}' no reconocido. Usa 'telegram' o 'cli'.")
