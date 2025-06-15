import os
import json
import openai
from notion_client import Client as NotionClient
from dotenv import load_dotenv
from datetime import datetime
import dateparser

# Telegram imports
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

# Prompt mejorado para el asistente
SYSTEM_PROMPT = (
    "Eres Olivia, una asistente virtual que ayuda a los usuarios a gestionar tareas en Notion. "
    "Tu objetivo es facilitar la vida del usuario, guiándolo paso a paso y usando un lenguaje sencillo. "
    "Solo puedes usar las siguientes categorías: Estudios, Domésticas, Laborales. "
    "Si el usuario menciona una categoría no reconocida, sugiere la más cercana o pídele que elija una válida. "
    "Acepta fechas en cualquier formato (ej: 'mañana', '21-06-2025', 'el viernes') y conviértelas a formato ISO 8601 (YYYY-MM-DD). "
    "Si falta información, pregunta solo lo necesario. "
    "Antes de crear, editar o borrar una tarea, confirma con el usuario. "
    "Nunca inventes etiquetas nuevas. "
    "Si el usuario comete errores de tipeo, intenta adivinar la intención y sugiere correcciones."
)

# Carga variables de entorno
env_api_key = os.getenv("OPENAI_API_KEY")
load_dotenv()
client = openai.OpenAI(api_key=env_api_key)
notion = NotionClient(auth=os.getenv("NOTION_API_TOKEN"))
DATABASE_ID = os.getenv("NOTION_DATABASE_ID")

# ─── Normalización de categorías con sinónimos y sugerencias ──────────────────
CATEGORY_MAP = {
    "estudio": "Estudios",
    "estudios": "Estudios",
    "academico": "Estudios",
    "académico": "Estudios",
    "universidad": "Estudios",
    "trabajo": "Laboral",
    "laboral": "Laboral",
    "laborales": "Laboral",
    "empleo": "Laboral",
    "oficio": "Laboral",
    "profesional": "Laboral",
    "domestica": "Domésticas",
    "doméstica": "Domésticas",
    "domesticas": "Domésticas",
    "casa": "Domésticas",
    "hogar": "Domésticas",
    "limpieza": "Domésticas",
}
VALID_CATEGORIES = set(CATEGORY_MAP.values())

# Sugerencia de categoría más cercana (fuzzy matching)
def suggest_category(cat):
    from difflib import get_close_matches
    matches = get_close_matches(cat.lower(), CATEGORY_MAP.keys(), n=1, cutoff=0.6)
    if matches:
        return CATEGORY_MAP[matches[0]]
    return None

def normalize_category(cat: str) -> str:
    if not cat:
        return None
    key = cat.strip().lower()
    if key in CATEGORY_MAP:
        return CATEGORY_MAP[key]
    # Sugerir la más cercana
    return suggest_category(key)

# ─── Conversión flexible de fechas ─────────────────────────────────────────────
def normalize_date(date_str):
    if not date_str:
        return None
    # Intenta parsear con dateparser (acepta lenguaje natural y varios formatos)
    dt = dateparser.parse(date_str, languages=["es", "en"])
    if dt:
        return dt.strftime("%Y-%m-%d")
    # Si no reconoce el formato, retorna None
    return None

# ─── Helper para buscar ID por título ─────────────────────────────────────────
def find_task_id_by_title(title: str):
    for t in list_tasks_notion():
        if t["title"].lower() == title.lower():
            return t["id"]
    return None

