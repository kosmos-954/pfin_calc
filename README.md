# Настройка инфраструктуры GCP для Stellar Bot

В данном документе собраны команды Google Cloud CLI (`gcloud`) для инициализации окружения, настройки прав доступа и развертывания serverless-инфраструктуры (Cloud Functions + Cloud Scheduler) для бота.

Все команды предполагают выполнение в авторизованной консоли (например, GCP Cloud Shell) в контексте выбранного проекта.

## 1. Инициализация проекта

Включение необходимых API для работы функций 2-го поколения и планировщика:
```bash
gcloud services enable \
  cloudfunctions.googleapis.com \
  cloudbuild.googleapis.com \
  run.googleapis.com \
  cloudscheduler.googleapis.com
```

## 2. Настройка сервисного аккаунта (IAM) для GitHub Actions

Создание выделенного сервисного аккаунта для автоматического деплоя из CI/CD и выдача минимально необходимых прав.

**2.1. Задание переменных окружения:**
```bash
PROJECT_ID=$(gcloud config get-value project)
SA_NAME="github-deployer"
SA_EMAIL="${SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"
```

**2.2. Создание аккаунта:**
```bash
gcloud iam service-accounts create $SA_NAME \
  --display-name="GitHub Actions Deploy"
```

**2.3. Назначение ролей:**
```bash
# Право на управление функциями
gcloud projects add-iam-policy-binding $PROJECT_ID \
  --member="serviceAccount:$SA_EMAIL" \
  --role="roles/cloudfunctions.admin"

# Право на управление Cloud Run (требуется для Gen2 функций)
gcloud projects add-iam-policy-binding $PROJECT_ID \
  --member="serviceAccount:$SA_EMAIL" \
  --role="roles/run.admin"

# Право вызывать Cloud Run сервисы (для Scheduler)
gcloud projects add-iam-policy-binding $PROJECT_ID \
  --member="serviceAccount:$SA_EMAIL" \
  --role="roles/run.invoker"

# Право действовать от имени сервисного аккаунта
gcloud projects add-iam-policy-binding $PROJECT_ID \
  --member="serviceAccount:$SA_EMAIL" \
  --role="roles/iam.serviceAccountUser"

# Право на чтение/запись артефактов при сборке
gcloud projects add-iam-policy-binding $PROJECT_ID \
  --member="serviceAccount:$SA_EMAIL" \
  --role="roles/storage.admin"
```

**2.4. Генерация ключа доступа (для сохранения в GitHub Secrets):**
```bash
gcloud iam service-accounts keys create gcp-key.json \
  --iam-account=$SA_EMAIL
  
cat gcp-key.json
# Скопируйте вывод, затем удалите файл: rm gcp-key.json
```

> **GitHub Secrets** (Settings → Secrets → Actions):
> - `GCP_CREDENTIALS` — содержимое gcp-key.json
> - `GCP_PROJECT_ID` — ID проекта GCP
> - `TELEGRAM_BOT_TOKEN` — токен бота
> - `TELEGRAM_CHAT_ID` — ID чата

## 3. Ручной деплой функции (опционально)

Команда для деплоя функции локально или из Cloud Shell (в CI/CD пайплайне эта логика оборачивается в GitHub Action).

```bash
gcloud functions deploy stellar-bot \
  --gen2 \
  --runtime=python311 \
  --region=us-central1 \
  --source=. \
  --entry-point=handle_request \
  --trigger-http \
  --allow-unauthenticated \
  --memory=256MB
```

## 4. Настройка расписания (Cloud Scheduler)

Создание cron-задач для вызова развернутой функции.

> **Важно:** Gen2 функции требуют OIDC-аутентификации. Scheduler должен вызывать функцию от имени сервисного аккаунта с ролью `roles/run.invoker`.

**4.0. Выдача права на вызов функции сервисному аккаунту:**
```bash
gcloud run services add-iam-policy-binding stellar-bot \
  --region=us-central1 \
  --member="serviceAccount:${SA_EMAIL}" \
  --role="roles/run.invoker"
```

**4.1. Получение URL функции:**
```bash
FUNCTION_URL=$(gcloud functions describe stellar-bot \
  --gen2 \
  --region=us-central1 \
  --format="value(serviceConfig.uri)")

echo $FUNCTION_URL
```

**4.2. Задача 1: Мониторинг сети (каждые 15 минут)**
```bash
gcloud scheduler jobs create http trigger-monitor \
  --location="us-central1" \
  --schedule="*/15 * * * *" \
  --time-zone="UTC" \
  --uri="$FUNCTION_URL" \
  --http-method=POST \
  --headers="Content-Type=application/json" \
  --message-body='{"task": "monitor"}' \
  --oidc-service-account-email="$SA_EMAIL"
```

**4.3. Задача 2: Ежедневные напоминания (10:00 UTC)**
```bash
gcloud scheduler jobs create http trigger-reminder \
  --location="us-central1" \
  --schedule="0 10 * * *" \
  --time-zone="UTC" \
  --uri="$FUNCTION_URL" \
  --http-method=POST \
  --headers="Content-Type=application/json" \
  --message-body='{"task": "reminder"}' \
  --oidc-service-account-email="$SA_EMAIL"
```

**4.4. Задача 3: Еженедельный аудит (понедельник, 00:00 UTC)**
```bash
gcloud scheduler jobs create http trigger-audit \
  --location="us-central1" \
  --schedule="0 0 * * 1" \
  --time-zone="UTC" \
  --uri="$FUNCTION_URL" \
  --http-method=POST \
  --headers="Content-Type=application/json" \
  --message-body='{"task": "audit"}' \
  --oidc-service-account-email="$SA_EMAIL"
```

## 5. Эксплуатация и мониторинг

**Принудительный запуск задачи вне расписания (тестирование):**
```bash
gcloud scheduler jobs run trigger-monitor --location="us-central1"
```

**Просмотр логов (Gen2 → Cloud Run логи):**
```bash
# Последние 20 записей
gcloud run services logs read stellar-bot \
  --region=us-central1 \
  --limit=20

# Расширенный фильтр через Cloud Logging
gcloud logging read \
  "resource.type=cloud_run_revision AND resource.labels.service_name=stellar-bot" \
  --limit=20 \
  --format="table(timestamp, textPayload)"
```

**Проверка статуса деплоя:**
```bash
gcloud functions describe stellar-bot --gen2 --region=us-central1
```

**Ручной тест через curl (без scheduler):**
```bash
TOKEN=$(gcloud auth print-identity-token)
curl -X POST "$FUNCTION_URL" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"task": "audit"}'
```

```