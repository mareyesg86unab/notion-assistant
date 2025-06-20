# utils.py

import sqlite3
import logging
from datetime import datetime, timedelta
import re
from difflib import get_close_matches
from unidecode import unidecode
import string

from telegram.ext import Application
from notion_client import Client as NotionClient

# --- Configuración de Logging ---
logging.basicConfig(level=logging.INFO)

# --- Base de Datos de Recordatorios ---
DB_FILE = "reminders.db"

def init_db():
    """Inicializa la base de datos para recordatorios y alias."""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    
    # Tabla de recordatorios
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS reminders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id INTEGER NOT NULL,
        task_title TEXT NOT NULL,
        remind_time TIMESTAMP NOT NULL,
        status TEXT DEFAULT 'pending'
    )
    """)
    
    # Tabla de alias para tareas
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS task_aliases (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        alias_text TEXT NOT NULL UNIQUE,
        task_id TEXT NOT NULL
    )
    """)
    
    conn.commit()
    conn.close()
    logging.info("Base de datos de recordatorios y alias inicializada.")

# --- Funciones de gestión de Alias ---

def add_alias(alias_text: str, task_id: str):
    """Guarda o actualiza un alias para un ID de tarea."""
    norm_alias = normalize_title(alias_text)
    if not norm_alias:
        return
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    # Usar INSERT OR REPLACE para que si el alias ya existe, se actualice su task_id
    cursor.execute("INSERT OR REPLACE INTO task_aliases (alias_text, task_id) VALUES (?, ?)", (norm_alias, task_id))
    conn.commit()
    conn.close()
    logging.info(f"Alias guardado: '{norm_alias}' -> {task_id}")

def find_task_id_by_alias(alias_text: str) -> str | None:
    """Busca un ID de tarea usando un alias."""
    norm_alias = normalize_title(alias_text)
    if not norm_alias:
        return None
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT task_id FROM task_aliases WHERE alias_text = ?", (norm_alias,))
    result = cursor.fetchone()
    conn.close()
    return result[0] if result else None

def get_task_by_id(notion: NotionClient, task_id: str) -> tuple[str | None, str | None]:
    """Obtiene una tarea de Notion directamente por su ID."""
    try:
        page = notion.pages.retrieve(page_id=task_id)
        title_prop = page.get("properties", {}).get("Nombre de tarea", {}).get("title", [])
        if title_prop and title_prop[0].get("plain_text"):
            return task_id, title_prop[0]["plain_text"]
    except Exception as e:
        logging.error(f"Error al obtener tarea por ID {task_id}: {e}")
    return None, None

# --- Funciones de Búsqueda de Tareas Mejorada ---

def normalize_title(title: str) -> str:
    """Normaliza un título para búsqueda: minúsculas, sin tildes, sin puntuación."""
    if not title:
        return ""
    title = unidecode(title.lower())
    title = title.translate(str.maketrans('', '', string.punctuation))
    return " ".join(title.split())

