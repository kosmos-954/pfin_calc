# PFIN Stellar Monitor Bot

Автоматизированный мониторинг выплат и выкупа токена **PFIN** на блокчейне Stellar.

## Описание

Бот отслеживает целевой кошелёк на Stellar и контролирует соблюдение программы выплат держателям токена PFIN и программы выкупа токенов.


**Логика программы:**
- Каждое **1-е число месяца** каждый холдер PFIN получает выплату в USDM (1 PFIN = 1 USDM) в размере `N%` от своего баланса
- Каждое **1-е число месяца** выставляется ордер на выкуп PFIN за USDM на эквивалентную сумму
- Ставка `N%` начинается с `base_rate_pct` (0.1%) в год старта и увеличивается на `base_rate_pct` каждый следующий год
- Кошелёк-эмитент (`target_account`) исключается из расчётов как получатель

**Конфигурация** (`config.yaml`):
| Параметр | Описание |
|---|---|
| `schedule_start` | Дата начала обязательства (не дата первой выплаты) |
| `payment_delay_months` | Через сколько месяцев после старта — первая выплата |
| `base_rate_pct` | Ставка в год старта, % (по умолчанию 0.1) |
| `monitor_window_minutes` | Окно мониторинга в минутах (= периодичность крона) |

---

## Команды (`--task`)

### `monitor` — Мониторинг за окно времени
Сканирует операции целевого кошелька за последние `monitor_window_minutes` минут.
Отправляет **одно итоговое сообщение** если были события:
- Исходящие выплаты в USDM → суммарный объём + количество уникальных кошельков получателей
- Ордера на выкуп/продажу в паре PFIN/USDM → список с ценой и объёмом

```
python bot.py --task monitor
```

---

### `audit` — Аудит выплат с момента старта
Сравнивает **ожидаемые** выплаты и выкупы (по графику) с **фактическими** из блокчейна.

Особенности:
- **FIFO-атрибуция**: платёж, пришедший позже срока, автоматически закрывает самое старое невыполненное обязательство — например, платёж от 10 февраля закрывает долг январского периода
- **Зачёт переплаты**: переплата в одном периоде уменьшает обязательство следующего
- Отображает просрочку исполнения для выполненных платежей и текущую просрочку для невыполненных

Формат вывода — разбивка по датам:
```
📅 01.11.2025  ставка 0.1%
  💸 Выплата: 500.0000 / 500.0000 USDM  ✅
     ⏰ Просрочка исполнения: +5 дн. (завершено: 06.11.2025)
  🔄 Выкуп:   500.0000 / 500.0000 USDM  ✅

━━━ Итоговая задолженность ━━━
💸 Выплаты: ❌ долг 500.0000 USDM
🔄 Выкуп  : ✅ без задолженности
```

```
python bot.py --task audit
```

---

### `calculate` — Расчёт ожидаемых сумм
Запрашивает текущих холдеров PFIN из блокчейна, вычисляет ставку для текущего года и отправляет в Telegram ожидаемые суммы на **следующую дату выплат**:
- Итоговая сумма выплат всем холдерам
- Сумма ордера выкупа
- Общий отток из кошелька

```
python bot.py --task calculate
```

---

### `reminder` — Напоминание накануне выплат
Запускается ежедневно. Если **завтра 1-е число** и дата >= первой плановой выплаты — отправляет в Telegram напоминание с расчётом ожидаемых сумм. В остальные дни ничего не делает.

```
python bot.py --task reminder
```

---

### `debug` — Дамп операций
Выводит в консоль все операции целевого кошелька за последние 30 дней с типом, активом, суммой и адресами. Используется для диагностики: проверить какой тип операций (`payment`, `manage_sell_offer` и т.д.) фактически присутствует в блокчейне.

```
python bot.py --task debug
```

---

## Переменные и аргументы

Приоритет источников токена и chat_id: **CLI > env > config.yaml**

```bash
# Через аргументы CLI
python bot.py --task audit --token 123:ABC --chat-id -100123456

# Через переменные окружения (GCP Cloud Functions)
export TELEGRAM_BOT_TOKEN="123:ABC"
export TELEGRAM_CHAT_ID="-100123456"
python bot.py --task audit
```

---

# Настройка инфраструктуры GCP

В данном документе собраны команды Google Cloud CLI (`gcloud`) для инициализации окружения, настройки прав доступа и развертывания serverless-инфраструктуры (Cloud Functions + Cloud Scheduler) для бота.

Все команды предполагают выполнение в авторизованной консоли (например, GCP Cloud Shell) в контексте выбранного проекта.

## 1. Инициализация проекта

Включение необходимых API для работы функций 2-го поколения и планировщика.

> Выполняется **один раз** от имени владельца проекта (не сервисного аккаунта).