# ─── Handlers para Notion ─────────────────────────────────────────────────────
def create_task_notion(**kwargs):
    title = kwargs.get("title")
    description = kwargs.get("description", "")
    raw_cat = kwargs.get("category", "")
    category = normalize_category(raw_cat)
    due_date = normalize_date(kwargs.get("due_date"))

    if not category:
        return {"status": "error", "error": f"La categoría '{raw_cat}' no es válida. Usa una de estas: {', '.join(VALID_CATEGORIES)}"}
    if not due_date:
        return {"status": "error", "error": f"La fecha '{kwargs.get('due_date')}' no es válida. Usa formato DD-MM-YYYY o una fecha en lenguaje natural."}

    try:
        notion.pages.create(parent={"database_id": DATABASE_ID},
                            properties={
                                "Nombre de tarea": {
                                    "title": [{
                                        "text": {
                                            "content": title
                                        }
                                    }]
                                },
                                "Etiquetas": {
                                    "multi_select": [{
                                        "name": category
                                    }]
                                },
                                "Fecha límite": {
                                    "date": {
                                        "start": due_date
                                    }
                                },
                                "Descripción": {
                                    "rich_text": [{
                                        "text": {
                                            "content": description
                                        }
                                    }]
                                },
                                "Estado": {
                                    "status": {
                                        "name": "Por hacer"
                                    }
                                }
                            })
        return {"status": "success", "action": "create_task", "title": title, "category": category, "due_date": due_date}
    except Exception as e:
        return {"status": "error", "error": f"Error al crear la tarea en Notion: {str(e)}"}


def list_tasks_notion(category=None, status=None):
    filters = []
    if category:
        cat = normalize_category(category)
        filters.append({
            "property": "Etiquetas",
            "multi_select": {
                "contains": cat
            }
        })
    if status:
        filters.append({"property": "Estado", "status": {"equals": status}})

    query = {"database_id": DATABASE_ID}
    if filters:
        query["filter"] = {"and": filters}

    results = notion.databases.query(**query).get("results", [])
    tasks = []
    for p in results:
        props = p["properties"]
        tasks.append({
            "id": p["id"],
            "title": props["Nombre de tarea"]["title"][0]["plain_text"],
            "due": props["Fecha límite"]["date"]["start"],
            "status": props["Estado"]["status"]["name"],
        })
    return tasks


def update_task_notion(task_id: str = None,
                       title: str = None,
                       status: str = None):
    if not task_id and title:
        task_id = find_task_id_by_title(title)
        if not task_id:
            return {
                "status": "error",
                "error": f"Tarea '{title}' no encontrada"
            }
    notion.pages.update(page_id=task_id,
                        properties={"Estado": {
                            "status": {
                                "name": status
                            }
                        }})
    return {"status": "success", "action": "update_task", "task_id": task_id}


def delete_task_notion(task_id: str = None, title: str = None):
    if not task_id and title:
        task_id = find_task_id_by_title(title)
        if not task_id:
            return {
                "status": "error",
                "error": f"Tarea '{title}' no encontrada"
            }
    notion.pages.update(page_id=task_id, archived=True)
    return {"status": "success", "action": "delete_task", "task_id": task_id}


# ─── Definición de funciones para OpenAI Function Calling ─────────────────────
functions = [{
    "name": "create_task",
    "description": "Crea una tarea nueva en Notion",
    "parameters": {
        "type": "object",
        "properties": {
            "title": {
                "type": "string"
            },
            "description": {
                "type": "string"
            },
            "category": {
                "type": "string"
            },
            "due_date": {
                "type": "string",
                "format": "date"
            }
        },
        "required": ["title", "category", "due_date"]
    }
}, {
    "name": "list_tasks",
    "description": "Recupera tareas filtradas por Etiquetas o Estado",
    "parameters": {
        "type": "object",
        "properties": {
            "category": {
                "type": "string"
            },
            "status": {
                "type": "string",
                "enum": ["Por hacer", "En progreso", "Hecho"]
            }
        }
    }
}, {
    "name": "update_task",
    "description": "Actualiza el estado de una tarea existente",
    "parameters": {
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string"
            },
            "title": {
                "type": "string"
            },
            "status": {
                "type": "string",
                "enum": ["Por hacer", "En progreso", "Hecho"]
            }
        },
        "required": ["status"]
    }
}, {
    "name": "delete_task",
    "description": "Archiva una tarea existente",
    "parameters": {
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string"
            },
            "title": {
                "type": "string"
            }
        }
    }
}]


