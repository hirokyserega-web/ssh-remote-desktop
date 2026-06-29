<div align="center">

# 🖥️ SSH Remote Desktop

**Графический рабочий стол поверх SSH. Быстро, безопасно, без лишних портов.**

[![CI](https://github.com/hirokyserega-web/ssh-remote-desktop/actions/workflows/ci.yml/badge.svg)](https://github.com/hirokyserega-web/ssh-remote-desktop/actions/workflows/ci.yml)
[![Release](https://github.com/hirokyserega-web/ssh-remote-desktop/actions/workflows/release.yml/badge.svg)](https://github.com/hirokyserega-web/ssh-remote-desktop/actions/workflows/release.yml)
[![Latest release](https://img.shields.io/github/v/release/hirokyserega-web/ssh-remote-desktop?label=latest%20release)](https://github.com/hirokyserega-web/ssh-remote-desktop/releases)
[![License: MIT](https://img.shields.io/github/license/hirokyserega-web/ssh-remote-desktop?label=license)](LICENSE)

**RU** · [**EN**](README.en.md)

---

**Забудьте про VNC и RDP.** SSH Remote Desktop мультиплексирует видео, ввод, буфер обмена и файлы через одно SSH-соединение. Работает через NAT и Firewall, если у вас есть SSH-доступ.

</div>

## 🚀 Быстрый старт (Установка за 1 минуту)

### 1. Установка

**На сервере (Linux):**
```bash
curl -fsSL https://raw.githubusercontent.com/hirokyserega-web/ssh-remote-desktop/main/scripts/install.sh | sudo bash -s -- --component server
```

**На клиенте (Linux / macOS):**
```bash
curl -fsSL https://raw.githubusercontent.com/hirokyserega-web/ssh-remote-desktop/main/scripts/install.sh | bash -s -- --component client
```

**На клиенте (Windows):**
```powershell
iwr -useb https://raw.githubusercontent.com/hirokyserega-web/ssh-remote-desktop/main/scripts/install-client-windows.ps1 | iex
```

### 2. Настройка доступа
Если у вас уже настроен вход по SSH-ключам (`authorized_keys`), этот шаг можно пропустить. Если нет:
1.  **На клиенте:** `rd-client --keygen` (создаст ключ в `~/.config/ssh-remote-desktop/id_ed25519`).
2.  **На сервере:** Скопируйте содержимое `id_ed25519.pub` в файл `~/.ssh/authorized_keys`.

### 3. Подключение
```bash
rd-client --host IP_СЕРВЕРА --user ИМЯ_ПОЛЬЗОВАТЕЛЯ
```

---

## 🔄 Обновление
Вы всегда можете обновиться до последней версии одной командой:
```bash
rd-server --update  # На сервере
rd-client --update  # На клиенте
```

---

## ✨ Ключевые возможности

- 🔐 **Только SSH.** Никаких лишних портов — всё в одном туннеле.
- 🖼️ **X11 и Wayland.** Поддержка современных Linux-дистрибутивов.
- 🎥 **H.264 / H.265 / JPEG.** Адаптивное сжатие для любой скорости сети.
- 📋 **Общий буфер обмена.** Копируйте текст между машинами мгновенно.
- 📁 **SFTP Jail.** Безопасная передача файлов, ограниченная одной папкой.
- ⚙️ **Удобная панель.** `rd-server-gui` для управления сервером из трея.

## 🛠️ Поддержка платформ

| Компонент | Linux | Windows | macOS |
|---|:---:|:---:|:---:|
| **Сервер** | ✅ X11 / ✅ Wayland | ❌ | ❌ |
| **Клиент** | ✅ | ✅ | ✅ (из исходников) |

---

<details>
<summary><b>📖 Подробная документация</b></summary>

### Аргументы сервера (`rd-server`)
- `--port N` — сменить порт (по умолчанию 2222).
- `--install-service` — установить как системную службу (systemd).
- `--daemon` — запустить в фоновом режиме.

### Конфигурация
Файлы настроек находятся в:
- Сервер: `/etc/ssh-remote-desktop/server.toml` или `~/.config/ssh-remote-desktop/server.toml`
- Клиент: `~/.config/ssh-remote-desktop/client.toml`

Подробности об архитектуре и разработке читайте в [CONTRIBUTING.md](CONTRIBUTING.md) и [AGENTS.md](AGENTS.md).
</details>

---

## 🤝 Разработка и лицензия

Лицензия [MIT](LICENSE). Мы приветствуем Pull Requests и отчеты об ошибках!