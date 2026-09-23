#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
launcher_gui.py — ЭКСПЕРИМЕНТАЛЬНАЯ графическая оболочка (PySide6).

Движок не дублируется: весь рабочий код берётся из launcher_cleanup.py,
GUI только показывает и настраивает. Стабильная версия — сам скрипт CLI.

Запуск:  python3 launcher_gui.py
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import re
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import launcher_cleanup as core

try:
    from PySide6.QtCore import Qt, QObject, QThread, Signal, QSize
    from PySide6.QtGui import QFont, QIcon, QTextCursor, QAction
    from PySide6.QtWidgets import (
        QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QSplitter,
        QListWidget, QListWidgetItem, QPushButton, QLabel, QTextEdit, QTabWidget,
        QCheckBox, QComboBox, QLineEdit, QGroupBox, QFormLayout, QMessageBox,
        QPlainTextEdit, QProgressBar, QFrame, QSizePolicy,
    )
except ImportError:
    sys.exit("Нужен PySide6:  pip install PySide6")

CONFIG_PATH = os.path.expanduser("~/.config/wunder-tablet/gui.json")
GUI_VERSION = "0.1 (экспериментальная)"

# ─────────────────────────── тема ───────────────────────────

BG = "#12141a"
BG2 = "#181b23"
BG3 = "#1f232d"
FG = "#e6e9f0"
DIM = "#8b93a7"
ACCENT = "#4da3ff"
OK = "#4ade80"
WARN = "#fbbf24"
ERR = "#f87171"
LINE = "#2a2f3c"

QSS = f"""
QWidget {{ background: {BG}; color: {FG}; font-size: 13px; }}
QMainWindow {{ background: {BG}; }}
QGroupBox {{
    border: 1px solid {LINE}; border-radius: 10px; margin-top: 14px;
    padding: 12px 10px 10px 10px; background: {BG2};
}}
QGroupBox::title {{
    subcontrol-origin: margin; left: 12px; padding: 0 6px;
    color: {DIM}; font-weight: 600; text-transform: uppercase; font-size: 11px;
}}
QPushButton {{
    background: {BG3}; border: 1px solid {LINE}; border-radius: 8px;
    padding: 8px 16px; font-weight: 600;
}}
QPushButton:hover {{ border-color: {ACCENT}; color: {ACCENT}; }}
QPushButton:disabled {{ color: {DIM}; border-color: {LINE}; }}
QPushButton#primary {{ background: {ACCENT}; color: #06121f; border: none; }}
QPushButton#primary:hover {{ background: #6fb6ff; }}
QPushButton#danger {{ background: #3a1d22; border-color: #5c2b33; color: {ERR}; }}
QListWidget, QTextEdit, QPlainTextEdit, QLineEdit, QComboBox {{
    background: {BG2}; border: 1px solid {LINE}; border-radius: 8px; padding: 6px;
    selection-background-color: {ACCENT}; selection-color: #06121f;
}}
QListWidget::item {{ padding: 8px; border-radius: 6px; }}
QListWidget::item:selected {{ background: {BG3}; color: {ACCENT}; }}
QTabWidget::pane {{ border: 1px solid {LINE}; border-radius: 10px; background: {BG2}; }}
QTabBar::tab {{
    background: transparent; color: {DIM}; padding: 9px 18px;
    border-bottom: 2px solid transparent; font-weight: 600;
}}
QTabBar::tab:selected {{ color: {FG}; border-bottom: 2px solid {ACCENT}; }}
QCheckBox {{ spacing: 8px; padding: 3px; }}
QCheckBox::indicator {{
    width: 17px; height: 17px; border: 1px solid {LINE};
    border-radius: 5px; background: {BG3};
}}
QCheckBox::indicator:checked {{ background: {ACCENT}; border-color: {ACCENT}; }}
QProgressBar {{
    border: none; background: {BG3}; border-radius: 4px; height: 6px; text-align: center;
}}
QProgressBar::chunk {{ background: {ACCENT}; border-radius: 4px; }}
QLabel#h1 {{ font-size: 18px; font-weight: 700; }}
QLabel#sub {{ color: {DIM}; font-size: 12px; }}
QSplitter::handle {{ background: {LINE}; width: 1px; }}
"""

LEVEL_COLORS = {
    "ok": OK, "warn": WARN, "err": ERR, "info": ACCENT,
    "step": "#c4b5fd", "cmd": "#5c6478", "raw": FG, "banner": ACCENT,
}


