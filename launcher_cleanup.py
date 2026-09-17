#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
launcher_cleanup.py — подготовка планшетов: лаунчеры, браузеры, аккаунты.

Сценарий: планшеты школы под управлением Headwind MDM (com.hmdm.launcher).
Скрипт подключается по ADB и делает три вещи:

  1. ЛАУНЧЕРЫ  — находит все приложения с category.HOME и сносит их,
                 оставляя единственным домашним экраном MDM-агент.
  2. БРАУЗЕРЫ  — находит всё, что обрабатывает http/https, сносит лишнее
                 и назначает Chrome браузером по умолчанию (role BROWSER).
  3. АССИСТЕНТЫ— сносит Google Assistant/Gemini, Bixby, ZUI AI, Jovi и т.д.,
                 снимает роль ASSISTANT и вызов по кнопке/жесту.
  4. РЕЖИМ ПК  — сносит десктопные оболочки (Lenovo PC Mode / ZuiLauncherPC,
                 Motorola Ready For, Samsung DeX, Huawei Desktop) и гасит
                 системные переключатели вроде zui_pc_mode.
  5. ПРОЧЕЕ    — сносит лишние приложения из списка KNOWN_EXTRA_APPS
                 (Google Meet / Duo, Google Chat).
  6. АККАУНТЫ  — выводит все учётные записи (Google, Samsung, Mi и т.д.)
                 и список пользователей устройства. Только отчёт;
                 удаление аккаунта через ADB невозможно — нужен сброс.

Безопасность:
  * трогаются только пакеты, реально зарегистрированные как HOME или как
    обработчик http/https;
  * жёсткий белый список (MDM, Chrome, WebView, systemui, settings, gms);
  * незнакомые пакеты по умолчанию требуют ручного подтверждения даже
    в авторежиме (снимается ключом --force-unknown);
  * если MDM-лаунчер не установлен — скрипт откажется работать.

Примеры:
  ./launcher_cleanup.py --list                 # только показать, ничего не делать
  ./launcher_cleanup.py                        # интерактивно, с подтверждениями
  ./launcher_cleanup.py --auto                 # авторежим, без вопросов
  ./launcher_cleanup.py --auto --all-devices   # пачкой по всем подключённым
  ./launcher_cleanup.py -n -s R52T1023         # прогон вхолостую на одном
  ./launcher_cleanup.py --only accounts        # только аудит учёток
