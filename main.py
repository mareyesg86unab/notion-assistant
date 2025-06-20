from apscheduler.schedulers.asyncio import AsyncIOScheduler
import utils
import os
import json
import openai
from notion_client import Client as NotionClient
from dotenv import load_dotenv
from datetime import datetime, date
import dateparser
import asyncio
from difflib import get_close_matches
import nest_asyncio
import logging
from unidecode import unidecode
import string
import re
import sys

# Importar la función de búsqueda mejorada desde utils
from utils import find_task_by_title_enhanced, set_reminder_db, init_db, check_reminders

# Configuración de logging
logging.basicConfig(level=logging.INFO)

# Telegram imports (AQUÍ ESTÁ LA CORRECCIÓN)
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, CallbackQueryHandler

# -----------------------------------------------------------------------------
# 1. CONFIGURACIÓN Y CONSTANTES
# (El resto del código es idéntico y correcto)
# -----------------------------------------------------------------------------

# Carga variables de entorno desde un archivo .env (para desarrollo local)
load_dotenv()

# Prompt mejorado para el asistente
SYSTEM_PROMPT = (
    "Eres Olivia, una asistente virtual experta en productividad que gestiona tareas en Notion. "
    "Tu objetivo es interpretar la petición del usuario y traducirla a un objeto JSON. "
    "Responde SIEMPRE con un objeto JSON válido, y nada más. "
    "El objeto JSON debe tener una clave 'action' y una clave 'parameters'.\n\n"
    "ACCIONES VÁLIDAS ('action'):\n"
    "1. 'create_task': Crea una tarea. Parámetros: 'title' (str, obligatorio), 'category' (str, opcional), 'due_date' (str, opcional).\n"
    "2. 'list_tasks': Lista tareas. Parámetros opcionales: 'category' (str), 'status' (str: 'Por hacer', 'En progreso', 'Hecho'). Si no hay parámetros, asume que se listan las tareas 'Por hacer'.\n"
    "3. 'update_task': Modifica una tarea. Requiere 'title' (str, para buscar la tarea) y al menos uno de: 'new_status' (str), 'new_due_date' (str), 'new_category' (str).\n"
    "4. 'delete_task': Archiva (elimina) una tarea. Requiere 'title' (str).\n"
    "5. 'set_reminder': Establece un recordatorio. Requiere 'title' (str) y 'reminder_str' (str, ej: '1 hora antes', 'mañana a las 9am').\n"
    "6. 'unknown': Si la intención no es clara o no se puede realizar.\n\n"
    "REGLAS IMPORTANTES:\n"
    "- Normaliza las fechas a formato YYYY-MM-DD. 'Mañana' es el día siguiente a hoy.\n"
    "- Normaliza las categorías. Posibles valores para 'category': 'Estudios', 'Laboral', 'Domésticas'.\n"
    "- Para 'list_tasks', si el usuario dice 'tareas hechas' o 'completadas', el 'status' es 'Hecho'.\n\n"
    "EJEMPLO:\n"
    "Usuario: 'recuérdame revisar el informe mañana a las 10'\\n"
    'Tu respuesta JSON: {"action": "set_reminder", "parameters": {"title": "revisar el informe", "reminder_str": "mañana a las 10"}}\\n'
)

# Diccionario para gestionar conversaciones contextuales (ej. confirmaciones)
USER_CONTEXT = {}

# Inicialización de clientes de APIs
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
NOTION_API_TOKEN = os.getenv("NOTION_API_TOKEN")
NOTION_DATABASE_ID = os.getenv("NOTION_DATABASE_ID")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID") # Para el briefing diario
BRIEFING_TIME = os.getenv("BRIEFING_TIME", "08:00") # Formato HH:MM

if not all([OPENAI_API_KEY, NOTION_API_TOKEN, NOTION_DATABASE_ID, TELEGRAM_TOKEN]):
    print("ERROR: Faltan una o más variables de entorno (API keys).")
