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
    "Eres Olivia, una asistente virtual experta en productividad que ayuda a los usuarios a gestionar tareas en Notion. "
    "Tu objetivo es facilitar la vida del usuario, anticipando sus necesidades para ofrecer siempre la forma más rápida y eficiente de completar una acción. "
    "Tu comunicación debe ser clara, concisa y profesional, como la de un asistente ejecutivo de alto nivel. "
    "Puedes realizar las siguientes acciones: "
    "1. Crear nuevas tareas. "
    "2. Listar tareas (con filtros por categoría o estado). "
    "3. Modificar tareas existentes: puedes cambiar su estado (Por hacer, En progreso, Hecho), su fecha límite o su categoría. "
    "4. Establecer recordatorios para tareas existentes (ej: 'recuérdame la tarea X 1 hora antes'). "
    "5. Eliminar (archivar) tareas. "
    "Acepta fechas en cualquier formato (ej: 'mañana', '21-06-2025') y conviértelas a YYYY-MM-DD. "
    "Si falta información, pregunta solo lo necesario. "
    "Si el usuario comete errores de tipeo o usa un nombre de tarea ambiguo, sugiere la tarea más parecida y pide confirmación antes de actuar. "
    "Si un nombre corto se usa repetidamente para una tarea, ofrécete a guardarlo como un atajo (alias)."
)

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
    """Maneja los clics en los botones del teclado interactivo."""
    query = update.callback_query
    await query.answer()  # Confirmar la recepción del clic

    chat_id = query.message.chat_id
    action, *params = query.data.split('_')

    if action == "noop":
        return

    tasks = LAST_TASKS_LIST.get(chat_id, [])
    if not tasks:
        await query.edit_message_text("Parece que tu lista de tareas ha expirado. Por favor, vuelve a listarlas.")
        return

    if action == "page":
        page = int(params[0])
        keyboard = create_task_keyboard(tasks, page)
        await query.edit_message_text(text="Aquí están tus tareas:", reply_markup=keyboard)

    elif action == "complete":
        page = int(params[0])
        task_id = params[1]
        
        task_title = next((task['title'] for task in tasks if task['id'] == task_id), 'la tarea')
        
        result = edit_task_properties(task_id=task_id, status="Hecho")
        
        if result.get("status") == "success":
            # Actualizar la lista en memoria
            updated_tasks = [task for task in tasks if task['id'] != task_id]
            LAST_TASKS_LIST[chat_id] = updated_tasks
            
            # Recalcular la página actual por si era la última tarea de la página
            total_pages = (len(updated_tasks) + TASKS_PER_PAGE - 1) // TASKS_PER_PAGE
            current_page = min(page, total_pages - 1)
            
            if not updated_tasks:
                await query.edit_message_text(text=f"✅ ¡Excelente! Has completado '{task_title}'.\n\n¡No quedan más tareas pendientes en esta lista!")
            else:
                keyboard = create_task_keyboard(updated_tasks, current_page)
                await query.edit_message_text(text=f"✅ ¡Bien hecho! Has completado '{task_title}'.\n\nAquí está tu lista actualizada:", reply_markup=keyboard)
        else:
            await query.message.reply_text(f"❌ No pude actualizar la tarea: {result.get('message', 'Error desconocido')}")

# -----------------------------------------------------------------------------
# 6. LÓGICA DEL BOT DE TELEGRAM
# -----------------------------------------------------------------------------
USER_CONTEXT = {}  # {chat_id: {"last_intent": "...", "data": {...}}}
LAST_TASKS_LIST = {} # {chat_id: [lista de tareas]}

