# Запуск «Стража» через Docker

Нужен только Docker Desktop (Windows, macOS) или Docker Engine (Linux). Python, Ollama и `uv` на компьютере ставить не нужно.

В образ **не входят** папка `отчет по проделанной работе`, `.venv` и локальные базы. Сборка копирует код `src`, страницы `static` и `entrypoint.py`.

## Быстрый запуск из этого репозитория

В корне проекта:

```bash
docker compose up
```

Первый раз скачает образы и модель `qwen2.5:3b` (около 2 ГБ). Дальше открывается сразу.

- Страж: http://localhost:8080
- Почта: http://127.0.0.1:8090

Порт почты опубликован только на `127.0.0.1` этого компьютера. Секрет почты контейнер записывает в том `strazh-data`, файл `mail_secret`. В репозиторий и в переменные compose он не кладётся. Торговый JWT от него отдельный. Сначала заведите ящик на :8090, потом регистрируйтесь в Страже на эту почту. Демо-вход: `client@demo` / `demo` и `katya@demo` / `katya`.

Остановка: `Ctrl+C`, затем при необходимости `docker compose down`. Счета и письма остаются в томе `strazh-data`.

Локальный `start_mode_a.ps1` в корне репозитория по-прежнему работает без Docker.

## Скачать готовый стек с Docker Hub

После того как образ Стража выложен:

```bash
docker compose pull
docker compose up
```

По умолчанию берётся `rinakt/strazh:latest`. Другой тег: `STRAZH_IMAGE=другой/образ:latest`.

Официальный `ollama/ollama` тоже берётся с Docker Hub. Модель качается при первом старте и остаётся в томе `ollama`.

## Как выложить образ на Docker Hub

1. Войдите: `docker login`
2. Соберите и отправьте:

```bash
docker compose build
docker tag rinakt/strazh:latest rinakt/strazh:latest
docker push rinakt/strazh:latest
```

Если собирали локальный тег `strazh-stand:latest`:

```bash
docker tag strazh-stand:latest rinakt/strazh:latest
docker push rinakt/strazh:latest
```

Другому человеку достаточно `docker-compose.yml` и `docker compose up`.

## Что слушает контейнер

| Порт | Сервис |
|---|---|
| 8080 | Страж (чат, вход, заявки) |
| 8090 | Почта, на хосте только `127.0.0.1` |

На хосте порт 8090 не слушает другие интерфейсы. Внутри контейнера почтовый процесс принимает проброс Docker со своих интерфейсов.

Ollama внутри сети compose не публикуется наружу. Исходящий HTTPS к MOEX и ЦБ должен быть разрешён.