if not TELEGRAM_CHAT_ID:
    print("ADVERTENCIA: No se ha configurado TELEGRAM_CHAT_ID. El briefing diario no funcionará.")

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
    _, similar_title, _ = find_task_by_title_enhanced(notion, NOTION_DATABASE_ID, title)
    if similar_title:
        return {
            "status": "confirm_creation",
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

def edit_task_properties(task_id: str = None, title: str = None, new_status: str = None, new_due_date: str = None, new_category: str = None):
    """
    Edita una o varias propiedades de una tarea en Notion.
    Esta es una función versátil que reemplaza a la antigua `update_task_notion`.
    """
    if not task_id and title:
        task_id, real_title, search_method = find_task_by_title_enhanced(notion, NOTION_DATABASE_ID, title)
        
        if not task_id:
            return {"status": "error", "message": f"Tarea '{title}' no encontrada."}
        
        # Oportunidad de aprendizaje: si se encontró de forma ambigua
        if search_method in ["substring", "fuzzy"]:
            original_intent = {
                "action": "edit",
                "new_status": new_status,
                "new_due_date": new_due_date,
                "new_category": new_category
            }
            # Filtra las claves que son None para no guardar datos innecesarios
            original_intent = {k: v for k, v in original_intent.items() if v is not None}
            
            return {
                "status": "confirm_alias",
                "message": f"He encontrado la tarea '{real_title}'. ¿Te refieres a esa?",
                "data": {
                    "task_id": task_id,
                    "real_title": real_title,
                    "potential_alias": title,
                    "original_intent": original_intent
                }
            }
        title = real_title  # Usar el nombre real para feedback
    
    if not task_id:
        return {"status": "error", "message": "Se necesita el título o ID de la tarea para editarla."}
        
    properties_to_update = {}
    if new_status:
        properties_to_update["Estado"] = {"status": {"name": new_status}}
    if new_due_date:
        normalized_date = normalize_date(new_due_date)
        if not normalized_date:
            return {"status": "error", "message": f"La fecha '{new_due_date}' no es válida."}
        properties_to_update["Fecha límite"] = {"date": {"start": normalized_date}}
    if new_category:
        normalized_cat = normalize_category(new_category)
        if not normalized_cat:
            return {"status": "error", "message": f"La categoría '{new_category}' no es válida."}
        properties_to_update["Etiquetas"] = {"multi_select": [{"name": normalized_cat}]}
        
    if not properties_to_update:
        return {"status": "error", "message": "No has especificado qué quieres cambiar (estado, fecha o categoría)."}

    try:
        notion.pages.update(page_id=task_id, properties=properties_to_update)
        return {"status": "success", "action": "edit_task", "title": title or f"ID {task_id}", "changes": properties_to_update}
    except Exception as e:
        return {"status": "error", "message": f"Error al editar la tarea: {e}"}

def set_reminder_for_task(chat_id: int, task_id: str = None, title: str = None, reminder_str: str = None):
    """Encuentra una tarea y establece un recordatorio para ella."""
    if not task_id and title:
        task_id, real_title, search_method = find_task_by_title_enhanced(notion, NOTION_DATABASE_ID, title)
        
        if not task_id:
            return {"status": "error", "message": f"Tarea '{title}' no encontrada."}

        # Oportunidad de aprendizaje
        if search_method in ["substring", "fuzzy"]:
            return {
                "status": "confirm_alias",
                "message": f"He encontrado la tarea '{real_title}'. ¿Te refieres a esa?",
                "data": {
                    "task_id": task_id,
                    "real_title": real_title,
                    "potential_alias": title,
                    "original_intent": {"action": "set_reminder", "reminder_str": reminder_str}
                }
            }
        title = real_title
        
    if not task_id:
        return {"status": "error", "message": "Se necesita el título o ID de la tarea."}
        
    try:
        page = notion.pages.retrieve(page_id=task_id)
        due_date_prop = page.get("properties", {}).get("Fecha límite", {}).get("date")
        if not due_date_prop or not due_date_prop.get("start"):
            return {"status": "error", "message": f"No puedo crear un recordatorio para '{title}' porque no tiene una fecha límite establecida."}
        
        due_date = due_date_prop["start"]
        result_msg = set_reminder_db(chat_id, title, due_date, reminder_str)
        return {"status": "success", "message": result_msg}

    except Exception as e:
        logging.error(f"Error en set_reminder_for_task: {e}")
        return {"status": "error", "message": f"Ocurrió un error al procesar el recordatorio: {e}"}

def delete_task_notion(task_id: str = None, title: str = None):
    if not task_id and title:
        task_id, real_title, search_method = find_task_by_title_enhanced(notion, NOTION_DATABASE_ID, title)

        if not task_id:
            return {"status": "error", "message": f"Tarea '{title}' no encontrada."}

        # Oportunidad de aprendizaje
        if search_method in ["substring", "fuzzy"]:
            return {
                "status": "confirm_alias",
                "message": f"He encontrado la tarea '{real_title}'. ¿Te refieres a esa?",
                "data": {
                    "task_id": task_id,
                    "real_title": real_title,
                    "potential_alias": title,
                    "original_intent": {"action": "delete"}
                }
            }
        title = real_title # Usar el nombre real para feedback

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
# 5. TELEGRAM INTERACTIVE COMPONENTS
# -----------------------------------------------------------------------------
TASKS_PER_PAGE = 5

def create_task_keyboard(tasks: list, page: int = 0) -> InlineKeyboardMarkup:
    """Crea un teclado interactivo para la lista de tareas con paginación."""
    keyboard = []
    
    start_index = page * TASKS_PER_PAGE
    end_index = start_index + TASKS_PER_PAGE
    tasks_on_page = tasks[start_index:end_index]

    # Botones para cada tarea en la página actual
    for task in tasks_on_page:
        # El callback_data incluye la página actual para poder volver a ella después de una acción
        callback_data = f"complete_{page}_{task['id']}"
        keyboard.append([InlineKeyboardButton(f"✅ {task['title']}", callback_data=callback_data)])

    # Botones de paginación
    total_pages = (len(tasks) + TASKS_PER_PAGE - 1) // TASKS_PER_PAGE
    if total_pages > 1:
        pagination_row = []
        if page > 0:
            pagination_row.append(InlineKeyboardButton("◀️ Anterior", callback_data=f"page_{page-1}"))
        
        pagination_row.append(InlineKeyboardButton(f"Pág {page+1}/{total_pages}", callback_data="noop"))
        
        if page < total_pages - 1:
            pagination_row.append(InlineKeyboardButton("Siguiente ▶️", callback_data=f"page_{page+1}"))
        
        keyboard.append(pagination_row)
        
    return InlineKeyboardMarkup(keyboard)

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    action, task_id = query.data.split(':')

    if action == "complete_task":
        # CORRECCIÓN: Usar 'new_status' en lugar de 'status' para que coincida con la firma de la función.
        result = edit_task_properties(task_id=task_id, new_status="Hecho")
        if result.get("status") == "success":
            await query.edit_message_text(text=f"✅ ¡Tarea completada!\n\n_{result.get('title')}_", parse_mode=ParseMode.MARKDOWN)
        else:
            await query.edit_message_text(text=f"❌ Error al completar la tarea: {result.get('message')}")
            
    # Aquí se podrían añadir más acciones para otros botones (ej: 'posponer', 'editar')

# -----------------------------------------------------------------------------
# 6. LÓGICA DEL BOT DE TELEGRAM
# -----------------------------------------------------------------------------

def add_to_history(history, role, content):
    """Añade una entrada al historial de conversación."""
    history.append({"role": role, "content": content})

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("¡Hola! Soy Olivia, tu asistente para Notion. ¿En qué puedo ayudarte hoy?")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text
    chat_id = update.message.chat_id

    # 1. GESTIONAR RESPUESTAS CONTEXTUALES (CRUCIAL PARA APRENDIZAJE Y CONFIRMACIONES)
    user_context = USER_CONTEXT.get(chat_id)
    if user_context:
        last_intent = user_context.get("last_intent")
        
        # --- Flujo de confirmación de creación de tarea ---
        if last_intent == "confirm_creation":
            if user_text.lower() in ["sí, crear", "si, crear", "si", "crear"]:
                task_data = user_context["data"]
                try:
                    # Se crea la tarea sin la verificación de similitud esta vez
                    notion.pages.create(
                        parent={"database_id": NOTION_DATABASE_ID},
                        properties={
                            "Nombre de tarea": {"title": [{"text": {"content": task_data["title"]}}]},
                            "Etiquetas": {"multi_select": [{"name": task_data["category"]}]},
                            "Fecha límite": {"date": {"start": task_data["due_date"]}},
                            "Descripción": {"rich_text": [{"text": {"content": task_data.get("description", "")}}]},
                            "Estado": {"status": {"name": "Por hacer"}}
                        }
                    )
                    await update.message.reply_text(f"✅ ¡Nueva tarea creada con éxito!")
                except Exception as e:
                    await update.message.reply_text(f"❌ Error al crear la tarea: {e}")
            else:
                await update.message.reply_text("Creación de tarea cancelada.")
            USER_CONTEXT.pop(chat_id, None)
            return

        # --- Flujo de aprendizaje de alias ---
        elif last_intent == "confirm_alias":
            context_data = user_context["data"]
            original_intent = context_data["original_intent"]
            task_id = context_data["task_id"]
            real_title = context_data["real_title"]
            potential_alias = context_data["potential_alias"]

            feedback_msg = ""
            if user_text.lower() in ["si", "sí", "yes", "ok", "vale", "guárdalo"]:
                utils.add_alias(potential_alias, task_id)
                feedback_msg = f"👍 ¡Entendido! He guardado '{potential_alias}' como un atajo.\n\n"
            else:
                feedback_msg = "OK. No guardaré el atajo esta vez.\n\n"
            
            # Ejecutar la acción original que se había pausado
            result = {}
            if original_intent["action"] == "update_task":
                 result = edit_task_properties(
                    task_id=task_id, 
                    new_status=original_intent.get("new_status"),
                    new_due_date=original_intent.get("new_due_date"),
                    new_category=original_intent.get("new_category")
                )
                 if result.get("status") == "success":
                     await update.message.reply_text(f"{feedback_msg}✅ ¡Tarea '{real_title}' actualizada!")
                 else:
                     await update.message.reply_text(f"{feedback_msg}❌ Error al actualizar: {result.get('message')}")
            
            USER_CONTEXT.pop(chat_id, None)
            return

    # 2. SI NO HAY CONTEXTO, INTERPRETAR CON OPENAI
    if 'history' not in context.user_data:
        context.user_data['history'] = []

    add_to_history(context.user_data['history'], "user", user_text)

    try:
        # Llamada a OpenAI para interpretar la intención del usuario
        response = client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                *context.user_data["history"],
                # Se corrige user_input por user_text
                {"role": "user", "content": f"Basado en el historial y la nueva petición '{user_text}', determina la acción a realizar. Responde en formato JSON."}
            ],
            response_format={"type": "json_object"},
            temperature=0.1
        )
        intent_json = json.loads(response.choices[0].message.content)
        
        # Guardar la respuesta de la IA en el historial
        add_to_history(context.user_data['history'], "assistant", json.dumps(intent_json))
        
        # Limitar el historial a las últimas 10 interacciones para no exceder el límite de tokens
        context.user_data['history'] = context.user_data['history'][-10:]

        intent = intent_json.get("action", "unknown")
        params = intent_json.get("parameters", {})

        # 3. Ejecutar la acción correspondiente
        # --- CREAR TAREA ---
        if intent == "create_task":
            result = create_task_notion(
                title=params.get("title"),
                description=params.get("description"),
                category=params.get("category"),
                due_date=params.get("due_date")
            )
            if result["status"] == "success":
                await update.message.reply_text(f"✅ ¡Tarea creada! Título: {result['title']}")
                 # Preguntar por recordatorio si hay fecha
                if result.get("due_date"):
                    USER_CONTEXT[chat_id] = {"last_intent": "ask_reminder", "data": {"title": result["title"], "due_date": result["due_date"]}}
                    await update.message.reply_text("¿Quieres que te ponga un recordatorio para esta tarea? (ej: 'sí, 30 minutos antes')")
            elif result["status"] == "confirm_creation":
                await update.message.reply_text(result["message"])
                task_data = {
                    "title": params.get("title"),
                    "description": params.get("description", ""),
                    "category": normalize_category(params.get("category")),
                    "due_date": normalize_date(params.get("due_date")),
                }
                USER_CONTEXT[chat_id] = {"last_intent": "confirm_creation", "data": task_data}
            else:
                await update.message.reply_text(f"❌ Error: {result['message']}")
        
        # --- GESTIONAR RECORDATORIOS ---
        elif USER_CONTEXT.get(chat_id) and USER_CONTEXT[chat_id].get("last_intent") == "ask_reminder":
            if user_text.lower().startswith("si") or user_text.lower().startswith("sí"):
                reminder_str = user_text
                task_data = USER_CONTEXT[chat_id]["data"]
                result_msg = set_reminder_db(chat_id, task_data["title"], task_data["due_date"], reminder_str)
                await update.message.reply_text(f"👍 {result_msg}")
            else:
                await update.message.reply_text("OK, no crearé un recordatorio.")
            USER_CONTEXT.pop(chat_id, None)

        # --- LISTAR TAREAS ---
        elif intent == "list_tasks":
            tasks = list_tasks_notion(
                category=params.get("category"),
                status=params.get("status") or "Por hacer"  # Por defecto, mostrar solo las tareas por hacer
            )
            if isinstance(tasks, list):
                if not tasks:
                    await update.message.reply_text("¡Felicidades! No tienes tareas pendientes con esos criterios.")
                else:
                    LAST_TASKS_LIST[chat_id] = tasks # Guardar lista para referencia futura
                    keyboard = create_task_keyboard(tasks, page=0)
                    await update.message.reply_text("Aquí están tus tareas pendientes. ¡Puedes completarlas directamente desde aquí!", reply_markup=keyboard)
            else:
                await update.message.reply_text(f"❌ Error: {tasks.get('message', 'Error desconocido')}")

        # --- ACTUALIZAR TAREA ---
        elif intent == "update_task":
            title_to_find = params.get("title")
            new_status = params.get("new_status")
            new_due_date = params.get("new_due_date")
            new_category = params.get("new_category")
            task_index = extract_task_index(user_text)
            task_id = None
            
            # Si el usuario usa un índice (ej. "la segunda tarea")
            if task_index is not None and chat_id in LAST_TASKS_LIST:
                if 0 <= task_index < len(LAST_TASKS_LIST[chat_id]):
                    task_id = LAST_TASKS_LIST[chat_id][task_index]["id"]
                    title_to_find = LAST_TASKS_LIST[chat_id][task_index]["title"] # Para el mensaje de confirmación
            
            result = edit_task_properties(task_id=task_id, title=title_to_find, 
                                            new_status=new_status, new_due_date=new_due_date, new_category=new_category)
            
            if result["status"] == "success":
                changes_list = []
                if "Estado" in result["changes"]: changes_list.append(f"nuevo estado a '{result['changes']['Estado']['status']['name']}'")
                if "Fecha límite" in result["changes"]: changes_list.append(f"nueva fecha a '{result['changes']['Fecha límite']['date']['start']}'")
                if "Etiquetas" in result["changes"]: changes_list.append(f"nueva categoría a '{result['changes']['Etiquetas']['multi_select'][0]['name']}'")
                await update.message.reply_text(f"✅ ¡Tarea '{result['title']}' actualizada! Se estableció {', '.join(changes_list)}.")
            elif result["status"] == "confirm_alias":
                USER_CONTEXT[chat_id] = {"last_intent": "confirm_alias", "data": result["data"]}
                await update.message.reply_text(f"{result['message']}\n\n**¿Quieres que guarde '{result['data']['potential_alias']}' como un atajo para el futuro?**\n(Sí/No)", parse_mode=ParseMode.MARKDOWN)
            else:
                await update.message.reply_text(f"❌ Error: {result['message']}")

        # --- ESTABLECER RECORDATORIO ---
        elif intent == "set_reminder":
            title_to_find = params.get("title")
            reminder_str = params.get("reminder_str")
            task_index = extract_task_index(user_text)
            task_id = None

            if task_index is not None and chat_id in LAST_TASKS_LIST:
                if 0 <= task_index < len(LAST_TASKS_LIST[chat_id]):
                    task_id = LAST_TASKS_LIST[chat_id][task_index]["id"]
                    title_to_find = LAST_TASKS_LIST[chat_id][task_index]["title"]
            
            result = set_reminder_for_task(chat_id=chat_id, task_id=task_id, title=title_to_find, reminder_str=reminder_str)
            if result["status"] == "success":
                await update.message.reply_text(f"👍 {result['message']}")
            elif result["status"] == "confirm_alias":
                USER_CONTEXT[chat_id] = {"last_intent": "confirm_alias", "data": result["data"]}
                await update.message.reply_text(f"{result['message']}\n\n**¿Quieres que guarde '{result['data']['potential_alias']}' como un atajo para el futuro?**\n(Sí/No)", parse_mode=ParseMode.MARKDOWN)
            else:
                await update.message.reply_text(f"❌ Error: {result['message']}")

        # --- ELIMINAR TAREA ---
        elif intent == "delete_task":
            title_to_find = params.get("title")
            task_index = extract_task_index(user_text)
            task_id = None
            
            if task_index is not None and chat_id in LAST_TASKS_LIST:
                if 0 <= task_index < len(LAST_TASKS_LIST[chat_id]):
                    task_id = LAST_TASKS_LIST[chat_id][task_index]["id"]
                    title_to_find = LAST_TASKS_LIST[chat_id][task_index]["title"]
            
            result = delete_task_notion(task_id=task_id, title=title_to_find)
            if result["status"] == "success":
                await update.message.reply_text(f"🗑️ ¡Tarea '{result['title']}' archivada con éxito!")
            elif result["status"] == "confirm_alias":
                USER_CONTEXT[chat_id] = {"last_intent": "confirm_alias", "data": result["data"]}
                await update.message.reply_text(f"{result['message']}\n\n**¿Quieres que guarde '{result['data']['potential_alias']}' como un atajo para el futuro?**\n(Sí/No)", parse_mode=ParseMode.MARKDOWN)
            else:
                await update.message.reply_text(f"❌ Error: {result['message']}")
            
        # --- INTENCIÓN DESCONOCIDA ---
        else:
            await update.message.reply_text("No estoy segura de cómo ayudarte con eso. Puedo crear, listar, actualizar o borrar tareas.")

    except Exception as e:
        logging.error(f"Error al procesar el mensaje: {e}")
        await update.message.reply_text("Lo siento, no pude procesar tu solicitud. Intenta de nuevo más tarde.")

