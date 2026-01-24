# Інструкція з деплою на Northflank

## ✅ Що вже зроблено

1. **Dockerfile** - оновлено для хмарного деплою
2. **run.sh** - оновлено для нормального Linux середовища
3. **forwarder.py** - додано підтримку environment variables та StringSession
4. **make_session.py** - оновлено для генерації StringSession

## 📋 Кроки для деплою

### 1. Підготуй сесію Telegram (ВАЖЛИВО!)

**Варіант A: StringSession (рекомендовано для хмари)**

```bash
# 1. Онови API_ID та API_HASH у make_session.py
# 2. Переконайся що у тебе є файл tele_alert.session (запусти forwarder.py локально один раз)
# 3. Згенеруй StringSession:
python3 make_session.py
```

Скопіюй виведений `SESSION_STRING` - він знадобиться для Northflank.

**Варіант B: Файл сесії (тільки якщо репо приватне!)**

Якщо хочеш використати файл `tele_alert.session`:
- Поклади його в репо поруч з `forwarder.py`
- Додай в Dockerfile: `COPY tele_alert.session /opt/tele_alert.session`

### 2. Налаштуй Environment Variables

У Northflank потрібно встановити такі **Runtime Secrets**:

**Обов'язкові:**
- `TG_API_ID` - твій Telegram API ID (число)
- `TG_API_HASH` - твій Telegram API Hash (рядок)
- `TG_TARGET_CHANNEL` - ID цільового каналу (число, з мінусом для приватних)
- `TG_SESSION_STRING` - StringSession (якщо використовуєш Варіант A)

**Опціональні:**
- `TG_SESSION` - ім'я файлу сесії (за замовчуванням: `tele_alert`)
- `TG_SOURCE_CHANNELS` - список каналів через кому (за замовчуванням: стандартний список)
- `ALERT_API_URL` - URL API для перевірки тривог (за замовчуванням: Kyiv)
- `TEST_MODE` - тестовий режим: встанови `true` щоб форвардити всі повідомлення незалежно від тривоги (для тестування)

### 3. Деплой на Northflank

1. Створи проект на [northflank.com](https://northflank.com)
2. Створи **Combined Service** (Git repo → build → run)
3. Підключи GitHub репозиторій
4. Northflank автоматично збере Dockerfile і запустить контейнер

**Налаштування сервісу:**
- **Instances:** 1
- **Autoscale:** off
- **Restart on failure:** on

### 4. Перевірка

Після деплою перевір логи в Northflank. Маєш побачити:
- ✅ "Using StringSession for authentication" (або "Using session file")
- ✅ "Already authorized"
- ✅ "Resolved source channel: ..."
- ✅ "Starting alert monitor..."
- ✅ "Listening to X channels..."

## 🔧 Типові проблеми

### ❌ "Authorization required" і зависання
**Причина:** У контейнері немає валідної сесії  
**Рішення:** Переконайся що `TG_SESSION_STRING` встановлено правильно, або файл сесії скопійовано в контейнер

### ❌ "Invalid API ID or API Hash"
**Причина:** Неправильні ключі  
**Рішення:** Перевір що `TG_API_ID` - це число, а `TG_API_HASH` - рядок

### ❌ Нічого не форвардить
**Можливі причини:**
1. Зараз немає активної тривоги (це нормально!)
2. Бот не має доступу до target channel
3. Бот не підписався на source channels

Перевір логи - там буде видно чи є тривога і чи отримує бот повідомлення.

### 🧪 Тестування: як перевірити що бот форвардить
**Для тестування роботи бота без очікування тривоги:**

1. Встанови `TEST_MODE=true` в Runtime Secrets у Northflank
2. Перезапусти сервіс
3. У логах побачиш: `🧪 TEST MODE ENABLED - All messages will be forwarded regardless of alert status!`
4. Тепер бот буде форвардити **всі** повідомлення з source channels
5. Після тестування встанови `TEST_MODE=false` або видали змінну

**Важливо:** Не забудь вимкнути тестовий режим після перевірки!

## 📝 Приклад налаштування в Northflank

```
Runtime Secrets:
  TG_API_ID = 25989420
  TG_API_HASH = 13144bcea846e6c8b3fe92b185ffde1f
  TG_TARGET_CHANNEL = -1002899760792
  TG_SESSION_STRING = 1A... (з make_session.py)
  TG_SOURCE_CHANNELS = war_monitor,Ukraine_UA_24_7,kievreal1,...
```

## 🎯 Готово!

Після деплою твій бот буде працювати 24/7 у хмарі, навіть коли немає світла локально.
