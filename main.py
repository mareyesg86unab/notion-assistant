import os
import json
import openai
from notion_client import Client as NotionClient
from dotenv import load_dotenv

# Carga variables de entorno
load_dotenv()
openai.api_key = os.getenv("OPENAI_API_KEY")
notion = NotionClient(auth=os.getenv("NOTION_API_TOKEN"))
DATABASE_ID = os.getenv("NOTION_DATABASE_ID")

# ─── Normalización de categorías ─────────────────────────────────────────────
CATEGORY_MAP = {
    "estudio":    "Estudios",
    "estudios":   "Estudios",
    "domestica":  "Domésticas",
    "doméstica":  "Domésticas",
    "domesticas": "Domésticas",
    "laboral":    "Laborales",
    "laborales":  "Laborales",
}

def normalize_category(cat: str) -> str:
    if not cat:
        return cat
    key = cat.strip().lower()
    return CATEGORY_MAP.get(key, cat.title())

# ─── Helper para buscar ID por título ─────────────────────────────────────────
def find_task_id_by_title(title: str):
    """Busca la primera tarea en Notion cuyo título coincida (case-insensitive)."""
    for t in list_tasks_notion():
        if t["title"].lower() == title.lower():
            return t["id"]
    return None

# ─── Handlers para Notion ─────────────────────────────────────────────────────
def create_task_notion(**kwargs):
    title       = kwargs.get("title")
    description = kwargs.get("description", "")
    raw_cat     = kwargs.get("category", "")
    category    = normalize_category(raw_cat)
    due_date    = kwargs.get("due_date")

    notion.pages.create(
        parent={"database_id": DATABASE_ID},
        properties={
            "Nombre de tarea": {"title": [{"text": {"content": title}}]},
            "Etiquetas": {"multi_select": [{"name": category}]},
            "Fecha límite": {"date": {"start": due_date}},
            "Descripción": {"rich_text": [{"text": {"content": description}}]},
            "Estado": {"status": {"name": "Por hacer"}}
        }
    )
    return {"status": "success", "action": "create_task", "title": title}


def list_tasks_notion(category=None, status=None):
    filters = []
    if category:
        cat = normalize_category(category)
        filters.append({"property": "Etiquetas", "multi_select": {"contains": cat}})
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
            "id":    p["id"],
            "title": props["Nombre de tarea"]["title"][0]["plain_text"],
            "due":   props["Fecha límite"]["date"]["start"],
            "status":props["Estado"]["status"]["name"],
        })
    return tasks


def update_task_notion(task_id: str = None, title: str = None, status: str = None):
    if not task_id and title:
        task_id = find_task_id_by_title(title)
        if not task_id:
            return {"status": "error", "error": f"Tarea '{title}' no encontrada"}
    notion.pages.update(
        page_id=task_id,
        properties={"Estado": {"status": {"name": status}}}
    )
    return {"status": "success", "action": "update_task", "task_id": task_id}


def delete_task_notion(task_id: str = None, title: str = None):
    if not task_id and title:
        task_id = find_task_id_by_title(title)
        if not task_id:
            return {"status": "error", "error": f"Tarea '{title}' no encontrada"}
    notion.pages.update(page_id=task_id, archived=True)
    return {"status": "success", "action": "delete_task", "task_id": task_id}

# ─── Definición de funciones para OpenAI Function Calling ─────────────────────
functions = [
    {
        "name": "create_task",
        "description": "Crea una tarea nueva en Notion",
        "parameters": {
            "type": "object",
            "properties": {
                "title":       {"type": "string"},
                "description": {"type": "string"},
                "category":    {"type": "string"},
                "due_date":    {"type": "string", "format": "date"}
            },
            "required": ["title", "category", "due_date"]
        }
    },
    {
        "name": "list_tasks",
        "description": "Recupera tareas filtradas por Etiquetas o Estado",
        "parameters": {
            "type": "object",
            "properties": {
                "category": {"type": "string"},
                "status":   {"type": "string", "enum": ["Por hacer","En progreso","Hecho"]}
            }
        }
    },
    {
        "name": "update_task",
        "description": "Actualiza el estado de una tarea existente en Notion",
        "parameters": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "title":   {"type": "string"},
                "status":  {"type": "string", "enum": ["Por hacer","En progreso","Hecho"]}
            },
            "required": ["status"]
        }
    },
    {
        "name": "delete_task",
        "description": "Archiva una tarea existente en Notion",
        "parameters": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "title":   {"type": "string"}
            }
        }
    }
]

# ─── Bucle principal ───────────────────────────────────────────────────────────
def run_assistant():
    print("🟣 Olivia iniciada. Escribe 'salir' para terminar.\n")
    while True:
        user_input = input("Tú: ")
        if user_input.lower().strip() in ("salir","exit","quit"):
            break

        response = openai.ChatCompletion.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": "Eres Olivia, la asistente que organiza tareas en Notion."},
                {"role": "user",   "content": user_input}
            ],
            functions=functions,
            function_call="auto"
        )

        msg = response.choices[0].message
        if msg.get("function_call"):
            fn, args = msg["function_call"]["name"], json.loads(msg["function_call"]["arguments"])
            if fn == "create_task":
                result = create_task_notion(**args)
            elif fn == "list_tasks":
                result = list_tasks_notion(**args)
            elif fn == "update_task":
                result = update_task_notion(**args)
            elif fn == "delete_task":
                result = delete_task_notion(**args)
            else:
                result = {"status": "error", "error": "Función desconocida"}
            print("Olivia (función):", result, "\n")
        else:
            print("Olivia:", msg.content, "\n")

if __name__ == "__main__":
    run_assistant()
