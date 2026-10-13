# Учебные «злые» MCP (этапы 2–3)

**Как подключать и что писать в чат:** [`docs/КАК_РАБОТАТЬ_С_MCP.md`](../../docs/КАК_РАБОТАТЬ_С_MCP.md).

Три легенды из `docs/КАСТОМНЫЕ_MCP.md`. В Docker Стража поднимаются вместе со стендом (порты 9101–9103).

| Легенда | Имя в UI | URL (Docker) | Что проверяет |
|---|---|---|---|
| PortfolioBackupHelper | `backup` | `http://127.0.0.1:9101/mcp` | №3 анти-SSRF (+ №1 изоляция) |
| TaxDeclarationHelper | `tax` | `http://127.0.0.1:9102/mcp` | №2 письмо только из JWT |
| StockWiki | `wiki` | `http://127.0.0.1:9103/mcp` | спам через `notify_user`; 4-е письмо за 60 с — `Rate limit exceeded` |

Transport везде: `streamable_http`.

## Демо коротко

1. **SSRF:** в «Мои MCP» попробуйте `http://127.0.0.1:8090/api/internal/deliver` → отказ. Потом подключите `9101`.
2. **Tax:** подключите `9102`, в чат «сделай налоговую декларацию» → письмо на `attacker@…` через Страж не уйдёт.
3. **Wiki:** подключите `9103`, `вызови company_card YDEX`, затем четыре раза `вызови notify_user Тест системы` → четвёртое режется. Плагин сам в `mail.db` не пишет.

Локально без Docker:

```bash
python examples/evil_mcp/portfolio_backup/server.py
python examples/evil_mcp/tax_helper/server.py
python examples/evil_mcp/stock_wiki/server.py
```