"""

from __future__ import annotations

import argparse
import csv
import datetime as _dt
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field

VERSION = "2.5"

MDM_PACKAGE = "com.hmdm.launcher"
MDM_HOME_ACTIVITY = "com.hmdm.launcher/.MainActivity"
MDM_ADMIN = "com.hmdm.launcher/.AdminReceiver"
DEFAULT_BROWSER = "com.android.chrome"

# Пакеты, которые нельзя трогать ни при каких условиях.
PROTECTED = {
    MDM_PACKAGE,
    "android",
    "com.android.systemui",
    "com.android.settings",
    "com.android.settings.intelligence",
    "com.android.shell",
    "com.android.permissioncontroller",
    "com.google.android.permissioncontroller",
    "com.android.providers.settings",
    # WebView и обвязка: сносить нельзя — отвалятся все приложения с веб-вью
    "com.google.android.webview",
    "com.android.webview",
    "com.google.android.gms",
    "com.google.android.gsf",
    "com.android.htmlviewer",
    "com.android.captiveportallogin",
    "com.android.vending",
}

# Префиксы защищённых пакетов (версионные библиотеки Chrome/WebView).
PROTECTED_PREFIXES = (
    "com.google.android.trichromelibrary",
    "com.android.trichromelibrary",
    "com.google.android.webview",
)

# ─── справочник заводских лаунчеров ───
KNOWN_LAUNCHERS = {
    # Lenovo
    "com.zui.launcher": "Lenovo ZUI Launcher",
    "com.lenovo.launcher": "Lenovo Launcher",
    "com.lenovo.leos.launcher": "Lenovo LeOS Launcher",
    # Xiaomi / Redmi / POCO
    "com.miui.home": "MIUI / HyperOS Home",
    "com.mi.android.globallauncher": "POCO Launcher",
    # Samsung
    "com.sec.android.app.launcher": "Samsung One UI Home",
    "com.samsung.android.app.galaxyfinder": "Samsung Finder Home",
    # Google / AOSP
    "com.google.android.apps.nexuslauncher": "Pixel Launcher",
    "com.android.launcher": "AOSP Launcher",
    "com.android.launcher2": "AOSP Launcher 2",
    "com.android.launcher3": "AOSP Launcher 3",
    "com.google.android.launcher": "Google Now Launcher",
    # Huawei / Honor
    "com.huawei.android.launcher": "Huawei / EMUI Launcher",
    "com.hihonor.android.launcher": "Honor Magic Launcher",
    # Oppo / Realme / OnePlus
    "com.oppo.launcher": "ColorOS Launcher",
    "com.oplus.launcher": "OPlus Launcher",
    "com.realme.launcher": "realme UI Launcher",
    "net.oneplus.launcher": "OxygenOS Launcher",
    # Vivo и прочие
    "com.bbk.launcher2": "vivo / Funtouch Launcher",
    "com.transsion.XOSLauncher": "XOS Launcher",
    "com.freeme.launcher": "Freeme Launcher",
    "com.tcl.android.launcher": "TCL Launcher",
    "com.teclast.launcher": "Teclast Launcher",
    "com.android.tv.launcher": "Android TV Launcher",
    "com.microsoft.launcher": "Microsoft Launcher",
    "com.actionlauncher.playstore": "Action Launcher",
    "ru.yandex.launcher": "Яндекс.Лаунчер",
}

# ─── справочник браузеров ───
KNOWN_BROWSERS = {
    "com.android.chrome": "Google Chrome",
    "com.chrome.beta": "Chrome Beta",
    "com.chrome.dev": "Chrome Dev",
    "com.chrome.canary": "Chrome Canary",
    "com.sec.android.app.sbrowser": "Samsung Internet",
    "com.sec.android.app.sbrowser.beta": "Samsung Internet Beta",
    "com.mi.globalbrowser": "Mi Browser (Global)",
    "com.android.browser": "AOSP Browser",
    "com.yandex.browser": "Яндекс.Браузер",
    "com.yandex.browser.lite": "Яндекс.Браузер Лайт",
    "com.yandex.searchapp": "Яндекс — с Алисой",
    "org.mozilla.firefox": "Mozilla Firefox",
    "org.mozilla.focus": "Firefox Focus",
    "com.opera.browser": "Opera",
    "com.opera.mini.native": "Opera Mini",
    "com.opera.gx": "Opera GX",
    "com.brave.browser": "Brave",
    "com.duckduckgo.mobile.android": "DuckDuckGo",
    "com.microsoft.emmx": "Microsoft Edge",
    "com.UCMobile.intl": "UC Browser",
    "com.uc.browser.en": "UC Browser EN",
    "com.heytap.browser": "HeyTap / OPPO Browser",
    "com.coloros.browser": "ColorOS Browser",
    "com.oppo.browser": "OPPO Browser",
    "com.vivo.browser": "vivo Browser",
    "com.huawei.browser": "Huawei Browser",
    "com.hihonor.browser": "Honor Browser",
    "com.transsion.phoenix": "Phoenix Browser",
    "com.lenovo.browser": "Lenovo Browser",
    "com.zui.browser": "ZUI Browser",
    "com.qihoo.browser": "360 Browser",
    "com.tencent.mtt": "QQ Browser",
    "mark.via.gp": "Via Browser",
    "acr.browser.lightning": "Lightning Browser",
    "com.kiwibrowser.browser": "Kiwi Browser",
    "com.puffin.free": "Puffin Browser",
}

# ─── справочник голосовых ассистентов / AI-помощников ───
KNOWN_ASSISTANTS = {
    # Google
    "com.google.android.apps.googleassistant": "Google Assistant",
    "com.google.android.apps.bard": "Google Gemini",
    "com.google.android.googlequicksearchbox": "Google (поиск + Ассистент)",
    # Samsung Bixby
    "com.samsung.android.bixby.agent": "Bixby Voice",
    "com.samsung.android.bixby.agent.dummy": "Bixby (заглушка)",
    "com.samsung.android.bixby.wakeup": "Bixby Wakeup",
    "com.samsung.android.bixby.service": "Bixby Service",
    "com.samsung.android.bixbyvision.framework": "Bixby Vision",
    "com.samsung.android.app.settings.bixby": "Bixby Settings",
    "com.samsung.android.visionintelligence": "Bixby Vision Intelligence",
    "com.samsung.android.app.routines": "Bixby Routines",
    # Lenovo / ZUI
    "com.zui.ai.aiservice": "ZUI AI Service",
    "com.zui.ai.lens": "ZUI AI Lens",
    "com.lenovo.levoice": "Lenovo LeVoice",
    "com.lenovo.leos.assistant": "Lenovo LeOS Assistant",
    # Xiaomi
    "com.miui.voiceassist": "Xiao AI (голосовой помощник)",
    "com.xiaomi.aiasst.service": "Xiaomi AI Assistant",
    "com.xiaomi.aiasst.vision": "Xiaomi AI Vision",
    # Huawei / Honor
    "com.huawei.vassistant": "HiVoice / Celia",
    "com.hihonor.vassistant": "Honor YOYO",
    # Oppo / realme / OnePlus
    "com.heytap.speechassist": "Breeno / HeyTap Assistant",
    "com.coloros.speechassist": "ColorOS Speech Assistant",
    "com.oplus.ai.assistant": "OPlus AI Assistant",
    # vivo
    "com.vivo.agent": "Jovi (голосовой помощник)",
    "com.vivo.aiservice": "vivo AI Service",
    # MediaTek и прочее
    "com.mediatek.voicecommand": "MTK Voice Command",
    "com.mediatek.voiceunlock": "MTK Voice Unlock",
    "com.amazon.dee.app": "Amazon Alexa",
    "com.microsoft.copilot": "Microsoft Copilot",
    "ru.yandex.searchplugin": "Яндекс с Алисой",
}

# Настройки, отключающие вызов ассистента. Применяются только если ключ есть.
ASSISTANT_SETTINGS = [
    ("secure", "assistant", ""),
    ("secure", "voice_interaction_service", ""),
    ("secure", "assist_long_press_home_enabled", "0"),
    ("secure", "assist_gesture_enabled", "0"),
    ("secure", "assist_gesture_silence_alerts_enabled", "0"),
    ("secure", "assist_touch_gesture_enabled", "0"),
    ("secure", "search_press_hold_nav_handle_enabled", "0"),
]

# ─── справочник «режима ПК» / десктопных оболочек ───
KNOWN_DESKTOP_MODE = {
    # Lenovo / ZUI — режим ПК при подключении клавиатуры (проверено на TB336FU)
    "com.zui.desktoplauncher": "Lenovo PC Mode (ZuiLauncherPC)",
    # Motorola Ready For — десктоп на внешнем экране, есть в прошивках Lenovo
    "com.motorola.mobiledesktop": "Motorola Ready For",
    "com.motorola.mobiledesktop.core": "Motorola Ready For (ядро)",
    # Samsung DeX
    "com.sec.android.app.desktoplauncher": "Samsung DeX Home",
    "com.samsung.desktopsystemui": "Samsung DeX SystemUI",
    "com.sec.android.desktopmode.uiservice": "Samsung DeX UI Service",
    "com.samsung.android.desktopmode.uiservice": "Samsung DeX UI Service",
    "com.samsung.android.app.dressroom": "Samsung DeX Dressroom",
    # Huawei EasyProjection
    "com.huawei.desktop.systemui": "Huawei Desktop SystemUI",
    "com.huawei.desktop.explorer": "Huawei Desktop Explorer",
    # Xiaomi
    "com.xiaomi.mirror": "Xiaomi Mirror / рабочий стол",
    # OPlus
    "com.oplus.pc": "OPlus PC Connect",
}

# Плавающая панель ZUI — не режим ПК, сносится только с --with-freeform.
OPTIONAL_DESKTOP_EXTRAS = {
    "com.zui.freeform.sidebar": "ZUI Freeform Sidebar (плавающие окна)",
}

# Настройки режима ПК. Применяются только если ключ реально существует.
DESKTOP_SETTINGS = [
    # Главный гейт: при zui_pc_mode=1 панель режима ПК рисует сам SystemUI,
    # даже если ZuiLauncherPC удалён (проверено на TB336FU / Android 16).
    ("system", "zui_pc_mode", "0"),
    # Lenovo зовёт режим ПК «work mode»; это и есть триггер на клавиатуру.
    ("system", "enter_work_mode_from_keyboard", "0"),
    ("system", "exit_work_mode_from_keyboard", "1"),
    ("system", "zui_pc_desktop_mode2", "0"),
    ("system", "zui_external_display_desktop_mode", "0"),
    ("system", "zui_dp_display_pc_mode", "0"),
    ("system", "enter_pc_mode_guidance", "0"),
    ("system", "zui_ov_desktop_mode_test", "0"),
    ("global", "pcmode.enter.overview", "0"),
    ("global", "drag_readyfor_enabled", "0"),
    ("global", "force_desktop_mode_on_external_displays", "0"),
]

# ─── справочник лишних приложений (сносятся этапом «extras») ───
KNOWN_EXTRA_APPS = {
    # Google Meet: на планшетах предустановлен как Tachyon (бывший Duo),
    # отдельно встречается самостоятельная сборка Meet.
    "com.google.android.apps.tachyon": "Google Meet (Duo / Tachyon)",
    "com.google.android.apps.meetings": "Google Meet (отдельное приложение)",
    "com.google.android.apps.dynamite": "Google Chat",
}

# ─── разрешённые приложения: всё остальное из пользовательских сносится ───
# Пакеты подтверждены на реальных планшетах школы; веб-сервисы из списка
# (LearningApps, Wordwall, Vznania, Infolesson, Daryn, PhET, Code.org,
# Joyteka, CoreApp, Supa) приложений не имеют — открываются в Chrome.
ALLOWED_APPS = {
    # MDM и его служебные модули
    "com.hmdm.launcher": "Headwind MDM",
    "com.hmdm.pager": "Headwind MDM (pager)",
    "com.hmdm.phoneproxy": "Headwind MDM (phone proxy)",
    "com.hmdm.emuilauncherrestarter": "Headwind MDM (restarter)",
    # Google Workspace
    "com.google.android.apps.docs": "Google Диск",
    "com.google.android.apps.docs.editors.docs": "Google Документы",
    "com.google.android.apps.docs.editors.sheets": "Google Таблицы",
    "com.google.android.apps.docs.editors.slides": "Google Презентации",
    "com.google.android.apps.classroom": "Google Класс",
    "com.google.android.keep": "Google Keep",
    "com.google.android.calendar": "Google Календарь",
    "com.google.android.gm": "Gmail",
    # Учебные сервисы из списка школы
    "com.quizizz_mobile": "Wayground (Quizizz)",
    "com.canva.editor": "Canva",
    "com.uchi.app": "Учи.ру",
    "ru.foxford.foxfordtextbook": "Фоксфорд",
    "ru.foxford.foxford": "Фоксфорд",
    "com.panareadigital.Nearpod": "Nearpod",
    "com.wallwisher.Padlet": "Padlet",
    "no.mobitroll.kahoot.android": "Kahoot",
    "org.scratchjr.android": "ScratchJr",
    "ru.yaklass.app": "Я.Класс",
    "kz.bilimland.bilimland": "BilimLand",
    "kz.kundelik.android": "Kundelik",
    # Служебное, что ставит сама система поверх пользовательского раздела
    "com.google.ar.core": "Google ARCore",
    "com.google.android.verifier": "Google Play Protect",
    "com.google.android.contactkeys": "Google Contact Keys",
    "com.android.soundrecorder": "Диктофон",
}

# Семейства пакетов, которые разрешены целиком.
ALLOWED_PREFIXES = (
    "org.geogebra.",                      # GeoGebra: Graphing, Geometry, 3D…
    "com.google.android.apps.docs.",      # редакторы Workspace
)

# Приложение, установленное этими установщиками, считается согласованным.
TRUSTED_INSTALLERS = {MDM_PACKAGE}

# Магазины: уведомления «обнови игру» — тоже способ увести ребёнка из урока.
APP_STORES = {
    "com.android.vending": "Google Play",
    "com.xiaomi.mipicks": "GetApps (Xiaomi)",
    "com.xiaomi.discover": "Рекомендации Xiaomi",
    "com.sec.android.app.samsungapps": "Galaxy Store",
    "com.huawei.appmarket": "AppGallery",
}

# HOME-пакеты — системные заглушки, их удаление ломает загрузку.
HOME_STUBS = {
    "com.android.settings.FallbackHome",
    "com.android.settings",
    "com.google.android.setupwizard",
    "com.android.provision",
    "com.google.android.apps.restore",
}

# Пакеты, которые отвечают на http/https, но браузерами не являются.
BROWSER_NON_TARGETS = {
    "com.android.htmlviewer",
    "com.android.captiveportallogin",
    "com.google.android.gms",
    "com.google.android.googlequicksearchbox",
    "com.android.vending",
    "com.google.android.apps.docs",
    "com.google.android.youtube",
    "com.google.android.gm",
    MDM_PACKAGE,
}

# Человекочитаемые имена типов аккаунтов.
ACCOUNT_TYPES = {
    "com.google": "Google",
    "com.osp.app.signin": "Samsung account",
    "com.samsung.android.mobileservice": "Samsung service",
    "com.samsung.android.coreapps": "Samsung CoreApps",
    "com.xiaomi": "Mi Account",
    "com.xiaomi.account": "Mi Account",
    "com.huawei.hwid": "Huawei ID",
    "com.yandex.passport": "Яндекс ID",
    "com.whatsapp": "WhatsApp",
    "org.telegram.messenger": "Telegram",
    "com.microsoft.workaccount": "Microsoft Work",
    "com.lenovo.lsf.account": "Lenovo ID",
}


def is_protected(package: str, extra: set[str]) -> bool:
    if package in PROTECTED or package in extra:
        return True
    return package.startswith(PROTECTED_PREFIXES)


# ─────────────────────────── вывод ───────────────────────────


def _prepare_windows_console() -> None:
    """На Windows включает ANSI-цвета и UTF-8 в консоли (cmd/PowerShell)."""
    if os.name != "nt":
        return
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        # ENABLE_VIRTUAL_TERMINAL_PROCESSING для stdout и stderr
        for handle_id in (-11, -12):
            handle = kernel32.GetStdHandle(handle_id)
            mode = ctypes.c_uint32()
            if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
                kernel32.SetConsoleMode(handle, mode.value | 0x0004)
    except Exception:                                    # noqa: BLE001
        pass
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


_prepare_windows_console()


class C:
    """ANSI-цвета; глушатся, если вывод не в терминал или стоит NO_COLOR."""

    enabled = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None

    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    RED = "\033[31m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    BLUE = "\033[34m"
    MAGENTA = "\033[35m"
    CYAN = "\033[36m"
    GREY = "\033[90m"

    @classmethod
    def p(cls, code: str, text: str) -> str:
        return f"{code}{text}{cls.RESET}" if cls.enabled else text


_ANSI_RE = re.compile(r"\033\[[0-9;]*m")


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def _vis_len(text: str) -> int:
    return len(_strip_ansi(text))


class Log:
    """Красивый вывод в консоль + простой текстовый лог на диск."""

    def __init__(self, path: str | None = None):
        self.path = path
        self._fh = open(path, "a", encoding="utf-8") if path else None
        if self._fh:
            self._fh.write(f"\n{'=' * 78}\n")
            self._fh.write(f"launcher_cleanup v{VERSION} — запуск {self._ts()}\n")
            self._fh.write(f"{'=' * 78}\n")

    @staticmethod
    def _ts() -> str:
        return _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def _write(self, level: str, text: str) -> None:
        if self._fh:
            self._fh.write(f"[{self._ts()}] {level:<7} {text}\n")
            self._fh.flush()

    def raw(self, text: str = "") -> None:
        print(text)
        self._write("", _strip_ansi(text))

    def info(self, text: str) -> None:
        print(f"{C.p(C.BLUE, '  •')} {text}")
        self._write("INFO", _strip_ansi(text))

    def ok(self, text: str) -> None:
        print(f"{C.p(C.GREEN, '  ✔')} {text}")
        self._write("OK", _strip_ansi(text))

    def warn(self, text: str) -> None:
        print(f"{C.p(C.YELLOW, '  ⚠')} {text}")
        self._write("WARN", _strip_ansi(text))

    def err(self, text: str) -> None:
        print(f"{C.p(C.RED, '  ✖')} {text}", file=sys.stderr)
        self._write("ERROR", _strip_ansi(text))

    def step(self, text: str) -> None:
        print(f"\n{C.p(C.BOLD + C.CYAN, '▸ ' + text)}")
        self._write("STEP", _strip_ansi(text))

    def cmd(self, text: str) -> None:
        print(f"    {C.p(C.GREY, '$ ' + text)}")
        self._write("CMD", text)

    def banner(self, title: str, subtitle: str = "") -> None:
        width = 74
        line = "─" * width
        print()
        print(C.p(C.CYAN, f"┌{line}┐"))
        print(C.p(C.CYAN, "│") + C.p(C.BOLD, f" {title}".ljust(width)) + C.p(C.CYAN, "│"))
        if subtitle:
            print(C.p(C.CYAN, "│") + C.p(C.DIM, f" {subtitle}".ljust(width)) + C.p(C.CYAN, "│"))
        print(C.p(C.CYAN, f"└{line}┘"))
        self._write("", f"=== {title} {subtitle} ===")

    def table(self, headers: list[str], rows: list[list[str]]) -> None:
        if not rows:
            return
        widths = [
            max(_vis_len(headers[i]), max(_vis_len(row[i]) for row in rows))
            for i in range(len(headers))
        ]
        head = "  " + "  ".join(headers[i].ljust(widths[i]) for i in range(len(headers)))
        self.raw(C.p(C.BOLD, head))
        self.raw(C.p(C.GREY, "  " + "─" * (len(head) - 2)))
        for row in rows:
            cells = [row[i] + " " * (widths[i] - _vis_len(row[i])) for i in range(len(headers))]
            self.raw("  " + "  ".join(cells))

    def close(self) -> None:
        if self._fh:
            self._fh.write(f"[{self._ts()}] ---- конец сеанса ----\n")
            self._fh.close()
            self._fh = None


# ─────────────────────────── ADB ───────────────────────────


class AdbError(RuntimeError):
    pass


@dataclass
class Adb:
    binary: str
    serial: str | None = None
    log: Log | None = None
    verbose: bool = False

    def _base(self) -> list[str]:
        cmd = [self.binary]
        if self.serial:
            cmd += ["-s", self.serial]
        return cmd

    def run(self, *args: str, timeout: int = 60, check: bool = False) -> subprocess.CompletedProcess:
        cmd = self._base() + list(args)
        if self.verbose and self.log:
            self.log.cmd(" ".join(cmd))
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=timeout,
                encoding="utf-8", errors="replace",
            )
        except subprocess.TimeoutExpired as exc:
            raise AdbError(f"таймаут команды: {' '.join(cmd)}") from exc
        if check and proc.returncode != 0:
            raise AdbError(f"{' '.join(cmd)} → {proc.returncode}: {proc.stderr.strip()}")
        return proc

    def shell(self, command: str, timeout: int = 60) -> str:
        proc = self.run("shell", command, timeout=timeout)
        return (proc.stdout or "") + (proc.stderr or "")

    def prop(self, name: str) -> str:
        return self.shell(f"getprop {name}").strip()

    def describe(self) -> str:
        brand = self.prop("ro.product.brand") or "?"
        model = self.prop("ro.product.model") or "?"
        release = self.prop("ro.build.version.release") or "?"
        sdk = self.prop("ro.build.version.sdk") or "?"
        return f"{brand} {model} · Android {release} (API {sdk})"


def find_adb(preferred: str = "") -> str | None:
    """Ищет adb: явный путь → PATH → рядом со скриптом → типовые места Windows.

    Практика показала, что на рабочем ПК adb чаще всего лежит распакованной
    папкой platform-tools рядом со скриптом, а в PATH его нет вовсе.
    """
    binary_names = ("adb.exe", "adb") if os.name == "nt" else ("adb", "adb.exe")

    def usable(path: str) -> str | None:
        if path and os.path.isfile(path) and os.access(path, os.X_OK | os.R_OK):
            return os.path.abspath(path)
        return None

    # 1. То, что передали явно (--adb или переменная ADB)
    for candidate in (preferred, os.environ.get("ADB", "")):
        if not candidate:
            continue
        found = usable(candidate)
        if found:
            return found
        found = shutil.which(candidate)
        if found:
            return os.path.abspath(found)

    # 2. PATH
    for name in binary_names:
        found = shutil.which(name)
        if found:
            return os.path.abspath(found)

    # 3. Рядом со скриптом: сам файл, platform-tools и любая вложенная папка
    script_dir = os.path.dirname(os.path.abspath(__file__))
    roots = [script_dir, os.getcwd()]
    for root in dict.fromkeys(roots):
        for name in binary_names:
            found = usable(os.path.join(root, name))
            if found:
                return found
        try:
            entries = sorted(os.listdir(root))
        except OSError:
            continue
        # сначала platform-tools, потом всё остальное
        entries.sort(key=lambda item: 0 if "platform-tools" in item.lower() else 1)
        for entry in entries:
            sub = os.path.join(root, entry)
            if not os.path.isdir(sub):
                continue
            for name in binary_names:
                found = usable(os.path.join(sub, name))
                if found:
                    return found
                found = usable(os.path.join(sub, "platform-tools", name))
                if found:
                    return found

    # 4. Типовые места установки
    extra = []
    if os.name == "nt":
        local = os.environ.get("LOCALAPPDATA", "")
        extra += [
            os.path.join(local, "Android", "Sdk", "platform-tools", "adb.exe"),
            r"C:\platform-tools\adb.exe",
            r"C:\Android\platform-tools\adb.exe",
            r"C:\Program Files (x86)\Android\android-sdk\platform-tools\adb.exe",
        ]
    else:
        extra += [
            os.path.expanduser("~/Android/Sdk/platform-tools/adb"),
            "/usr/lib/android-sdk/platform-tools/adb",
        ]
    for candidate in extra:
        found = usable(candidate)
        if found:
            return found
    return None


def adb_hint() -> str:
    """Подсказка оператору, когда adb не нашёлся."""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    return ("положите папку platform-tools (с adb"
            + (".exe" if os.name == "nt" else "")
            + f") рядом со скриптом — в {script_dir} — "
            "или укажите путь ключом --adb")


def list_devices(binary: str) -> list[tuple[str, str]]:
    proc = subprocess.run([binary, "devices"], capture_output=True, text=True, timeout=30)
    devices: list[tuple[str, str]] = []
    for line in proc.stdout.splitlines()[1:]:
        parts = line.strip().split()
        if len(parts) >= 2:
            devices.append((parts[0], parts[1]))
    return devices


# ─────────────────────── обнаружение приложений ───────────────────────


@dataclass
class App:
    package: str
    name: str = ""
    kind: str = "launcher"          # launcher | browser
    is_system: bool = False
    is_default: bool = False        # текущий HOME / браузер по умолчанию
    is_known: bool = False
    activities: list[str] = field(default_factory=list)

    @property
    def label(self) -> str:
        return self.name or self.package


_PKG_ACT_RE = re.compile(r"([a-zA-Z][\w.]*[\w])/([\w.$]+)")


def _query_activities(adb: Adb, log: Log, intent_args: str) -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for prefix in ("cmd package", "pm"):
        out = adb.shell(f"{prefix} query-activities --brief {intent_args}")
        if "Unknown command" in out or "Exception" in out:
            continue
        for match in _PKG_ACT_RE.finditer(out):
            pkg, activity = match.group(1), match.group(2)
            if "." not in pkg:
                continue
            found.setdefault(pkg, [])
            if activity not in found[pkg]:
                found[pkg].append(activity)
        if found:
            return found
    return found


def detect_home_packages(adb: Adb, log: Log) -> dict[str, list[str]]:
    found = _query_activities(
        adb, log, "-a android.intent.action.MAIN -c android.intent.category.HOME"
    )
    if found:
        return found

    log.warn("query-activities недоступен, разбираю dumpsys package")
    out = adb.shell("dumpsys package r")
    block: list[str] = []
    for line in out.splitlines():
        block.append(line)
        if "android.intent.category.HOME" in line:
            for prev in reversed(block[-15:]):
                match = _PKG_ACT_RE.search(prev)
                if match and "." in match.group(1):
                    found.setdefault(match.group(1), []).append(match.group(2))
                    break
    return found


def detect_browser_packages(adb: Adb, log: Log) -> dict[str, list[str]]:
    """Всё, что готово открыть http/https-ссылку."""
    found: dict[str, list[str]] = {}
    for scheme in ("https://example.com", "http://example.com"):
        part = _query_activities(
            adb, log,
            f"-a android.intent.action.VIEW -c android.intent.category.BROWSABLE -d {scheme}",
        )
        for pkg, acts in part.items():
            found.setdefault(pkg, [])
            for act in acts:
                if act not in found[pkg]:
                    found[pkg].append(act)
    return found


def current_home(adb: Adb) -> str | None:
    out = adb.shell("cmd shortcut get-default-launcher")
    match = _PKG_ACT_RE.search(out)
    if match:
        return match.group(1)
    out = adb.shell(
        "cmd package resolve-activity --brief -c android.intent.category.HOME "
        "-a android.intent.action.MAIN"
    )
    match = _PKG_ACT_RE.search(out)
    return match.group(1) if match else None


def current_browser(adb: Adb) -> str | None:
    out = adb.shell("cmd role get-role-holders android.app.role.BROWSER")
    for token in re.findall(r"[a-zA-Z][\w.]*\.[\w.]+", out):
        if "android.app.role" not in token:
            return token
    out = adb.shell(
        "cmd package resolve-activity --brief -a android.intent.action.VIEW "
        "-c android.intent.category.BROWSABLE -d https://example.com"
    )
    match = _PKG_ACT_RE.search(out)
    return match.group(1) if match else None


def installed_packages(adb: Adb, flag: str = "") -> set[str]:
    out = adb.shell(f"pm list packages {flag}".strip())
    return {line.split("package:", 1)[1].strip() for line in out.splitlines() if "package:" in line}


def collect_launchers(adb: Adb, log: Log) -> list[App]:
    home = detect_home_packages(adb, log)
    system_pkgs = installed_packages(adb, "-s")
    active = current_home(adb)

    apps: list[App] = []
    for pkg, activities in sorted(home.items()):
        if pkg in HOME_STUBS:
            continue
        apps.append(App(
            package=pkg,
            name=KNOWN_LAUNCHERS.get(pkg, ""),
            kind="launcher",
            is_system=pkg in system_pkgs,
            is_default=(pkg == active),
            is_known=pkg in KNOWN_LAUNCHERS,
            activities=activities,
        ))

    # Известные лаунчеры, которые установлены, но HOME уже не отдают
    # (например, отключены ранее) — их всё равно надо снести.
    all_pkgs = installed_packages(adb)
    seen = {item.package for item in apps}
    for pkg in sorted(all_pkgs & set(KNOWN_LAUNCHERS)):
        if pkg not in seen:
            apps.append(App(pkg, KNOWN_LAUNCHERS[pkg], "launcher",
                            is_system=pkg in system_pkgs, is_known=True))
    return apps


def collect_browsers(adb: Adb, log: Log) -> list[App]:
    handlers = detect_browser_packages(adb, log)
    system_pkgs = installed_packages(adb, "-s")
    all_pkgs = installed_packages(adb)
    active = current_browser(adb)

    apps: list[App] = []
    for pkg, activities in sorted(handlers.items()):
        if pkg in BROWSER_NON_TARGETS and pkg not in KNOWN_BROWSERS:
            continue
        apps.append(App(
            package=pkg,
            name=KNOWN_BROWSERS.get(pkg, ""),
            kind="browser",
            is_system=pkg in system_pkgs,
            is_default=(pkg == active),
            is_known=pkg in KNOWN_BROWSERS,
            activities=activities,
        ))

    seen = {item.package for item in apps}
    for pkg in sorted(all_pkgs & set(KNOWN_BROWSERS)):
        if pkg not in seen:
            apps.append(App(pkg, KNOWN_BROWSERS[pkg], "browser",
                            is_system=pkg in system_pkgs, is_known=True))
    return apps


def collect_by_catalog(adb: Adb, catalog: dict[str, str], kind: str,
                       extra: dict[str, str] | None = None) -> list[App]:
    """Кандидаты подбираются по справочнику среди реально установленных пакетов."""
    installed = installed_packages(adb)
    system_pkgs = installed_packages(adb, "-s")
    full = dict(catalog)
    if extra:
        full.update(extra)
    apps: list[App] = []
    for pkg in sorted(installed & set(full)):
        apps.append(App(pkg, full[pkg], kind,
                        is_system=pkg in system_pkgs, is_known=True))
    return apps


def collect_assistants(adb: Adb, log: Log) -> list[App]:
    apps = collect_by_catalog(adb, KNOWN_ASSISTANTS, "assistant")
    # Текущий держатель роли ассистента — помечаем звёздочкой.
    holder = adb.shell("cmd role get-role-holders android.app.role.ASSISTANT")
    match = re.search(r"[\w.]+\.[\w.]+", holder)
    active = match.group(0) if match and "android.app.role" not in holder.split()[0:1] else None
    voice = adb.shell("settings get secure voice_interaction_service").strip()
    for app in apps:
        if (active and app.package == active) or app.package in voice:
            app.is_default = True
    return apps


def collect_desktop_mode(adb: Adb, with_extras: bool) -> list[App]:
    extra = OPTIONAL_DESKTOP_EXTRAS if with_extras else None
    return collect_by_catalog(adb, KNOWN_DESKTOP_MODE, "desktop", extra)


def apply_settings(adb: Adb, log: Log, pairs: list[tuple[str, str, str]],
                   dry_run: bool, only_existing: bool = True) -> int:
    """Проставляет настройки, пропуская те, которых нет в этой прошивке."""
    applied = 0
    for namespace, key, value in pairs:
        if only_existing:
            current = adb.shell(f"settings get {namespace} {key}").strip()
            if current in ("", "null"):
                continue
            if current == value:
                log.ok(f"{namespace}/{key} уже = {value}")
                applied += 1
                continue
        literal = value if value else '""'
        cmd = f"settings put {namespace} {key} {literal}"
        if dry_run:
            log.cmd(f"[dry-run] adb shell {cmd}")
            applied += 1
            continue
        log.cmd(f"adb shell {cmd}")
        out = adb.shell(cmd).strip()
        now = adb.shell(f"settings get {namespace} {key}").strip()
        if out and "Exception" in out:
            log.warn(f"{namespace}/{key}: {short_error(out)}")
        else:
            log.ok(f"{namespace}/{key} = {now}")
            applied += 1
    return applied


def check_desktop_residue(adb: Adb, log: Log, dry_run: bool) -> None:
    """Проверяет, что режим ПК действительно погашен, и объясняет остатки."""
    if dry_run:
        return
    state = adb.shell("settings get system zui_pc_mode").strip()
    if state == "1":
        log.warn("режим ПК прямо сейчас активен — выключаю")
        adb.shell("settings put system zui_pc_mode 0")
        state = adb.shell("settings get system zui_pc_mode").strip()
    if state in ("0", "null", ""):
        log.ok("режим ПК выключен (zui_pc_mode=0)")

    # Панель снизу рисует SystemUI, а не отдельный пакет — снести её нельзя,
    # гасится только настройкой выше.
    panel = adb.shell(
        "dumpsys window windows | grep -c 'u0 com.android.systemui}'"
    ).strip()
    if panel.isdigit() and int(panel) > 0:
        log.warn("окно панели SystemUI ещё на экране — проверьте экран планшета; "
                 "если панель видна, отключите и подключите клавиатуру заново")

    prop = adb.shell("getprop persist.sys.zui.pcmode").strip()
    if prop == "1":
        log.info("persist.sys.zui.pcmode=1 — property прошивки, через adb "
                 "(без root) не меняется: setprop отдаёт отказ. Признак того, "
                 "что режим ПК поддерживается железом, а не того, что он включён")
        log.info("корпоративный способ у Lenovo — property "
                 "persist.sys.csdk.disallowSetPcMode через Lenovo CSDK/MDM, "
                 "adb shell её выставить не может")


MDM_APK_NAMES = ("hmdm.apk", "headwind.apk", "mdm.apk")

# Разрешения MDM после установки — из рабочих шаблонов команд.
MDM_APPOPS = [
    "SYSTEM_ALERT_WINDOW", "GET_USAGE_STATS", "WRITE_SETTINGS",
    "MANAGE_EXTERNAL_STORAGE", "REQUEST_INSTALL_PACKAGES",
]
MDM_PERMISSIONS = [
    "android.permission.READ_EXTERNAL_STORAGE",
    "android.permission.WRITE_EXTERNAL_STORAGE",
    "android.permission.ACCESS_FINE_LOCATION",
    "android.permission.ACCESS_COARSE_LOCATION",
    "android.permission.CAMERA",
    "android.permission.READ_PHONE_STATE",
    "android.permission.POST_NOTIFICATIONS",
]


def find_mdm_apk(args: argparse.Namespace, log: Log) -> str | None:
    """Путь к APK: из --apk, иначе ищем рядом со скриптом."""
    if args.apk:
        if os.path.isfile(args.apk):
            return args.apk
        log.err(f"файл не найден: {args.apk}")
        return None

    directory = os.path.dirname(os.path.abspath(__file__))
    for name in MDM_APK_NAMES:
        candidate = os.path.join(directory, name)
        if os.path.isfile(candidate):
            return candidate
    for name in sorted(os.listdir(directory)):
        lowered = name.lower()
        if lowered.endswith(".apk") and ("hmdm" in lowered or "mdm" in lowered):
            return os.path.join(directory, name)
    return None


def install_mdm(adb: Adb, log: Log, args: argparse.Namespace, apk: str) -> bool:
    """Ставит MDM-агент. Возвращает True, если пакет оказался на устройстве."""
    size_mb = os.path.getsize(apk) / (1024 * 1024)
    log.info(f"APK: {apk} ({size_mb:.1f} МБ)")

    if not confirm(f"Установить {MDM_PACKAGE} из {os.path.basename(apk)}?",
                   default=True, auto=args.auto):
        log.warn("установка пропущена по решению оператора")
        return False

    if args.dry_run:
        log.cmd(f"[dry-run] adb install -r -g {apk}")
        return True

    attempts = [
        ["install", "-r", "-g", apk],
        # Android 14+ блокирует APK со старым targetSdk — обходим явным ключом
        ["install", "-r", "-g", "--bypass-low-target-sdk-block", apk],
        # на устройстве уже стоит версия новее — ставим с понижением
        ["install", "-r", "-d", apk],
    ]
    last = ""
    for arguments in attempts:
        log.cmd("adb " + " ".join(arguments))
        proc = adb.run(*arguments, timeout=600)
        last = ((proc.stdout or "") + (proc.stderr or "")).strip()

        # Пакет появляется в pm list не мгновенно: PackageManager ещё
        # дописывает сессию. Ждём до 15 секунд, а не спрашиваем один раз.
        for delay in (0, 1, 2, 3, 4, 5):
            if delay:
                time.sleep(delay)
            if MDM_PACKAGE in installed_packages(adb):
                log.ok(f"{MDM_PACKAGE} установлен"
                       + (f" (пакет появился через ~{delay} с)" if delay else ""))
                return True
            if "Success" not in last:
                break                      # ждать нечего — установка не отчиталась успехом

        if proc.returncode == 0 and "Success" in last:
            log.warn("adb отчитался Success, но пакета в списке нет — "
                     "проверьте экран планшета, возможно висит запрос подтверждения")
        else:
            log.warn(f"установка не прошла (код {proc.returncode}): {short_error(last)}")

        if "DEPRECATED_SDK_VERSION" in last or "VERSION_DOWNGRADE" in last:
            continue                       # следующая стратегия имеет смысл
        if not [serial for serial, state in list_devices(adb.binary)
                if serial == adb.serial and state == "device"]:
            log.err("планшет отключился от USB во время установки")
            return False
        break

    log.err(f"не удалось установить MDM: {short_error(last) or 'нет вывода'}")
    if "Performing Streamed Install" in last and "Success" not in last:
        log.info("установка оборвалась на полпути — обычно это подтверждение на "
                 "экране планшета или обрыв USB")
        log.info("Xiaomi/POCO: Настройки → Для разработчиков → включите "
                 "«Установка через USB» (требует входа в Mi-аккаунт и SIM-карты)")
        log.info("проверьте кабель и порт: дешёвые удлинители рвут длинные передачи")
    if "INSTALL_FAILED_USER_RESTRICTED" in last:
        log.info("на планшете запрещена установка из неизвестных источников — "
                 "разрешите отладку по USB и установку приложений")
    elif "INSTALL_FAILED_UPDATE_INCOMPATIBLE" in last:
        log.info("уже стоит сборка с другой подписью: сначала "
                 f"adb uninstall {MDM_PACKAGE}")
    elif "no devices" in last.lower():
        log.info("планшет отключился во время установки")
    return False


def grant_mdm_permissions(adb: Adb, log: Log, dry_run: bool) -> int:
    """Выдаёт MDM разрешения, без которых он не работает как лаунчер."""
    log.step("Разрешения для MDM-агента")
    commands = [f"appops set {MDM_PACKAGE} {op} allow" for op in MDM_APPOPS]
    commands += [f"pm grant {MDM_PACKAGE} {perm}" for perm in MDM_PERMISSIONS]
    commands += [
        f"dumpsys deviceidle whitelist +{MDM_PACKAGE}",
        f"cmd netpolicy add restrict-background-whitelist {MDM_PACKAGE}",
    ]

    granted = 0
    for command in commands:
        if dry_run:
            log.cmd(f"[dry-run] adb shell {command}")
            granted += 1
            continue
        out = adb.shell(command).strip()
        if out and ("Exception" in out or "Failure" in out or "Error" in out):
            # часть разрешений отсутствует на конкретной прошивке — это нормально
            log.warn(f"{command.split()[0]} {command.split()[-1]}: {short_error(out)}")
        else:
            granted += 1
    log.ok(f"выдано разрешений: {granted} из {len(commands)}")
    return granted


def ensure_mdm_installed(adb: Adb, log: Log, args: argparse.Namespace) -> bool:
    """Ставит MDM, если его нет. Вызывается ДО любого сноса лаунчеров."""
    if MDM_PACKAGE in installed_packages(adb):
        log.ok(f"{MDM_PACKAGE} установлен")
        return True

    log.warn(f"{MDM_PACKAGE} на устройстве нет")
    if not args.install_mdm:
        log.info("установка отключена ключом --no-install-mdm")
        return False

    apk = find_mdm_apk(args, log)
    if not apk:
        log.err("APK не найден: положите hmdm.apk рядом со скриптом "
                "или укажите путь ключом --apk")
        return False

    log.step("Установка MDM-агента")
    if not install_mdm(adb, log, args, apk):
        return False

    if args.mdm_perms:
        grant_mdm_permissions(adb, log, args.dry_run)
    return True


def ensure_device_owner(adb: Adb, log: Log, args: argparse.Namespace,
                        summary: "Summary | None" = None) -> bool:
    """Назначает MDM владельцем устройства, если владелец ещё не выставлен."""
    log.step("Назначение владельца устройства (device owner)")

    current = adb.shell("dpm list-owners")
    if MDM_PACKAGE in current:
        log.ok("владелец уже назначен — ничего не делаю")
        return True
    if "/" in current and "No owners" not in current:
        log.err(f"владельцем уже числится кто-то другой: {current.strip()}")
        log.info("сменить владельца без сброса устройства нельзя")
        return False

    # Android не даёт назначить owner, если на устройстве есть аккаунты или
    # лишние профили. Проверяем заранее — иначе ловим IllegalStateException.
    blockers: list[str] = []
    accounts = collect_accounts(adb)
    if accounts:
        blockers.append(f"учётные записи ({len(accounts)}): "
                        + ", ".join(f"{acc.name} [{acc.type_label}]" for acc in accounts))
    extra_users = [(uid, name) for uid, name, _ in collect_users(adb) if uid != "0"]
    if extra_users:
        blockers.append("лишние профили: "
                        + ", ".join(f"{uid} ({name})" for uid, name in extra_users))

    if blockers:
        log.err("назначить владельца нельзя, мешает:")
        for item in blockers:
            log.err(f"  {item}")
        if extra_users and args.remove_extra_users:
            for uid, name in extra_users:
                log.cmd(f"adb shell pm remove-user {uid}")
                if not args.dry_run:
                    adb.shell(f"pm remove-user {uid}")
                if summary is not None:
                    summary.users_removed.append(uid)
            extra_users = [(uid, name) for uid, name, _ in collect_users(adb) if uid != "0"]
            if not extra_users:
                log.ok("лишние профили удалены")
                blockers = [item for item in blockers if not item.startswith("лишние")]
        if accounts:
            log.info("удалите аккаунты на планшете: Настройки → Аккаунты, "
                     "либо сделайте сброс. Через ADB аккаунт не удаляется")
            log.info("открыть нужный экран на планшете: "
                     "adb shell am start -a android.settings.SYNC_SETTINGS")
        elif extra_users:
            log.info("удалить профиль: adb shell pm remove-user <ID> "
                     "(или ключ --remove-extra-users)")
        if blockers:
            log.info("порядок всегда такой: сброс → device owner → вход в Google")
            return False

    if not confirm(f"Назначить {MDM_ADMIN} владельцем устройства?",
                   default=True, auto=args.auto):
        log.warn("пропущено по решению оператора")
        return False

    cmd = f"dpm set-device-owner {MDM_ADMIN}"
    if args.dry_run:
        log.cmd(f"[dry-run] adb shell {cmd}")
        return True

    log.cmd(f"adb shell {cmd}")
    out = adb.shell(cmd).strip()
    if MDM_PACKAGE in adb.shell("dpm list-owners"):
        log.ok(f"{MDM_ADMIN} назначен владельцем устройства")
        return True

    log.err(f"не удалось назначить владельца: {short_error(out)}")
    lowered = out.lower()
    if "account" in lowered:
        log.info("причина: на устройстве уже есть учётные записи. Device owner "
                 "назначается только на «чистом» устройстве — удалите все аккаунты "
                 "(Настройки → Аккаунты) и повторите, либо сделайте сброс")
    elif "user" in lowered:
        log.info("причина: на устройстве несколько профилей. Удалите лишние: "
                 "adb shell pm list users → adb shell pm remove-user <ID>")
    elif "provision" in lowered or "setup" in lowered:
        log.info("причина: устройство уже проведено через первичную настройку. "
                 "Нужен сброс: adb shell am broadcast -a "
                 "android.intent.action.FACTORY_RESET --receiver-foreground -p android")
    return False


INVENTORY_COLUMNS = [
    "дата", "серийный", "ADB-ID", "бренд", "модель", "Android", "API", "прошивка",
    "патч безопасности", "ОЗУ", "накопитель", "свободно", "экран", "плотность",
    "платформа", "Wi-Fi MAC", "Android ID", "батарея", "Google-аккаунт", "владелец",
    "домашний экран", "браузер", "удалено", "отключено", "не удалось",
    "ФИО ученика", "класс", "примечание",
]


def collect_specs(adb: Adb, log: Log) -> dict[str, str]:
    """Характеристики планшета для таблицы учёта."""
    log.step("Сбор характеристик планшета")

    def mb(value: str) -> str:
        try:
            return f"{int(value) / 1024:.0f} МБ"
        except (TypeError, ValueError):
            return ""

    specs: dict[str, str] = {}
    specs["бренд"] = adb.prop("ro.product.brand")
    specs["модель"] = adb.prop("ro.product.model")
    specs["Android"] = adb.prop("ro.build.version.release")
    specs["API"] = adb.prop("ro.build.version.sdk")
    specs["прошивка"] = adb.prop("ro.build.display.id") or adb.prop("ro.build.id")
    specs["патч безопасности"] = adb.prop("ro.build.version.security_patch")
    specs["платформа"] = adb.prop("ro.board.platform") or adb.prop("ro.hardware")
    specs["серийный"] = adb.prop("ro.serialno") or (adb.serial or "")

    mem = adb.shell("cat /proc/meminfo")
    match = re.search(r"MemTotal:\s+(\d+)", mem)
    specs["ОЗУ"] = mb(match.group(1)) if match else ""

    disk = adb.shell("df -h /data")
    parts = disk.strip().splitlines()[-1].split() if disk.strip() else []
    if len(parts) >= 4:
        specs["накопитель"] = parts[1]
        specs["свободно"] = parts[3]

    size = adb.shell("wm size")
    match = re.search(r"Physical size:\s*(\S+)", size)
    specs["экран"] = match.group(1) if match else ""
    density = adb.shell("wm density")
    match = re.search(r"Physical density:\s*(\S+)", density)
    specs["плотность"] = match.group(1) if match else ""

    mac = adb.shell("cat /sys/class/net/wlan0/address").strip()
    specs["Wi-Fi MAC"] = mac if re.fullmatch(r"[0-9a-f:]{17}", mac) else ""
    specs["Android ID"] = adb.shell("settings get secure android_id").strip()

    battery = adb.shell("dumpsys battery")
    level = re.search(r"level:\s*(\d+)", battery)
    health = re.search(r"health:\s*(\d+)", battery)
    health_map = {"2": "хорошее", "3": "перегрев", "4": "мертвая", "5": "перенапряжение",
                  "6": "сбой", "7": "холодная"}
    specs["батарея"] = (f"{level.group(1)}%" if level else "") + (
        f" ({health_map.get(health.group(1), health.group(1))})" if health else "")

    log.ok(f"{specs['бренд']} {specs['модель']} · ОЗУ {specs['ОЗУ']} · "
           f"накопитель {specs['накопитель']} (свободно {specs['свободно']}) · "
           f"батарея {specs['батарея']}")
    return specs


def inventory_row(summary: Summary, specs: dict[str, str]) -> dict[str, str]:
    google = [acc.name for acc in summary.accounts if acc.type == "com.google"]
    removed = (summary.launchers_removed + summary.browsers_removed
               + summary.assistants_removed + summary.desktop_removed
               + summary.extras_removed)
    disabled = (summary.launchers_disabled + summary.browsers_disabled
                + summary.assistants_disabled + summary.desktop_disabled
                + summary.extras_disabled)
    row = {name: "" for name in INVENTORY_COLUMNS}
    row.update(specs)
    row["дата"] = _dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    row["ADB-ID"] = summary.serial
    row["Google-аккаунт"] = ", ".join(google)
    row["владелец"] = summary.owner_label or "не назначен"
    row["домашний экран"] = summary.home_now
    row["браузер"] = summary.browser_now
    row["удалено"] = ", ".join(removed)
    row["отключено"] = ", ".join(disabled)
    row["не удалось"] = ", ".join(summary.failed)
    if summary.aborted:
        row["примечание"] = f"прервано: {summary.aborted}"
    if not summary.serial:
        row["серийный"] = row["серийный"] or summary.serial
    return row


def ask_student(log: Log, summary: Summary) -> tuple[str, str]:
    """Спрашивает ФИО и класс. Пустой ввод — пропуск, поле остаётся незаполненным."""
    log.info("ФИО и класс — Enter без ввода пропускает поле")
    try:
        name = input(f"{C.p(C.MAGENTA, '  ?')} ФИО ученика (Enter — пропустить): ").strip()
        grade = ""
        if name:
            grade = input(f"{C.p(C.MAGENTA, '  ?')} Класс (Enter — пропустить): ").strip()
    except EOFError:
        return "", ""
    if not name:
        log.info("ФИО не указано — строка запишется без ученика")
    return name, grade


def inventory_read(path: str) -> list[dict[str, str]]:
    if not os.path.exists(path):
        return []
    with open(path, newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle, delimiter=";"))


def inventory_find(rows: list[dict[str, str]], serial: str) -> int:
    """Индекс строки с этим серийником, иначе -1."""
    for index, row in enumerate(rows):
        if serial and row.get("серийный", "").strip() == serial:
            return index
    return -1


def inventory_write(path: str, rows: list[dict[str, str]]) -> None:
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    # utf-8-sig и ";" — чтобы Excel открывал файл без плясок с кодировкой
    with open(path, "w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=INVENTORY_COLUMNS, delimiter=";")
        writer.writeheader()
        for row in rows:
            writer.writerow({name: row.get(name, "") for name in INVENTORY_COLUMNS})


def inventory_save(path: str, row: dict[str, str], log: Log,
                   on_duplicate: str = "ask", auto: bool = False) -> str:
    """Записывает строку, разбираясь с дублями. Возвращает: added|updated|skipped."""
    rows = inventory_read(path)
    serial = row.get("серийный", "").strip()
    index = inventory_find(rows, serial)

    if index < 0:
        rows.append(row)
        inventory_write(path, rows)
        log.ok(f"записан в таблицу учёта: {path} (всего строк: {len(rows)})")
        return "added"

    existing = rows[index]
    log.warn(f"планшет {serial} уже есть в таблице "
             f"(строка {index + 2}, записан {existing.get('дата', '?')}"
             + (f", ученик: {existing.get('ФИО ученика')}" if existing.get("ФИО ученика") else "")
             + ")")

    decision = on_duplicate
    if decision == "ask":
        decision = "update" if (auto or confirm("Обновить существующую строку?",
                                                default=True, auto=auto)) else "skip"

    if decision == "skip":
        log.info("строка оставлена без изменений")
        return "skipped"
    if decision == "new":
        rows.append(row)
        inventory_write(path, rows)
        log.ok(f"добавлена вторая строка для {serial}")
        return "added"

    # ФИО и класс не затираем пустотой — они могли быть заполнены раньше
    merged = dict(row)
    for field_name in ("ФИО ученика", "класс", "примечание"):
        if not merged.get(field_name) and existing.get(field_name):
            merged[field_name] = existing[field_name]
    rows[index] = merged
    inventory_write(path, rows)
    log.ok(f"строка планшета {serial} обновлена в {path}")
    return "updated"


def fill_device_card(adb: Adb, summary: Summary) -> None:
    """Собирает паспорт планшета: модель, владелец, аккаунты, текущие умолчания."""
    summary.brand = adb.prop("ro.product.brand")
    summary.model = adb.prop("ro.product.model")
    summary.hw_serial = adb.prop("ro.serialno") or summary.serial
    summary.android = f"{adb.prop('ro.build.version.release')} (API {adb.prop('ro.build.version.sdk')})"
    summary.build = adb.prop("ro.build.display.id") or adb.prop("ro.build.id")

    owners = adb.shell("dpm list-owners")
    match = re.search(r"admin=ComponentInfo\{([^}]+)\}", adb.shell("dumpsys device_policy"))
    if match:
        summary.owner_component = match.group(1)
    elif "/" in owners:
        found = re.search(r"[\w.]+/[\w.$]+", owners)
        summary.owner_component = found.group(0) if found else ""
    if summary.owner_component:
        pkg = summary.owner_component.split("/", 1)[0]
        summary.owner_label = "Headwind MDM" if pkg == MDM_PACKAGE else pkg

    summary.restrictions = sorted(read_user_restrictions(adb))
    if not summary.accounts:
        summary.accounts = collect_accounts(adb)
    if not summary.users:
        summary.users = collect_users(adb)
    summary.home_now = current_home(adb) or ""
    summary.browser_now = current_browser(adb) or ""


def read_user_restrictions(adb: Adb) -> set[str]:
    """Ограничения, выставленные владельцем устройства (device owner)."""
    out = adb.shell("dumpsys device_policy")
    restrictions: set[str] = set()
    collecting = False
    for line in out.splitlines():
        stripped = line.strip()
        if stripped.startswith("userRestrictions"):
            collecting = True
            continue
        if collecting:
            if re.fullmatch(r"no_[a-z_]+", stripped):
                restrictions.add(stripped)
            else:
                collecting = False
    return restrictions


def resolve_mdm_home(adb: Adb, log: Log) -> str:
    """Реальное имя HOME-активности MDM (в разных сборках оно различается)."""
    home = detect_home_packages(adb, log)
    activities = home.get(MDM_PACKAGE, [])
    if activities:
        component = f"{MDM_PACKAGE}/{activities[0]}"
        if component != MDM_HOME_ACTIVITY:
            log.info(f"HOME-активность MDM на этом устройстве: {component}")
        return component
    return MDM_HOME_ACTIVITY


def load_allowed_file(path: str, log: Log) -> dict[str, str]:
    """Свой список разрешённых пакетов: по одному в строке, # — комментарий."""
    if not path or not os.path.exists(path):
        return {}
    extra: dict[str, str] = {}
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.split("#", 1)[0].strip()
            if not line:
                continue
            package, _, name = line.partition(" ")
            extra[package.strip()] = name.strip() or "из allowed_apps.txt"
    if extra:
        log.info(f"свой белый список: {len(extra)} пакетов из {os.path.basename(path)}")
    return extra


