# Что проверено для VPS-пакета (22 ноября 2026)

- Синтаксис Bash: `bash -n server/entrypoint.sh server/console.sh server/doctor.sh` — OK.
- YAML (`compose.yaml`): распарсен, проверены `127.0.0.1:6080:6080`, `restart: unless-stopped` и `init: true` — OK.
- Python: `python -m compileall -q .` — OK.
- `TEST_BROWSER_PATH=/usr/bin/chromium python -m unittest discover -q` — 67 тестов OK, без пропусков на локальном Chromium.
- ZIP: `zipfile.testzip()` без ошибок; `.env`, токены, состояния, browser-profile и cookies не включены.
- CLI: `python watcher.py --help` — OK.

**НЕ проверялось в текущей среде:**

- Реальный `docker compose build`, так как Docker Engine недоступен в среде сборки. При установке на Ubuntu может потребоваться устранение отличий системных пакетов.
- Подъём Xvfb/x11vnc/websockify из готового Docker-образа и вход через SSH/Tailscale.
- CAPTCHA в живом виджете, резервирование места на Яндексе и оплата по СБП.
- Автоматические селекторы текущего UI и Request Payload. Используемый checkout v6 всё ещё экспериментальный.

**Безопасность:** трафик noVNC транслируется только на loopback VPS, внешнее подключение — исключительно через SSH-туннель или приватный Tailscale Serve. Перед запуском обязательно задать VNC_PASSWORD в `.env` и отозвать ранее опубликованные сессионные cookies.


## v6.1: лимит до двух билетов
Добавлено 12 локальных тестов сценариев 1–2 места, разные сектора/ряды, лимиты цены и конфигурация. Всего 79 юнит/интеграционных проверок офлайн, включая 4 браузерных при наличии установленного Chromium. Проверка живой покупки и брони Яндексом не проводилась.