# ─── Historial de conversación ────────────────────────────────────────────────
cli_history = [
    {"role": "system", "content": SYSTEM_PROMPT}
]

def add_to_history(history, role, content):
    history.append({"role": role, "content": content})
    if len(history) > 10:
        del history[1]


# ─── Handlers de Telegram ──────────────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["history"] = [
        {"role": "system", "content": SYSTEM_PROMPT}
    ]
    await update.message.reply_text(
        "¡Hola! Soy Olivia 🤖. Escríbeme tu comando para Notion.")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if "history" not in context.user_data:
        context.user_data["history"] = [
            {"role": "system", "content": SYSTEM_PROMPT}
        ]
    history = context.user_data["history"]
    user_input = update.message.text
    add_to_history(history, "user", user_input)
    response = client.chat.completions.create(
        model="gpt-4",
        messages=history,
        functions=functions,
        function_call="auto")
    msg = response.choices[0].message
    add_to_history(history, "assistant", msg.content)
    if hasattr(msg, "function_call") and msg.function_call is not None:
        fn = msg.function_call.name
        args = json.loads(msg.function_call.arguments)
        if fn == "create_task":
            result = create_task_notion(**args)
            if result["status"] == "success":
                await update.message.reply_text(f"✅ Tarea creada: {result['title']} (Categoría: {result['category']}, Fecha: {result['due_date']})")
            else:
                await update.message.reply_text(f"❌ {result['error']}")
        elif fn == "list_tasks":
            result = list_tasks_notion(**args)
            await update.message.reply_text(f"Tareas: {result}")
        elif fn == "update_task":
            result = update_task_notion(**args)
            await update.message.reply_text(f"{result}")
        elif fn == "delete_task":
            result = delete_task_notion(**args)
            await update.message.reply_text(f"{result}")
        else:
            await update.message.reply_text("❌ Función desconocida")
    else:
        await update.message.reply_text(msg.content)


# ─── Lanzadores ──────────────────────────────────────────────────────────────
def run_cli():
    print("🟣 Olivia iniciada. Escribe 'salir' para terminar.\n")
    global cli_history
    while True:
        user_input = input("Tú: ")
        if user_input.lower().strip() in ("salir", "exit", "quit"): break
        add_to_history(cli_history, "user", user_input)
        response = client.chat.completions.create(
            model="gpt-4",
            messages=cli_history,
            functions=functions,
            function_call="auto")
        msg = response.choices[0].message
        add_to_history(cli_history, "assistant", msg.content)
        if hasattr(msg, "function_call") and msg.function_call is not None:
            fn = msg.function_call.name
            args = json.loads(msg.function_call.arguments)
            if fn == "create_task":
                res = create_task_notion(**args)
                if res["status"] == "success":
                    print(f"✅ Tarea creada: {res['title']} (Categoría: {res['category']}, Fecha: {res['due_date']})\n")
                else:
                    print(f"❌ {res['error']}\n")
            elif fn == "list_tasks":
                res = list_tasks_notion(**args)
                print("Tareas:", res, "\n")
            elif fn == "update_task":
                res = update_task_notion(**args)
                print(res, "\n")
            elif fn == "delete_task":
                res = delete_task_notion(**args)
                print(res, "\n")
            else:
                print("❌ Función desconocida\n")
        else:
            print("Olivia:", msg.content, "\n")


async def run_telegram_bot():
    application = Application.builder().token(os.getenv("TELEGRAM_TOKEN")).build()
    
    # Add handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    # Start the bot
    await application.run_polling()


if __name__ == "__main__":
    mode = os.getenv("MODE", "cli")
    if mode == "telegram":
        import asyncio
        try:
            asyncio.run(run_telegram_bot())
        except RuntimeError:
            # Si ya hay un event loop corriendo (como en Render), usa el loop actual
            loop = asyncio.get_event_loop()
            loop.run_until_complete(run_telegram_bot())
    else:
        run_cli()