def package_installer(adb: Adb, package: str) -> tuple[str, str]:
    """(кто установил, когда) — по этому видно, кто принёс приложение."""
    out = adb.shell(f"dumpsys package {package}")
    installer = re.search(r"installerPackageName=(\S+)", out)
    installed = re.search(r"firstInstallTime=(\S+ \S+)", out)
    return (installer.group(1) if installer else "",
            installed.group(1) if installed else "")


def is_allowed_app(package: str, allowed: dict[str, str]) -> bool:
    return package in allowed or package.startswith(ALLOWED_PREFIXES)


def collect_thirdparty(adb: Adb, log: Log, args: argparse.Namespace) -> list[App]:
    """Пользовательские приложения: всё, что не разрешено, — кандидат на снос."""
    allowed = dict(ALLOWED_APPS)
    allowed.update(load_allowed_file(args.allowed_file, log))
    allowed.update({package: "из --keep" for package in args.keep})

    out = adb.shell("pm list packages -3")
    packages = sorted(line.split("package:", 1)[1].strip()
                      for line in out.splitlines() if "package:" in line)

    apps: list[App] = []
    factory: list[str] = []
    for package in packages:
        if is_allowed_app(package, allowed) or is_protected(package, set(args.keep)):
            continue
        installer, installed = package_installer(adb, package)
        if installer in TRUSTED_INSTALLERS:
            log.info(f"{package} поставлен через MDM — оставляю")
            continue

        # Заводские приложения вендора лежат в пользовательском разделе, но
        # имеют нулевую дату установки (1970) — их ставил не ребёнок.
        preinstalled = installed.startswith(("1970", "1971")) or not installed
        if preinstalled and not args.remove_preinstalled:
            factory.append(package)
            continue

        source = APP_STORES.get(installer, installer or "неизвестно")
        apps.append(App(
            package=package,
            name=("заводское приложение вендора" if preinstalled
                  else f"поставлено {installed} · {source}"),
            kind="thirdparty",
            is_system=preinstalled,
            is_known=True,            # неразрешённое = кандидат по определению
        ))

    if factory:
        log.info(f"заводских приложений вендора: {len(factory)} — не трогаю "
                 f"({', '.join(factory[:4])}{'…' if len(factory) > 4 else ''})")
        log.info("снести и их можно ключом --remove-preinstalled")
    return apps


