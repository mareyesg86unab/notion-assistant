# notion-assistant

Olivia es un bot de Telegram que te ayuda a gestionar tareas en Notion usando inteligencia artificial (OpenAI) y comandos naturales en español. Puedes crear, listar, actualizar y eliminar tareas desde Telegram, y Olivia se encarga de interactuar con tu base de datos de Notion.

---

## 🚀 Despliegue en Render.com

Este proyecto está preparado para funcionar en [Render](https://render.com/) como un **Web Service** usando webhooks de Telegram.

### **Pasos para desplegar:**

1. **Clona este repositorio en tu cuenta de GitHub.**
2. **Crea un nuevo Web Service en Render** y conecta tu repo.
3. **Asegúrate de que Render use el archivo `render.yaml`** (Render lo detecta automáticamente).
4. **Configura las variables de entorno sensibles** (API keys) en el panel de Render, no en el repo.
5. **Haz deploy!**

---

## ⚙️ Dependencias principales

- `python-telegram-bot[webhooks]==21.1.1`  ← ¡IMPORTANTE! Incluye `[webhooks]` para que Render instale Tornado y soporte webhooks.
- `notion-client`
- `openai`
- `python-dotenv`
- `dateparser`
- `nest_asyncio`  ← Para compatibilidad con event loop en servidores modernos.

---

## 📝 Variables de entorno necesarias

Debes definir estas variables en Render (panel web o en `render.yaml` con `sync: false`):

- `MODE=telegram`
- `PORT=8080` (Render la gestiona automáticamente)
- `WEBHOOK_URL=https://<tu-app>.onrender.com/`  ← ¡Debe terminar en `/`!
- `OPENAI_API_KEY`
- `NOTION_API_TOKEN`
- `NOTION_DATABASE_ID`
- `TELEGRAM_TOKEN`

**Nunca subas tus claves al repo.**

---

## 🛠️ Problemas comunes y soluciones

### 1. **Error de event loop ya corriendo**
- Solución: Usar `nest_asyncio` y `loop.run_until_complete(run_telegram_bot())` en vez de `asyncio.run()`.

### 2. **Error 404 en el webhook de Telegram**
- Solución: El endpoint debe ser la raíz `/`, no `/webhook`. Pon `WEBHOOK_URL=https://<tu-app>.onrender.com/`.

### 3. **Error: PTB must be installed via [webhooks]**
- Solución: En `requirements.txt` usa `python-telegram-bot[webhooks]==21.1.1`.

### 4. **El bot no responde a /start**
- Solución: Verifica que el webhook esté bien configurado y que no haya errores en los logs de Render.

---

## 📦 Estructura del proyecto

```
notion-assistant/
├── main.py
├── requirements.txt
├── render.yaml
├── .gitignore
└── README.md
```

---

## 🧠 Mejoras y aprendizajes implementados
- Uso correcto de webhooks con python-telegram-bot 21.x en Render.
- Manejo de event loop compatible con servidores modernos.
- Configuración de endpoint raíz `/` para webhooks.
- Separación de variables sensibles fuera del repo.
- Uso de `render.yaml` para portabilidad y despliegue automático.

---

## ✨ ¿Cómo contribuir?
1. Haz un fork del repo.
2. Crea una rama para tu mejora.
3. Haz un Pull Request.

---

## 📞 Soporte
Si tienes problemas, revisa los logs de Render y consulta la sección de problemas comunes. Si necesitas ayuda, abre un issue en GitHub o contacta al autor.

---

¡Disfruta tu asistente Olivia! 🤖 