```bash
gcloud services enable \
  cloudresourcemanager.googleapis.com \
  cloudfunctions.googleapis.com \
  cloudbuild.googleapis.com \
  run.googleapis.com \
  cloudscheduler.googleapis.com \
  billingbudgets.googleapis.com \
  artifactregistry.googleapis.com
```

> `cloudresourcemanager.googleapis.com` должен быть включён **до** первого запуска GitHub Actions — без него деплой упадёт с ошибкой `does not have permission`.

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

# Право включать API (нужно для шага Enable required APIs в CI)
gcloud projects add-iam-policy-binding $PROJECT_ID \
  --member="serviceAccount:$SA_EMAIL" \
  --role="roles/serviceusage.serviceUsageAdmin"
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

> Функция разворачивается **с аутентификацией** (без `--allow-unauthenticated`) — вызов только через OIDC-токен (Scheduler или curl с токеном).

```bash
gcloud functions deploy stellar-bot \
  --gen2 \
  --runtime=python311 \
  --region=us-central1 \
  --source=. \
  --entry-point=handle_request \
  --trigger-http \
  --no-allow-unauthenticated \
  --memory=512MB \
  --timeout=540s
```

## 4. Настройка расписания (Cloud Scheduler)

Создание cron-задач для вызова развернутой функции.

> **Важно:** Gen2 функции требуют OIDC-аутентификации. Scheduler должен вызывать функцию от имени сервисного аккаунта с ролью `roles/run.invoker`.

**4.0. Выдача права на вызов функции сервисному аккаунту:**

> Роль `roles/run.invoker` уже выдана на уровне проекта в п. 2.3. Эта команда дублирует её на уровне конкретного сервиса — нужна только если хотите ограничить права одним сервисом вместо всего проекта.

```bash
gcloud run services add-iam-policy-binding stellar-bot \
  --region=us-central1 \
  --member="serviceAccount:${SA_EMAIL}" \
  --role="roles/run.invoker"
```

**4.1. Получение URL функции:**
```bash
FUNCTION_URL=$(gcloud functions describe stellar-bot \
  --v2 \
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
  --oidc-service-account-email="$SA_EMAIL" \
  --attempt-deadline=120s
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
  --oidc-service-account-email="$SA_EMAIL" \
  --attempt-deadline=180s
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
  --oidc-service-account-email="$SA_EMAIL" \
  --attempt-deadline=540s
```

**4.5. Задача 4: Расчёт ожидаемых сумм (каждое 28-е число, 09:00 UTC)**

> Отправляет в Telegram расчёт выплат на следующий месяц заранее, перед днём выплат.

```bash
gcloud scheduler jobs create http trigger-calculate \
  --location="us-central1" \
  --schedule="0 9 28 * *" \
  --time-zone="UTC" \
  --uri="$FUNCTION_URL" \
  --http-method=POST \
  --headers="Content-Type=application/json" \
  --message-body='{"task": "calculate"}' \
  --oidc-service-account-email="$SA_EMAIL" \
  --attempt-deadline=300s
```

## 5. Эксплуатация и мониторинг

### 5.0. Диагностика после деплоя

Если `gcloud run services list --region=us-central1` возвращает `Listed 0 items` после деплоя через GitHub Actions:

```bash
# 1. Проверить в каком проекте вы находитесь локально
gcloud config get-value project

# GCP_CREDENTIALS в GitHub Actions могут указывать на ДРУГОЙ проект.
# project_id содержится в JSON-файле секрета GCP_CREDENTIALS.

# 2. Переключиться в нужный проект
gcloud config set project YOUR_PROJECT_ID

# 3. Проверить список функций Gen2
gcloud functions list --v2 --regions=us-central1

# 4. Проверить список функций Gen1 (если случайно задеплоилась как Gen1)
gcloud functions list --regions=us-central1

# 5. Получить статус и URL конкретной функции
gcloud functions describe stellar-bot \
  --v2 \
  --region=us-central1 \
  --format="table(name, state, serviceConfig.uri)"
```

> **Частая причина:** GitHub Actions деплоил в проект из `GCP_CREDENTIALS`, а локальный `gcloud` смотрит в другой проект. Проверьте PROJECT_ID в содержимом секрета `GCP_CREDENTIALS` (поле `"project_id"`).

---**Принудительный запуск задачи вне расписания (тестирование):**
```bash
gcloud scheduler jobs run trigger-audit --location="us-central1"
```

**Просмотр логов (Gen2 → Cloud Run логи):**
```bash
# Последние 20 записей
gcloud run services logs read stellar-bot \
  --region=us-central1 \
  --limit=20

# Расширенный фильтр через Cloud Logging (выполнять в Cloud Shell / Linux)
gcloud logging read \
  "resource.type=cloud_run_revision AND resource.labels.service_name=stellar-bot" \
  --limit=20 \
  --format="table(timestamp, textPayload)"
```