def find_task_by_title_enhanced(notion: NotionClient, db_id: str, title_to_find: str) -> tuple[str | None, str | None, str | None]:
    """
    Busca una tarea en Notion con una lógica de búsqueda por niveles para la mejor experiencia de usuario.
    Niveles de búsqueda:
    1. Búsqueda por Alias: La más rápida y personalizada.
    2. Búsqueda por Relevancia: Calcula un puntaje para cada tarea y elige la mejor.
    """
    try:
        norm_title_to_find = normalize_title(title_to_find)
        if not norm_title_to_find:
            return None, None, None

        # --- Nivel 1: Búsqueda por Alias ---
        task_id = find_task_id_by_alias(norm_title_to_find)
        if task_id:
            found_id, real_title = get_task_by_id(notion, task_id)
            if found_id:
                return found_id, real_title, "alias"

        # --- Nivel 2: Búsqueda por Relevancia ---
        response = notion.databases.query(database_id=db_id, filter={"property": "Estado", "status": {"does_not_equal": "Hecho"}})
        
        best_match = {"id": None, "title": None, "score": 0}
        
        search_words = set(norm_title_to_find.split())

        for page in response.get("results", []):
            title_prop = page.get("properties", {}).get("Nombre de tarea", {}).get("title", [])
            if not (title_prop and title_prop[0].get("plain_text")):
                continue

            real_title = title_prop[0]["plain_text"]
            norm_title = normalize_title(real_title)
            
            # Calcular puntaje de relevancia
            # a) Coincidencia de palabras clave
            title_words = set(norm_title.split())
            common_words = search_words.intersection(title_words)
            keyword_score = len(common_words)
            
            # b) Similitud general (difflib)
            similarity_score = get_close_matches(norm_title_to_find, [norm_title], n=1, cutoff=0.6)
            
            # El puntaje total es una combinación. Le damos más peso a las palabras clave.
            total_score = (keyword_score * 2) + (1 if similarity_score else 0)

            if total_score > best_match["score"]:
                best_match = {"id": page["id"], "title": real_title, "score": total_score}

        if best_match["id"]:
            # Determinamos si fue una coincidencia "exacta" o "aproximada" (fuzzy)
            # para decidir si ofrecemos guardar un alias.
            is_exact_match = normalize_title(best_match["title"]) == norm_title_to_find
            search_method = "exact" if is_exact_match else "fuzzy"
            return best_match["id"], best_match["title"], search_method
                    
        return None, None, None
    except Exception as e:
        logging.error(f"Error en find_task_by_title_enhanced: {e}")
        return None, None, None

# --- Funciones de Recordatorios ---

def set_reminder_db(chat_id: int, task_title: str, due_date: str, reminder_str: str) -> str:
    """
    Parsea la petición de recordatorio y la guarda en la BD.
    Ej: reminder_str = "30 minutos antes"
    """
    # Parsear el tiempo del recordatorio, ahora acepta 'dia' o 'día'
    match = re.search(r"(\d+)\s*(minuto|hora|d[ií]a)s?", reminder_str, re.IGNORECASE)
    if not match:
        return "No entendí el formato del recordatorio. Prueba con '30 minutos antes', '1 hora antes', etc."

    value = int(match.group(1))
    unit = match.group(2).lower()
    
    # Normalizar la unidad para que 'día' funcione con timedelta
    if 'd' in unit:
        unit = 'dia'
        
    delta_map = {"minuto": "minutes", "hora": "hours", "dia": "days"}
    delta = timedelta(**{delta_map[unit]: value})

    # Calcular la hora del recordatorio
    try:
        # Asumimos que la fecha de la tarea es a las 23:59:59 si no se especifica hora
        due_datetime = datetime.strptime(due_date, "%Y-%m-%d").replace(hour=23, minute=59, second=59)
        remind_time = due_datetime - delta
    except ValueError:
        return "La fecha de la tarea no es válida para crear un recordatorio."

    # Guardar en la base de datos
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO reminders (chat_id, task_title, remind_time) VALUES (?, ?, ?)",
        (chat_id, task_title, remind_time)
    )
    conn.commit()
    conn.close()
    
    return f"OK. Te recordaré sobre '{task_title}' el {remind_time.strftime('%Y-%m-%d a las %H:%M')}."

async def check_reminders(application: Application):
    """Función que se ejecuta periódicamente para enviar recordatorios pendientes."""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    
    now = datetime.now()
    cursor.execute(
        "SELECT id, chat_id, task_title FROM reminders WHERE remind_time <= ? AND status = 'pending'", (now,)
    )
    reminders_to_send = cursor.fetchall()
    
    for r_id, chat_id, task_title in reminders_to_send:
        try:
            message = f"🔔 **Recordatorio** 🔔\n\nNo te olvides de tu tarea: **{task_title}**"
            await application.bot.send_message(chat_id=chat_id, text=message, parse_mode='Markdown')
            
            # Marcar como enviado
            cursor.execute("UPDATE reminders SET status = 'sent' WHERE id = ?", (r_id,))
            conn.commit()
            logging.info(f"Recordatorio enviado para la tarea '{task_title}' al chat {chat_id}")
        except Exception as e:
            logging.error(f"Error al enviar recordatorio {r_id}: {e}")

    conn.close()
