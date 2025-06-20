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
    """Inicializa la base de datos para los recordatorios."""
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
    logging.info("Base de datos de recordatorios inicializada.")

# --- Funciones de Búsqueda de Tareas Mejorada ---

def normalize_title(title: str) -> str:
    """Normaliza un título para búsqueda: minúsculas, sin tildes, sin puntuación."""
    if not title:
        return ""
    title = unidecode(title.lower())
    title = title.translate(str.maketrans('', '', string.punctuation))
    return " ".join(title.split())

def find_task_by_title_enhanced(notion: NotionClient, db_id: str, title_to_find: str) -> tuple[str | None, str | None]:
    """
    Busca una tarea en Notion con una lógica mejorada:
    1. Coincidencia exacta (normalizada)
    2. Búsqueda por subcadena (normalizada)
    3. Búsqueda por similitud (fuzzy matching)
    Devuelve (task_id, real_title) o (None, None).
    """
    try:
        norm_title_to_find = normalize_title(title_to_find)
        response = notion.databases.query(database_id=db_id)
        all_tasks = response.get("results", [])
        
        tasks_data = []
        for p in all_tasks:
            title_prop = p.get("properties", {}).get("Nombre de tarea", {}).get("title", [])
            if title_prop:
                real_title = title_prop[0]["plain_text"]
                tasks_data.append({
                    "id": p["id"],
                    "real_title": real_title,
                    "norm_title": normalize_title(real_title)
                })

        # 1. Búsqueda por coincidencia exacta
        for task in tasks_data:
            if norm_title_to_find == task["norm_title"]:
                return task["id"], task["real_title"]

        # 2. Búsqueda por subcadena
        for task in tasks_data:
            if norm_title_to_find in task["norm_title"]:
                return task["id"], task["real_title"]

        # 3. Búsqueda por similitud
        norm_task_titles = [task["norm_title"] for task in tasks_data]
        matches = get_close_matches(norm_title_to_find, norm_task_titles, n=1, cutoff=0.6)
        if matches:
            match_title = matches[0]
            for task in tasks_data:
                if task["norm_title"] == match_title:
                    return task["id"], task["real_title"]
                    
        return None, None
    except Exception as e:
        logging.error(f"Error en find_task_by_title_enhanced: {e}")
        return None, None

# --- Funciones de Recordatorios ---

def set_reminder_db(chat_id: int, task_title: str, due_date: str, reminder_str: str) -> str:
    """
    Parsea la petición de recordatorio y la guarda en la BD.
    Ej: reminder_str = "30 minutos antes"
    """
    # Parsear el tiempo del recordatorio
    match = re.search(r"(\d+)\s*(minuto|hora|dia)s?", reminder_str, re.IGNORECASE)
    if not match:
        return "No entendí el formato del recordatorio. Prueba con '30 minutos antes', '1 hora antes', etc."

    value = int(match.group(1))
    unit = match.group(2).lower()
    
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