# -----------------------------------------------------------------------------
# 7. TAREAS PROGRAMADAS Y COMANDOS ESPECIALES
# -----------------------------------------------------------------------------

async def generate_and_send_briefing(application: Application, chat_id: int):
    """Función central que genera y envía el briefing."""
    logging.info(f"Generando briefing para el chat_id: {chat_id}")
    tasks = list_tasks_notion(status="Por hacer")
    if isinstance(tasks, dict) and tasks.get("status") == "error":
        await application.bot.send_message(chat_id=chat_id, text=f"Error al obtener tareas para el briefing: {tasks['message']}")
        return
    if not tasks:
        await application.bot.send_message(chat_id=chat_id, text="¡Buenos días! No tienes tareas pendientes para hoy. ¡Aprovecha el día! ☀️")
        return

    task_list_str = "\n".join([f"- {task['title']}" for task in tasks])
    
    try:
        response = client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[
                {"role": "system", "content": "Eres un asistente de productividad. Resume la siguiente lista de tareas para hoy en un solo párrafo conciso y motivador. Responde en formato JSON con una clave 'briefing'."},
                {"role": "user", "content": f"Tareas para hoy ({date.today().strftime('%d de %B')}): {task_list_str}"}
            ],
            response_format={"type": "json_object"},
        )
        briefing_text = json.loads(response.choices[0].message.content).get("briefing", "No se pudo generar el resumen de hoy.")
        message = f"¡Buenos días! ☀️ Aquí tienes tu briefing para hoy:\n\n{briefing_text}"
        await application.bot.send_message(chat_id=chat_id, text=message)
    except Exception as e:
        logging.error(f"Error al generar briefing con OpenAI: {e}")
        await application.bot.send_message(chat_id=chat_id, text="Tuve problemas para generar el resumen de hoy, pero estas son tus tareas pendientes:\n\n" + task_list_str)

