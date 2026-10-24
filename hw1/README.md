# HW1 — FastAPI Monitoring

## Запуск

### 1. Настройка окружения

Скопировать `env.example` в `.env`.

Установить собственный пароль в переменной `GRAFANA_ADMIN_PASSWORD`.

### 2. Запуск инфраструктуры

```powershell
docker compose up -d --build app victoriametrics grafana
```

Открыть Grafana: http://localhost:3000

Авторизоваться с логином `admin` и паролем из `.env`.

### 3. Создание Service Account Token

1. Создать Service Account `dashboard-generator` с ролью **Editor**.
2. Создать для него токен `dashboard-generator-token`.
3. Скопировать токен в переменную `GRAFANA_SERVICE_ACCOUNT_TOKEN` файла `.env`.

### 4. Запуск всей системы

```powershell
docker compose up -d --build
```