# ─────────────────────── мост лога в GUI ───────────────────────


class Bridge(QObject):
    line = Signal(str, str)              # level, text
    ask = Signal(str, bool)              # вопрос, значение по умолчанию
    done = Signal(object)                # core.Summary | None
    progress = Signal(str)
    stores = Signal(object)              # list[core.StoreInfo]


class GuiLog(core.Log):
    """core.Log, который вместо print() отправляет строки в интерфейс."""

    def __init__(self, bridge: Bridge, path: str | None = None):
        super().__init__(path)
        self.bridge = bridge

    def _send(self, level: str, text: str) -> None:
        self.bridge.line.emit(level, core._strip_ansi(text))

    def raw(self, text: str = "") -> None:
        self._send("raw", text)
        self._write("", core._strip_ansi(text))

    def info(self, text: str) -> None:
        self._send("info", "• " + text)
        self._write("INFO", core._strip_ansi(text))

    def ok(self, text: str) -> None:
        self._send("ok", "✔ " + text)
        self._write("OK", core._strip_ansi(text))

    def warn(self, text: str) -> None:
        self._send("warn", "⚠ " + text)
        self._write("WARN", core._strip_ansi(text))

    def err(self, text: str) -> None:
        self._send("err", "✖ " + text)
        self._write("ERROR", core._strip_ansi(text))

    def step(self, text: str) -> None:
        self._send("step", "\n▸ " + text)
        self.bridge.progress.emit(text)
        self._write("STEP", core._strip_ansi(text))

    def cmd(self, text: str) -> None:
        # в логе GUI путь до adb только мешает: /usr/bin/adb -s X … → adb -s X …
        short = re.sub(r"^\S*/([\w.-]+)(?=\s)", r"\1", text)
        self._send("cmd", "    $ " + short)
        self._write("CMD", text)

    def banner(self, title: str, subtitle: str = "") -> None:
        self._send("banner", f"\n╭─ {title}")
        if subtitle:
            self._send("cmd", f"╰─ {subtitle}")
        self._write("", f"=== {title} {subtitle} ===")


# ─────────────────────── рабочий поток ───────────────────────


class Worker(QThread):
    def __init__(self, serial: str, args: argparse.Namespace, bridge: Bridge):
        super().__init__()
        self.serial = serial
        self.args = args
        self.bridge = bridge
        self._answer: bool | None = None
        self._event = threading.Event()

    def answer(self, value: bool) -> None:
        self._answer = value
        self._event.set()

    def _confirm(self, question: str, default: bool = False, auto: bool = False) -> bool:
        if auto or getattr(self.args, "auto", False):
            return True
        self._event.clear()
        self.bridge.ask.emit(question, default)
        self._event.wait()
        return bool(self._answer)

    def run(self) -> None:
        log = GuiLog(self.bridge, self.args.log_path)
        original = core.confirm
        core.confirm = self._confirm
        try:
            summary = core.process_device(self.serial, self.args, log)
            self.bridge.done.emit(summary)
        except Exception as exc:                      # noqa: BLE001 — показать оператору
            log.err(f"сбой: {exc}")
            self.bridge.done.emit(None)
        finally:
            core.confirm = original
            log.close()


class StoresWorker(QThread):
    """Отдельный поток для вкладки «Магазины»: сканирование и вкл/выкл."""

    def __init__(self, serial: str, adb_binary: str, action: str,
                 packages: list[str], bridge: Bridge, deep: bool = True):
        super().__init__()
        self.serial = serial
        self.adb_binary = adb_binary
        self.action = action                  # scan | disable | enable | remove
        self.packages = packages
        self.bridge = bridge
        self.deep = deep

    def run(self) -> None:
        log = GuiLog(self.bridge)
        adb = core.Adb(binary=self.adb_binary, serial=self.serial, log=log, verbose=True)
        try:
            if self.action != "scan":
                titles = {"disable": "Отключение магазинов",
                          "enable": "Включение магазинов",
                          "remove": "Снос магазинов"}[self.action]
                log.step(titles)
                for package in self.packages:
                    if self.action == "enable":
                        core.enable_app(adb, log, package, dry_run=False)
                        continue
                    app = core.App(package=package, name=core.APP_STORES.get(package, ""),
                                   kind="store", is_system=True, is_known=True)
                    core.remove_app(adb, log, app,
                                    "uninstall" if self.action == "remove" else "disable",
                                    dry_run=False)
            log.step("Сканирование магазинов приложений")
            stores = core.collect_stores(adb, log, deep=self.deep)
            log.ok(f"найдено магазинов: {len(stores)}")
            self.bridge.stores.emit(stores)
        except Exception as exc:                      # noqa: BLE001 — показать оператору
            log.err(f"сбой: {exc}")
            self.bridge.stores.emit([])
        finally:
            log.close()