def add_to_history(history, role, content):
    """Añade una entrada al historial de conversación."""
    history.append({"role": role, "content": content})

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("¡Hola! Soy Olivia, tu asistente para Notion. ¿En qué puedo ayudarte hoy?")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Manejador principal de mensajes."""
    chat_id = update.effective_chat.id
    user_input = update.message.text
    
    # 1. Gestionar respuestas de confirmación contextual
    user_context = USER_CONTEXT.get(chat_id)
    
    # -- Flujo de confirmación para crear tarea (cuando hay una similar) --
    if user_context and user_context.get("last_intent") == "confirm_creation":
        if user_input.lower() in ["sí, crear", "si, crear", "si", "crear"]:
            task_data = user_context["data"]
            # Forzamos la creación sin verificar similitud esta vez
            try:
                notion.pages.create(
                    parent={"database_id": NOTION_DATABASE_ID},
                    properties={
                        "Nombre de tarea": {"title": [{"text": {"content": task_data["title"]}}]},
                        "Etiquetas": {"multi_select": [{"name": task_data["category"]}]},
                        "Fecha límite": {"date": {"start": task_data["due_date"]}},
                        "Descripción": {"rich_text": [{"text": {"content": task_data["description"]}}]},
                        "Estado": {"status": {"name": "Por hacer"}}
                    }
                )
                await update.message.reply_text(f"✅ ¡Nueva tarea creada con éxito!\n\n*Título:* {task_data['title']}\n*Categoría:* {task_data['category']}\n*Fecha:* {task_data['due_date']}", parse_mode=ParseMode.MARKDOWN)
            except Exception as e:
                await update.message.reply_text(f"❌ Error al crear la tarea: {e}")
            USER_CONTEXT.pop(chat_id, None)
            return
        else:
            await update.message.reply_text("Creación de tarea cancelada.")
            USER_CONTEXT.pop(chat_id, None)
            return

    # -- Flujo de aprendizaje de alias --
    elif user_context and user_context.get("last_intent") == "confirm_alias":
        context_data = user_context["data"]
        original_intent = context_data["original_intent"]
        task_id = context_data["task_id"]
        real_title = context_data["real_title"]
        potential_alias = context_data["potential_alias"]
        
        # Si el usuario acepta, guardar el alias
        if user_input.lower() in ["si", "sí", "yes", "ok", "vale", "guárdalo"]:
            utils.add_alias(potential_alias, task_id)
            feedback_msg = f"👍 ¡Entendido! He guardado '{potential_alias}' como un atajo.\n\n"
        else:
            feedback_msg = "OK. No guardaré el atajo esta vez.\n\n"

        # Ejecutar la acción original
        result = {}
        if original_intent["action"] == "edit":
            result = edit_task_properties(task_id=task_id, title=real_title, 
                                          new_status=original_intent.get("new_status"),
                                          new_due_date=original_intent.get("new_due_date"),
                                          new_category=original_intent.get("new_category"))
            if result.get("status") == "success":
                # Crear un mensaje de confirmación más detallado
                changes_list = []
                if "Estado" in result["changes"]: changes_list.append(f"nuevo estado a '{result['changes']['Estado']['status']['name']}'")
                if "Fecha límite" in result["changes"]: changes_list.append(f"nueva fecha a '{result['changes']['Fecha límite']['date']['start']}'")
                if "Etiquetas" in result["changes"]: changes_list.append(f"nueva categoría a '{result['changes']['Etiquetas']['multi_select'][0]['name']}'")
                
                await update.message.reply_text(f"{feedback_msg}✅ ¡Tarea '{real_title}' actualizada! Se estableció {', '.join(changes_list)}.")

        elif original_intent["action"] == "delete":
            result = delete_task_notion(task_id=task_id, title=real_title)
            if result.get("status") == "success":
                await update.message.reply_text(f"{feedback_msg}🗑️ ¡Tarea '{real_title}' archivada con éxito!")
        
        elif original_intent["action"] == "set_reminder":
            result = set_reminder_for_task(chat_id=chat_id, task_id=task_id, title=real_title, reminder_str=original_intent.get("reminder_str"))
            if result.get("status") == "success":
                await update.message.reply_text(f"{feedback_msg}👍 {result['message']}")

        if result.get("status") != "success":
            await update.message.reply_text(f"❌ Vaya, algo salió mal al ejecutar la acción original: {result.get('message')}")
            
        USER_CONTEXT.pop(chat_id, None)
        return

    # 2. Interpretar la intención del usuario con OpenAI
    history = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_input}
    ]
    
    try:
        response = client.chat.completions.create(
            model="gpt-4-turbo-preview",
            messages=history,
            response_format={"type": "json_object"}
        )
        intent_json = json.loads(response.choices[0].message.content)
        intent = intent_json.get("intent", "unknown")
    except Exception as e:
        logging.error(f"Error al llamar a OpenAI: {e}")
        await update.message.reply_text("Lo siento, no pude procesar tu solicitud. Intenta de nuevo.")
        return

    # 3. Ejecutar la acción correspondiente
    # --- CREAR TAREA ---
    if intent == "create_task":
        result = create_task_notion(
            title=intent_json.get("title"),
            description=intent_json.get("description"),
            category=intent_json.get("category"),
            due_date=intent_json.get("due_date")
        )
        if result["status"] == "success":
            await update.message.reply_text(f"✅ ¡Tarea creada! Título: {result['title']}")
             # Preguntar por recordatorio si hay fecha
            if result.get("due_date"):
                USER_CONTEXT[chat_id] = {"last_intent": "ask_reminder", "data": {"title": result["title"], "due_date": result["due_date"]}}
                await update.message.reply_text("¿Quieres que te ponga un recordatorio para esta tarea? (ej: 'sí, 30 minutos antes')")
        elif result["status"] == "confirm_creation":
            # Guardar contexto para la confirmación
            task_data = {
                "title": intent_json.get("title"),
                "description": intent_json.get("description", ""),
                "category": normalize_category(intent_json.get("category")),
                "due_date": normalize_date(intent_json.get("due_date")),
            }
            USER_CONTEXT[chat_id] = {"last_intent": "confirm_creation", "data": task_data}
            await update.message.reply_text(result["message"])
        else:
            await update.message.reply_text(f"❌ Error: {result['message']}")
    
    # --- GESTIONAR RECORDATORIOS ---
    elif user_context and user_context.get("last_intent") == "ask_reminder":
        if user_input.lower().startswith("si") or user_input.lower().startswith("sí"):
            reminder_str = user_input
            task_data = user_context["data"]
            result_msg = set_reminder_db(chat_id, task_data["title"], task_data["due_date"], reminder_str)
            await update.message.reply_text(f"👍 {result_msg}")
        else:
            await update.message.reply_text("OK, no crearé un recordatorio.")
        USER_CONTEXT.pop(chat_id, None)

    # --- LISTAR TAREAS ---
    elif intent == "list_tasks":
        tasks = list_tasks_notion(
            category=intent_json.get("category"),
            status=intent_json.get("status") or "Por hacer"  # Por defecto, mostrar solo las tareas por hacer
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
        title_to_find = intent_json.get("title")
        new_status = intent_json.get("new_status")
        new_due_date = intent_json.get("new_due_date")
        new_category = intent_json.get("new_category")
        task_index = extract_task_index(user_input)
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
            await update.message.reply_text(f"{result['message']}\n\n**¿Quieres que guarde '{result['data']['potential_alias']}' como un atajo para el futuro?** (Sí/No)")
        else:
            await update.message.reply_text(f"❌ Error: {result['message']}")

    # --- ESTABLECER RECORDATORIO ---
    elif intent == "set_reminder":
        title_to_find = intent_json.get("title")
        reminder_str = intent_json.get("reminder_str")
        task_index = extract_task_index(user_input)
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
            await update.message.reply_text(f"{result['message']}\n\n**¿Quieres que guarde '{result['data']['potential_alias']}' como un atajo para el futuro?** (Sí/No)")
        else:
            await update.message.reply_text(f"❌ Error: {result['message']}")

    # --- ELIMINAR TAREA ---
    elif intent == "delete_task":
        title_to_find = intent_json.get("title")
        task_index = extract_task_index(user_input)
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
            await update.message.reply_text(f"{result['message']}\n\n**¿Quieres que guarde '{result['data']['potential_alias']}' como un atajo para el futuro?** (Sí/No)")
        else:
            await update.message.reply_text(f"❌ Error: {result['message']}")
            
    # --- INTENCIÓN DESCONOCIDA ---
    else:
        await update.message.reply_text("No estoy segura de cómo ayudarte con eso. Puedo crear, listar, actualizar o borrar tareas.")

# -----------------------------------------------------------------------------
# 7. TAREAS PROGRAMADAS
# -----------------------------------------------------------------------------
async def send_daily_briefing(application: Application):
    """Prepara y envía un resumen diario de las tareas del día."""
    if not TELEGRAM_CHAT_ID:
        logging.warning("No se puede enviar el briefing diario: TELEGRAM_CHAT_ID no está configurado.")
        return
    
    logging.info("Ejecutando briefing diario...")
    
    today_str = date.today().isoformat()
    all_tasks = list_tasks_notion(status="Por hacer") # Obtener todas las tareas pendientes
    
    if isinstance(all_tasks, dict) and all_tasks.get("status") == "error":
        logging.error(f"Error al obtener tareas para el briefing: {all_tasks['message']}")
        return
        
    tasks_for_today = [task for task in all_tasks if task.get("due") and task["due"] == today_str]
    
    if not tasks_for_today:
        message = "¡Buenos días! ☀️ No he encontrado tareas programadas para hoy. ¡Que tengas un día despejado y productivo!"
        await application.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=message)
        return
        
    try:
        task_list_str = "\n".join([f"- {task['title']}" for task in tasks_for_today])
        
        briefing_prompt = (
            "Eres un asistente ejecutivo de élite. A continuación se presenta una lista de tareas para hoy. "
            "Tu trabajo es redactar un resumen matutino para tu jefe. Sé breve, profesional y motivador. "
            "Destaca la tarea que parezca más importante o urgente. No uses más de 70 palabras."
            f"\n\nTareas de hoy:\n{task_list_str}"
        )
        
        response = client.chat.completions.create(
            model="gpt-4-turbo-preview",
            messages=[{"role": "system", "content": briefing_prompt}],
            temperature=0.5,
        )
        
        summary = response.choices[0].message.content
        message = f"¡Buenos días! ☀️ Aquí tienes tu briefing para hoy:\n\n{summary}"
        
        await application.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=message)
        logging.info("Briefing diario enviado con éxito.")
        
    except Exception as e:
        logging.error(f"Error al generar o enviar el briefing diario: {e}")

async def run_telegram_bot():
    """Inicializa y corre el bot de Telegram."""
    nest_asyncio.apply()
    
    # Inicializar la BD de recordatorios
    init_db()

    # Configurar el scheduler para recordatorios y briefings
    scheduler = AsyncIOScheduler()
    
    application = Application.builder().token(TELEGRAM_TOKEN).build()

    # Job para recordatorios (cada minuto)
    scheduler.add_job(check_reminders, 'interval', minutes=1, args=[application])
    
    # Job para el briefing diario
    if TELEGRAM_CHAT_ID:
        try:
            hour, minute = map(int, BRIEFING_TIME.split(':'))
            scheduler.add_job(send_daily_briefing, 'cron', hour=hour, minute=minute, args=[application])
            logging.info(f"Briefing diario programado para las {BRIEFING_TIME} todos los días.")
        except ValueError:
            logging.error(f"El formato de BRIEFING_TIME ('{BRIEFING_TIME}') no es válido. Debe ser HH:MM.")

    # Handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    application.add_handler(CallbackQueryHandler(button_handler))

    logging.info("Bot de Telegram iniciado...")
    try:
        await application.run_polling()
    finally:
        scheduler.shutdown()

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