async def scheduled_briefing(context: ContextTypes.DEFAULT_TYPE):
    """Tarea programada que llama a la función de briefing."""
    application = context.application
    await generate_and_send_briefing(application, TELEGRAM_CHAT_ID)

async def briefing_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Manejador para el comando /briefing."""
    await update.message.reply_text("Generando tu briefing, un momento por favor...")
    await generate_and_send_briefing(context.application, update.effective_chat.id)

# -----------------------------------------------------------------------------
# 8. INICIO DEL BOT
# -----------------------------------------------------------------------------
async def run_telegram_bot():
    """Configura y corre el bot de Telegram."""
    init_db()

    # Configura el scheduler para los recordatorios y el briefing
    scheduler = AsyncIOScheduler(timezone="America/Santiago")
    scheduler.add_job(check_reminders, "interval", minutes=1)
    
    # Tarea programada para el briefing diario
    if BRIEFING_TIME and TELEGRAM_CHAT_ID:
        try:
            hour, minute = map(int, BRIEFING_TIME.split(':'))
            scheduler.add_job(scheduled_briefing, "cron", hour=hour, minute=minute)
            logging.info(f"Briefing diario programado para las {BRIEFING_TIME} todos los días.")
        except ValueError:
            logging.error("Formato de BRIEFING_TIME incorrecto. Debe ser HH:MM.")

    # Crea la aplicación de Telegram
    application = Application.builder().token(TELEGRAM_TOKEN).build()
    
    # Handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("briefing", briefing_command)) # Handler para /briefing
    application.add_handler(CallbackQueryHandler(button_handler))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    # Inicia el scheduler
    scheduler.start()

def run_cli():
    """Ejecuta el asistente en modo línea de comandos para pruebas."""
    print("Modo CLI activado. Escribe 'salir' para terminar.")
    while True:
        user_input = input("Tú: ")
        if user_input.lower() == 'salir':
            break
        # Aquí iría la lógica para procesar el input en modo CLI (simplificado)
        # Esto requeriría adaptar `handle_message` para que no dependa de `update` y `context`
        print("Olivia: (Lógica CLI no implementada en este ejemplo)")

if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == 'cli':
        run_cli()
    else:
        asyncio.run(run_telegram_bot())