# ─────────────────────── карточка устройства ───────────────────────


def card_html(summary: core.Summary | None) -> str:
    if summary is None:
        return (f"<div style='color:{DIM};padding:14px'>Выберите планшет и нажмите "
                f"<b>Сканировать</b> — здесь появится паспорт устройства.</div>")

    def row(label: str, value: str, color: str = FG) -> str:
        return (f"<tr><td style='color:{DIM};padding:3px 14px 3px 0;white-space:nowrap'>{label}</td>"
                f"<td style='color:{color};padding:3px 0'><b>{value or '—'}</b></td></tr>")

    google = [acc for acc in summary.accounts if acc.type == "com.google"]
    other = [acc for acc in summary.accounts if acc.type != "com.google"]
    extra_users = [uid for uid, _, _ in summary.users if uid != "0"]
    owner_ok = summary.owner_component.startswith(core.MDM_PACKAGE)

    rows = [
        row("Модель", f"{summary.brand} {summary.model}".strip()),
        row("Серийный номер", summary.hw_serial),
        row("Android", summary.android),
        row("Прошивка", summary.build),
        row("Владелец (owner)", summary.owner_component or "не назначен",
            OK if owner_ok else ERR),
        row("Кто держит owner", summary.owner_label or "никто", OK if owner_ok else ERR),
        row("Ограничения owner", ", ".join(summary.restrictions) or "нет",
            WARN if summary.restrictions else FG),
        row("Wi-Fi MAC", f"{summary.specs.get('Wi-Fi MAC','—')} · "
            f"{summary.specs.get('рандомизация MAC','неизвестно')}"),
        row("Google-аккаунт",
            "<br>".join(acc.name for acc in google) if google else "НЕТ",
            FG if google else ERR),
    ]
    if other:
        rows.append(row("Прочие аккаунты",
                        ", ".join(f"{a.name} [{a.type_label}]" for a in other)))
    rows.append(row("Пользователи",
                    f"{len(summary.users)}" + (f", лишние: {', '.join(extra_users)}"
                                               if extra_users else ", только владелец"),
                    WARN if extra_users else OK))
    rows.append(row("Домашний экран", summary.home_now,
                    OK if summary.home_now == core.MDM_PACKAGE else WARN))
    rows.append(row("Браузер", summary.browser_now,
                    OK if summary.browser_now == core.DEFAULT_BROWSER else WARN))

    stores_value = core.stores_card_value(summary)
    if stores_value:
        working = [st for st in summary.stores if st.state == "installed"]
        rows.append(row("Магазины", stores_value, WARN if working else OK))
    if summary.locale_before or summary.locale_now:
        if summary.locale_ok:
            locale_value, locale_color = summary.locale_now or summary.locale_before, OK
        elif summary.locale_state == "reboot":
            locale_value = f"{summary.locale_now} — после перезагрузки"
            locale_color = WARN
        elif summary.locale_state == "failed":
            locale_value = f"{summary.locale_before or '?'} — сменить не удалось"
            locale_color = ERR
        else:
            locale_value, locale_color = summary.locale_now or summary.locale_before, WARN
        rows.append(row("Язык системы", locale_value, locale_color))
    if summary.time_note:
        rows.append(row("Время", summary.time_note, OK if summary.time_auto else ERR))
    if summary.settings_applied:
        rows.append(row("Настройки",
                        f"заблокировано ограничений: {summary.settings_applied}", OK))

    plates = ""
    if not summary.owner_component:
        plates += plate_html("ВЛАДЕЛЕЦ УСТРОЙСТВА НЕ НАЗНАЧЕН",
                             "MDM не сможет управлять планшетом в полном объёме")
    if not google:
        plates += plate_html("НА ПЛАНШЕТЕ НЕТ GOOGLE-АККАУНТА",
                             "добавьте учётную запись перед выдачей планшета в школу")

    return f"<table style='padding:10px'>{''.join(rows)}</table>{plates}"


