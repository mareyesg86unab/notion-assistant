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

def get_openai_response(user_input: str, chat_id: int) -> dict:
    """Llama a la API de OpenAI para interpretar el texto del usuario y devolver un JSON con la acción."""
    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": f"Interpreta la siguiente petición del usuario en el contexto de gestión de tareas de Notion. Petición: '{user_input}'. Responde en formato JSON."}
            ],
            response_format={"type": "json_object"},
            temperature=0.1
        )
        return json.loads(response.choices[0].message.content)
    except Exception as e:
        logging.error(f"Error al llamar a OpenAI para el chat {chat_id}: {e}")
        return {"action": "unknown", "parameters": {}}

# -----------------------------------------------------------------------------
# 3. LÓGICA DE INTERACCIÓN CON NOTION
# -----------------------------------------------------------------------------

def create_task_notion(**kwargs):
    """Crea una tarea en Notion. Ahora no necesita verificar duplicados aquí."""
    title = kwargs.get("title")
    description = kwargs.get("description", "")
    raw_cat = kwargs.get("category", "")
    category = normalize_category(raw_cat)
    due_date = normalize_date(kwargs.get("due_date"))

    if not title:
         return {"status": "error", "message": "El título es obligatorio para crear una tarea."}

    props_to_create = {
        "Nombre de tarea": {"title": [{"text": {"content": title}}]},
        "Estado": {"status": {"name": "Por hacer"}}
    }
    if category:
        props_to_create["Etiquetas"] = {"multi_select": [{"name": category}]}
    if due_date:
        props_to_create["Fecha límite"] = {"date": {"start": due_date}}
    if description:
        props_to_create["Descripción"] = {"rich_text": [{"text": {"content": description}}]}

    try:
        notion.pages.create(
            parent={"database_id": NOTION_DATABASE_ID},
            properties=props_to_create
        )
        msg = f"✅ ¡Tarea '{title}' creada con éxito!"
        return {"status": "success", "message": msg}
    except Exception as e:
        logging.error(f"Error al crear la tarea en Notion: {e}")
        return {"status": "error", "message": f"Error al crear la tarea en Notion: {e}"}

def list_tasks_notion(category=None, status=None):
    filters = []
    if category:
        cat = normalize_category(category)
        if cat:
            filters.append({"property": "Etiquetas", "multi_select": {"contains": cat}})
    
    # Si no se especifica estado, por defecto no se filtra por estado (se muestran todas)
    # excepto si explícitamente se pide un estado.
    if status:
        filters.append({"property": "Estado", "status": {"equals": status}})

    query = {"database_id": NOTION_DATABASE_ID}
    if filters:
        # Si hay más de un filtro, los une con "and"
        query["filter"] = {"and": filters}

    try:
        response = notion.databases.query(**query)
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
        logging.error(f"Error al listar tareas de Notion: {e}")
        return {"status": "error", "message": f"Error al listar tareas: {e}"}

def edit_task_properties(task_id: str = None, **kwargs):
    """
    Edita una o varias propiedades de una tarea en Notion.
    """
    if not task_id:
        return {"status": "error", "message": "Se requiere un ID de tarea para editar."}

    properties_to_update = {}
    
    new_status = kwargs.get("new_status")
    new_due_date = kwargs.get("new_due_date")
    new_category = kwargs.get("new_category")

    if new_status:
        properties_to_update["Estado"] = {"status": {"name": new_status}}
    if new_due_date:
        normalized_date = normalize_date(new_due_date)
        if normalized_date:
            properties_to_update["Fecha límite"] = {"date": {"start": normalized_date}}
        else:
            return {"status": "error", "message": f"La fecha '{new_due_date}' no es válida."}
    if new_category:
        normalized_cat = normalize_category(new_category)
        if normalized_cat:
            properties_to_update["Etiquetas"] = {"multi_select": [{"name": normalized_cat}]}
        else:
            return {"status": "error", "message": f"La categoría '{new_category}' no es válida."}
            
    if not properties_to_update:
        return {"status": "info", "message": "No especificaste qué cambiar."}

    try:
        notion.pages.update(page_id=task_id, properties=properties_to_update)
        # Generar un mensaje de éxito más descriptivo
        updates_str = []
        if new_status: updates_str.append(f"estado a '{new_status}'")
        if new_due_date: updates_str.append(f"fecha a '{new_due_date}'")
        if new_category: updates_str.append(f"categoría a '{new_category}'")
        
        task_info = notion.pages.retrieve(page_id=task_id)
        task_title = task_info['properties']['Nombre de tarea']['title'][0]['plain_text']

        return {"status": "success", "message": f"✅ Tarea '{task_title}' actualizada: " + ", ".join(updates_str) + "."}
    except Exception as e:
        logging.error(f"Error al editar la tarea {task_id}: {e}")
        return {"status": "error", "message": f"Error al editar la tarea: {e}"}

