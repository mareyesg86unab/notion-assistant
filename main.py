import os
import json
import openai
from notion_client import Client as NotionClient
from dotenv import load_dotenv

# Carga variables de entorno desde .env (en desarrollo)
load_dotenv()

# Inicializa API clients
openai.api_key = os.getenv("OPENAI_API_KEY")
notion = NotionClient(auth=os.getenv("NOTION_API_TOKEN"))
DATABASE_ID = os.getenv("NOTION_DATABASE_ID")

# ─── Handlers para Notion ──────────────────────────────────────────────────────
def create_task_notion(title: str, description: str, category: str, assignee: str, due_date: str):
    """Crea una nueva página en la base de datos de Notion."""
    notion.pages.create(
        parent={"database_id": DATABASE_ID},
        properties={
            "Título": {
                "title": [{"text": {"content": title}}]
            },
            "Descripción": {
                "rich_text": [{"text": {"content": description or ""}}]
            },
            "Categoría": {
                "select": {"name": category}
            },
            "Responsable": {
                "people": [{"name": assignee}]
            },
            "Fecha de vencimiento": {
                "date": {"start": due_date}
            },
            "Estado": {
                "select": {"name": "Por hacer"}
            }
        }
    )
    return {"status": "success", "action": "create_task", "title": title}

def list_tasks_notion(category=None, assignee=None, status=None):
    """Consulta la base de datos y devuelve tareas filtradas."""
    filters = []
    if category:
        filters.append({
            "property": "Categoría",
            "select": { "equals": category }
        })
    if assignee:
        filters.append({
            "property": "Responsable",
            "people": { "contains": assignee }
        })
    if status:
        filters.append({
            "property": "Estado",
            "select": { "equals": status }
        })
    query = {"database_id": DATABASE_ID}
    if filters:
        query["filter"] = {"and": filters}
    results = notion.databases.query(**query).get("results", [])
    # Extraemos información relevante
    tasks = []
    for p in results:
        props = p["properties"]
        tasks.append({
            "id":      p["id"],
            "title":   props["Título"]["title"][0]["plain_text"],
            "due":     props["Fecha de vencimiento"]["date"]["start"],
            "status":  props["Estado"]["select"]["name"],
            "assignee": [u["name"] for u in props["Responsable"]["people"]]
        })
    return tasks

def update_task_notion(task_id: str, status=None, assignee=None):
    """Actualiza estado o responsable de una tarea existente."""
    props = {}
    if status:
        props["Estado"] = {"select": {"name": status}}
    if assignee:
        props["Responsable"] = {"people": [{"name": assignee}]}
    notion.pages.update(page_id=task_id, properties=props)
    return {"status": "success", "action": "update_task", "task_id": task_id}

def delete_task_notion(task_id: str):
    """Elimina (archiva) una tarea en Notion."""
    # Marcamos como archivada
    notion.pages.update(page_id=task_id, archived=True)
    return {"status": "success", "action": "delete_task", "task_id": task_id}

# ─── Definición de funciones para OpenAI ───────────────────────────────────────
functions = [
    {
      "name": "create_task",
      "description": "Crea una tarea nueva en Notion",
      "parameters": {
        "type": "object",
        "properties": {
          "title":       { "type": "string" },
          "description": { "type": "string" },
          "category":    { "type": "string", "enum": ["Domésticas","Laborales","Estudios"] },
          "assignee":    { "type": "string" },
          "due_date":    { "type": "string", "format": "date" }
        },
        "required": ["title","category","assignee","due_date"]
      }
    },
    {
      "name": "list_tasks",
      "description": "Recupera tareas filtradas por categoría, responsable o estado",
      "parameters": {
        "type": "object",
        "properties": {
          "category": { "type": "string", "enum": ["Domésticas","Laborales","Estudios"] },
          "assignee": { "type": "string" },
          "status":   { "type": "string", "enum": ["Por hacer","En progreso","Hecho"] }
        }
      }
    },
    {
      "name": "update_task",
      "description": "Actualiza el estado o responsable de una tarea existente en Notion",
      "parameters": {
        "type": "object",
        "properties": {
          "task_id":  { "type": "string" },
          "status":   { "type": "string", "enum": ["Por hacer","En progreso","Hecho"] },
          "assignee": { "type": "string" }
        },
        "required": ["task_id"]
      }
    },
    {
      "name": "delete_task",
      "description": "Elimina o marca una tarea como eliminada en Notion",
      "parameters": {
        "type": "object",
        "properties": {
          "task_id": { "type": "string" }
        },
        "required": ["task_id"]
      }
    }
]

# ─── Función principal que interactúa con el usuario ───────────────────────────
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
        # Si devuelve function_call, despacho la función
        if msg.get("function_call"):
            name = msg["function_call"]["name"]
            args = json.loads(msg["function_call"]["arguments"])
            # Mapea al handler correspondiente
            if name == "create_task":
                result = create_task_notion(**args)
            elif name == "list_tasks":
                result = list_tasks_notion(**args)
            elif name == "update_task":
                result = update_task_notion(**args)
            elif name == "delete_task":
                result = delete_task_notion(**args)
            else:
                result = {"status":"error","error":"Función desconocida"}

            print("Olivia (función):", result, "\n")
        else:
            # Respuesta directa
            print("Olivia:", msg.content, "\n")

if __name__ == "__main__":
    run_assistant()
