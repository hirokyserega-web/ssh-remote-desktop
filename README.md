# SSH Remote Desktop

Кросс-платформенный клиент-сервер для удалённого графического доступа к
**Linux**-машинам. Транспорт — **только SSH** (никаких VNC/RDP), поверх
одного соединения мультиплексируются видео, ввод, буфер обмена и передача
файлов. Сервер умеет работать как под **X11** (Xvfb, XTEST, XDamage,
XFixes), так и под **Wayland** (PipeWire + xdg-desktop-portal,
wlr-протоколы, `uinput` / `ydotool`). Клиент собирается под **Windows** и
**Linux** (Qt / PySide6, корректно работает и под X11, и под Wayland).

---

## Содержание

1. [Архитектура](#архитектура)
2. [Требования](#требования)
3. [Быстрый старт](#быстрый-старт)
4. [Установка](#установка)
   - [Установка одной командой (curl / PowerShell)](#установка-одной-командой-curl--powershell)
   - [Установка вручную (extras)](#установка-вручную-extras)
5. [Запуск сервера](#запуск-сервера)
   - [Демон и systemd](#демон-и-systemd)
   - [Графическая панель (rd-server-gui)](#графическая-панель-rd-server-gui)
   - [Под X11 (Xvfb)](#под-x11-xvfb)
   - [Под Wayland (headless-композитор)](#под-wayland-headless-композитор)
6. [Запуск клиента](#запуск-клиента)
   - [На Linux X11 / Wayland](#на-linux-x11--wayland)
   - [На Windows](#на-windows)
7. [SSH-ключи](#ssh-ключи)
8. [Буфер обмена](#буфер-обмена)
9. [Общие папки и передача файлов](#общие-папки-и-передача-файлов)
10. [Многопользовательский режим и сессии](#многопользовательский-режим-и-сессии)
11. [Конфигурация](#конфигурация)
12. [Сборка исполняемых файлов](#сборка-исполняемых-файлов)
13. [Troubleshooting](#troubleshooting)
14. [Безопасность](#безопасность)
15. [Лицензия](#лицензия)

---

## Архитектура

```
[Клиент: Windows / Linux X11+Wayland]  ──SSH──►  [Сервер: Linux X11 / Wayland]
  │ GUI (PySide6, xcb/wayland)                     │ Backend X11|Wayland
  │ Генерация SSH-ключей                           │ Захват экрана
  │ Декодер кадров (pyav / JPEG-delta)             │ Эмуляция ввода
  │ Отправка ввода                                 │ Кодер кадров
  │ Буфер обмена (QClipboard)                      │ Буфер: xclip / wl-clipboard
  │ Файловый менеджер (SFTP)                       │ SFTP-сервер (jail)
  └── SSH-мультиплекс: control / video / input / clipboard / files (SFTP) ──┘
```

См. подробности протокола в `file 'common/protocol.py'` (формат фрейма) и
`file 'common/messages.py'` (словарь управляющих сообщений).

---

## Требования

### Сервер (Linux)

- Python 3.11+
- Системные пакеты — зависят от выбранного backend'а.

**X11:**

```bash
sudo apt install -y libx11-6 libxext6 libxtst6 libxfixes3 libxdamage1 \
  xauth xclip xvfb x11-utils
# (опционально) для H.264:
sudo apt install -y ffmpeg
```

**Wayland:**

```bash
sudo apt install -y pipewire xdg-desktop-portal \
  xdg-desktop-portal-gnome | xdg-desktop-portal-kde | xdg-desktop-portal-wlr \
  wl-clipboard ydotool sway   # sway --headless, как headless-композитор
# для /dev/uinput (виртуальный ввод):
sudo usermod -aG input $USER
```

### Клиент

- **Linux:** Qt-плагины для X11 и Wayland (обычно идут в комплекте с
  PySide6).
- **Windows:** ничего дополнительного — Qt-плагин `windows` встроен в
  PySide6.

---

## Быстрый старт

```bash
# 1. Клонируем
git clone https://github.com/hirokyserega-web/ssh-remote-desktop.git
cd ssh-remote-desktop

# 2. Ставим зависимости
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
# На Linux (сервер/X11/Wayland) дополнительно:
#   pip install -r requirements-linux.txt

# 3. На сервере запускаем (под root, чтобы иметь права на PAM и запуск Xvfb)
sudo -E .venv/bin/python -m server --port 2222

# 4. На клиенте подключаемся
.venv/bin/python -m client --host 192.168.1.10 --port 2222 --user alice --auth key
```

---

## Установка

### Установка одной командой (curl / PowerShell)

Инсталляторы в `scripts/` сами ставят системные зависимости, создают venv,
`pip install` проект и симлинкают `rd-server` / `rd-client` в `~/.local/bin`.

**Универсально** (авто-определение ОС/дистрибутива, клиент + сервер):

```bash
curl -fsSL https://raw.githubusercontent.com/hirokyserega-web/ssh-remote-desktop/main/scripts/install.sh | bash
```

Сборка бинарей (system-пакеты + editable-чекаут + Nuitka):

```bash
curl -fsSL https://raw.githubusercontent.com/hirokyserega-web/ssh-remote-desktop/main/scripts/install.sh | bash -s -- --both --build
```

**Только клиент (Linux):**

```bash
curl -fsSL https://raw.githubusercontent.com/hirokyserega-web/ssh-remote-desktop/main/scripts/install-client-linux.sh | bash
```

**Только сервер (Linux, нужен sudo):**

```bash
curl -fsSL https://raw.githubusercontent.com/hirokyserega-web/ssh-remote-desktop/main/scripts/install-server-linux.sh | sudo bash
```

**Windows (PowerShell):**

```powershell
irm https://raw.githubusercontent.com/hirokyserega-web/ssh-remote-desktop/main/scripts/install.ps1 | iex
```

Флаги `install.sh`: `--dev` (git clone) / `--run` (release-тарбол, по умолчанию) /
`--both`, `--build|--no-build`, `--dir PATH`, `--python BIN`, `--uninstall`.
Если релиз-тег `v{VERSION}` не существует, инсталлятор автоматически
откатывается на `archive/refs/heads/main.tar.gz` — установка не падает с 404.

### Установка вручную (extras)

Пакет разбит на опциональные extras, чтобы клиент можно было установить на
Windows/macOS без Linux-зависимостей, а сервер — на Linux без GUI-библиотек.

#### Только клиент (Windows / macOS / Linux)

```bash
pip install .[client]
```

#### Сервер (Linux: X11 + Wayland)

```bash
pip install .[server]
```

#### Всё сразу (dev / Linux-десктоп)

```bash
pip install -e .[client,server,dev]
```


Пакет разбит на опциональные extras, чтобы клиент можно было установить на
Windows/macOS без Linux-зависимостей, а сервер — на Linux без GUI-библиотек.

### Только клиент (Windows / macOS / Linux)

```bash
pip install .[client]
```

### Сервер (Linux: X11 + Wayland)

```bash
pip install .[server]
```

### Всё сразу (dev / Linux-десктоп)

```bash
pip install -e .[client,server,dev]
```

---

## Запуск сервера

```bash
# Минимум: запустить на порту 2222
sudo -E python -m server

# С явными параметрами
sudo -E python -m server --port 2222 --backend auto --fps 30 \
  --shared-dir ~/shared --max-sessions 5
```

При первом запуске сервер автоматически сгенерирует SSH host-ключ
(`~/.config/ssh-remote-desktop/host_ed25519`). Если планируете ставить
клиент в production — лучше заранее положить стабильный ключ по этому пути.

### Демон и systemd

Помимо запуска в foreground, сервер умеет работать как классический демон
(double-fork + `setsid`) и как systemd-юнит:

```bash
# Запустить в фоне (PID пишется в --pidfile, по умолчанию /run/... или
# ~/.config/ssh-remote-desktop/rd-server.pid при запуске без root).
sudo rd-server --daemon --port 2222
sudo rd-server --status     # state / pid / port
sudo rd-server --stop       # SIGTERM + ожидание выхода

# Установить/удалить системный юнит (генерирует packaging/systemd/*.service
# с реальным путём к бинарю и опциональным --config).
sudo rd-server --install-service
sudo rd-server --uninstall-service
sudo rd-server --enable-service     # автозапуск при загрузке
```

Под systemd сервер всегда работает в foreground (без double-fork), а логи
собирает journald. Юнит запускается от root (нужны PAM, Xvfb, setuid), сам
сервер сбрасывает привилегии до подключающегося пользователя.

### Графическая панель (rd-server-gui)

Для управления сервером без редактирования TOML вручную есть отдельная Qt
панель — `rd-server-gui` (PySide6, те же тема и RU/EN, что у клиента):

```bash
rd-server-gui                    # окно настроек
rd-server-gui --tray             # свернуть в системный трей
rd-server-gui --minimized        # стартовать свёрнутым в трей
```

Панель показывает: сеть (host/port/backend), лимиты сессий, кодирование
(codec/fps/bitrate), общую папку, тумблеры аутентификации
(`allow_password` / `allow_publickey`), запуск от имени пользователя и
журналирование. Секция «Управление сервером» — старт/стоп/перезапуск и
живой статус (PID, порт, состояние) через systemd (если юнит установлен)
или daemon-режим; тумблер «Автозапуск при загрузке» включает/выключает
юнит. Снизу — хвост лога сервера. **Секреты (пароли, приватные ключи) в
GUI не редактируются и не сохраняются** — только безопасные поля; при
загрузке `server.toml` со случайным `password = "..."` это поле
молча отбрасывается.

### Под X11 (Xvfb)

По умолчанию `backend=auto` определяет тип сессии по `XDG_SESSION_TYPE`,
`WAYLAND_DISPLAY` и `DISPLAY`. Чтобы явно поднять X11 даже на машине без
дисплея, используйте `--backend x11`.

Для каждого подключения сервер:

1. выделяет свободный `:N`,
2. поднимает `Xvfb :N -screen 0 1920x1080x24 -auth <xauth-файл>` от имени
   целевого пользователя,
3. генерирует XAUTHORITY-cookie (`xauth add`),
4. (опционально) запускает WM/DE из `window_manager` конфига,
5. сбрасывает привилегии на UID/GID пользователя и подключается к
   дисплею через python-xlib.

Права:

- `Xvfb`, `xauth`, `python-xlib` — без root не работают; сервер
  запускается от root, worker'ы — от имени пользователя (через
  `setuid/setgid` + `initgroups`).

### Под Wayland (headless-композитор)

```bash
sudo -E python -m server --backend wayland --wayland-compositor sway
```

Поддерживаются: `sway` (`WLR_BACKENDS=headless`), `weston`
(`--backend=headless-backend.so`), `kwin_wayland --virtual`,
`gnome-remote-desktop` (headless).

Каждый пользователь получает:

- свой `WAYLAND_DISPLAY=wayland-N`,
- свой `XDG_RUNTIME_DIR=/run/user/<uid>`,
- отдельный headless-композитор, запущенный от его имени.

Захват экрана идёт через `org.freedesktop.portal.ScreenCast` (PipeWire) —
у пользователя в сессии должен быть запущен `xdg-desktop-portal` (любой
backend: `-gtk`, `-kde`, `-wlr`). Для wlroots-композиторов лёгкая
альтернатива — протокол `wlr-screencopy` (см.
`file 'server/backend/wayland_pipewire.py'` — модуль подключается, если
в окружении есть `gi.repository` с PipeWire).

Ввод:

- `uinput` (`/dev/uinput`, `python-evdev`) — основной путь;
- `ydotool` — fallback через CLI;
- Wayland-протоколы `zwlr_virtual_pointer_v1` / `zwp_virtual_keyboard_v1` —
  для wlroots-композиторов;
- `org.freedesktop.portal.RemoteDesktop` — для GNOME/KDE.

---

## Запуск клиента

```bash
# По SSH-ключу (по умолчанию)
python -m client --host my.linux.box --user alice --auth key

# По паролю
python -m client --host my.linux.box --user alice --auth password

# С указанием путей
python -m client --host 1.2.3.4 --port 2222 --user alice --auth key \
  --key-path ~/.ssh/id_ed25519 --codec h264
```

Полный список флагов: `python -m client --help`.

### На Linux X11 / Wayland

Клиент автоматически выбирает Qt-платформу:

| Условие | `QT_QPA_PLATFORM` |
| --- | --- |
| есть `WAYLAND_DISPLAY` | `wayland;xcb` (фолбэк на XWayland, если нативный wayland-плагин недоступен) |
| иначе | `xcb` |

Чтобы форсировать: `--qt-platform xcb` или `--qt-platform wayland`.

HiDPI: Qt6 включает дробное масштабирование по умолчанию. Клиент
учитывает device pixel ratio при пересчёте координат мыши, чтобы окно
рабочего стола не «мылилось» и не сбивало клики.

Полноэкранный режим и захват клавиатуры:

- **X11** — окно получает фокус, нажатия перехватываются окном
  автоматически.
- **Wayland** — глобальный grab ограничен из соображений безопасности.
  Системные сочетания (`Super`, `Alt+Tab`, `Ctrl+Alt+Del`) не
  перехватываются. Для них в тулбаре клиента есть меню **«Спец.
  сочетания»** — отправляет нужный chord через протокол напрямую.

### На Windows

PySide6 сам выбирает платформу `windows`. Никаких дополнительных
настроек не нужно.

---

## SSH-ключи

В приложении есть встроенный менеджер ключей: **тулбар → SSH-ключи**.

- Тип: Ed25519 (по умолчанию) или RSA.
- Опциональная passphrase.
- Сохранение в выбранную папку (приватный ключ с правами `0600`).
- Кнопка «Копировать команду установки» даёт однострочник для добавления
  публичного ключа в `authorized_keys` на сервере:

  ```bash
  mkdir -p ~/.ssh && chmod 700 ~/.ssh && \
  echo 'ssh-ed25519 AAAA…' >> ~/.ssh/authorized_keys && \
  chmod 600 ~/.ssh/authorized_keys
  ```

Из CLI:

```bash
python -m client --keygen
```

---

## Буфер обмена

Двусторонняя синхронизация текста (UTF-8) между клиентом и сервером:

- **Сервер.** X11: `xclip` или чтение `CLIPBOARD`/`PRIMARY` selections
  через `python-xlib`. Wayland: `wl-paste` / `wl-copy` или протокол
  `wlr-data-control`.
- **Клиент.** Qt API `QClipboard` — одинаково работает на X11 и Wayland.
- **Защита от циклов.** Каждое сообщение несёт `origin` (`client` или
  `server`); только что полученное содержимое запоминается и не
  отправляется обратно.
- **Приватность.** В тулбаре переключатель **«Синхр. буфер»** —
  отключает двустороннюю синхронизацию. По умолчанию ограничение на
  размер — 1 МБ (`clipboard_max_bytes` в конфиге).

---

## Общие папки и передача файлов

Передача байтов идёт через **SFTP-подсистему SSH** (отдельный демон не
нужен — `asyncssh` умеет и SFTP-клиент, и SFTP-сервер). Управляющие
команды (список, mkdir, удаление) — по каналу `files`.

Возможности:

- **Drag-and-drop** файлов в окно рабочего стола → загрузка в
  `~/shared` (или иной путь из `shared_dir`).
- **Файловый менеджер** в клиенте: навигация, upload, download, mkdir,
  удаление.
- **Прогресс** и **отмена** в реальном времени.
- **Jail.** Сервер ограничивает все пути заданной папкой — клиент не
  может выйти за пределы `~/shared` целевого пользователя (см.
  `file 'server/files.py'`).
- **Чанки.** `asyncssh` сам стримит крупные файлы блоками, видеоканал
  не блокируется.

---

## Многопользовательский режим и сессии

- Аутентификация — по логину/паролю (через PAM) или по SSH-ключу из
  `~user/.ssh/authorized_keys`.
- На каждое подключение сервер **создаёт новую изолированную сессию**
  (отдельный `:N` / `wayland-N`, отдельный cookie, отдельный процесс
  композитора). Несколько пользователей работают параллельно, не мешая
  друг другу.
- Каждая сессия запускается **от имени целевого пользователя** (после
  успешной аутентификации сервер сбрасывает привилегии на UID/GID и
  ставит `HOME`, `USER`, `DISPLAY`/`WAYLAND_DISPLAY`, `XAUTHORITY`).
- Жизненный цикл:
  - **Создание** при подключении → **работа** → **завершение** при
    отключении.
  - Опция `persistent`: оставить сессию висеть для переподключения.
  - `idle_timeout` (по умолчанию 600 с) — убивает простаивающие
    не-persistent сессии.
  - `max_sessions` — глобальный лимит одновременных сессий.

---

## Конфигурация

Файлы конфигурации — TOML или JSON. Пути:

- Сервер: `~/.config/ssh-remote-desktop/server.toml` (или
  `/etc/ssh-remote-desktop/server.toml`), переменная
  `RD_SERVER_CONFIG`, флаг `--config`.
- Клиент: `~/.config/ssh-remote-desktop/client.toml`, переменная
  `RD_CLIENT_CONFIG`, флаг `--config`.

**Пример server.toml:**

```toml
host = "0.0.0.0"
port = 2222
backend = "auto"            # "auto" | "x11" | "wayland"
max_sessions = 10
idle_timeout = 600
persistent_default = false
session_geometry = [1920, 1080]
session_depth = 24

codec = "h264"              # h264 | h265 | jpeg | webp
fps = 30
bitrate_kbps = 6000
jpeg_quality = 80
cursor_mode = "embedded"    # embedded | metadata

xvfb_bin = "Xvfb"
window_manager = ""         # команда WM/DE на сессию (опц.)
wayland_compositor = "sway" # sway | weston | kwin | gnome
use_uinput = true

clipboard_enabled = true
clipboard_max_bytes = 1048576

files_enabled = true
shared_dir = "~/shared"
sftp_chunk_size = 262144

allow_password = true
allow_publickey = true
run_as_user = true

log_level = "INFO"
log_file = ""
```

**Пример client.toml:**

```toml
host = "myserver.lan"
port = 2222
user = "alice"
auth = "key"                # key | password | agent
key_path = "~/.config/ssh-remote-desktop/id_ed25519"
known_hosts = "~/.config/ssh-remote-desktop/known_hosts"
accept_unknown_host = false

new_session = true
persistent = false
geometry = [1920, 1080]
codec = "h264"

qt_platform = "auto"        # auto | xcb | wayland
start_fullscreen = false
scale_to_window = true
hidpi = true

clipboard_enabled = true
clipboard_max_bytes = 1048576
files_enabled = true
local_shared_dir = "~/ssh-remote-desktop-shared"

auto_reconnect = true
reconnect_delay = 2.0
max_reconnect_attempts = 0  # 0 = бесконечно

log_level = "INFO"
```

---

## Сборка исполняемых файлов

И клиент, и сервер компилируются в **самостоятельные исполняемые
файлы**. Предпочтительный путь — **Nuitka** (Python → C → нативный
бинарь). Альтернатива — PyInstaller (`--onefile`).

```bash
# Клиент под Linux
./build_client_linux.sh
# -> ./rd-client

# Клиент под Windows (на Windows-машине)
build_client_windows.sh
# -> rd-client.exe

# Сервер под Linux
./build_server_linux.sh
# -> ./rd-server
```

Скрипты лежат в корне репозитория (`build_*.sh`) и по сути делают:

```bash
python -m nuitka --standalone --onefile \
  --enable-plugin=pyside6 \
  client/__main__.py        # или server/__main__.py
```

Все нативные зависимости (Qt-плагины, libxcb, libwayland, evdev) Nuitka
тянет автоматически. Размер итогового бинаря — порядка 60–100 МБ
(standalone включает Python-рантайм и все библиотеки).

**Альтернатива: PyInstaller.**

```bash
pip install pyinstaller
pyinstaller --noconfirm --onefile --name rd-client   client/__main__.py
pyinstaller --noconfirm --onefile --name rd-server   server/__main__.py
```

---

## Troubleshooting

**`No module named 'av'`** — H.264 недоступен. Клиент автоматически
переключится на JPEG-delta. Чтобы включить H.264: `pip install av` (нужен
ffmpeg в системе).

**`No module named 'Xlib'`** — X11-backend отключает ввод, остаётся
только захват через `mss`. Поставьте `pip install python-xlib`.

**`Wayland backend ... uinput unavailable`** — пользователю не хватает
прав на `/dev/uinput`. Решение:

```bash
sudo usermod -aG input $USER       # затем перелогиниться
# или:
sudo apt install -y uinput-tools   # некоторые дистрибутивы требуют модуль ядра
sudo modprobe uinput
```

**Сессия создаётся, но экран чёрный.** Проверьте, что
`xdg-desktop-portal` запущен в пользовательской сессии
(`systemctl --user status xdg-desktop-portal`).

**`Password auth failed` на сервере** — сервер требует `python-pam`
(или PAM-стек через CLI). Установите системный пакет (`apt install
python3-pam` на Debian) либо используйте аутентификацию по ключу.

**Клиент не видит нативный Wayland-плагин** — клиент автоматически
откатится на XWayland (`xcb`). Чтобы форсировать: запустите с
`--qt-platform xcb`.

**`known_hosts` ругается на незнакомый хост** — при первом подключении
клиент спросит, добавить ли отпечаток сервера в
`~/.config/ssh-remote-desktop/known_hosts`. Чтобы принять молча (не
рекомендуется): `--accept-unknown-host`.

**Мышка «отстаёт»** — снизьте битрейт (`--bitrate-kbps 3000`) и
частоту (`--fps 24`). Клиент также автоматически адаптирует параметры
по получаемой обратной связи (`stats` → снижение bitrate при loss > 5%
или RTT > 250 мс).

---

## Безопасность

- **Аутентификация** — SSH-ключи или PAM (пароль пользователя
  системы). Публичный ключ сверяется с
  `~user/.ssh/authorized_keys` ровно как у OpenSSH.
- **SFTP-jail** — все файловые операции ограничены `shared_dir`
  пользователя; попытки выхода через `..` отбрасываются
  (`file 'server/files.py'`).
- **Буфер обмена** — имеет жёсткое ограничение на размер
  (`clipboard_max_bytes`) и может быть отключён на стороне клиента.
- **Привилегии** — сервер запускается от root (нужен для PAM и
  старта Xvfb), session worker'ы — от имени целевого пользователя
  через `setuid/setgid/initgroups`.
- **Изоляция сессий** — разные пользователи получают разные `:N`
  (X11) / `WAYLAND_DISPLAY` (Wayland) и разные XAUTHORITY-cookie; они
  не видят экраны друг друга.

Подробности — в `file 'SECURITY.md'`.

---

## Лицензия

MIT — см. `file 'LICENSE'`.
