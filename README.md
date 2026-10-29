# Lab 2 — Автоматизація мережевих задач (Docker CLI + Docker Engine API)

Python-утиліта для інвентаризації та перевірки контейнерної мережі
Compose-проєкту ЛР1 через SSH.

## Що робить

1. Підключається до Linux-хоста через SSH (Paramiko) і перевіряє Docker Engine.
2. Збирає інвентаризацію двома незалежними способами:
   - Docker CLI (`docker ps`, `docker inspect`, `docker port`);
   - Docker Engine API через Docker SDK for Python (`client.containers.list`, `container.attrs`).
3. Нормалізує результати до спільної структури й порівнює їх за повним ID.
4. Перевіряє, що лише веб-проксі має публікацію портів на хост.
5. Виконує два HTTP-запити через проксі й фіксує статус і час.
6. Зберігає `inventory.json` і `report.csv`.
7. (Додатково) Періодичний моніторинг із записом у `monitoring.csv`.

## Вимоги

- Python 3.10+
- Docker Engine і Docker Compose на цільовому хості
- SSH-доступ за ключем до цільового хоста
- Проєкт ЛР1, розгорнутий через `docker compose up -d`

## Встановлення

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