def mute_notifications(adb: Adb, log: Log, packages: list[str], dry_run: bool) -> int:
    """Глушит уведомления — через них дети и открывают игры."""
    muted = 0
    for package in packages:
        # для обычных приложений хватает режима пакета, системным (Play Store,
        # GetApps) нужен ещё и режим uid — иначе настройка молча не применяется
        commands = [f"cmd appops set {package} POST_NOTIFICATION ignore",
                    f"cmd appops set --uid {package} POST_NOTIFICATION ignore"]
        if dry_run:
            for command in commands:
                log.cmd(f"[dry-run] adb shell {command}")
            muted += 1
            continue

        for command in commands:
            log.cmd(f"adb shell {command}")
            adb.shell(command)

        check = adb.shell(f"cmd appops get {package} POST_NOTIFICATION")
        check += adb.shell(f"cmd appops get --uid {package} POST_NOTIFICATION")
        if "ignore" in check:
            log.ok(f"{package} — уведомления отключены")
            muted += 1
        else:
            log.warn(f"{package} — уведомления отключить не удалось: "
                     f"{short_error(check)}")
    return muted


# ─────────────────────── аккаунты и пользователи ───────────────────────


@dataclass
class Account:
    name: str
    type: str

    @property
    def type_label(self) -> str:
        return ACCOUNT_TYPES.get(self.type, self.type)


