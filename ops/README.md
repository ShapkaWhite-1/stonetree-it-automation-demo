# Linux deployment and operations runbook

Этот раздел показывает, как безопасно разместить и сопровождать CLI-инструмент на Linux-сервере. Команды являются шаблоном: пути, пользователя и способ хранения секретов нужно согласовать с политиками конкретной компании.

## 1. Установка через SSH

Рекомендуемая схема:

- код: `/opt/stonetree-it-automation`;
- данные: `/var/lib/stonetree-it-automation`;
- резервные копии: `/var/backups/stonetree-it-automation`;
- переменные окружения: `/etc/stonetree-it-automation/environment`;
- отдельный непривилегированный пользователь: `stonetree-automation`.

После получения кода создаётся виртуальное окружение и выполняется offline-проверка:

```bash
cd /opt/stonetree-it-automation
python3 -m venv .venv
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python main.py demo
```

Файл `/etc/stonetree-it-automation/environment` не должен попадать в Git. Минимальные права:

```bash
sudo chown root:stonetree-automation /etc/stonetree-it-automation/environment
sudo chmod 640 /etc/stonetree-it-automation/environment
```

Пример содержимого:

```text
BITRIX_WEBHOOK_URL=https://company.bitrix24.com/rest/USER_ID/WEBHOOK_TOKEN/
AUDIT_DB_PATH=/var/lib/stonetree-it-automation/operations.sqlite3
```

Перед первым реальным запуском webhook проверяется на минимально необходимые права. Его значение нельзя передавать в аргументах командной строки или выводить в журнал.

## 2. systemd и журналы

`stonetree-it-smoke-test.service` запускает безопасный offline demo от отдельного пользователя. После копирования unit-файла:

```bash
sudo systemctl daemon-reload
sudo systemctl start stonetree-it-smoke-test.service
sudo systemctl status stonetree-it-smoke-test.service
sudo journalctl -u stonetree-it-smoke-test.service --since today
```

Для реальных onboarding/offboarding запросов лучше использовать внутреннюю очередь или утверждённый оператором запуск, а не хранить персональные данные сотрудника непосредственно в unit-файле.

## 3. Резервное копирование SQLite

Копирование файла работающей SQLite-базы обычной файловой командой может создать несогласованный backup. Скрипт `backup-audit-db.sh` использует команду SQLite `.backup`, проверяет целостность копии и удаляет архивы старше заданного срока.

```bash
sudo install -m 750 ops/backup-audit-db.sh /usr/local/sbin/stonetree-backup-audit-db
sudo install -m 644 ops/stonetree-it-backup.service /etc/systemd/system/
sudo install -m 644 ops/stonetree-it-backup.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now stonetree-it-backup.timer
sudo systemctl list-timers stonetree-it-backup.timer
```

Резервная копия считается рабочей только после тестового восстановления в отдельный путь и выполнения `PRAGMA integrity_check`.

## 4. Сеть, DNS, SSL и nginx

Текущий CLI не слушает TCP-порт, поэтому nginx ему не нужен. Если появится внутренний HTTP adapter, приложение должно слушать только loopback-адрес, например `127.0.0.1:8000`, а nginx — завершать TLS и проксировать запросы.

Порядок проверки после развёртывания API:

```bash
ss -lntp
curl -fsS http://127.0.0.1:8000/health
sudo nginx -t
dig +short automation.example.com
openssl s_client -connect automation.example.com:443 -servername automation.example.com
curl -fsS https://automation.example.com/health
```

Если nginx возвращает `502 Bad Gateway`:

1. Проверить, запущен ли backend и слушает ли ожидаемый адрес через `ss -lntp`.
2. Вызвать `/health` напрямую по loopback.
3. Проверить `proxy_pass` и выполнить `nginx -t`.
4. Проверить `journalctl` приложения и error log nginx.
5. Проверить firewall/SELinux/AppArmor, если loopback-вызов работает, а проксирование — нет.

Для DNS сначала уменьшается TTL, затем создаётся запись, проверяется распространение через `dig`, и только после этого выпускается сертификат. Закрытый ключ TLS должен быть доступен только root и nginx. Автоматическое продление сертификата проверяется тестовым запуском клиента ACME.

## 5. Контроль после изменений

Минимальный checklist:

- тесты и offline demo завершились успешно;
- в Git и логах нет webhook URL и других секретов;
- процесс работает от непривилегированного пользователя;
- каталог данных доступен на запись только сервисному пользователю;
- backup создан, проверен и хотя бы один раз восстановлен;
- настроены ограничения хранения журналов и резервных копий;
- для HTTP API настроены health check, TLS и мониторинг срока сертификата.
