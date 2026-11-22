#!/usr/bin/env bash
set -u
cd /app
umask 077
printf '\n===============================================\n'
printf 'WOW CUP v6.1 -- удалённая консоль браузера\n'
printf '===============================================\n'
printf 'Все действия происходят в ЭТОМ контейнере.\n'
printf 'Сначала обновите настройки и проверьте бюджет.\n'
if [[ "${START_MODE:-manual}" == "watch" ]]; then
  printf '\nRUN_MODE=watch: запускается мониторинг.\n'
  printf 'Для CAPTCHA: щёлкните браузер, решите проверку, затем\n'
  printf 'вернитесь в ЭТУ консоль и нажмите Enter.\n\n'
  python -u watcher.py 2>&1 | tee -a /app/data/watcher.log
  code=${PIPESTATUS[0]}
  printf '\nБот завершён. Код выхода: %s\n' "$code"
  printf 'ПОЖАЛУЙСТА, проверьте возможный неоплаченный заказ перед повторным запуском.\n'
else
  printf '\nSTART_MODE=manual: бот ещё не мониторит.\n'
  printf 'Для входа и сохранения браузера: python watcher.py --setup\n'
  printf 'Для снимка структуры UI:      python watcher.py --capture\n'
  printf 'Один запрос без оформления:     python watcher.py --check\n'
  printf 'Тест Telegram:                 python watcher.py --test\n'
fi
printf '\nЭта консоль остаётся открытой для ручных команд.\n\n'
exec /bin/bash --noprofile --norc -i