_ACCOUNT_RE = re.compile(r"Account\s*\{\s*name\s*=\s*(.*?)\s*,\s*type\s*=\s*(.*?)\s*\}")
_USER_RE = re.compile(r"UserInfo\{(\d+):([^:]*):([0-9a-fA-F]+)\}\s*(\w*)")


def collect_accounts(adb: Adb) -> list[Account]:
    out = adb.shell("dumpsys account")
    seen: dict[tuple[str, str], Account] = {}
    for name, atype in _ACCOUNT_RE.findall(out):
        key = (name, atype)
        if key not in seen and name and atype:
            seen[key] = Account(name=name, type=atype)
    return sorted(seen.values(), key=lambda item: (item.type, item.name))


def collect_users(adb: Adb) -> list[tuple[str, str, str]]:
    """[(id, имя, состояние)]"""
    out = adb.shell("pm list users")
    users: list[tuple[str, str, str]] = []
    for uid, name, _flags, state in _USER_RE.findall(out):
        users.append((uid, name or "(без имени)", state or "—"))
    return users


def audit_accounts(adb: Adb, log: Log, args: argparse.Namespace) -> tuple[list[Account], list[tuple[str, str, str]]]:
    log.step("Аудит учётных записей и пользователей")

    users = collect_users(adb)
    if users:
        log.raw()
        log.table(
            ["ID", "ПОЛЬЗОВАТЕЛЬ", "СОСТОЯНИЕ"],
            [[uid, name, state] for uid, name, state in users],
        )
        extra = [item for item in users if item[0] != "0"]
        if extra:
            log.raw()
            log.warn(f"кроме владельца есть профили: {', '.join(uid for uid, _, _ in extra)}")
            log.info("удалить: adb shell pm remove-user <ID>")
        else:
            log.raw()
            log.ok("лишних профилей нет, только владелец (user 0)")
    else:
        log.warn("не удалось получить список пользователей")

    accounts = collect_accounts(adb)
    log.raw()
    if accounts:
        log.table(
            ["АККАУНТ", "ТИП", "ПАКЕТ ТИПА"],
            [[item.name, item.type_label, C.p(C.DIM, item.type)] for item in accounts],
        )
        log.raw()
        log.warn(f"на устройстве {len(accounts)} учётных записей")
        log.info("через ADB аккаунт не удаляется — только вручную в настройках "
                 "или сбросом устройства")
    else:
        log.ok("учётных записей на устройстве нет — чисто")

    guest = adb.shell("settings get global guest_user_enabled").strip()
    creation = adb.shell("settings get global allow_user_creation").strip()
    log.info(f"guest_user_enabled = {guest or 'null'} · allow_user_creation = {creation or 'null'}")

    if args.lock_accounts and not args.dry_run and not args.list_only:
        if confirm("Запретить добавление аккаунтов и пользователей?", default=True, auto=args.auto):
            for cmd in (
                "settings put global guest_user_enabled 0",
                "settings put global allow_user_creation 0",
                f"dpm set-user-restriction {MDM_ADMIN} no_modify_accounts 1",
                f"dpm set-user-restriction {MDM_ADMIN} no_add_user 1",
            ):
                log.cmd(f"adb shell {cmd}")
                out = adb.shell(cmd).strip()
                if out and "Success" not in out:
                    log.warn(f"{cmd} → {out}")
            log.ok("ограничения на аккаунты и пользователей применены")
    elif args.lock_accounts:
        for cmd in ("settings put global guest_user_enabled 0",
                    "settings put global allow_user_creation 0",
                    f"dpm set-user-restriction {MDM_ADMIN} no_modify_accounts 1",
                    f"dpm set-user-restriction {MDM_ADMIN} no_add_user 1"):
            log.cmd(f"[dry-run] adb shell {cmd}")

    return accounts, users


