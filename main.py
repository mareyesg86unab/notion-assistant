import nest_asyncio
import string
import re
import sys
import os
import json
import openai
from notion_client import Client as NotionClient
from dotenv import load_dotenv
from datetime import datetime, date
import dateparser
import asyncio
from difflib import get_close_matches
import logging
from unidecode import unidecode
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# Importar la función de búsqueda mejorada desde utils
from utils import find_task_by_title_enhanced, set_reminder_db, init_db, check_reminders

# Aplicar el parche para permitir bucles de eventos anidados.
# Esto es CRUCIAL para que APScheduler y python-telegram-bot coexistan.
nest_asyncio.apply()

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

# Almacenamiento en memoria para las listas de tareas paginadas
LAST_TASKS_LIST = {}

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
        return {"status": "success", "action": "edit", "title": title, "changes": properties_to_update}
    except Exception as e:
        logging.error(f"Error al editar la tarea en Notion: {e}")
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
        return {"status": "error", "message": f"Error al archivar la tarea: {e}"}

# -----------------------------------------------------------------------------
# 4. COMPONENTES DE LA INTERFAZ DE TELEGRAM (TECLADOS)
# -----------------------------------------------------------------------------
TASKS_PER_PAGE = 5

def create_task_keyboard(tasks: list, page: int = 0) -> InlineKeyboardMarkup:
    keyboard = []
    start_index = page * TASKS_PER_PAGE
    end_index = start_index + TASKS_PER_PAGE
    
    # Botones para cada tarea en la página actual
    for task in tasks[start_index:end_index]:
        # El callback_data ahora incluye la página actual para poder volver a ella
        button = [InlineKeyboardButton(f"✅ {task['title']}", callback_data=f"complete_{page}_{task['id']}")]
        keyboard.append(button)
        
    # Botones de paginación
    pagination_buttons = []
    if page > 0:
        pagination_buttons.append(InlineKeyboardButton("⬅️ Anterior", callback_data=f"page_{page-1}"))
    if end_index < len(tasks):
        pagination_buttons.append(InlineKeyboardButton("Siguiente ➡️", callback_data=f"page_{page+1}"))
        
    if pagination_buttons:
        keyboard.append(pagination_buttons)
        
    return InlineKeyboardMarkup(keyboard)