**Проверка статуса деплоя:**
```bash
gcloud functions describe stellar-bot --v2 --region=us-central1
```

**Ручной тест через curl (без scheduler):**
```bash
TOKEN=$(gcloud auth print-identity-token)
FUNCTION_URL=$(gcloud functions describe stellar-bot \
  --v2 --region=us-central1 \
  --format="value(serviceConfig.uri)")

curl -X POST "$FUNCTION_URL" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"task": "audit"}'
```

---

## 6. Защита от несанкционированных вызовов и контроль бюджета

### 6.1. Запрет публичных вызовов функции

> ⚠️ **Порядок важен:** Cloud Run сервис создаётся только при первом деплое функции. Все команды этого раздела выполняются **после** успешного деплоя (раздел 3 или GitHub Actions).

**Убедиться что сервис существует и найти его точное имя:**
```bash
# Проверить что сервис задеплоен
gcloud run services list --region=us-central1

# Если пусто — сначала выполните деплой из раздела 3
```

**Проверить, что публичный доступ отсутствует:**
```bash
gcloud run services get-iam-policy stellar-bot --region=us-central1
# В выводе НЕ должно быть "allUsers" или "allAuthenticatedUsers"
```

**Явно зафиксировать IAM-политику сервиса (только SA-планировщика).**

Создать файл `iam-policy.yaml` (замените `PROJECT_ID`):
```yaml
version: 1
bindings:
- role: roles/run.invoker
  members:
  - serviceAccount:github-deployer@PROJECT_ID.iam.gserviceaccount.com
```

```bash
# Убедиться что PROJECT_ID совпадает с реальным проектом
echo $PROJECT_ID
# или: gcloud config get-value project

# Применить политику (только после успешного деплоя)
gcloud run services set-iam-policy stellar-bot \
  --region=us-central1 \
  iam-policy.yaml
```

> Файл `iam-policy.yaml` не нужен в репозитории — добавьте его в `.gitignore`.

**Ограничить максимальное число одновременных экземпляров (защита от флуда):**
```bash
gcloud run services update stellar-bot \
  --region=us-central1 \
  --max-instances=1
```

---

### 6.2. Бюджет и контроль расходов

```bash
# 1. Включить Billing Budgets API
gcloud services enable billingbudgets.googleapis.com

# 2. Получить ID биллинг-аккаунта
#    name возвращает "billingAccounts/XXXXXX-XXXXXX-XXXXXX" — отрезаем префикс
BILLING_ID=$(gcloud billing accounts list \
  --filter="open=true" \
  --format="value(name)" \
  | head -1 \
  | sed 's|billingAccounts/||')
echo "Billing account ID: $BILLING_ID"
# Должно быть вида: XXXXXX-XXXXXX-XXXXXX (без префикса billingAccounts/)

# 3. Создать бюджет $1/мес с уведомлениями при 50%, 90%, 100%
#    percent: float от 0.0 до 1.0 (0.5 = 50%, 1.0 = 100%)
gcloud billing budgets create \
  --billing-account="$BILLING_ID" \
  --display-name="stellar-bot-budget" \
  --budget-amount="1.00USD" \
  --threshold-rule="percent=0.5" \
  --threshold-rule="percent=0.9" \
  --threshold-rule="percent=1.0"
```

> Уведомления отправляются на email биллинг-аккаунта. Для Telegram-уведомлений потребуется отдельно настроить Pub/Sub → Cloud Function.

**Рекомендуется: развернуть бота в изолированном проекте GCP** — тогда при превышении бюджета можно безопасно отключить биллинг только для него:

```bash
# Создать и настроить отдельный проект
gcloud projects create pfin-stellar-bot --name="PFIN Stellar Bot"
gcloud config set project pfin-stellar-bot

# Привязать биллинг-аккаунт
gcloud billing projects link pfin-stellar-bot \
  --billing-account="$BILLING_ID"
```

---

### 6.3. Чеклист безопасности

| | Мера |
|---|---|
| ☐ | Функция деплоится с `--no-allow-unauthenticated` |
| ☐ | Scheduler вызывает через `--oidc-service-account-email` |
| ☐ | IAM-политика сервиса содержит только `github-deployer` SA (`iam-policy.yaml` применён) |
| ☐ | `max-instances=1` установлен на Cloud Run сервисе |
| ☐ | Бюджет $1/мес с уведомлениями создан |
| ☐ | Бот деплоится в изолированный проект GCP |
| ☐ | Секреты (`GCP_CREDENTIALS`, токены) не закоммичены в репозиторий |
| ☐ | `config.yaml` и `iam-policy.yaml` добавлены в `.gitignore` |