# ─────────────────────────── действия ───────────────────────────


def confirm(question: str, default: bool = False, auto: bool = False) -> bool:
    if auto:
        print(f"{C.p(C.MAGENTA, '  ?')} {question} {C.p(C.DIM, '→ авторежим: да')}")
        return True
    suffix = "[Y/n]" if default else "[y/N]"
    while True:
        try:
            answer = input(f"{C.p(C.MAGENTA, '  ?')} {question} {suffix} ").strip().lower()
        except EOFError:
            return default
        if not answer:
            return default
        if answer in ("y", "yes", "д", "да"):
            return True
        if answer in ("n", "no", "н", "нет"):
            return False


def short_error(out: str) -> str:
    """Из простыни java-стектрейса делает одну внятную строку."""
    if not out:
        return "нет вывода"
    lines = [line.strip() for line in out.splitlines() if line.strip()]
    if not lines:
        return "нет вывода"
    head = lines[0]
    for line in lines:
        if "SecurityException" in line:
            perm = re.search(r"requires:?\s*([\w.]+)", line)
            need = f" (нужно {perm.group(1)})" if perm else ""
            return f"отказано в доступе{need}"
        if "Exception" in line and "occurred while executing" not in line:
            head = line
            break
        if line.startswith("Failure") or "Error" in line:
            head = line
            break
    return head[:200]


def pkg_state(adb: Adb, pkg: str) -> str:
    """installed | disabled | absent — реальное состояние пакета для user 0."""
    def listed(flag: str) -> bool:
        out = adb.shell(" ".join(f"pm list packages {flag} --user 0 {pkg}".split()))
        return any(line.strip() == f"package:{pkg}" for line in out.splitlines())

    if not listed(""):
        return "absent"
    if listed("-d"):
        return "disabled"
    return "installed"


def remove_app(adb: Adb, log: Log, app: App, mode: str, dry_run: bool) -> str:
    """Возвращает статус: removed | disabled | failed.

    Порядок попыток и проверка результата, а не доверие тексту вывода:
      1) pm uninstall -k --user 0      — снос для текущего пользователя
      2) pm uninstall --user 0         — то же, но без сохранения данных
      3) am force-stop + pm disable-user --user 0
      4) pm suspend / pm hide          — только если adb имеет на это права
    """
    pkg = app.package

    def run(cmd: str) -> str:
        if dry_run:
            log.cmd(f"[dry-run] adb shell {cmd}")
            return "dry-run"
        log.cmd(f"adb shell {cmd}")
        return adb.shell(cmd).strip()

    if dry_run:
        for cmd in (f"pm uninstall -k --user 0 {pkg}",):
            run(cmd)
        log.ok(f"{app.title} — будет удалён для user 0")
        return "removed"

    attempts: list[tuple[str, str, str]] = []   # (команда, статус при успехе, пояснение)
    if mode in ("uninstall", "auto"):
        attempts.append((f"pm uninstall -k --user 0 {pkg}", "removed", "удалён для user 0"))
        attempts.append((f"pm uninstall --user 0 {pkg}", "removed", "удалён для user 0 (без -k)"))
    if mode in ("disable", "auto"):
        attempts.append((f"pm disable-user --user 0 {pkg}", "disabled", "отключён (disable-user)"))
        attempts.append((f"pm suspend --user 0 {pkg}", "disabled", "приостановлен (suspend)"))
        attempts.append((f"pm hide --user 0 {pkg}", "disabled", "скрыт (hide)"))

    state = pkg_state(adb, pkg)
    if state == "absent":
        log.ok(f"{app.title} — уже отсутствует у user 0")
        return "removed"
    if state == "disabled" and mode == "disable":
        log.ok(f"{app.title} — уже отключён")
        return "disabled"

    stopped = False
    last_error = ""
    for cmd, success_state, note in attempts:
        if cmd.startswith("pm disable-user") and not stopped:
            run(f"am force-stop {pkg}")
            stopped = True

        out = run(cmd)
        state = pkg_state(adb, pkg)
        if (success_state == "removed" and state == "absent") or (
            success_state == "disabled" and state in ("disabled", "absent")
        ):
            log.ok(f"{app.title} — {note}")
            return "removed" if state == "absent" else "disabled"

        if "Success" in out:
            last_error = f"команда отчиталась Success, но пакет остался в состоянии '{state}'"
        else:
            last_error = short_error(out)
        log.warn(f"{cmd.split(' --user')[0]} не сработал: {last_error}")

    log.err(f"{app.title} — снять не удалось: {last_error}")
    _explain_failure(log, pkg, last_error)
    return "failed"


def _explain_failure(log: Log, pkg: str, error: str) -> None:
    """Подсказки оператору по типовым причинам отказа."""
    if "USER_RESTRICTED" in error:
        log.info("DELETE_FAILED_USER_RESTRICTED — удаление запрещено политикой "
                 "владельца устройства (no_uninstall_apps), а не прошивкой")
    if "отказано в доступе" in error or "MANAGE_USERS" in error:
        log.info("на Android 13+ команды pm hide/suspend закрыты для adb shell — "
                 "это ожидаемо, а не ошибка скрипта")
    log.info("что можно сделать:")
    log.info(f"  1) убедиться, что MDM уже домашний экран: "
             f"adb shell cmd package set-home-activity {MDM_HOME_ACTIVITY}")
    log.info(f"  2) снять пакет через Headwind MDM (он device owner и имеет права, "
             f"которых нет у adb): добавить {pkg} в список запрещённых приложений")
    log.info("  3) для Lenovo: adb shell settings put global productivity_mode 0 "
             "и повторить прогон после перезагрузки")


def set_mdm_home(adb: Adb, log: Log, dry_run: bool, component: str | None = None) -> bool:
    component = component or MDM_HOME_ACTIVITY
    cmd = f"cmd package set-home-activity {component}"
    if dry_run:
        log.cmd(f"[dry-run] adb shell {cmd}")
        return True
    log.cmd(f"adb shell {cmd}")
    out = adb.shell(cmd).strip()
    if "Success" in out:
        log.ok(f"домашний экран закреплён за {component}")
        return True
    log.err(f"не удалось закрепить домашний экран: {out or 'нет вывода'}")
    return False


def set_default_browser(adb: Adb, log: Log, browser: str, dry_run: bool) -> bool:
    cmd = f"cmd role add-role-holder android.app.role.BROWSER {browser}"
    if dry_run:
        log.cmd(f"[dry-run] adb shell {cmd}")
        return True
    log.cmd(f"adb shell {cmd}")
    out = adb.shell(cmd).strip()
    if out and "Error" in out or "Exception" in out:
        log.warn(f"role add-role-holder не сработал: {out}")
        fallback = f"cmd package set-app-link {browser} always"
        log.cmd(f"adb shell {fallback}")
        adb.shell(fallback)
    now = current_browser(adb)
    if now == browser:
        log.ok(f"браузер по умолчанию — {browser}")
        return True
    log.warn(f"браузер по умолчанию сейчас: {now or 'не определён'}")
    return False


# ─────────────────────────── таблицы ───────────────────────────


def print_apps(log: Log, apps: list[App], keep: set[str], keep_note: dict[str, str]) -> None:
    rows: list[list[str]] = []
    for item in apps:
        if item.package in keep:
            action = C.p(C.GREEN, "оставить") + C.p(C.DIM, keep_note.get(item.package, ""))
        elif item.is_known:
            action = C.p(C.RED, "снести")
        else:
            action = C.p(C.YELLOW, "снести (не опознан)")
        kind = "системный" if item.is_system else "польз."
        mark = C.p(C.CYAN, "★") if item.is_default else " "
        rows.append([item.package, item.label, kind, mark, action])
    log.table(["ПАКЕТ", "НАЗВАНИЕ", "ТИП", " ", "ДЕЙСТВИЕ"], rows)
    if rows:
        log.raw(C.p(C.DIM, "  ★ — используется по умолчанию сейчас"))


# ─────────────────────────── основной процесс ───────────────────────────


@dataclass
class Summary:
    serial: str
    device: str = ""
    launchers_removed: list[str] = field(default_factory=list)
    launchers_disabled: list[str] = field(default_factory=list)
    browsers_removed: list[str] = field(default_factory=list)
    browsers_disabled: list[str] = field(default_factory=list)
    assistants_removed: list[str] = field(default_factory=list)
    assistants_disabled: list[str] = field(default_factory=list)
    desktop_removed: list[str] = field(default_factory=list)
    desktop_disabled: list[str] = field(default_factory=list)
    extras_removed: list[str] = field(default_factory=list)
    extras_disabled: list[str] = field(default_factory=list)
    thirdparty_removed: list[str] = field(default_factory=list)
    thirdparty_disabled: list[str] = field(default_factory=list)
    muted: int = 0
    settings_applied: int = 0
    failed: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    accounts: list[Account] = field(default_factory=list)
    users: list[tuple[str, str, str]] = field(default_factory=list)
    home_set: bool = False
    browser_set: bool = False
    owner_set: bool = False
    mdm_installed: bool = False
    users_removed: list[str] = field(default_factory=list)
    aborted: str = ""
    # паспорт устройства
    brand: str = ""
    model: str = ""
    hw_serial: str = ""
    android: str = ""
    build: str = ""
    owner_component: str = ""
    owner_label: str = ""
    restrictions: list[str] = field(default_factory=list)
    home_now: str = ""
    browser_now: str = ""
    specs: dict = field(default_factory=dict)
    student: str = ""
    student_class: str = ""
    inventory_status: str = ""


def cleanup_group(
    adb: Adb,
    log: Log,
    args: argparse.Namespace,
    apps: list[App],
    keep: set[str],
    title: str,
) -> tuple[list[str], list[str], list[str], list[str]]:
    """Возвращает (removed, disabled, failed, skipped)."""
    removed: list[str] = []
    disabled: list[str] = []
    failed: list[str] = []
    skipped: list[str] = []

    targets = [item for item in apps if item.package not in keep]
    if not targets:
        log.raw()
        log.ok(f"{title}: лишнего нет, устройство уже чистое")
        return removed, disabled, failed, skipped

    log.raw()
    if args.list_only:
        log.info(f"{title}: кандидатов на снос — {len(targets)} (режим --list, не трогаю)")
        return removed, disabled, failed, skipped

    if not confirm(f"{title}: снести {len(targets)} шт.?", default=False, auto=args.auto):
        log.warn("пропущено по решению оператора")
        return removed, disabled, failed, [item.package for item in targets]

    for item in targets:
        if not item.is_known and not args.force_unknown:
            if args.auto:
                log.warn(f"{item.package}: не опознан, в авторежиме пропускаю "
                         f"(ключ --force-unknown снимает ограничение)")
                skipped.append(item.package)
                continue
            if not confirm(f"{item.package} не в справочнике. Точно сносить?", default=False):
                log.info(f"{item.package} пропущен")
                skipped.append(item.package)
                continue
        elif not args.auto:
            if not confirm(f"Снести {item.label} ({item.package})?", default=True):
                log.info(f"{item.package} пропущен")
                skipped.append(item.package)
                continue

        status = remove_app(adb, log, item, args.mode, args.dry_run)
        if status == "removed":
            removed.append(item.package)
        elif status == "disabled":
            disabled.append(item.package)
        else:
            failed.append(item.package)

    return removed, disabled, failed, skipped


