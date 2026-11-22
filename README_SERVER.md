# WOW CUP v6 на VPS: 24/7 мониторинг + удалённая CAPTCHA и оплата

**Что это:** Docker-обёртка над экспериментальным `yandex_tickets_checkout_v6`. Она предоставляет постоянный Chromium с визуальным экраном Xvfb, терминалом, браузерным доступом noVNC и сохраняемым профилем. **Не гарантирует реальную бронь**: актуальные DOM-селекторы и Request Payload `orders/create` ещё не подтверждены. В присланных логах create возвращал `booked:false`, reserve после `running` завершался `failed / order-validation-failed`.

## 0. Сначала безопасность

В чате ранее были опубликованы cookies Яндекс ID, session-токены и CSRF. Они потенциально позволяют действовать в рамках чужой авторизованной сессии. **В Яндекс ID → Безопасность → Устройства нажмите «Выйти везде»**, проверьте список устройств, при необходимости смените пароль и заново войдите в браузере бота. Не копируйте опубликованные cookies/CSRF в `.env` или Playwright. Не публикуйте полный HAR/Copy as cURL.

Для сервера нужен отдельный VPS под вашим контролем. Рекомендуем Ubuntu 24.04 LTS, x86_64, 2 vCPU, **4 ГБ RAM**, SSD >= 20 ГБ. Это ориентир, а не гарантия производительности. Публично доступен только SSH; порт 6080 **не открываем**. noVNC защищается SSH-туннелем (с компьютера) или Tailscale Serve (с телефона/ПК).

## 1. Установите Docker (Ubuntu 24.04)

Войдите на сервер по SSH. Используйте учётную запись с `sudo` и авторизацией по SSH-ключу.

```bash
sudo apt update
sudo apt install -y ca-certificates curl unzip
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
sudo tee /etc/apt/sources.list.d/docker.sources >/dev/null <<EOF_DOCKER
Types: deb
URIs: https://download.docker.com/linux/ubuntu
Suites: $(. /etc/os-release && echo "${UBUNTU_CODENAME:-$VERSION_CODENAME}")
Components: stable
Architectures: $(dpkg --print-architecture)
Signed-By: /etc/apt/keyrings/docker.asc
EOF_DOCKER
sudo apt update
sudo apt install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo systemctl enable --now docker
sudo docker compose version
```

