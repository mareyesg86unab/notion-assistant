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
    "Si el usuario comete errores de tipeo, intenta adivinar la intención y sugiere correcciones."
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

def find_task_id_by_title(title: str) -> str | None:
    try:
        results = notion.databases.query(
            database_id=NOTION_DATABASE_ID,
            filter={"property": "Nombre de tarea", "title": {"equals": title}}
        ).get("results", [])
        return results[0]["id"] if results else None
    except Exception:
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
        results = notion.databases.query(**query).get("results", [])
        tasks = []
        for p in results:
            props = p["properties"]
            tasks.append({
                "id": p["id"],
                "title": props["Nombre de tarea"]["title"][0]["plain_text"],
                "due": props["Fecha límite"]["date"]["start"] if props.get("Fecha límite", {}).get("date") else "N/A",
                "status": props["Estado"]["status"]["name"],
            })
        return tasks
    except Exception as e:
        return {"status": "error", "message": f"Error al listar tareas: {e}"}


def update_task_notion(task_id: str = None, title: str = None, status: str = None):
    if not task_id and title:
        task_id = find_task_id_by_title(title)
        if not task_id:
            return {"status": "error", "message": f"Tarea '{title}' no encontrada."}
    
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

    try:
        response = client.chat.completions.create(
            model="gpt-4",
            messages=history,
            functions=functions,
            function_call="auto"
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
        print(f"Error en handle_message: {e}")
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