def plate_html(title: str, subtitle: str) -> str:
    return (
        f"<div style='background:#3a1216;border:2px solid {ERR};border-radius:10px;"
        f"margin:14px 8px 4px 8px;padding:14px;text-align:center'>"
        f"<div style='color:{ERR};font-size:11px;letter-spacing:3px'>!!! ВНИМАНИЕ !!!</div>"
        f"<div style='color:{ERR};font-size:16px;font-weight:800;padding:6px 0'>{title}</div>"
        f"<div style='color:#e8b4b8;font-size:12px'>{subtitle}</div></div>"
    )


# ─────────────────────── главное окно ───────────────────────


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"Wunder Tablet · подготовка планшетов — GUI {GUI_VERSION}")
        self.resize(1280, 820)
        self.worker: Worker | None = None
        self.stores_worker: StoresWorker | None = None
        self.summary: core.Summary | None = None
        self.settings = load_settings()

        core.C.enabled = False   # ANSI в GUI не нужен

        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(self._build_left())
        splitter.addWidget(self._build_right())
        splitter.setSizes([420, 860])
        self.setCentralWidget(splitter)

        self.bridge = Bridge()
        self.bridge.line.connect(self.append_line)
        self.bridge.ask.connect(self.on_ask)
        self.bridge.done.connect(self.on_done)
        self.bridge.progress.connect(lambda text: self.status.setText(text))
        self.bridge.stores.connect(self.on_stores)

        self.refresh_devices()

    # ── левая колонка ──

    def _build_left(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(14, 14, 7, 14)

        title = QLabel("Wunder Tablet")
        title.setObjectName("h1")
        subtitle = QLabel(f"движок launcher_cleanup v{core.VERSION} · GUI {GUI_VERSION}")
        subtitle.setObjectName("sub")
        layout.addWidget(title)
        layout.addWidget(subtitle)

        devices_box = QGroupBox("Планшеты")
        devices_layout = QVBoxLayout(devices_box)
        self.device_list = QListWidget()
        self.device_list.setMinimumHeight(150)
        devices_layout.addWidget(self.device_list)
        refresh = QPushButton("Обновить список")
        refresh.clicked.connect(self.refresh_devices)
        devices_layout.addWidget(refresh)
        layout.addWidget(devices_box)

        card_box = QGroupBox("Паспорт планшета")
        card_layout = QVBoxLayout(card_box)
        self.card = QTextEdit()
        self.card.setReadOnly(True)
        self.card.setHtml(card_html(None))
        card_layout.addWidget(self.card)
        layout.addWidget(card_box, 1)
        return panel

    # ── правая колонка ──

    def _build_right(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(7, 14, 14, 14)

        tabs = QTabWidget()
        tabs.addTab(self._build_run_tab(), "Прогон")
        tabs.addTab(self._build_stores_tab(), "Магазины")
        tabs.addTab(self._build_settings_tab(), "Настройки")
        layout.addWidget(tabs, 1)
        return panel

    def _build_run_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)

        buttons = QHBoxLayout()
        self.btn_scan = QPushButton("Сканировать")
        self.btn_scan.clicked.connect(lambda: self.start(list_only=True))
        self.btn_dry = QPushButton("Сухой прогон")
        self.btn_dry.clicked.connect(lambda: self.start(dry_run=True))
        self.btn_run = QPushButton("Выполнить зачистку")
        self.btn_run.setObjectName("primary")
        self.btn_run.clicked.connect(lambda: self.start())
        self.btn_clear = QPushButton("Очистить лог")
        self.btn_clear.clicked.connect(lambda: self.log_view.clear())
        for widget in (self.btn_scan, self.btn_dry, self.btn_run):
            buttons.addWidget(widget)
        buttons.addStretch(1)
        buttons.addWidget(self.btn_clear)
        layout.addLayout(buttons)

        self.status = QLabel("готов")
        self.status.setObjectName("sub")
        self.progress = QProgressBar()
        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        self.progress.setTextVisible(False)
        self.progress.setFixedHeight(6)
        layout.addWidget(self.status)
        layout.addWidget(self.progress)

        self.log_view = QTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setFont(QFont("monospace", 10))
        layout.addWidget(self.log_view, 1)
        return page

    def _build_stores_tab(self) -> QWidget:
        """Что за магазины стоят на планшете и что с ними сделать."""
        page = QWidget()
        layout = QVBoxLayout(page)

        hint = QLabel("Магазины — это второй способ поставить игру мимо MDM. "
                      "Отметьте лишние и выключите: выключается любой магазин, "
                      "включая Google Play, пакет остаётся на месте и "
                      "возвращается кнопкой «Включить обратно». Столбец "
                      "состояния показывает, выключен он сейчас или нет.")
        hint.setObjectName("sub")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        buttons = QHBoxLayout()
        self.btn_stores_scan = QPushButton("Показать магазины")
        self.btn_stores_scan.clicked.connect(lambda: self.run_stores("scan"))
        self.btn_stores_off = QPushButton("Выключить отмеченные")
        self.btn_stores_off.setObjectName("primary")
        self.btn_stores_off.clicked.connect(lambda: self.run_stores("disable"))
        self.btn_stores_on = QPushButton("Включить обратно")
        self.btn_stores_on.clicked.connect(lambda: self.run_stores("enable"))
        self.btn_stores_rm = QPushButton("Снести совсем")
        self.btn_stores_rm.setObjectName("danger")
        self.btn_stores_rm.clicked.connect(lambda: self.run_stores("remove"))
        for widget in (self.btn_stores_scan, self.btn_stores_off,
                       self.btn_stores_on, self.btn_stores_rm):
            buttons.addWidget(widget)
        buttons.addStretch(1)
        layout.addLayout(buttons)

        self.stores_list = QListWidget()
        self.stores_list.setSelectionMode(QListWidget.NoSelection)
        layout.addWidget(self.stores_list, 1)

        self.stores_status = QLabel("нажмите «Показать магазины»")
        self.stores_status.setObjectName("sub")
        layout.addWidget(self.stores_status)
        return page

    def run_stores(self, action: str) -> None:
        serial = self.selected_serial()
        if not serial:
            QMessageBox.warning(self, "Нет планшета", "Сначала выберите устройство в списке.")
            return
        if self.stores_worker and self.stores_worker.isRunning():
            return

        packages: list[str] = []
        if action != "scan":
            packages = self.checked_stores()
            if not packages:
                QMessageBox.information(self, "Ничего не отмечено",
                                        "Отметьте галочками магазины в списке.")
                return
            verb = {"disable": "Выключить", "enable": "Включить обратно",
                    "remove": "Снести совсем"}[action]
            answer = QMessageBox.question(
                self, "Подтверждение",
                f"{verb} на {serial}:\n\n" + "\n".join(packages))
            if answer != QMessageBox.Yes:
                return

            # Снос необратим, поэтому защищённые пакеты (Google Play) требуют
            # отдельного согласия — так же, как ключ --include-play в CLI.
            if action == "remove":
                protected = [pkg for pkg in packages if core.is_protected(pkg, set())]
                if protected:
                    second = QMessageBox.warning(
                        self, "Снос Google Play необратим",
                        "Вы собираетесь НАВСЕГДА удалить:\n\n"
                        + "\n".join(protected)
                        + "\n\nЭто ломает обновления GMS и WebView и вернётся "
                          "только сбросом планшета к заводским настройкам.\n"
                          "Чтобы просто закрыть магазин, пользуйтесь кнопкой "
                          "«Выключить отмеченные» — она обратима.\n\n"
                          "Всё равно удалить?",
                        QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
                    if second != QMessageBox.Yes:
                        return

        deep = not self.flag_boxes["stores_catalog_only"].isChecked()
        self.set_stores_busy(True)
        self.stores_status.setText("работаю…")
        self.stores_worker = StoresWorker(serial, self.adb_path(), action,
                                          packages, self.bridge, deep)
        self.stores_worker.start()

    def checked_stores(self) -> list[str]:
        packages = []
        for index in range(self.stores_list.count()):
            item = self.stores_list.item(index)
            if item.checkState() == Qt.Checked and item.data(Qt.UserRole):
                packages.append(item.data(Qt.UserRole))
        return packages

    def on_stores(self, stores: object) -> None:
        self.set_stores_busy(False)
        self.stores_list.clear()
        if not isinstance(stores, list) or not stores:
            self.stores_status.setText("магазинов не найдено")
            return
        working = 0
        for store in stores:
            state = store.state_label
            if store.state == "installed":
                working += 1
            flags = []
            if store.protected:
                flags.append("Play Store — выключается, но не сносится")
            if not store.known:
                flags.append(f"не в справочнике, найден по {store.source}")
            note = ("  ·  " + "  ·  ".join(flags)) if flags else ""
            label = (f"{store.label}\n{store.package}  ·  {state}"
                     f"  ·  версия {store.version or '?'}"
                     f"  ·  уведомления {store.notifications}{note}")
            item = QListWidgetItem(label)
            item.setData(Qt.UserRole, store.package)
            item.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled)
            item.setCheckState(Qt.Unchecked)
            if store.state == "disabled":
                item.setForeground(Qt.gray)
            elif store.protected:
                item.setForeground(Qt.cyan)
            self.stores_list.addItem(item)
        self.stores_status.setText(
            f"всего {len(stores)} · работают {working} · "
            f"отключены {len(stores) - working}")

    def set_stores_busy(self, busy: bool) -> None:
        for button in (self.btn_stores_scan, self.btn_stores_off,
                       self.btn_stores_on, self.btn_stores_rm):
            button.setEnabled(not busy)

    def _build_settings_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        saved = self.settings

        stages_box = QGroupBox("Этапы")
        stages_layout = QVBoxLayout(stages_box)
        self.stage_boxes = {}
        for key, label in (
            ("launchers", "Лаунчеры — снести заводские, оставить MDM"),
            ("browsers", "Браузеры — снести всё кроме Chrome"),
            ("assistants", "Ассистенты — Google Assistant, Bixby, ZUI AI…"),
            ("pcmode", "Режим ПК — десктопные оболочки и переключатели"),
            ("thirdparty", "Сторонние — игры и всё, что поставили дети"),
            ("stores", "Магазины — выключить все, включая Google Play"),
            ("extras", "Прочее — Google Meet, Google Chat"),
            ("mac", "MAC-адрес — запись и отключение рандомизации"),
            ("system", "Система — русский язык и автоматическое время"),
            ("accounts", "Аккаунты — аудит учёток и профилей"),
        ):
            box = QCheckBox(label)
            box.setChecked(saved.get("stages", {}).get(key, True))
            self.stage_boxes[key] = box
            stages_layout.addWidget(box)
        layout.addWidget(stages_box)

        options_box = QGroupBox("Поведение")
        form = QFormLayout(options_box)
        self.mode_combo = QComboBox()
        self.mode_combo.addItems(["auto — удалить, иначе отключить",
                                  "uninstall — только удалять",
                                  "disable — только отключать"])
        self.mode_combo.setCurrentIndex(saved.get("mode_index", 0))
        form.addRow("Способ", self.mode_combo)

        self.browser_edit = QLineEdit(saved.get("browser", core.DEFAULT_BROWSER))
        form.addRow("Оставить браузер", self.browser_edit)

        self.locale_edit = QLineEdit(saved.get("locale", core.SYSTEM_LOCALE))
        form.addRow("Язык системы", self.locale_edit)

        self.keep_edit = QLineEdit(saved.get("keep", ""))
        self.keep_edit.setPlaceholderText("пакеты через запятую — никогда не трогать")
        form.addRow("Белый список", self.keep_edit)

        self.adb_edit = QLineEdit(saved.get("adb", ""))
        self.adb_edit.setPlaceholderText(core.find_adb() or "adb не найден — укажите путь")
        form.addRow("Путь к adb", self.adb_edit)
        layout.addWidget(options_box)

        flags_box = QGroupBox("Ключи")
        flags_layout = QVBoxLayout(flags_box)
        self.flag_boxes = {}
        for key, label, default in (
            ("install_mdm", "Ставить MDM-агент, если его нет (hmdm.apk рядом)", True),
            ("mdm_perms", "Выдавать разрешения MDM после установки", True),
            ("force_reinstall_mdm", "Ставить APK поверх всегда, даже если сборка та же", False),
            ("mute_notifications", "Глушить уведомления неразрешённых приложений", True),
            ("mute_stores", "Глушить уведомления магазинов (Play, GetApps)", True),
            ("remove_preinstalled", "Сносить заводские приложения вендора", False),
            ("fix_mac", "Отключать рандомизацию MAC-адреса", True),
            ("remove_extra_users", "Удалять лишние профили ради device owner", False),
            ("auto", "Авторежим — не спрашивать подтверждений", False),
            ("force_unknown", "Сносить и неопознанные пакеты", False),
            ("with_freeform", "Заодно снести плавающую панель ZUI", False),
            ("lock_accounts", "Запретить гостя, новых пользователей и смену аккаунтов", False),
            ("lock_settings", "Заблокировать настройки, кроме Wi-Fi и Bluetooth", False),
            ("block_stores", "Сносить/отключать сторонние магазины приложений", False),
            ("disable_stores", "Этап «Магазины»: отключать найденные магазины", True),
            ("keep_stores", "Этап «Магазины»: только показать, ничего не выключать", False),
            ("set_locale", "Ставить русский язык системы, если стоит другой", True),
            ("auto_time", "Включать автоматические дату, время и часовой пояс", True),
            ("enable_stores", "Этап «Магазины»: включать магазины обратно", False),
            ("remove_stores", "Этап «Магазины»: сносить магазины совсем", False),
            ("keep_play", "НЕ выключать Google Play (по умолчанию выключается)", False),
            ("include_play", "Разрешить СНОСИТЬ Google Play (необратимо)", False),
            ("stores_catalog_only", "Магазины искать только по справочнику (быстрее)", False),
            ("no_set_home", "НЕ закреплять MDM как домашний экран", False),
            ("no_set_owner", "НЕ назначать MDM владельцем устройства", False),
            ("restart_mdm", "Перезапустить MDM в конце", False),
            ("ignore_missing_mdm", "Работать даже без установленного MDM (опасно)", False),
        ):
            box = QCheckBox(label)
            box.setChecked(saved.get("flags", {}).get(key, default))
            self.flag_boxes[key] = box
            flags_layout.addWidget(box)
        layout.addWidget(flags_box)

        save = QPushButton("Сохранить настройки")
        save.clicked.connect(self.save_settings)
        layout.addWidget(save)
        layout.addStretch(1)
        return page

    # ── данные ──

    def refresh_devices(self) -> None:
        self.device_list.clear()
        adb = self.adb_path()
        try:
            devices = core.list_devices(adb)
        except Exception as exc:                      # noqa: BLE001
            self.append_line("err", f"не удалось опросить adb: {exc}")
            return
        if not core.find_adb(self.adb_edit.text().strip() if hasattr(self, "adb_edit") else ""):
            self.append_line("err", "adb не найден")
            self.append_line("info", core.adb_hint())
        if not devices:
            item = QListWidgetItem("устройств не найдено")
            item.setFlags(Qt.NoItemFlags)
            self.device_list.addItem(item)
            return
        for serial, state in devices:
            label = f"{serial}"
            if state == "device":
                adb_obj = core.Adb(binary=adb, serial=serial)
                try:
                    label = f"{serial}\n{adb_obj.describe()}"
                except Exception:                     # noqa: BLE001
                    pass
            else:
                label = f"{serial}\nсостояние: {state}"
            item = QListWidgetItem(label)
            item.setData(Qt.UserRole, serial)
            if state != "device":
                item.setForeground(Qt.gray)
            self.device_list.addItem(item)
        self.device_list.setCurrentRow(0)

    def adb_path(self) -> str:
        raw = self.adb_edit.text().strip() if hasattr(self, "adb_edit") else ""
        return core.find_adb(raw) or raw

    def selected_serial(self) -> str | None:
        item = self.device_list.currentItem()
        return item.data(Qt.UserRole) if item else None

    def build_args(self, list_only: bool, dry_run: bool) -> argparse.Namespace:
        stages = [key for key, box in self.stage_boxes.items() if box.isChecked()]
        only = ("all" if len(stages) == len(self.stage_boxes)
                else (stages[0] if len(stages) == 1 else "all"))
        mode = ["auto", "uninstall", "disable"][self.mode_combo.currentIndex()]
        keep = [part.strip() for part in self.keep_edit.text().split(",") if part.strip()]
        log_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
        os.makedirs(log_dir, exist_ok=True)
        import datetime
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")

        args = argparse.Namespace(
            adb=self.adb_path(), device=None, all_devices=False, only=only,
            mode=mode, browser=self.browser_edit.text().strip() or core.DEFAULT_BROWSER,
            keep=keep, list_only=list_only, dry_run=dry_run, apk="",
            allowed_file=os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                      "allowed_apps.txt"),
            inventory="", ask_student=False, auto_student_skip=True,
            list_stores=False,
            reinstall_mdm=("force" if self.flag_boxes["force_reinstall_mdm"].isChecked()
                           else "auto"),
            locale=self.locale_edit.text().strip() or core.SYSTEM_LOCALE,
            student="", student_class="", on_duplicate="update",
            log_path=os.path.join(log_dir, f"gui_{stamp}.log"),
            **{key: box.isChecked() for key, box in self.flag_boxes.items()},
        )
        # чекбоксы этапов, не покрытые --only, отключаем через белый список этапов
        args.selected_stages = stages
        return args

    # ── прогон ──

    def start(self, list_only: bool = False, dry_run: bool = False) -> None:
        serial = self.selected_serial()
        if not serial:
            QMessageBox.warning(self, "Нет планшета", "Сначала выберите устройство в списке.")
            return
        if self.worker and self.worker.isRunning():
            return

        args = self.build_args(list_only, dry_run)
        stages = args.selected_stages
        if not stages:
            QMessageBox.warning(self, "Нет этапов", "Отметьте хотя бы один этап в настройках.")
            return

        if not list_only and not dry_run and not args.auto:
            answer = QMessageBox.question(
                self, "Подтверждение",
                f"Запустить зачистку на {serial}?\n\nЭтапы: {', '.join(stages)}\n"
                f"Способ: {args.mode}",
            )
            if answer != QMessageBox.Yes:
                return

        self.set_busy(True)
        mode_label = "сканирование" if list_only else ("сухой прогон" if dry_run else "зачистка")
        self.append_line("banner", f"\n═══ {mode_label}: {serial} ═══")

        # Этапы гоняем по очереди: движок принимает ровно один --only за проход.
        # «all» — только когда отмечены действительно все этапы: при жёстком
        # пороге снятые галочки молча возвращались в прогон.
        self.queue = ["all"] if len(stages) == len(self.stage_boxes) else list(stages)
        self.queue_args = args
        self.queue_serial = serial
        self.summary = None
        self.run_next()

    def run_next(self) -> None:
        if not self.queue:
            self.set_busy(False)
            self.status.setText("готово")
            return
        stage = self.queue.pop(0)
        args = argparse.Namespace(**vars(self.queue_args))
        args.only = stage
        self.worker = Worker(self.queue_serial, args, self.bridge)
        self.worker.start()

    def set_busy(self, busy: bool) -> None:
        for button in (self.btn_scan, self.btn_dry, self.btn_run):
            button.setEnabled(not busy)
        self.progress.setRange(0, 0 if busy else 1)

    # ── сигналы ──

    def append_line(self, level: str, text: str) -> None:
        color = LEVEL_COLORS.get(level, FG)
        weight = "600" if level in ("step", "banner", "err") else "400"
        safe = (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                    .replace("\n", "<br>").replace(" ", "&nbsp;"))
        self.log_view.append(
            f"<span style='color:{color};font-weight:{weight}'>{safe}</span>"
        )
        self.log_view.moveCursor(QTextCursor.End)

    def on_ask(self, question: str, default: bool) -> None:
        answer = QMessageBox.question(self, "Подтверждение", question)
        if self.worker:
            self.worker.answer(answer == QMessageBox.Yes)

    def on_done(self, summary: object) -> None:
        if isinstance(summary, core.Summary):
            # этапы идут по очереди — в карточке должен собраться весь прогон
            self.summary = core.merge_summaries(self.summary, summary)
            self.card.setHtml(card_html(self.summary))
        self.run_next()

    # ── настройки ──

    def save_settings(self) -> None:
        data = {
            "stages": {key: box.isChecked() for key, box in self.stage_boxes.items()},
            "flags": {key: box.isChecked() for key, box in self.flag_boxes.items()},
            "mode_index": self.mode_combo.currentIndex(),
            "browser": self.browser_edit.text().strip(),
            "locale": self.locale_edit.text().strip(),
            "keep": self.keep_edit.text().strip(),
            "adb": self.adb_edit.text().strip(),
        }
        os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
        with open(CONFIG_PATH, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
        self.status.setText(f"настройки сохранены: {CONFIG_PATH}")


def load_settings() -> dict:
    try:
        with open(CONFIG_PATH, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return {}


def main() -> int:
    app = QApplication(sys.argv)
    app.setStyleSheet(QSS)
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