# -----------------------------------------------------------------------------
# 5. MANEJADORES DE TELEGRAM (HANDLERS)
# -----------------------------------------------------------------------------

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Maneja los clics en los botones del teclado interactivo."""
    query = update.callback_query
    await query.answer()

    chat_id = query.message.chat_id
    action, *params = query.data.split('_')
    
    # Recuperar la lista de tareas de la memoria del bot
    tasks = LAST_TASKS_LIST.get(chat_id, [])
    if not tasks:
        await query.edit_message_text("Esta lista de tareas ha expirado. Por favor, pídemela de nuevo con 'listar tareas'.")
        return

    if action == "page":
        page = int(params[0])
        keyboard = create_task_keyboard(tasks, page)
        await query.edit_message_text(text="Aquí están tus tareas pendientes:", reply_markup=keyboard)

    elif action == "complete":
        page = int(params[0])
        task_id = params[1]
        
        task_title = next((task['title'] for task in tasks if task['id'] == task_id), 'la tarea')
        
        result = edit_task_properties(task_id=task_id, new_status="Hecho")
        
        if result.get("status") == "success":
            # Actualizar la lista en memoria eliminando la tarea completada
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
            # Si hay un error, no editar el mensaje, sino enviar uno nuevo para no perder el teclado
            await query.message.reply_text(f"❌ No pude actualizar la tarea: {result.get('message', 'Error desconocido')}")

def add_to_history(history, role, content):
    """Añade una entrada al historial de conversación."""
    history.append({"role": role, "content": content})

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"¡Hola! Soy Olivia, tu asistente para Notion. ¿En qué puedo ayudarte hoy?")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Manejador principal de mensajes. Determina si el mensaje es una respuesta
    a una pregunta anterior (contexto) o una nueva instrucción.
    """
    user_id = update.effective_user.id
    user_input = update.message.text
    chat_id = update.message.chat_id

    # --- 1. GESTIÓN DE CONVERSACIONES CON CONTEXTO ---
    # Si el bot está esperando una respuesta del usuario a una pregunta específica
    if user_id in USER_CONTEXT:
        context_type, data = USER_CONTEXT[user_id]
        
        # Eliminar el contexto para que el siguiente mensaje sea tratado como una nueva instrucción
        del USER_CONTEXT[user_id]

        # ---- Contexto: Confirmar la creación de una tarea duplicada ----
        if context_type == 'confirm_creation':
            # El usuario debe confirmar explícitamente para evitar duplicados
            if re.search(r"\bsi\b|s[ií], crear|crear", user_input, re.IGNORECASE):
                # Reintentar la creación, esta vez forzándola
                await create_task_handler(context, chat_id, force_create=True, **data['task_info'])
            else:
                await update.message.reply_text("Ok, cancelo la creación de la tarea.")

        # ---- Contexto: Confirmar si se guarda un alias para una tarea ----
        elif context_type == 'confirm_alias':
            normalized_input = normalize_title(user_input)
            if 'si' in normalized_input.split():
                # El usuario dijo sí, guardamos el alias
                task_id = data['task_id']
                alias_text = data['alias_text']
                real_title = data['real_title']
                
                add_alias(alias_text, task_id)
                await update.message.reply_text(f"¡Hecho! He guardado '{alias_text}' como un atajo para '{real_title}'.")
                
                # Ahora que el alias está guardado, procedemos con la acción original (ej: completar la tarea)
                original_intent = data['original_intent']
                await edit_task_properties_handler(context, chat_id, task_id=task_id, **original_intent)
            else:
                # El usuario dijo no (o cualquier otra cosa), no guardamos el alias pero sí completamos la acción
                await update.message.reply_text("Entendido. No guardaré el atajo, pero procederé con la acción solicitada.")
                original_intent = data['original_intent']
                task_id = data['task_id']
                await edit_task_properties_handler(context, chat_id, task_id=task_id, **original_intent)
        
        return # Importante: terminamos el procesamiento aquí

    # --- 2. GESTIÓN DE NUEVAS INSTRUCCIONES (SIN CONTEXTO) ---
    # Si no hay contexto, usamos OpenAI para interpretar la instrucción
    try:
        # Extraer índice de tarea si el usuario usa "la primera", "tarea 2", etc.
        idx = extract_task_index(user_input)
        if idx is not None and user_id in LAST_TASKS_LIST:
            tasks = LAST_TASKS_LIST[user_id]
            if 0 <= idx < len(tasks):
                task = tasks[idx]
                user_input = user_input.replace(user_input.split()[0], "").strip()
                user_input += f" sobre la tarea '{task['title']}'"

        response_json = get_openai_response(user_input, chat_id)
        action = response_json.get("action")
        params = response_json.get("parameters", {})

        # Seleccionar el manejador de acción adecuado
        if action == "create_task":
            await create_task_handler(context, chat_id, **params)
        elif action == "list_tasks":
            await list_tasks_handler(context, chat_id, **params)
        elif action == "update_task":
            await edit_task_properties_handler(context, chat_id, **params)
        elif action == "delete_task":
            await delete_task_handler(context, chat_id, **params)
        elif action == "set_reminder":
            await set_reminder_handler(context, chat_id, **params)
        else: # 'unknown'
            await unknown_handler(context, chat_id, user_input)

    except Exception as e:
        logging.error(f"Error en handle_message: {e}")
        await update.message.reply_text("Lo siento, ocurrió un error inesperado al procesar tu solicitud.")

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
            model="gpt-4o-mini",
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

async def scheduled_briefing(application: Application):
    """Tarea programada que llama a la función de briefing. Ahora recibe 'application' directamente."""
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

    # Se crea la aplicación ANTES para poder pasarla a los jobs del scheduler.
    application = Application.builder().token(TELEGRAM_TOKEN).build()

    # Configura el scheduler para los recordatorios y el briefing
    scheduler = AsyncIOScheduler(timezone="America/Santiago")
    
    # CORRECCIÓN: Se pasa 'application' como argumento a los jobs.
    scheduler.add_job(check_reminders, "interval", minutes=1, args=[application])
    
    # Tarea programada para el briefing diario
    if BRIEFING_TIME and TELEGRAM_CHAT_ID:
        try:
            hour, minute = map(int, BRIEFING_TIME.split(':'))
            scheduler.add_job(scheduled_briefing, "cron", hour=hour, minute=minute, args=[application])
            logging.info(f"Briefing diario programado para las {BRIEFING_TIME} todos los días.")
        except ValueError:
            logging.error("Formato de BRIEFING_TIME incorrecto. Debe ser HH:MM.")

    # Handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("briefing", briefing_command)) # Handler para /briefing
    application.add_handler(CallbackQueryHandler(button_handler))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    # Inicia el scheduler
    scheduler.start()
    
    # Lógica de arranque: Webhook para producción, Polling para desarrollo
    webhook_url = os.getenv("WEBHOOK_URL")
    if webhook_url:
        port = int(os.getenv("PORT", 8080))
        logging.info(f"Iniciando bot en modo WEBHOOK en el puerto {port}")
        try:
            await application.run_webhook(
                listen="0.0.0.0",
                port=port,
                webhook_url=webhook_url
            )
        finally:
            scheduler.shutdown()
            logging.info("Webhook detenido y scheduler apagado.")
    else:
        logging.info("Iniciando bot en modo POLLING")
        try:
            await application.run_polling()
        finally:
            scheduler.shutdown()
            logging.info("Polling detenido y scheduler apagado.")

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