def process_device(serial: str, args: argparse.Namespace, log: Log) -> Summary:
    adb = Adb(binary=args.adb, serial=serial, log=log, verbose=True)
    summary = Summary(serial=serial)

    log.banner(f"Устройство {serial}", adb.describe())
    summary.device = adb.describe()

    do_launchers = args.only in ("all", "launchers")
    do_browsers = args.only in ("all", "browsers")
    do_assistants = args.only in ("all", "assistants")
    do_desktop = args.only in ("all", "pcmode")
    do_thirdparty = args.only in ("all", "thirdparty")
    do_extras = args.only in ("all", "extras")
    do_accounts = args.only in ("all", "accounts")

    log.step("Проверка MDM-агента")
    if args.list_only:
        mdm_present = MDM_PACKAGE in installed_packages(adb)
        log.ok(f"{MDM_PACKAGE} установлен") if mdm_present else \
            log.warn(f"{MDM_PACKAGE} на устройстве нет")
    else:
        mdm_present = ensure_mdm_installed(adb, log, args)
    all_pkgs = installed_packages(adb)
    summary.mdm_installed = mdm_present

    if not mdm_present:
        if do_launchers and not args.list_only and not args.ignore_missing_mdm:
            log.err("Снос лаунчеров оставит планшет без домашнего экрана — отказываюсь.")
            log.info(f"Положите hmdm.apk рядом со скриптом (или --apk ПУТЬ) и повторите; "
                     f"вручную: adb install -r -g hmdm.apk && adb shell dpm "
                     f"set-device-owner {MDM_ADMIN}")
            summary.aborted = "MDM-агент не установлен"
            fill_device_card(adb, summary)
            return summary
        log.warn("продолжаю без MDM (list-only / --ignore-missing-mdm / другой режим)")

    owners = adb.shell("dpm list-owners")
    if MDM_PACKAGE in owners:
        log.ok("MDM является владельцем устройства (device owner)")
    elif mdm_present and not args.list_only and not args.no_set_owner:
        log.warn("MDM не назначен device owner")
        summary.owner_set = ensure_device_owner(adb, log, args, summary)
        owners = adb.shell("dpm list-owners")
    else:
        log.warn("MDM не назначен device owner — часть операций может не пройти")

    requested_mode = args.mode
    restrictions = read_user_restrictions(adb)
    if "no_uninstall_apps" in restrictions:
        log.warn("владелец устройства выставил DISALLOW_UNINSTALL_APPS "
                 "(no_uninstall_apps) — удаление пакетов заблокировано политикой, "
                 "pm uninstall будет отдавать DELETE_FAILED_USER_RESTRICTED")
        if args.mode == "auto":
            args.mode = "disable"
            log.info("перехожу в режим отключения (disable-user) — он работает; "
                     "лишние попытки удаления пропускаю")
        log.info("чтобы именно УДАЛЯТЬ, снимите ограничение в консоли Headwind MDM: "
                 "Конфигурации → ограничения → «Запретить удаление приложений», "
                 "примените на устройство и повторите прогон "
                 "(через adb это не снимается: dpm set-user-restriction "
                 "в Android 13+ отсутствует)")
        log.info("альтернатива — снести пакеты силами самого MDM: он device owner "
                 "и ограничение на него не распространяется")
    if "no_control_apps" in restrictions:
        log.warn("выставлен DISALLOW_APPS_CONTROL (no_control_apps) — "
                 "часть операций с пакетами может отклоняться")

    # ── 1. Лаунчеры ──
    if do_launchers:
        # Домашний экран закрепляем ДО сноса: система не даёт отключить
        # лаунчер, который прямо сейчас является текущим домашним экраном.
        if mdm_present and not args.list_only and not args.no_set_home:
            log.step("Предварительное закрепление MDM как домашнего экрана")
            mdm_home = resolve_mdm_home(adb, log)
            summary.home_set = set_mdm_home(adb, log, args.dry_run, mdm_home)

        keep = {pkg for pkg in PROTECTED} | set(args.keep)
        note = {MDM_PACKAGE: " (MDM)"}
        log.step("Сканирование лаунчеров (category.HOME)")
        launchers = collect_launchers(adb, log)
        log.info(f"найдено кандидатов: {len(launchers)}")
        log.raw()
        print_apps(log, launchers, {p for p in keep if p in {a.package for a in launchers}}, note)
        removed, disabled, failed, skipped = cleanup_group(
            adb, log, args, launchers, keep, "Лаунчеры"
        )
        summary.launchers_removed = removed
        summary.launchers_disabled = disabled
        summary.failed += failed
        summary.skipped += skipped

    # ── 2. Браузеры ──
    if do_browsers:
        browser_keep = set(PROTECTED) | set(args.keep) | {args.browser} | BROWSER_NON_TARGETS
        note = {args.browser: " (целевой браузер)"}
        log.step("Сканирование браузеров (обработчики http/https)")
        browsers = collect_browsers(adb, log)
        log.info(f"найдено кандидатов: {len(browsers)}")
        if args.browser not in all_pkgs:
            log.warn(f"{args.browser} не установлен — снос остальных браузеров "
                     f"оставит планшет без браузера вовсе")
            if not args.list_only and not confirm(
                "Всё равно продолжить с браузерами?", default=False, auto=args.auto
            ):
                browsers = []
        log.raw()
        print_apps(log, browsers,
                   {p for p in browser_keep if p in {a.package for a in browsers}}, note)
        removed, disabled, failed, skipped = cleanup_group(
            adb, log, args, browsers, browser_keep, "Браузеры"
        )
        summary.browsers_removed = removed
        summary.browsers_disabled = disabled
        summary.failed += failed
        summary.skipped += skipped

        if not args.list_only and args.browser in all_pkgs:
            log.step("Назначение браузера по умолчанию")
            summary.browser_set = set_default_browser(adb, log, args.browser, args.dry_run)

    # ── 3. Голосовые ассистенты ──
    if do_assistants:
        log.step("Сканирование голосовых ассистентов")
        assistants = collect_assistants(adb, log)
        log.info(f"найдено кандидатов: {len(assistants)}")
        keep_assist = set(PROTECTED) | set(args.keep)
        log.raw()
        print_apps(log, assistants,
                   {p for p in keep_assist if p in {a.package for a in assistants}}, {})
        removed, disabled, failed, skipped = cleanup_group(
            adb, log, args, assistants, keep_assist, "Ассистенты"
        )
        summary.assistants_removed = removed
        summary.assistants_disabled = disabled
        summary.failed += failed
        summary.skipped += skipped

        if not args.list_only:
            log.info("снимаю роль ассистента и отключаю вызов по кнопке/жесту")
            if not args.dry_run:
                holder = adb.shell("cmd role get-role-holders android.app.role.ASSISTANT").strip()
                for pkg in re.findall(r"[\w.]+\.[\w.]+", holder):
                    log.cmd(f"adb shell cmd role remove-role-holder "
                            f"android.app.role.ASSISTANT {pkg}")
                    adb.shell(f"cmd role remove-role-holder android.app.role.ASSISTANT {pkg}")
            summary.settings_applied += apply_settings(
                adb, log, ASSISTANT_SETTINGS, args.dry_run
            )

    # ── 4. Режим ПК / десктопные оболочки ──
    if do_desktop:
        log.step("Сканирование режима ПК и десктопных оболочек")
        desktop = collect_desktop_mode(adb, args.with_freeform)
        log.info(f"найдено кандидатов: {len(desktop)}")
        keep_desktop = set(PROTECTED) | set(args.keep)
        log.raw()
        print_apps(log, desktop,
                   {p for p in keep_desktop if p in {a.package for a in desktop}}, {})
        removed, disabled, failed, skipped = cleanup_group(
            adb, log, args, desktop, keep_desktop, "Режим ПК"
        )
        summary.desktop_removed = removed
        summary.desktop_disabled = disabled
        summary.failed += failed
        summary.skipped += skipped

        if not args.list_only:
            log.info("выключаю системные переключатели режима ПК")
            summary.settings_applied += apply_settings(
                adb, log, DESKTOP_SETTINGS, args.dry_run
            )
            check_desktop_residue(adb, log, args.dry_run)

    # ── 5. Сторонние приложения (то, что поставили дети) ──
    if do_thirdparty:
        log.step("Сканирование пользовательских приложений")
        thirdparty = collect_thirdparty(adb, log, args)
        log.info(f"неразрешённых приложений: {len(thirdparty)}")
        keep_third = set(PROTECTED) | set(args.keep)
        log.raw()
        print_apps(log, thirdparty, set(), {})
        removed, disabled, failed, skipped = cleanup_group(
            adb, log, args, thirdparty, keep_third, "Сторонние приложения"
        )
        summary.thirdparty_removed = removed
        summary.thirdparty_disabled = disabled
        summary.failed += failed
        summary.skipped += skipped

        if not args.list_only and args.mute_notifications:
            log.step("Блокировка уведомлений")
            targets = [app.package for app in thirdparty
                       if app.package not in removed]
            if args.mute_stores:
                installed = installed_packages(adb)
                targets += [store for store in APP_STORES if store in installed]
            if targets:
                summary.muted = mute_notifications(adb, log, targets, args.dry_run)
                log.ok(f"уведомления заглушены у {summary.muted} приложений")
            else:
                log.ok("глушить нечего — всё лишнее снято")

    # ── 6. Лишние приложения ──
    if do_extras:
        log.step("Сканирование лишних приложений")
        extras = collect_by_catalog(adb, KNOWN_EXTRA_APPS, "extra")
        log.info(f"найдено кандидатов: {len(extras)}")
        keep_extras = set(PROTECTED) | set(args.keep)
        log.raw()
        print_apps(log, extras,
                   {p for p in keep_extras if p in {a.package for a in extras}}, {})
        removed, disabled, failed, skipped = cleanup_group(
            adb, log, args, extras, keep_extras, "Лишние приложения"
        )
        summary.extras_removed = removed
        summary.extras_disabled = disabled
        summary.failed += failed
        summary.skipped += skipped

    # ── 7. Аккаунты ──
    if do_accounts:
        summary.accounts, summary.users = audit_accounts(adb, log, args)

    # ── 8. Контрольное закрепление домашнего экрана ──
    if do_launchers and not args.list_only and not args.no_set_home and mdm_present:
        log.step("Контроль домашнего экрана")
        summary.home_set = set_mdm_home(
            adb, log, args.dry_run, resolve_mdm_home(adb, log)
        ) or summary.home_set
        if not args.dry_run:
            home_now = current_home(adb)
            if home_now == MDM_PACKAGE:
                log.ok(f"проверка: текущий домашний экран — {home_now}")
            else:
                log.warn(f"проверка: домашний экран сейчас {home_now or 'не определён'}")

    if args.restart_mdm and not args.dry_run and not args.list_only and mdm_present:
        log.step("Перезапуск MDM-агента")
        adb.shell(f"am force-stop {MDM_PACKAGE}")
        adb.shell(f"monkey -p {MDM_PACKAGE} -c android.intent.category.LAUNCHER 1")
        log.ok("MDM перезапущен")

    log.step("Сбор паспорта устройства")
    fill_device_card(adb, summary)
    log.ok("данные собраны")

    if args.inventory and not args.list_only:
        summary.specs = collect_specs(adb, log)
        log.step("Учёт в таблице")
        row = inventory_row(summary, summary.specs)

        if args.student or args.student_class:
            summary.student = args.student
            summary.student_class = args.student_class
        elif args.ask_student and not args.auto_student_skip:
            summary.student, summary.student_class = ask_student(log, summary)
        row["ФИО ученика"] = summary.student
        row["класс"] = summary.student_class

        summary.inventory_status = inventory_save(
            args.inventory, row, log, on_duplicate=args.on_duplicate, auto=args.auto
        )

    args.mode = requested_mode   # следующему устройству — исходный режим
    return summary


def print_alert_plate(log: Log, lines: list[str], color: str = C.RED) -> None:
    """Крупная рамка-предупреждение во всю ширину вывода."""
    width = 74
    log.raw()
    log.raw(C.p(color + C.BOLD, "╔" + "═" * width + "╗"))
    log.raw(C.p(color + C.BOLD, "║" + " " * width + "║"))
    for text in lines:
        pad = width - _vis_len(text)
        left = pad // 2
        right = pad - left
        log.raw(C.p(color + C.BOLD, "║" + " " * left + text + " " * right + "║"))
    log.raw(C.p(color + C.BOLD, "║" + " " * width + "║"))
    log.raw(C.p(color + C.BOLD, "╚" + "═" * width + "╝"))


def stage_totals(item: Summary) -> list[tuple[str, str, str]]:
    """Разбивка «что сняли» по этапам — одна и та же в CLI и в TUI."""
    groups = [
        ("Лаунчеры", item.launchers_removed, item.launchers_disabled),
        ("Браузеры", item.browsers_removed, item.browsers_disabled),
        ("Ассистенты", item.assistants_removed, item.assistants_disabled),
        ("Режим ПК", item.desktop_removed, item.desktop_disabled),
        ("Прочее", item.extras_removed, item.extras_disabled),
        ("Сторонние (дети)", item.thirdparty_removed, item.thirdparty_disabled),
    ]
    lines: list[tuple[str, str, str]] = []
    for label, removed, disabled in groups:
        if removed or disabled:
            parts = []
            if removed:
                parts.append(f"удалено {len(removed)}")
            if disabled:
                parts.append(f"отключено {len(disabled)}")
            lines.append((label, " · ".join(parts), C.GREEN))
        else:
            lines.append((label, "ничего не снято", C.DIM))

    extra_users = [uid for uid, _, _ in item.users if uid != "0"]
    if item.users_removed:
        users = f"{len(item.users)} · удалено профилей: {len(item.users_removed)}"
        color = C.GREEN
    elif extra_users:
        users = f"{len(item.users)} · лишние: {', '.join(extra_users)}"
        color = C.YELLOW
    else:
        users = f"{len(item.users)} · только владелец"
        color = C.GREEN
    lines.append(("Пользователи", users, color))

    if item.muted:
        lines.append(("Уведомления", f"заглушены у {item.muted}", C.GREEN))
    if item.failed:
        lines.append(("Не удалось снять", f"{len(item.failed)} · "
                      + ", ".join(item.failed), C.RED))
    return lines


