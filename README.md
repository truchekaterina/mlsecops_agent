# Режим A — агент без защиты

Для защиты и разбора файлов: **[ДОКУМЕНТАЦИЯ.md](ДОКУМЕНТАЦИЯ.md)** (архитектура, схемы, возможности, как хранятся данные).

Учебный стенд. Слоёв «Стража» нет: паническая продажа и номер счёта проходят.

Агент собран на **LangChain**: `create_agent` (это граф **LangGraph** ReAct). Кнопки — только **MCP**. Чат — в браузере, любой человек регистрируется сам.

## Что нужно заранее

- Ollama: https://ollama.com/download
- модель `qwen2.5:3b` (`ollama pull qwen2.5:3b`)
- Python 3.12 в `.venv`
- `uv` (для MCP ЦБ): https://docs.astral.sh/uv/
- Mailpit: `stand\mailpit.exe` (веб http://127.0.0.1:8025, SMTP :1025)

На этой машине RTX 3050 4 ГБ — GPU **не** используем.

## Запуск сервиса

```powershell
cd "C:\Users\1\Desktop\neurohelp\MLSecOps Сириус"
.\stand\start_mode_a.ps1
```

Откройте http://127.0.0.1:8080

1. Вкладка **Регистрация**: имя, своя почта, пароль.
2. После входа пишите, что купить / продать / какой курс ЦБ.
3. Письма о сделках уходят на **почту из регистрации**, не из текста чата.
4. Учебный ящик Mailpit: http://127.0.0.1:8025. В чате также видна своя почта.
5. Учётки, счета, сделки и копии писем — в `stand/strazh.db` (SQLite). Новый пользователь сразу получает свой счёт 2000 $ / 5000 $. Старые `emails/*.txt` больше не пишутся.

Готовые демо-учётки (не обязательны): `client@demo` / `demo`, `katya@demo` / `katya`.

Терминальный чат по-прежнему: `.\.venv\Scripts\python.exe stand\chat_cli.py`

## Что из LangChain уже внутри

| Кусок | Файл |
|---|---|
| `langchain.agents.create_agent` | `agent_mode_a.py` |
| `ChatOllama` | `agent_mode_a.py` |
| `langchain-mcp-adapters` | тулы MCP как tools агента |
| граф для Agent Chat UI | `langgraph.json` → `make_graph` |

Официальный фронт [Agent Chat UI](https://github.com/langchain-ai/agent-chat-ui) можно потом направить на `langgraph dev` (`http://localhost:2024`, graph id `agent`). Сейчас рабочий сервис — страница на `:8080`, чтобы любой клиент регистрировался и получал письма на свою почту.

## Проверка без чата

```powershell
.\.venv\Scripts\python.exe stand\selfcheck.py
```

В чате после входа: «список акций», «состояние счёта», «какие акции куплены», «курс ЦБ доллара». Агент сам вызывает MCP, к брокеру не отсылает. Стартовый учебный счёт: 2000 $ основной, 5000 $ резерв; бумаги NVDA / AAPL / MSFT.