Команды установки основаны на официальной инструкции Docker (https://docs.docker.com/engine/install/ubuntu/). Старайтесь не добавлять свою учётную запись в группу `docker`: участники этой группы практически получают root-доступ. Используйте `sudo docker`.

Если настроен UFW, убедитесь, что SSH доступен. **Не** открывайте TCP 6080 / 5900 ни в UFW, ни в панели провайдера. В `compose.yaml` порт 6080 опубликован только на `127.0.0.1` VPS.

## 2. Загрузите архив

Скачайте архив `wowcup_vps_novnc_v6.zip` в ChatGPT на свой ПК. Пример из **PowerShell**:

```powershell
scp .\wowcup_vps_novnc_v6.zip ubuntu@SERVER_IP:/home/ubuntu/
```

Замените `ubuntu` и `SERVER_IP` на свои данные. Затем по SSH на сервере:

```bash
unzip -q ~/wowcup_vps_novnc_v6.zip -d ~
cd ~/yandex_tickets_checkout_v6_vps
cp .env.example .env
nano .env
```

Заполните в `.env`:

```env
VNC_PASSWORD=ABCDEFGH
START_MODE=manual
TELEGRAM_BOT_TOKEN=ВАШ_ТОКЕН
TELEGRAM_CHAT_IDS=111111111,222222222
BOOKING_OWNER_CHAT_ID=111111111
BUYER_FULL_NAME=ваше_ФИО
BUYER_EMAIL=ваш_email
BUYER_PHONE=ваш_телефон
BOOKING_MAX_PRICE_RUB=20000
BOOKING_MAX_TOTAL_RUB=20000
AUTO_BOOKING=false
AUTO_START_SBP=false
BOOKING_HEADLESS=false
CHECK_INTERVAL_SECONDS=10
```

**Это пример, замените `ABCDEFGH` на свои случайные 8 английских букв/цифр.** Классический VNC ограничивает пароль восемью символами, поэтому основной барьер — непубличность порта и SSH/Tailscale. ФИО, email и телефон из переписки намеренно не включены в архив. Для двух мест корректно настройте `BOOKING_SEATS=2` и оба лимита; обычно нужны соседние места. 

Создайте папку с приватными данными (контейнер работает с UID 10001), затем запустите:

```bash
chmod 600 .env
mkdir -p data
sudo chown -R 10001:10001 data
sudo chmod 700 data
sudo docker compose build
sudo docker compose up -d
sudo docker compose ps
```

Первый `docker compose build` скачивает браузерный образ и библиотеки, это может занять время. **Не вставляйте содержимое `.env` в чат.**

## 3. Подключитесь к удалённому экрану с компьютера (через SSH)

На **своём компьютере**, не на сервере, откройте второй терминал:

```bash
ssh -N -L 6080:127.0.0.1:6080 ubuntu@SERVER_IP
```

Оставьте эту SSH-сессию открытой. На своём ПК откройте:

`http://127.0.0.1:6080/vnc.html`

Нажмите Connect, введите **только VNC_PASSWORD**, заданный вами в `.env`. Покажется рабочий стол с терминалом. Внутри него запускается тот же Playwright и тот же браузер, которые будут работать 24/7. VPN или SSH должны защищать сам HTTP/noVNC; **прямую ссылку** `http://SERVER_IP:6080` использовать нельзя и она не должна работать.

Если локальный порт 6080 занят, вместо него используйте `ssh -N -L 16080:127.0.0.1:6080 ...`, а откройте `http://127.0.0.1:16080/vnc.html`.

## 4. Подключитесь с телефона (Tailscale Serve)

Если требуется доступ без постоянного SSH-туннеля с компьютера, установите Tailscale на **сервер и телефон**, войдите в собственную tailnet, затем на VPS:

```bash
# Установите Tailscale по официальной документации для Ubuntu.
sudo tailscale up
sudo tailscale serve --bg http://127.0.0.1:6080
sudo tailscale serve status
```

Tailscale Serve выдаст HTTPS-ссылку `https://<имя-устройства>.<ваша-tailnet>.ts.net`. Откройте её на телефоне **с подключённым Tailscale**, при необходимости добавьте `/vnc.html`. Не используйте `tailscale funnel`, иначе можно открыть доступ из публичного интернета. Отключить Serve: `sudo tailscale serve off`. См. https://tailscale.com/docs/features/tailscale-serve и https://tailscale.com/docs/reference/tailscale-cli/serve. Учтите, что для Serve могут понадобиться HTTPS-сертификаты в настройках tailnet.

В самом VNC будет вторая проверка паролем. Для оплаты через СБП удобнее открывать noVNC на ПК, чтобы сканировать QR-код банковским приложением на телефоне. Если виджет предлагает платёжную ссылку для мобильного устройства, используйте только проверенный способ в том же браузере/сессии — перенос заказа в другой браузер не гарантирован.

## 5. Подготовьте браузер и проверьте бота (обязательный этап)

В `.env` пока оставьте `START_MODE=manual`, `AUTO_BOOKING=false`. В открытом noVNC терминале по очереди:

```bash
python watcher.py --setup
```

Откроется Chromium. Зайдите в свой Яндекс-аккаунт через официальный виджет, **своими руками** пройдите CAPTCHA, если появится, затем вернитесь в терминал бота и нажмите Enter. Профиль сохранится в `data/browser-profile/`. Не скопируйте старые cookies из чата.

Затем (в том же noVNC-терминале):

```bash
python watcher.py --check
python watcher.py --test
python watcher.py --capture
```

`--check`: только чтение API. `--test`: проверка Telegram. `--capture`: ручной проход для подготовки обезличенной структуры интерфейса, **без автоматических кликов**. После осмотра нажмите Enter в терминале, чтобы сохранить `data/diagnostics.jsonl` и `data/ui-structure.json`. Просмотрите файлы перед отправкой: они могут содержать информацию, требующую удаления.

Если чек или логин не работает, не переводите бот в watch-режим.

## 6. Запуск мониторинга 24/7

Остановите старые копии v3–v5 и любые одновременные скрипты с тем же аккаунтом/сеансом. Проверьте, что нет активного/неоплаченного заказа. Затем на VPS (через SSH):

```bash
cd ~/yandex_tickets_checkout_v6_vps
nano .env
```

Для **первого безопасного** запуска рекомендуем:

```env
START_MODE=watch
CHECK_INTERVAL_SECONDS=10
POLL_JITTER_FRACTION=0.10
AUTO_BOOKING=false
AUTO_START_SBP=false
BOOKING_HEADLESS=false
```

Примените изменения:

```bash
sudo docker compose up -d --force-recreate
sudo docker compose ps
sudo ./server/doctor.sh
```

Откройте noVNC. В терминале должен появиться лог мониторинга. **10 секунд — это инженерная настройка экспериментального клиента, не согласованный Яндексом предел и не гарантия отсутствия антибот-ограничений.** В случае `403/429` соблюдаются паузы; CAPTCHA требует человека, и счётчик `availableSeatCount` не равен успешному резервированию.

`AUTO_BOOKING=true` и `AUTO_START_SBP=true` имеют смысл только ПОСЛЕ проверки фактического Request Payload у `/api/tickets/v2/orders/create`, первоначального POST preview и элементов текущего UI. В v6 неизвестное тело create блокируется `CREATE_GUARD_STOP`: это намеренное поведение безопасности, а не сбой noVNC. Здесь не предлагается просто снять проверку и надеяться на удачу.

## 7. CAPTCHA и ручная оплата

1. Бот распознал CAPTCHA → отправляет сообщение в приватный Telegram `BOOKING_OWNER_CHAT_ID` и приостанавливает автоматические действия.
2. Откройте noVNC через SSH/Tailscale. В самом Chromium вручную выполните проверку CAPTCHA.
3. Щёлкните видимый **терминал бота** (не браузер) и нажмите **Enter**. В версии v6 это обязательно: она сама не распознаёт, что человек закончил проверку.
4. Если сервер действительно подтвердил резервирование, в Chromium появится соответствующий этап оплаты. Для СБП на ПК отсканируйте QR с телефона и подтвердите транзакцию в банковском приложении. Бот не вводит банковские данные и не подтверждает платёж.
5. Если показано `order-validation-failed` или `booked:false`, **это не забронированный билет**. Не оплачивайте незнакомый заказ и не повторяйте create вслепую.

Нельзя считать, что 899 секунд — гарантированный срок удержания места: в ваших логах резервирование закончилось неудачей. На сервере важен **статус резерва** и валидность актуального заказа, а не только таймер.

## 8. Логи, ошибки и перезапуск

```bash
cd ~/yandex_tickets_checkout_v6_vps
sudo ./server/doctor.sh
sudo docker compose logs --tail=80 wowcup
sudo docker compose exec -T wowcup tail -n 100 /app/data/watcher.log
```

Если в noVNC терминале появился текст о завершении бота, мониторинг **остановился**: контейнер продолжает показывать удалённый экран для диагностики. Проверьте наличие действующих заказов и только затем решайте, перезапускать ли скрипт. **Это намеренная защита от бесконечного повторного создания заказов.** Если клиент ожидает Enter после CAPTCHA, контейнер может быть здоровым, но опрос фактически приостановлен.

После изменения `.env` или `ui.json`:

```bash
sudo docker compose up -d --force-recreate
```

Но **никогда не пересоздавайте контейнер, пока заказ находится в процессе создания, резервирования или оплаты**. Это уничтожит текущую вкладку. Профиль и `state.json` сохраняются, но браузерная сессия оплаты может не восстановиться.

Для выключения:

```bash
sudo docker compose stop
```

Для запуска после остановки:

```bash
sudo docker compose up -d
```

Для сброса попытки — только после проверки/отмены заказа на стороне Яндекса:

```bash
sudo docker compose exec -it wowcup python watcher.py --reset-attempt
```

Это **не** отмена заказа на Яндексе. После успеха не запускайте новый процесс бронирования того же сеанса.

## 9. Защита данных и резервное копирование

- `.env` содержит Telegram-токен и контакты, `data/browser-profile/` — вход в Яндекс, `data/state.json` — состояние заказа. Не публикуйте и не пересылайте эти файлы, не включайте их в Git и публичные бэкапы.
- `data/` и `.env` должны быть доступны только доверенному администратору; в образ Docker они не копируются (`.dockerignore`).
- Не выставляйте `6080`/`5900` в интернет, не используйте `tailscale funnel`. Помните, что Docker опубликованные порты могут обходить обычные правила UFW, поэтому уже в `compose.yaml` стоит `127.0.0.1`.
- В случае компрометации сервера/браузерного профиля завершите сессии Яндекс ID, смените Telegram-токен через BotFather, удалите старые credentials, затем заново настройте браузер.
- Не запускайте второй контейнер или локальный watcher одновременно: оба могут работать с одним аккаунтом/местами и создавать гонки.

## 10. Что не проверено и какие запросы ещё нужны

Docker Compose, shell-скрипты, локальные Python-тесты можно проверить без живых заказов. Но на текущем стенде не выполнялся реальный `docker compose build/up`, открытие сервера noVNC, успешная CAPTCHA в Яндексе, подтверждённый reserve или реальная оплата.

Данных для прямого checkout пока недостаточно. Новые запросы пользователя:

- `GET /session-order-preview/{id}` — проверка состояния preview, **без Payload**;
- `POST /orders/{id}/log` — логирование события, **не create**;
- `GET /orders/{id}/operation/reserve` — чтение статуса reserve, **без Payload**.

Нужны первоначальный **POST `/api/session-order-preview/`** и **POST `/api/tickets/v2/orders/create`** с `Request Payload`, `Content-Type`, `Method` без cookies, CSRF и токенов. Указанные прежде ответы этих двух первоначальных POST не восстанавливаются из `GET`. У успешного reserve полезно подтвердить итоговый статус и реальный платежный UI, а не синтетическую проверку.

Полезные ссылки:
- https://docs.docker.com/engine/install/ubuntu/
- https://playwright.dev/python/docs/docker
- https://tailscale.com/docs/features/tailscale-serve
- https://yandex.ru/support/id/ru/security