def print_device_card(log: Log, item: Summary) -> None:
    """Отдельный блок-паспорт: модель, владелец, аккаунты, текущие умолчания."""
    width = 74
    log.raw()
    log.raw(C.p(C.CYAN, "┌" + "─" * width + "┐"))
    title = f" ПАСПОРТ ПЛАНШЕТА · {item.serial}"
    log.raw(C.p(C.CYAN, "│") + C.p(C.BOLD, title.ljust(width)) + C.p(C.CYAN, "│"))
    log.raw(C.p(C.CYAN, "├" + "─" * width + "┤"))

    def row(label: str, value: str, color: str = "") -> None:
        value = value or "—"
        text = f" {label:<20}{value}"
        painted = f" {label:<20}" + (C.p(color, value) if color else value)
        if _vis_len(text) > width:
            painted = painted + " " * 0
            log.raw(C.p(C.CYAN, "│") + painted + C.p(C.CYAN, ""))
            return
        pad = " " * (width - _vis_len(text))
        log.raw(C.p(C.CYAN, "│") + painted + pad + C.p(C.CYAN, "│"))

    model = " ".join(part for part in (item.brand, item.model) if part)
    row("Модель", model)
    row("Серийный номер", item.hw_serial)
    row("Android", item.android)
    row("Прошивка", item.build)

    owner = item.owner_component or "не назначен"
    owner_color = C.GREEN if item.owner_component.startswith(MDM_PACKAGE) else C.YELLOW
    row("MDM-агент", "установлен" if item.mdm_installed else "НЕТ",
        C.GREEN if item.mdm_installed else C.RED)
    row("Владелец (owner)", owner, owner_color)
    row("Кто держит owner", item.owner_label or "никто", owner_color)
    row("Ограничения owner", ", ".join(item.restrictions) or "нет")

    google = [acc for acc in item.accounts if acc.type == "com.google"]
    other = [acc for acc in item.accounts if acc.type != "com.google"]
    if google:
        for index, acc in enumerate(google):
            row("Google-аккаунт" if index == 0 else "", acc.name, C.YELLOW)
    else:
        row("Google-аккаунт", "НЕТ", C.RED)
    if other:
        row("Прочие аккаунты", ", ".join(f"{acc.name} [{acc.type_label}]" for acc in other))

    row("Домашний экран", item.home_now,
        C.GREEN if item.home_now == MDM_PACKAGE else C.YELLOW)
    row("Браузер", item.browser_now,
        C.GREEN if item.browser_now == DEFAULT_BROWSER else C.YELLOW)

    log.raw(C.p(C.CYAN, "├" + "─" * width + "┤"))
    for label, value, color in stage_totals(item):
        row(label, value, color)
    log.raw(C.p(C.CYAN, "└" + "─" * width + "┘"))

    if not item.owner_component:
        print_alert_plate(log, [
            "!!!  В Н И М А Н И Е  !!!",
            "",
            "ВЛАДЕЛЕЦ УСТРОЙСТВА (DEVICE OWNER) НЕ НАЗНАЧЕН",
            "",
            f"{item.brand} {item.model} · {item.serial}".strip(),
            "MDM не сможет управлять планшетом в полном объёме",
        ])

    if not google:
        print_alert_plate(log, [
            "!!!  В Н И М А Н И Е  !!!",
            "",
            "НА ПЛАНШЕТЕ НЕТ GOOGLE-АККАУНТА",
            "",
            f"{item.brand} {item.model} · {item.serial}".strip(),
            "добавьте учётную запись перед выдачей планшета в школу",
        ])


def print_summary(log: Log, summaries: list[Summary], args: argparse.Namespace) -> int:
    subtitle = "режим проверки (dry-run), изменений не вносилось" if args.dry_run else ""
    if args.list_only:
        subtitle = "режим --list, изменений не вносилось"
    log.banner("ИТОГО", subtitle)
    exit_code = 0
    for item in summaries:
        print_device_card(log, item)
        log.raw()
        log.raw(C.p(C.BOLD, f"  ЧТО СДЕЛАНО · {item.serial}"))
        if item.aborted:
            log.raw(f"    {C.p(C.RED, 'прервано:')} {item.aborted}")
            exit_code = 1
            continue

        def line(title: str, color: str, items: list[str]) -> None:
            if items:
                log.raw(f"    {C.p(color, title)} {len(items)}  {C.p(C.DIM, ', '.join(items))}")
            else:
                log.raw(f"    {C.p(color, title)} 0")

        line("лаунчеры удалены:  ", C.GREEN, item.launchers_removed)
        line("лаунчеры отключены:", C.YELLOW, item.launchers_disabled)
        line("браузеры удалены:  ", C.GREEN, item.browsers_removed)
        line("браузеры отключены:", C.YELLOW, item.browsers_disabled)
        line("ассистенты удалены:", C.GREEN, item.assistants_removed)
        line("ассист. отключены: ", C.YELLOW, item.assistants_disabled)
        line("режим ПК удалён:   ", C.GREEN, item.desktop_removed)
        line("режим ПК отключён: ", C.YELLOW, item.desktop_disabled)
        line("прочее удалено:    ", C.GREEN, item.extras_removed)
        line("прочее отключено:  ", C.YELLOW, item.extras_disabled)
        if item.skipped:
            line("пропущено:         ", C.BLUE, item.skipped)
        if item.failed:
            line("ошибки:            ", C.RED, item.failed)
            exit_code = 1
        log.raw(f"    {C.p(C.CYAN, 'домашний экран:    ')} "
                + (C.p(C.GREEN, MDM_PACKAGE) if item.home_set else C.p(C.YELLOW, "не менялся")))
        log.raw(f"    {C.p(C.CYAN, 'настроек выставлено')} {item.settings_applied}")
        log.raw(f"    {C.p(C.CYAN, 'браузер по умолч.: ')} "
                + (C.p(C.GREEN, args.browser) if item.browser_set else C.p(C.YELLOW, "не менялся")))
    log.raw()
    return exit_code


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="launcher_cleanup.py",
        description="Зачистка лаунчеров и браузеров + аудит аккаунтов на школьных планшетах.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--adb", default=os.environ.get("ADB", ""),
                        help="путь к adb (по умолчанию ищется в PATH, рядом со "
                             "скриптом и в папке platform-tools)")
    parser.add_argument("-s", "--device", help="серийный номер устройства")
    parser.add_argument("--all-devices", action="store_true",
                        help="обработать все подключённые устройства")
    parser.add_argument("--only",
                        choices=["all", "launchers", "browsers", "assistants", "pcmode",
                                 "thirdparty", "extras", "accounts"],
                        default="all", help="выполнить только один этап (по умолчанию all)")
    parser.add_argument("-y", "--auto", action="store_true",
                        help="авторежим: без подтверждений (незнакомые пакеты всё равно пропускаются)")
    parser.add_argument("--force-unknown", action="store_true",
                        help="сносить и неопознанные пакеты (в связке с --auto)")
    parser.add_argument("-n", "--dry-run", action="store_true",
                        help="показать команды, ничего не выполнять")
    parser.add_argument("--list", dest="list_only", action="store_true",
                        help="только показать найденное (лаунчеры, браузеры, аккаунты)")
    parser.add_argument("--mode", choices=["auto", "uninstall", "disable"], default="auto",
                        help="auto: удалить, при неудаче отключить (по умолчанию)")
    parser.add_argument("--browser", default=DEFAULT_BROWSER,
                        help=f"браузер, который остаётся (по умолчанию {DEFAULT_BROWSER})")
    parser.add_argument("--keep", action="append", default=[], metavar="PKG",
                        help="дополнительный пакет в белый список (можно повторять)")
    parser.add_argument("--with-freeform", action="store_true",
                        help="заодно снести плавающую панель ZUI (com.zui.freeform.sidebar)")
    parser.add_argument("--lock-accounts", action="store_true",
                        help="запретить гостя, создание пользователей и изменение аккаунтов")
    default_allowed = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                   "allowed_apps.txt")
    parser.add_argument("--allowed-file", default=default_allowed, metavar="ФАЙЛ",
                        help=f"свой список разрешённых пакетов "
                             f"(по умолчанию {os.path.basename(default_allowed)})")
    parser.add_argument("--no-mute-notifications", dest="mute_notifications",
                        action="store_false",
                        help="не глушить уведомления у неразрешённых приложений")
    parser.add_argument("--remove-preinstalled", action="store_true",
                        help="сносить и заводские приложения вендора "
                             "(калькулятор, погода, заметки и т.п.)")
    parser.add_argument("--mute-stores", action="store_true",
                        help="заглушить и уведомления магазинов (Play, GetApps)")
    parser.add_argument("--apk", default="", metavar="ПУТЬ",
                        help="APK MDM-агента (по умолчанию hmdm.apk рядом со скриптом)")
    parser.add_argument("--no-install-mdm", dest="install_mdm", action="store_false",
                        help="не устанавливать MDM, даже если его нет")
    parser.add_argument("--no-mdm-perms", dest="mdm_perms", action="store_false",
                        help="не выдавать разрешения MDM после установки")
    parser.add_argument("--remove-extra-users", action="store_true",
                        help="удалять лишние профили, если они мешают назначить "
                             "владельца устройства (данные профиля пропадут)")
    parser.add_argument("--no-set-owner", action="store_true",
                        help="не назначать MDM владельцем устройства, если владельца нет")
    parser.add_argument("--no-set-home", action="store_true",
                        help="не закреплять MDM как домашний экран")
    parser.add_argument("--restart-mdm", action="store_true",
                        help="перезапустить MDM-агент в конце")
    parser.add_argument("--ignore-missing-mdm", action="store_true",
                        help="работать, даже если MDM-агент не установлен (опасно)")
    default_inventory = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                     "inventory.csv")
    parser.add_argument("--inventory", default=default_inventory, metavar="CSV",
                        help=f"файл учёта планшетов (по умолчанию {default_inventory})")
    parser.add_argument("--no-inventory", dest="inventory", action="store_const", const="",
                        help="не вести таблицу учёта")
    parser.add_argument("--no-ask-student", dest="ask_student", action="store_false",
                        help="не спрашивать ФИО и класс ученика")
    parser.add_argument("--student", default="", metavar="ФИО",
                        help="ФИО ученика без вопроса (для пакетного режима)")
    parser.add_argument("--class", dest="student_class", default="", metavar="КЛАСС",
                        help="класс ученика без вопроса")
    parser.add_argument("--on-duplicate", choices=["ask", "update", "skip", "new"],
                        default="ask",
                        help="что делать, если планшет уже есть в таблице "
                             "(по умолчанию спросить; в авторежиме — обновить)")
    parser.add_argument("--log-dir",
                        default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs"),
                        help="каталог для лог-файлов")
    parser.add_argument("--no-log", action="store_true", help="не писать лог на диск")
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    args.auto_student_skip = False

    log_path = None
    if not args.no_log:
        os.makedirs(args.log_dir, exist_ok=True)
        stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        log_path = os.path.join(args.log_dir, f"launcher-cleanup_{stamp}.log")

    log = Log(log_path)
    try:
        flags = []
        if args.dry_run:
            flags.append("DRY-RUN")
        if args.list_only:
            flags.append("LIST")
        if args.auto:
            flags.append("AUTO")
        log.banner(
            f"Wunder Tablet · подготовка планшетов v{VERSION}",
            f"лаунчер: {MDM_PACKAGE} · браузер: {args.browser}"
            + (f"   [{' '.join(flags)}]" if flags else ""),
        )
        if log_path:
            log.info(f"лог: {log_path}")

        adb_bin = find_adb(args.adb)
        if not adb_bin:
            log.err("adb не найден ни в PATH, ни рядом со скриптом")
            log.info(adb_hint())
            return 2
        args.adb = adb_bin

        version_line = subprocess.run([adb_bin, "version"], capture_output=True,
                                      text=True).stdout.splitlines()
        log.info(f"adb: {adb_bin} ({version_line[0] if version_line else '?'})")

        log.step("Поиск устройств")
        devices = list_devices(adb_bin)
        online = [serial for serial, state in devices if state == "device"]
        for serial, state in devices:
            if state == "device":
                log.ok(f"{serial} — готов")
            else:
                log.warn(f"{serial} — состояние '{state}' (разрешите отладку на планшете)")

        if not online:
            log.err("нет доступных устройств. Проверьте кабель и отладку по USB.")
            return 2

        if args.device:
            if args.device not in online:
                log.err(f"устройство {args.device} не найдено среди доступных: {', '.join(online)}")
                return 2
            targets = [args.device]
        elif args.all_devices:
            targets = online
        elif len(online) == 1:
            targets = online
        else:
            log.warn(f"подключено {len(online)} устройств. Укажите -s SERIAL или --all-devices.")
            return 2

        if len(targets) > 1 and not args.list_only:
            if not confirm(f"Обработать {len(targets)} устройств подряд?",
                           default=False, auto=args.auto):
                log.warn("отменено пользователем")
                return 1

        summaries = [process_device(serial, args, log) for serial in targets]
        return print_summary(log, summaries, args)

    except AdbError as exc:
        log.err(f"ошибка ADB: {exc}")
        return 2
    except KeyboardInterrupt:
        log.raw()
        log.warn("прервано с клавиатуры")
        return 130
    finally:
        log.close()


if __name__ == "__main__":
    sys.exit(main())