def set_reminder_for_task(chat_id: int, task_id: str, reminder_str: str):
    """Encuentra una tarea y establece un recordatorio para ella."""
    if not task_id:
        return {"status": "error", "message": "Se necesita el título o ID de la tarea."}
        
    try:
        page = notion.pages.retrieve(page_id=task_id)
        due_date_prop = page.get("properties", {}).get("Fecha límite", {}).get("date")
        if not due_date_prop or not due_date_prop.get("start"):
            return {"status": "error", "message": f"No puedo crear un recordatorio para '{task_id}' porque no tiene una fecha límite establecida."}
        
        due_date = due_date_prop["start"]
        result_msg = set_reminder_db(chat_id, task_id, due_date, reminder_str)
        return {"status": "success", "message": result_msg}

    except Exception as e:
        logging.error(f"Error en set_reminder_for_task: {e}")
        return {"status": "error", "message": f"Ocurrió un error al procesar el recordatorio: {e}"}

def delete_task_notion(task_id: str = None, title: str = None):
    """
    Archiva una tarea en Notion (equivalente a eliminarla de la vista principal).
    Busca la tarea si solo se proporciona el título.
    """
    if not task_id and title:
        task_id, _, _ = find_task_by_title_enhanced(notion, NOTION_DATABASE_ID, title)
        
        if not task_id:
            # Devolvemos un estado especial para que el handler sepa que no se encontró
            return {"status": "not_found"}
    
    if not task_id:
        return {"status": "error", "message": "Se requiere un título o ID de tarea para eliminarla."}

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
    """Maneja las pulsaciones de los botones de la botonera."""
    query = update.callback_query
    await query.answer() # Confirmar que se recibió la pulsación
    
    data = query.data
    chat_id = query.effective_chat.id
    user_id = query.effective_user.id
    
    # --- Paginación ---
    if data.startswith("page_"):
        page = int(data.split("_")[1])
        if user_id in LAST_TASKS_LIST:
            tasks = LAST_TASKS_LIST[user_id]
            keyboard = create_task_keyboard(tasks, page)
            await query.edit_message_text(text="Aquí tienes tus tareas:", reply_markup=keyboard)
        else:
            await query.edit_message_text(text="La lista de tareas ha expirado. Pide una nueva con /list.")
    
    # --- Completar Tarea ---
    elif data.startswith("complete_"):
        task_id = data.replace("complete_", "")
        await query.edit_message_text(text=f"✅ Completando tarea...", reply_markup=None)
        
        result = edit_task_properties(task_id=task_id, new_status="Hecho")
        
        message = result.get("message", "No se pudo obtener un mensaje de estado.")
        await context.bot.send_message(chat_id, message)
        
        # Opcional: Refrescar la lista de tareas después de completar una
        if user_id in LAST_TASKS_LIST:
            # Eliminar la tarea completada de la lista en memoria
            LAST_TASKS_LIST[user_id] = [t for t in LAST_TASKS_LIST[user_id] if t['id'] != task_id]
            tasks = LAST_TASKS_LIST[user_id]
            if tasks:
                keyboard = create_task_keyboard(tasks, page=0) # Volver a la primera página
                await context.bot.send_message(chat_id, "Tareas restantes:", reply_markup=keyboard)
            else:
                await context.bot.send_message(chat_id, "¡Felicidades, has completado todas las tareas de esta lista!")

    # --- Creación Proactiva ---
    elif data.startswith("proactive_create_"):
        # Extraemos el título del callback_data. El prefijo es "proactive_create_"
        title_to_create = data[17:]
        await query.edit_message_text(text=f"Creando tarea '{title_to_create}'...")
        
        # Llamamos directamente a la función de creación de Notion
        result = create_task_notion(title=title_to_create)
        
        message = result.get("message", "No se pudo obtener un mensaje de estado.")
        # Enviamos un nuevo mensaje con el resultado, en lugar de editar el anterior.
        await context.bot.send_message(chat_id, message)

    elif data == "proactive_cancel":
        await query.edit_message_text(text="Ok, acción cancelada.")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Mensaje de bienvenida."""
    await update.message.reply_text(
        "¡Hola! Soy Olivia, tu asistente para Notion. ¿En qué puedo ayudarte hoy?"
    )

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
                # Modificamos el input para que OpenAI entienda a qué tarea nos referimos
                # Ej: "complétala" -> "completa la tarea 'Hacer la compra'"
                cleaned_input = re.sub(r"\b(la|el|una|un|primera|segunda|tercera|cuarta|quinta)\b", "", user_input, flags=re.IGNORECASE).strip()
                user_input = f"{cleaned_input} de la tarea '{task['title']}'"

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

async def create_task_handler(context: ContextTypes.DEFAULT_TYPE, chat_id: int, force_create=False, **params):
    """Manejador para la acción de crear tareas."""
    title = params.get("title")
    if not title:
        await context.bot.send_message(chat_id, "Necesito al menos un título para crear la tarea.")
        return

    # Si no estamos forzando la creación, primero verificamos si existe una tarea similar.
    if not force_create:
        _, similar_title, _ = find_task_by_title_enhanced(notion, NOTION_DATABASE_ID, title)
        if similar_title:
            # Encontramos un duplicado potencial. Guardamos el contexto y preguntamos al usuario.
            user_id = chat_id # Asumimos que el user_id es el chat_id en chats privados
            USER_CONTEXT[user_id] = (
                'confirm_creation',
                {'task_info': params}
            )
            await context.bot.send_message(chat_id, f"⚠️ Ya existe una tarea similar llamada '{similar_title}'.\n\n¿Seguro que quieres crear una nueva llamada '{title}'?\nResponde 'Sí, crear' para confirmar.")
            return

    # Procedemos a crear la tarea (ya sea porque no había duplicados o porque el usuario confirmó)
    result = create_task_notion(**params)
    message = result.get("message", "No se pudo obtener un mensaje de estado.")
    await context.bot.send_message(chat_id, message)

async def list_tasks_handler(context: ContextTypes.DEFAULT_TYPE, chat_id: int, **params):
    """Manejador para la acción de listar tareas."""
    status = params.get("status") or "Por hacer" # Default to 'Por hacer'
    category = params.get("category")

    tasks = list_tasks_notion(category=category, status=status)
    
    if isinstance(tasks, dict) and tasks.get("status") == "error":
        await context.bot.send_message(chat_id, tasks["message"])
        return
    
    if not tasks:
        await context.bot.send_message(chat_id, "¡Felicidades! No hay tareas pendientes que coincidan con tu búsqueda.")
        return

    # Guardar la lista para poder referenciarla por índice ("completa la primera")
    user_id = chat_id
    LAST_TASKS_LIST[user_id] = tasks

    keyboard = create_task_keyboard(tasks)
    await context.bot.send_message(chat_id, "Aquí tienes tus tareas:", reply_markup=keyboard)

async def edit_task_properties_handler(context: ContextTypes.DEFAULT_TYPE, chat_id: int, **params):
    """Manejador para la acción de editar propiedades de una tarea."""
    task_id = params.get("task_id")
    title_to_find = params.get("title")
    
    if not task_id and not title_to_find:
        await context.bot.send_message(chat_id, "Necesito el título o ID de la tarea que quieres modificar.")
        return
    
    # Si no tenemos ID, buscamos la tarea por título
    if not task_id:
        task_id, real_title, search_method = find_task_by_title_enhanced(notion, NOTION_DATABASE_ID, title_to_find)
        
        if not task_id:
            await offer_proactive_creation(context, chat_id, title_to_find)
            return
        
        # Oportunidad de aprendizaje: si se encontró de forma ambigua, preguntamos para crear un alias
        if search_method in ["substring", "fuzzy"]:
            # Aquí iría la lógica para preguntar al usuario si quiere guardar un alias
            pass

    # Llamar a la función que interactúa con la BD
    result = edit_task_properties(task_id=task_id, **params)

    message = result.get("message", "No se pudo obtener un mensaje de estado.")
    await context.bot.send_message(chat_id, message)

async def delete_task_handler(context: ContextTypes.DEFAULT_TYPE, chat_id: int, **params):
    """Manejador para la acción de eliminar (archivar) tareas."""
    title_to_find = params.get("title")
    if not title_to_find:
        await context.bot.send_message(chat_id, "Necesito el título de la tarea que quieres eliminar.")
        return

    # A diferencia de otros handlers, para eliminar no buscamos ID, se lo pasamos a la función de Notion
    # que ya tiene la lógica de búsqueda.
    result = delete_task_notion(**params)

    # Si la función de borrado devuelve 'not_found', ofrecemos crear la tarea.
    if result.get("status") == "not_found":
        await offer_proactive_creation(context, chat_id, title_to_find)
        return
        
    message = result.get("message", "No se pudo obtener un mensaje de estado.")
    await context.bot.send_message(chat_id, message)

async def set_reminder_handler(context: ContextTypes.DEFAULT_TYPE, chat_id: int, **params):
    """Manejador para la acción de establecer recordatorios."""
    title_to_find = params.get("title")
    reminder_str = params.get("reminder_str")
    if not title_to_find or not reminder_str:
        await context.bot.send_message(chat_id, "Necesito el título de la tarea y la configuración del recordatorio.")
        return

    # Usamos nuestro nuevo buscador de tareas
    task_id, real_title, _ = find_task_by_title_enhanced(notion, NOTION_DATABASE_ID, title_to_find)

    if not task_id:
        await offer_proactive_creation(context, chat_id, title_to_find)
        return

    # Llamar a la función que interactúa con la BD
    result_message = set_reminder_for_task(chat_id=chat_id, task_id=task_id, reminder_str=reminder_str)

    await context.bot.send_message(chat_id, result_message)

async def unknown_handler(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_input: str):
    """Manejador para cuando la intención del usuario no es clara."""
    # Podríamos añadir una lógica más sofisticada aquí, como sugerir comandos.
    await context.bot.send_message(chat_id, "No estoy segura de cómo ayudarte con eso. Intenta ser más específica, por favor.")

async def offer_proactive_creation(context: ContextTypes.DEFAULT_TYPE, chat_id: int, title: str):
    """Función auxiliar para ofrecer la creación de una tarea no encontrada."""
    if not title: return
    
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Sí, crear", callback_data=f"proactive_create_{title}")],
        [InlineKeyboardButton("❌ No, gracias", callback_data="proactive_cancel")]
    ])
    await context.bot.send_message(
        chat_id,
        f"No he podido encontrar la tarea '{title}'.\n\n¿Quieres que cree una nueva tarea con ese nombre?",
        reply_markup=keyboard
    )

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
