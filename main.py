import sys
import platform
import time
import json
import sqlite3
import datetime
import ctypes
from ctypes import wintypes
import logging
from logging.handlers import TimedRotatingFileHandler
import os
import shutil 
from pathlib import Path
import threading
import urllib.request
import ssl
import csv # New: For data export


# Third-party imports
import psutil

# PyQt Imports
from PyQt6.QtCore import Qt, QTimer, QRectF, QStandardPaths, QObject, pyqtSignal, QThread
from PyQt6.QtWidgets import (
    QApplication, QLabel, QWidget, QPushButton, QLineEdit,
    QVBoxLayout, QDialog, QScrollArea, QComboBox, QRadioButton, QGroupBox,
    QGridLayout, QHBoxLayout, QSystemTrayIcon, QMenu, QMessageBox, QColorDialog
)
from PyQt6.QtGui import (
    QFont, QColor, QPainter, QPen, QBrush, QDoubleValidator, QIntValidator,
    QIcon, QAction, QPixmap, QLinearGradient
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ICON_PATH = os.path.join(BASE_DIR, "icon_output_fixed.ico")

# ---------------- CONFIG & PATHS ----------------
APP_NAME = "DataUsageMonitor"

# Determine a secure, writable user data directory
DATA_DIR = Path(QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppLocalDataLocation))
if not DATA_DIR.name.endswith(APP_NAME):
    DATA_DIR = DATA_DIR / APP_NAME

DATA_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR = DATA_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)

SETTINGS_FILE = DATA_DIR / "settings.json"
HISTORY_DB_FILE = DATA_DIR / "usage_history.db"

# --- DEFAULT STYLING CONSTANTS ---
BACKGROUND_COLOR = "rgba(22, 25, 30, 240)"
DARK_DIALOG_BG = "rgb(30, 35, 42)"
TEXT_COLOR = "#f0f0f0"
SECONDARY_TEXT_COLOR = "#99a2b5"
FONT_FAMILY = "Segoe UI"
BORDER_RADIUS = "18px"
SHUTDOWN_COLOR = QColor("#DC143C")

RING_THICKNESS = 14

# Define Windows power management constants if on Windows
if platform.system() == "Windows":
    WM_POWERBROADCAST = 0x0218
    PBT_APMSUSPEND = 0x0004
    PBT_APMRESUMESUSPEND = 0x0007
    PBT_APMRESUMEAUTOMATIC = 0x0018
    GWL_WNDPROC = -4
    WNDPROC_TYPE = ctypes.WINFUNCTYPE(ctypes.c_void_p, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)

# Set App User Model ID early for Windows Taskbar/Tray compatibility
if platform.system() == "Windows":
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(f"MyCompany.{APP_NAME}.v1")
    except Exception:
        pass

# ---------------- LOGGING SYSTEM ----------------
APP_LOG = []
MAX_LOG_ENTRIES = 1000 

def setup_logging():
    logger = logging.getLogger(APP_NAME)
    logger.setLevel(logging.DEBUG)
    
    if logger.hasHandlers():
        logger.handlers.clear()

    log_file = LOG_DIR / "app.log"
    handler = TimedRotatingFileHandler(
        log_file, when="midnight", backupCount=7, encoding="utf-8"
    )
    formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    return logger

logger = setup_logging()

def LOG(msg, level="info"):
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    entry = f"[{timestamp}] {msg}"
    
    if level == "debug":
        logger.debug(msg)
    elif level == "warning":
        logger.warning(msg)
    elif level == "error":
        logger.error(msg)
    else:
        logger.info(msg)
        
    APP_LOG.append(entry)
    if len(APP_LOG) > MAX_LOG_ENTRIES:
        APP_LOG.pop(0)

def show_error(msg):
    try:
        LOG(f"UI Error Popup: {msg}", level='error')
        box = QMessageBox()
        box.setIcon(QMessageBox.Icon.Critical)
        box.setWindowTitle("Error")
        box.setText("An error occurred:")
        box.setInformativeText(str(msg))
        box.setStandardButtons(QMessageBox.StandardButton.Ok)
        box.exec()
    except Exception:
        pass

def global_excepthook(exctype, value, tb):
    try:
        LOG(f"UNHANDLED EXCEPTION: {value}", level='error')
    except Exception:
        pass

sys.excepthook = global_excepthook


# ---------------- OS UTILITIES ----------------
def notify(title, message):
    try:
        if 'QSystemTrayIcon' in globals() and QSystemTrayIcon.isSystemTrayAvailable():
            try:
                # Use a dummy tray icon to send the message
                temp_tray = QSystemTrayIcon()
                temp_tray.show()
                temp_tray.showMessage(title, message)
            except Exception:
                LOG(f"NOTIFY: {title} - {message}", level='info')
        else:
            LOG(f"NOTIFY: {title} - {message}", level='info')
    except Exception as e:
        LOG(f"Notify error: {e}", level='warning')


# ---------------- DB HELPERS ----------------
MAX_RETRIES = 3
RETRY_DELAY = 0.5
BACKUP_DIR = DATA_DIR / 'backups'
BACKUP_DIR.mkdir(parents=True, exist_ok=True)

def execute_db_command(command, params=(), fetch_one=False):
    for attempt in range(MAX_RETRIES):
        try:
            with sqlite3.connect(HISTORY_DB_FILE) as conn:
                conn.execute("PRAGMA journal_mode=WAL;")
                conn.execute("PRAGMA synchronous=NORMAL;")
                
                cur = conn.cursor()
                cur.execute(command, params)
                conn.commit()
                if fetch_one:
                    return cur.fetchone()
                return cur.fetchall() if command.strip().upper().startswith("SELECT") else None
        except sqlite3.Error as e:
            if attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_DELAY)
            else:
                LOG(f"DB ERROR: {e}", level='error')
                return None

def setup_db():
    execute_db_command("""
        CREATE TABLE IF NOT EXISTS daily_usage (
            date TEXT PRIMARY KEY,
            bytes_used INTEGER
        )
    """)
    # New table to store per-SSID daily usage
    execute_db_command("""
        CREATE TABLE IF NOT EXISTS wifi_usage (
            date TEXT,
            ssid TEXT,
            bytes_used INTEGER,
            PRIMARY KEY (date, ssid)
        )
    """)

def backup_database():
    try:
        ts = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
        dst = BACKUP_DIR / f"usage_history_{ts}.db"
        shutil.copy2(HISTORY_DB_FILE, dst)
        files = sorted(BACKUP_DIR.glob('usage_history_*.db'), key=os.path.getmtime, reverse=True)
        for f in files[30:]:
            try:
                f.unlink()
            except Exception:
                pass
        LOG('Backup created: ' + str(dst))
    except Exception as e:
        LOG(f'Backup failed: {e}', level='warning')

def schedule_backup_timers(parent):
    try:
        now = datetime.datetime.now()
        first_run = (now + datetime.timedelta(days=1)).replace(hour=0, minute=5, second=0, microsecond=0)
        delta = (first_run - now).total_seconds()
        QTimer.singleShot(int(delta * 1000), lambda: (backup_database(), schedule_backup_interval(parent)))
    except Exception as e:
        LOG(f'Failed to schedule backup: {e}', level='warning')

def schedule_backup_interval(parent):
    try:
        timer = QTimer(parent)
        timer.setInterval(24 * 3600 * 1000)
        timer.timeout.connect(backup_database)
        timer.start()
    except Exception as e:
        LOG(f'Failed to start backup timer: {e}', level='warning')

def repair_database():
    rows = execute_db_command("SELECT date, bytes_used FROM daily_usage") or []
    for date, value in rows:
        try:
            int(value)
        except (ValueError, TypeError):
            LOG(f"DB Repair: Resetting invalid entry for {date}", level="warning")
            execute_db_command(
                "UPDATE daily_usage SET bytes_used = 0 WHERE date = ?",
                (date,)
            )
    execute_db_command("DELETE FROM daily_usage WHERE bytes_used IS NULL")

def log_daily_usage(date_str, bytes_used):
    try:
        bytes_used = int(bytes_used)
        execute_db_command(
            "REPLACE INTO daily_usage (date, bytes_used) VALUES (?, ?)",
            (date_str, bytes_used)
        )
    except Exception as e:
        LOG(f"DB ERROR in log_daily_usage: {e}", level='error')


# ---------------- NEW: WiFi SSID Helpers & Per-SSID DB ----------------
def get_wifi_ssid():
    """Attempts to read the connected WiFi SSID on Windows via netsh.
    Returns a nice SSID string or 'Unknown' if detection fails or not on Windows."""
    try:
        if platform.system() != "Windows":
            return "Unknown Network"
        # Use netsh to get the SSID of the connected WiFi
        out = os.popen("netsh wlan show interfaces").read()
        for line in out.splitlines():
            # match lines like: "    SSID                   : KZ_0099"
            if "SSID" in line and "BSSID" not in line:
                parts = line.split(":", 1)
                if len(parts) > 1:
                    ssid = parts[1].strip()
                    if ssid:
                        return ssid
        return "Unknown Network"
    except Exception as e:
        LOG(f"get_wifi_ssid error: {e}", level='warning')
        return "Unknown Network"


def log_wifi_usage(date_str, ssid, bytes_used):
    """Store per-SSID daily usage."""
    try:
        bytes_used = int(bytes_used)
        execute_db_command(
            "REPLACE INTO wifi_usage (date, ssid, bytes_used) VALUES (?, ?, ?)",
            (date_str, ssid, bytes_used)
        )
    except Exception as e:
        LOG(f"DB ERROR in log_wifi_usage: {e}", level='error')


def get_today_wifi_usage(date_str, ssid):
    row = execute_db_command("SELECT bytes_used FROM wifi_usage WHERE date = ? AND ssid = ?", (date_str, ssid), fetch_one=True)
    try:
        return int(row[0]) if row and row[0] is not None else 0
    except Exception:
        return 0
def get_today_usage(date_str):
    row = execute_db_command("SELECT bytes_used FROM daily_usage WHERE date = ?", (date_str,), fetch_one=True)
    try:
        return int(row[0]) if row and row[0] is not None else 0
    except Exception:
        return 0

def get_last_30_days_usage():
    return execute_db_command(
        "SELECT date, bytes_used FROM daily_usage ORDER BY date DESC LIMIT 30"
    ) or []

def get_total_usage():
    row = execute_db_command("SELECT SUM(bytes_used) FROM daily_usage", fetch_one=True)
    try:
        return int(row[0]) if row and row[0] is not None else 0
    except:
        return 0


# ---------------- SETTINGS ----------------
DEFAULT_SETTINGS = {
    "daily_limit_gb": 2.0,
    "monitoring_interface": "Total",
    "limit_type": "total", # 'total' or 'download'
    "notif_thresholds": [80, 95], # Percentages for warning, critical
    "accent_start_hex": "#00e0ff",
    "accent_end_hex": "#0099ff",
}

def get_interface_list():
    """Returns a list of active network interfaces plus 'Total'."""
    interfaces = ["Total"]
    try:
        stats = psutil.net_io_counters(pernic=True)
        interfaces.extend(sorted(list(stats.keys())))
    except Exception as e:
        LOG(f"Error getting interfaces: {e}", level="warning")
    return interfaces

def load_settings():
    settings = DEFAULT_SETTINGS.copy()
    try:
        if SETTINGS_FILE.exists():
            with open(SETTINGS_FILE, "r") as f:
                d = json.load(f)
                settings.update(d)
                # Validation and sanitization
                settings["daily_limit_gb"] = max(0.01, float(settings.get("daily_limit_gb", 2.0)))
                settings["limit_type"] = settings.get("limit_type", "total")
                settings["notif_thresholds"] = [max(0, min(100, float(t))) for t in settings.get("notif_thresholds", [80, 95])]
                settings["monitoring_interface"] = settings.get("monitoring_interface", "Total")
                settings["accent_start_hex"] = settings.get("accent_start_hex", DEFAULT_SETTINGS["accent_start_hex"])
                settings["accent_end_hex"] = settings.get("accent_end_hex", DEFAULT_SETTINGS["accent_end_hex"])
        return settings
    except Exception as e:
        LOG(f"Error loading settings: {e}", level="warning")
        return DEFAULT_SETTINGS

def save_settings(s):
    try:
        with open(SETTINGS_FILE, "w") as f:
            json.dump(s, f, indent=4)
        LOG(f"Settings saved: {s}")
    except Exception as e:
        LOG(f"ERROR [save_settings]: {e}", level='error')


# ---------------- UI DIALOGS ----------------
class ConsoleWindow(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("App Console")
        self.resize(700, 500)
        self.setStyleSheet(f"""
            QDialog {{ background-color: {DARK_DIALOG_BG}; color: {TEXT_COLOR}; border-radius: 12px; }}
            QLabel {{ color: {SECONDARY_TEXT_COLOR}; padding: 5px; }}
            QPushButton {{
                background-color: #00e0ff40;
                border: 1px solid #0099ff;
                border-radius: 8px;
                padding: 8px 16px;
                color: {TEXT_COLOR};
                font-weight: 600;
            }}
            QPushButton:hover {{ background-color: #00e0ff60; }}
            QScrollArea {{ border: none; }}
        """)
        layout = QVBoxLayout(self)

        self.scroll = QScrollArea(self)
        self.scroll.setWidgetResizable(True)
        self.inner_label = QLabel()
        self.inner_label.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self.inner_label.setWordWrap(True)
        self.inner_label.setStyleSheet(f"color:{SECONDARY_TEXT_COLOR}; font-size:12px; padding:10px; background-color: #101217;")
        self.inner_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)

        container_widget = QWidget()
        container_layout = QVBoxLayout(container_widget)
        container_layout.setContentsMargins(0,0,0,0)
        container_layout.addWidget(self.inner_label)
        self.scroll.setWidget(container_widget)
        layout.addWidget(self.scroll)

        btn_row = QHBoxLayout()
        refresh_btn = QPushButton("Refresh")
        refresh_btn.clicked.connect(self.refresh)
        clear_btn = QPushButton("Clear")
        clear_btn.clicked.connect(self.clear_log)
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.close)
        btn_row.addWidget(refresh_btn)
        btn_row.addWidget(clear_btn)
        btn_row.addWidget(close_btn)
        layout.addLayout(btn_row)

        self._log_timer = QTimer(self)
        self._log_timer.timeout.connect(self.refresh)
        self._log_timer.start(1000)
        self.refresh()

    def refresh(self):
        log_path = LOG_DIR / 'app.log'
        data = ''
        if log_path.exists():
            try:
                size = log_path.stat().st_size
                read_size = min(size, 50000) 
                with log_path.open('rb') as fh:
                    fh.seek(size - read_size)
                    data = fh.read().decode('utf-8', errors='ignore').strip()
            except Exception as e:
                data = f'[Error reading log file: {e}]'
        else:
            data = '\n'.join(APP_LOG)
            
        self.inner_label.setText(data)

    def clear_log(self):
        APP_LOG.clear()
        log_path = LOG_DIR / 'app.log'
        try:
            if log_path.exists():
                with open(log_path, 'w'): pass # Truncate file
        except Exception:
            pass
        self.refresh()


class SettingsWindow(QDialog):
    def __init__(self, parent):
        super().__init__(parent)
        self.parent = parent
        self.setWindowTitle("App Settings & Configuration")
        self.resize(450, 600)
        
        # Load current settings and colors dynamically
        self.settings = parent.settings
        ACCENT_START = self.settings['accent_start_hex']
        ACCENT_END = self.settings['accent_end_hex']
        ACCENT_HOVER_BG = f"{ACCENT_START}40" # Calculated from start color

        self.setStyleSheet(f"""
            QDialog {{ background-color: {DARK_DIALOG_BG}; color: {TEXT_COLOR}; border-radius: 12px; }}
            QLabel, QLineEdit, QComboBox, QRadioButton, QGroupBox {{ 
                color: {TEXT_COLOR}; font-family: {FONT_FAMILY}; 
            }}
            QLineEdit {{
                background-color: #1a1e24;
                border: 1px solid {ACCENT_END}80;
                border-radius: 8px;
                padding: 8px;
                font-size: 14px;
                color: {TEXT_COLOR};
            }}
            QGroupBox {{ margin-top: 10px; padding-top: 10px; border: 1px solid #333; border-radius: 8px; }}
            QGroupBox::title {{ subcontrol-origin: margin; subcontrol-position: top center; padding: 0 10px; }}
            QComboBox {{
                background-color: #1a1e24;
                border: 1px solid {ACCENT_END}80;
                border-radius: 8px;
                padding: 8px;
                color: {TEXT_COLOR};
            }}
            QPushButton {{
                border-radius: 10px;
                padding: 12px;
                font-weight: bold;
                margin-top: 8px;
                border: none;
            }}
            QPushButton#SaveButton {{
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 {ACCENT_START}, stop:1 {ACCENT_END});
                color: {DARK_DIALOG_BG};
            }}
            QPushButton#SaveButton:hover {{
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #40ffff, stop:1 #40aaff);
            }}
            QPushButton#ShutdownButton {{
                background-color: {SHUTDOWN_COLOR.name()};
                color: white;
            }}
            QPushButton#ShutdownButton:hover {{
                background-color: #e04040;
            }}
            #ConsoleButton, #RestoreButton, #HexButton {{
                background-color: {ACCENT_START}30;
                color: {TEXT_COLOR};
                border: 1px solid {ACCENT_END};
            }}
            #ConsoleButton:hover, #RestoreButton:hover, #HexButton:hover {{
                background-color: {ACCENT_HOVER_BG};
            }}
        """)
        
        main_layout = QVBoxLayout(self)
        
        # --- Group 1: Core Usage Settings ---
        usage_group = QGroupBox("Monitoring and Limits")
        usage_layout = QGridLayout(usage_group)
        
        # 1. Daily Limit
        usage_layout.addWidget(QLabel("Daily Limit (GB):"), 0, 0)
        self.limit_input = QLineEdit(self)
        self.limit_input.setText(f"{self.settings['daily_limit_gb']:.2f}")
        self.limit_input.setValidator(QDoubleValidator(0.01, 1000.0, 2))
        usage_layout.addWidget(self.limit_input, 0, 1)

        # 2. Interface Selection
        usage_layout.addWidget(QLabel("Interface to Monitor:"), 1, 0)
        self.interface_combo = QComboBox(self)
        self.interface_combo.addItems(get_interface_list())
        if self.settings['monitoring_interface'] in get_interface_list():
            self.interface_combo.setCurrentText(self.settings['monitoring_interface'])
        usage_layout.addWidget(self.interface_combo, 1, 1)
        
        # 3. Limit Type
        usage_layout.addWidget(QLabel("Limit Tracking Type:"), 2, 0)
        type_h_layout = QHBoxLayout()
        self.radio_total = QRadioButton("Total (DL+UL)")
        self.radio_download = QRadioButton("Download Only")
        
        if self.settings['limit_type'] == 'total':
            self.radio_total.setChecked(True)
        else:
            self.radio_download.setChecked(True)
            
        type_h_layout.addWidget(self.radio_total)
        type_h_layout.addWidget(self.radio_download)
        type_h_layout.addStretch(1)
        usage_layout.addLayout(type_h_layout, 2, 1)
        
        main_layout.addWidget(usage_group)
        
        # --- Group 2: Notification Thresholds ---
        notif_group = QGroupBox("Notification Thresholds (Percentage)")
        notif_layout = QGridLayout(notif_group)
        
        self.threshold_inputs = []
        # We handle up to 3 thresholds for better control, but only store the first two defaults
        # The UI is built for the existing settings structure
        current_thresholds = self.settings.get('notif_thresholds', [80, 95])
        
        # Ensure we have at least 2 input fields for the 2 default thresholds
        for i, default_pct in enumerate(current_thresholds):
            label = QLabel(f"Warning {i+1} (%):")
            input_field = QLineEdit(self)
            input_field.setText(str(int(default_pct)))
            input_field.setValidator(QIntValidator(1, 99))
            self.threshold_inputs.append(input_field)
            notif_layout.addWidget(label, i, 0)
            notif_layout.addWidget(input_field, i, 1)

        main_layout.addWidget(notif_group)
        
        # --- Group 3: Customization ---
        color_group = QGroupBox("Customization")
        color_layout = QGridLayout(color_group)
        
        # -------- COLOR PICKER BUTTONS --------
        def make_color_btn(current_hex):
            btn = QPushButton()
            btn.setFixedSize(40, 20)
            btn.setStyleSheet(f"background-color:{current_hex}; border:1px solid #555; border-radius:4px;")
            return btn

        color_layout.addWidget(QLabel("Accent Start Color:"), 0, 0)

        self.color_start_btn = make_color_btn(self.settings['accent_start_hex'])
        self.color_start_btn.clicked.connect(lambda: self.pick_color('start'))
        color_layout.addWidget(self.color_start_btn, 0, 1)

        color_layout.addWidget(QLabel("Accent End Color:"), 1, 0)

        self.color_end_btn = make_color_btn(self.settings['accent_end_hex'])
        self.color_end_btn.clicked.connect(lambda: self.pick_color('end'))
        color_layout.addWidget(self.color_end_btn, 1, 1)
        
        main_layout.addWidget(color_group)

        # --- Buttons ---
        save_btn = QPushButton("Save Settings & Apply (Restart Monitor)")
        save_btn.setObjectName("SaveButton")
        save_btn.clicked.connect(self.save_all_settings)
        main_layout.addWidget(save_btn)
        
        restore_btn = QPushButton("Restore from Backup")
        restore_btn.setObjectName("RestoreButton")
        restore_btn.clicked.connect(self.restore_from_backup)
        main_layout.addWidget(restore_btn)

        console_btn = QPushButton("Open Console")
        console_btn.setObjectName("ConsoleButton")
        console_btn.clicked.connect(self.open_console)
        main_layout.addWidget(console_btn)

        shutdown_btn = QPushButton("SHUTDOWN APP")
        shutdown_btn.setObjectName("ShutdownButton")
        shutdown_btn.clicked.connect(lambda: (self.parent.save_final_usage(), QApplication.instance().quit())) 
        main_layout.addWidget(shutdown_btn)
    def pick_color(self, which):
        dlg = QColorDialog()
        dlg.setOption(QColorDialog.ColorDialogOption.ShowAlphaChannel, False)
        color = dlg.getColor()

        if not color.isValid():
            return

        hex_code = color.name()

        if which == 'start':
            self.color_start_btn.setStyleSheet(f"background-color:{hex_code}; border:1px solid #555;")
        else:
            self.color_end_btn.setStyleSheet(f"background-color:{hex_code}; border:1px solid #555;")

        # Store temporarily in settings dict so save_all_settings picks it up
        if which == 'start':
            self.settings['accent_start_hex'] = hex_code
        else:
            self.settings['accent_end_hex'] = hex_code


    def save_all_settings(self):
        try:
            # 1. Core Usage
            new_limit = float(self.limit_input.text())
            if new_limit <= 0:
                self.limit_input.setText("Limit must be > 0")
                return
            
            new_settings = self.settings.copy()
            new_settings['daily_limit_gb'] = new_limit
            new_settings['monitoring_interface'] = self.interface_combo.currentText()
            new_settings['limit_type'] = 'total' if self.radio_total.isChecked() else 'download'
            
            # 2. Notifications
            new_thresholds = []
            for input_field in self.threshold_inputs:
                try:
                    pct = int(input_field.text())
                    new_thresholds.append(max(1, min(100, pct)))
                except ValueError:
                    continue
            new_settings['notif_thresholds'] = sorted(list(set(new_thresholds))) # Unique and sorted
            
            # 3. Customization
            # Simple hex validation (starts with # and has 6 hex chars)
            start_hex = self.settings['accent_start_hex']
            end_hex = self.settings['accent_end_hex']

                        

            new_settings['accent_start_hex'] = start_hex
            new_settings['accent_end_hex'] = end_hex

            save_settings(new_settings)
            
            # Restart the parent widget to apply interface/limit type changes
            self.parent.apply_new_settings(new_settings)
            self.close()
            
        except Exception as e:
            LOG(f"Error saving settings: {e}", level='error')
            self.limit_input.setText("Invalid input")

    def restore_from_backup(self):
        backup_files = sorted(BACKUP_DIR.glob('usage_history_*.db'), key=os.path.getmtime, reverse=True)
        if not backup_files:
            QMessageBox.warning(self, "Restore Error", "No backup files found to restore.")
            return

        q = QMessageBox.question(self, "Confirm Restore", 
            f"Are you sure you want to restore the history from the latest backup ({backup_files[0].name})?\n\n"
            "This will overwrite your current usage data and restart the monitor. Current day usage will be lost.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        
        if q == QMessageBox.StandardButton.Yes:
            try:
                # Ensure the primary DB file is closed before copying
                self.parent.save_final_usage() 
                shutil.copy2(backup_files[0], HISTORY_DB_FILE)
                LOG(f"Restored DB from {backup_files[0].name}. Restarting app.")
                
                # Simple restart by exiting application (assuming environment auto-restarts)
                QApplication.instance().quit() 
            except Exception as e:
                QMessageBox.critical(self, "Restore Failed", f"Could not restore database: {e}")
                LOG(f"Restore failed: {e}", level='error')


    def open_console(self):
        dlg = ConsoleWindow(self)
        dlg.exec()


class HistoryWindow(QDialog):
    def __init__(self, parent):
        super().__init__(parent)
        self.parent = parent
        self.setWindowTitle("Data Usage History")
        self.resize(500, 550)
        
        ACCENT_START = parent.settings['accent_start_hex']
        ACCENT_END = parent.settings['accent_end_hex']
        ACCENT_HOVER_BG = f"{ACCENT_START}40" 
        
        self.setStyleSheet(f"""
            QDialog {{ background-color: {DARK_DIALOG_BG}; color: {TEXT_COLOR}; border-radius: 12px; }}
            QLabel {{ color: {TEXT_COLOR}; }}
            #Title {{ color: {ACCENT_START}; }}
            .header_label {{ color: {ACCENT_END}; font-weight: bold; font-size: 14px; padding-bottom: 5px; }}
            .data_label {{ color: {SECONDARY_TEXT_COLOR}; padding: 5px 0; }}
            QPushButton {{
                background-color: {ACCENT_START}30;
                border: 1px solid {ACCENT_END};
                border-radius: 8px;
                padding: 10px 18px;
                color: {TEXT_COLOR};
                font-weight: 600;
            }}
            QPushButton:hover {{ background-color: {ACCENT_HOVER_BG}; }}
        """)
        main = QVBoxLayout(self)
        title = QLabel("Data Usage History (Last 30 Days)")
        title.setObjectName("Title")
        title.setFont(QFont(FONT_FAMILY, 18, QFont.Weight.Bold))
        main.addWidget(title)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet("QScrollArea { border: 1px solid #303030; border-radius: 8px; }")
        main.addWidget(scroll)

        content = QWidget()
        content.setStyleSheet(f"background-color: #1a1e24; padding: 10px;")
        scroll.setWidget(content)

        self.grid = QGridLayout(content)
        self.grid.setVerticalSpacing(0)
        self.grid.setHorizontalSpacing(15)

        headers = ["Date", "Used", "Limit %"]
        for i, h in enumerate(headers):
            lbl = QLabel(h)
            lbl.setProperty("class", "header_label")
            self.grid.addWidget(lbl, 0, i)

        self.data_history = self.load()
        
        # Footer layout for total and export button
        footer_layout = QHBoxLayout()

        total_bytes = get_total_usage()
        total_gb = total_bytes / (1024**3)

        total_label = QLabel(f"Total Used So Far: {total_gb:.2f} GB")
        total_label.setFont(QFont(FONT_FAMILY, 14, QFont.Weight.Bold))
        total_label.setStyleSheet("color: #00e0ff; padding: 10px;")
        
        export_btn = QPushButton("Export to CSV")
        export_btn.clicked.connect(self.export_data)
        
        footer_layout.addWidget(total_label)
        footer_layout.addStretch(1)
        footer_layout.addWidget(export_btn)
        
        main.addLayout(footer_layout)

        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.close)
        main.addWidget(close_btn)
        
        # Auto-refresh history while open
        try:
            self._refresh_timer = QTimer(self)
            self._refresh_timer.timeout.connect(self._refresh)
            self._refresh_timer.start(1000)
        except Exception:
            pass

    def _refresh(self):
        try:
            # clear existing rows (keep header, which is row 0)
            for i in range(self.grid.rowCount()-1, 0, -1):
                for j in range(self.grid.columnCount()):
                    item = self.grid.itemAtPosition(i, j)
                    if item and item.widget():
                        item.widget().setParent(None)
            self.data_history = self.load()
        except Exception as e:
            LOG(f"History refresh error: {e}", level='warning')

    def load(self):
        data = get_last_30_days_usage()
        r = 1
        limit_gb = self.parent.daily_limit_gb
        limit_bytes = max(1, int(limit_gb * (1024**3)))
        
        for d, b in data:
            used_val, used_unit = self.parent.convert_split(b)
            used = f"{used_val} {used_unit}"
            
            pct = (b / limit_bytes) * 100

            if pct >= 100:
                color = self.parent.EXCEEDED_COLOR.name()
            elif pct >= 90: # Using 90% as a generic high-water mark for history display
                color = self.parent.WARNING_COLOR.name()
            else:
                color = SECONDARY_TEXT_COLOR

            pct_txt = f"<span style='color:{color};'>{pct:.1f}%</span>"

            date_lbl = QLabel(d)
            used_lbl = QLabel(used)
            pct_lbl = QLabel(pct_txt)

            date_lbl.setProperty("class", "data_label")
            used_lbl.setProperty("class", "data_label")

            self.grid.addWidget(date_lbl, r, 0)
            self.grid.addWidget(used_lbl, r, 1)
            self.grid.addWidget(pct_lbl, r, 2)
            r += 1

        self.grid.setRowStretch(r, 1)
        return data # Return the data loaded

    def export_data(self):
        try:
            timestamp = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
            export_path = DATA_DIR / f"usage_history_export_{timestamp}.csv"
            
            with open(export_path, 'w', newline='', encoding='utf-8') as csvfile:
                writer = csv.writer(csvfile)
                writer.writerow(['Date', 'Bytes Used', 'Limit (GB)', 'Used (%)'])
                
                limit_gb = self.parent.daily_limit_gb
                limit_bytes = max(1, int(limit_gb * (1024**3)))
                
                for date, bytes_used in self.data_history:
                    pct = (bytes_used / limit_bytes) * 100
                    writer.writerow([date, bytes_used, limit_gb, f"{pct:.1f}"])
            
            QMessageBox.information(self, "Export Successful", f"History exported to:\n{export_path}")
            LOG(f"History exported to CSV: {export_path}")
            
        except Exception as e:
            QMessageBox.critical(self, "Export Failed", f"Could not export data: {e}")
            LOG(f"CSV export failed: {e}", level='error')


# ---------------- TIME CHECK WORKER (NON-BLOCKING) ----------------

class TimeCheckWorker(QObject):
    time_discrepancy = pyqtSignal(float)

    def run(self):
        try:
            context = ssl._create_unverified_context()
            url = "https://worldtimeapi.org/api/ip"

            req = urllib.request.Request(url, headers={'User-Agent': 'DataUsageMonitorApp'})
            with urllib.request.urlopen(req, timeout=10, context=context) as response:
                data = json.loads(response.read().decode())

            server_time_str = data['datetime']
            server_dt = datetime.datetime.fromisoformat(server_time_str)

            local_dt = datetime.datetime.now(datetime.timezone.utc).astimezone()

            discrepancy_seconds = abs((server_dt - local_dt).total_seconds())

            if discrepancy_seconds > 300:
                self.time_discrepancy.emit(discrepancy_seconds)
                LOG(f"TIME WARNING: System clock discrepancy of {discrepancy_seconds:.1f} seconds detected.")
            else:
                LOG("Time check successful: System clock is synchronized.")

        except Exception as e:
            LOG(f"Online time check failed (network or API issue): {e}", level='warning')

class DataWidget(QWidget):
    def __init__(self):
        super().__init__()
        
        # Load and set initial settings
        self.settings = load_settings()
        self.daily_limit_gb = self.settings["daily_limit_gb"]
        self.monitoring_interface = self.settings["monitoring_interface"]
        self.limit_type = self.settings["limit_type"]
        self.notif_thresholds = self.settings["notif_thresholds"]
        
        # Dynamic Color Setup
        self.ACCENT_START = self.settings['accent_start_hex']
        self.ACCENT_END = self.settings['accent_end_hex']
        self.ACCENT_HOVER_BG = f"{self.ACCENT_START}40"
        self.NORMAL_COLOR = QColor(self.ACCENT_START)
        self.WARNING_COLOR = QColor("#FFC837") # Fixed warning color
        self.EXCEEDED_COLOR = QColor("#FF6347") # Fixed exceeded color
        
        self.displayed_pct = 0.0
        self.target_pct = 0.0
        self.smooth_factor = 0.12
        self.notification_state = 0 # Corresponds to index in self.notif_thresholds

        self.check_system_time()

        setup_db()
        repair_database()
        
        self._initialize_monitoring_state()
        self._setup_ui()
        self._update_text()
        
        self.timer = QTimer()
        self.timer.timeout.connect(self.update_usage)
        self.timer.start(1000)
        try:
            schedule_backup_timers(self) 
        except Exception:
            pass

    def apply_new_settings(self, new_settings):
        """Applies new settings after saving and re-initializes state."""
        self.settings = new_settings
        self.daily_limit_gb = new_settings["daily_limit_gb"]
        self.monitoring_interface = new_settings["monitoring_interface"]
        self.limit_type = new_settings["limit_type"]
        self.notif_thresholds = new_settings["notif_thresholds"]
        
        # Update colors (and implicitly update style on next widget update)
        self.ACCENT_START = new_settings['accent_start_hex']
        self.ACCENT_END = new_settings['accent_end_hex']
        self.ACCENT_HOVER_BG = f"{self.ACCENT_START}40"
        self.NORMAL_COLOR = QColor(self.ACCENT_START)
        
        # Re-initialize monitoring state to use new interface/limit type
        self._initialize_monitoring_state()
        self.notification_state = 0 # Reset notification state
        self._update_text()
        self.update() # Repaint UI with new colors and data

    def _initialize_monitoring_state(self):
        """Sets up the initial usage counter based on settings."""
        self.today = datetime.date.today()
        self.today_str = self.today.strftime("%Y-%m-%d")
        # Detect current connected WiFi SSID and use per-SSID stored usage
        try:
            self.current_ssid = get_wifi_ssid()
        except Exception:
            self.current_ssid = 'Unknown Network'
        # Load today's usage for this SSID
        self.total_today = get_today_wifi_usage(self.today_str, self.current_ssid)

        # Get total network counters
        net_all = psutil.net_io_counters(pernic=True)
        
        if self.monitoring_interface == "Total" or self.monitoring_interface not in net_all:
            # Sum up all interfaces for Total or if interface is missing
            net_start = psutil.net_io_counters()
            current_total = net_start.bytes_recv + net_start.bytes_sent
        else:
            # Use specific interface
            net_start = net_all.get(self.monitoring_interface)
            current_total = net_start.bytes_recv + net_start.bytes_sent

        # Current total is bytes_recv + bytes_sent for the selected interface/total
        # Recalculate start_total based on the total read from the system minus today's saved usage
        self.start_total = current_total - self.total_today
        self.pre_suspend_net_total = current_total
        self.last_net = net_start
        self.last_speed_time = datetime.datetime.now()
        self.download_speed = 0.0
        self.upload_speed = 0.0
        self.is_asleep = False
        self.last_log_time = datetime.datetime.now()
        
        LOG(f"Monitoring initialized: Interface='{self.monitoring_interface}', Type='{self.limit_type}'")

    def _setup_ui(self):
        # UI flags
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint |
            Qt.WindowType.Tool |
            Qt.WindowType.WindowStaysOnBottomHint |
            Qt.WindowType.WindowDoesNotAcceptFocus
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        # Reverting to original size as requested
        self.setGeometry(50, 50, 480, 260) 

        # ---------------- background acrylic (DWM blur) ----------------
        bg = QLabel(self)
        bg.setGeometry(0, 0, 480, 260) # Adjusted size
        bg.setStyleSheet(f"""
            QLabel {{
                background: rgba(22, 25, 30, 200);
                border-radius: {BORDER_RADIUS};
                border: 1px solid rgba(255,255,255,0.03);
            }}
        """)
        # DWM hook installation (omitted for brevity, assume existing functionality)
        self._install_windows_hook() 

        # --- Main Layout ---
        # Adjusted positions for the original window size
        self.main_text = QLabel(self)
        # Moved down/adjusted height to 110 for better large font fit
        self.main_text.setGeometry(180, 15, 280, 110) 
        self.main_text.setFont(QFont(FONT_FAMILY, 15))
        self.main_text.setStyleSheet(f"color: {TEXT_COLOR}; background: transparent; padding: 5px; line-height: 1.4;")

        # --- Speed Display ---
        speed_widget = QWidget(self)
        # Moved down to sit cleanly below the main text area
        speed_widget.setGeometry(180, 125, 280, 30) 
        speed_layout = QHBoxLayout(speed_widget)
        speed_layout.setContentsMargins(0, 0, 0, 0)
        
        # Download Speed Label
        self.dl_speed_label = QLabel("DL: 0.0 KB/s")
        self.dl_speed_label.setFont(QFont(FONT_FAMILY, 12))
        self.dl_speed_label.setStyleSheet(f"color: #55aaff; font-weight: 500;")
        speed_layout.addWidget(self.dl_speed_label)

        # Upload Speed Label
        self.ul_speed_label = QLabel("UP: 0.0 KB/s")
        self.ul_speed_label.setFont(QFont(FONT_FAMILY, 12))
        self.ul_speed_label.setStyleSheet(f"color: #aaff55; font-weight: 500;")
        speed_layout.addWidget(self.ul_speed_label)
        
        speed_layout.addStretch(1)


        # settings button
        btn = QPushButton("⚙", self)
        btn.setGeometry(435, 15, 35, 35) # Adjusted X position
        btn.setStyleSheet(self._button_style(icon=True))
        btn.clicked.connect(self.open_settings)

        # bottom container (History and Reset)
        container = QWidget(self)
        # Moved up from 205 to 190 for better vertical spacing
        container.setGeometry(180, 190, 280, 45) 
        h = QHBoxLayout(container)
        h.setContentsMargins(0, 0, 0, 0)

        hist_btn = QPushButton("HISTORY", self)
        hist_btn.setStyleSheet(self._button_style(width_style="font-size: 11px;"))
        hist_btn.clicked.connect(self.open_history)

        reset_btn = QPushButton("RESET DAY", self)
        reset_btn.setStyleSheet(self._button_style(width_style="font-size: 11px;", extra_color="#A45E94", hover_color="#C07EB0"))
        reset_btn.clicked.connect(self.reset_today)

        h.addWidget(hist_btn)
        h.addWidget(reset_btn)

        # tray
        self.tray = self._make_tray()
        if QSystemTrayIcon.isSystemTrayAvailable():
            self.tray.show()

        # drag state
        self._drag_pos = None

    # ------------- time check handler -------------
    def check_system_time(self):
        QTimer.singleShot(5000, self._start_time_check_thread)

    def _start_time_check_thread(self):
        self._time_thread = QThread()
        self._time_worker = TimeCheckWorker()
        self._time_worker.moveToThread(self._time_thread)
        self._time_worker.time_discrepancy.connect(self.handle_time_discrepancy)
        self._time_thread.started.connect(self._time_worker.run)
        self._time_thread.start()

    def handle_time_discrepancy(self, discrepancy_seconds):
        minutes = int(discrepancy_seconds / 60)
        
        msg = QMessageBox(self)
        msg.setIcon(QMessageBox.Icon.Warning)
        msg.setWindowTitle("⚠️ CRITICAL TIME DISCREPANCY DETECTED")
        msg.setStyleSheet(f"""
            QMessageBox {{ background-color: {DARK_DIALOG_BG}; color: {TEXT_COLOR}; }}
            QLabel {{ color: {TEXT_COLOR}; }}
            QPushButton {{ background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 {self.ACCENT_START}, stop:1 {self.ACCENT_END}); color: {DARK_DIALOG_BG}; border-radius: 8px; padding: 10px; font-weight: bold; }}
        """)
        
        msg.setText(f"Your system clock is off by approximately **{minutes} minutes** compared to the online time source.")
        msg.setInformativeText(
            "This error will **CORRUPT** your daily usage tracking and history records.\n\n"
            "**ACTION REQUIRED:** Please go to your Operating System's Date and Time Settings and synchronize your clock immediately."
            "\n\n*Note: The application cannot automatically correct your system time due to security restrictions.*"
        )
        msg.setStandardButtons(QMessageBox.StandardButton.Ok)
        msg.exec()
        
        notify("CRITICAL ERROR: Time Discrepancy", f"System clock is off by {minutes} minutes. Usage tracking is compromised.")


    # ------------- Windows hook (for power events) -------------
    def _install_windows_hook(self):
        if platform.system() != "Windows":
            return
        try:
            hwnd = int(self.winId())
            user32 = ctypes.windll.user32

            SetWindowLongPtr = user32.SetWindowLongPtrW
            SetWindowLongPtr.restype = ctypes.c_void_p
            SetWindowLongPtr.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_void_p]

            CallWindowProc = user32.CallWindowProcW
            CallWindowProc.restype = ctypes.c_void_p
            CallWindowProc.argtypes = [ctypes.c_void_p, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]

            DefWindowProc = user32.DefWindowProcW
            DefWindowProc.restype = ctypes.c_void_p
            DefWindowProc.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]

            self._old_proc = None # Initialize to None

            def _proc(hWnd, msg, wParam, lParam):
                try:
                    # Check if PBT constants are defined and msg matches
                    if msg == WM_POWERBROADCAST:
                        if wParam == PBT_APMSUSPEND:
                            self.handle_suspend()
                        elif wParam in (PBT_APMRESUMESUSPEND, PBT_APMRESUMEAUTOMATIC):
                            self.handle_wake()
                except NameError:
                    # Constants might not be defined if environment setup failed
                    pass 
                except Exception as e:
                    LOG(f"WinProc err: {e}", level='error')

                # Call the original window procedure
                if self._old_proc:
                    try:
                        res = CallWindowProc(self._old_proc, hWnd, msg, wParam, lParam)
                    except Exception:
                        res = DefWindowProc(hWnd, msg, wParam, lParam)
                else:
                    res = DefWindowProc(hWnd, msg, wParam, lParam)

                return 0 if res is None else int(res)

            self._proc_ref = WNDPROC_TYPE(_proc)
            old = SetWindowLongPtr(hwnd, GWL_WNDPROC, self._proc_ref)
            self._old_proc = ctypes.c_void_p(old)
            LOG("Windows Hook installed")
        except Exception as e:
            LOG(f"WARN: Could not install Windows hook: {e}", level='error')

    # ------------- tray -------------
    def _make_tray(self):
        # Tray Icon generation logic (remains the same)
        pix = QPixmap(24, 24)
        pix.fill(QColor(0, 0, 0, 0))
        p = QPainter(pix)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        grad = QLinearGradient(0, 0, 24, 24)
        grad.setColorAt(0.0, QColor(self.ACCENT_START))
        grad.setColorAt(1.0, QColor(self.ACCENT_END))
        p.setBrush(QBrush(grad))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawEllipse(4, 4, 16, 16)
        p.end()

        icon = QIcon(pix)
        tray = QSystemTrayIcon(QIcon(ICON_PATH))
        tray.setToolTip("Data Usage Monitor")
        menu = QMenu()
        
        show_action = QAction("Show/Hide Monitor", self)
        show_action.triggered.connect(self.toggle_visibility) 
        menu.addAction(show_action)
        settings_action = QAction("Settings", self)
        settings_action.triggered.connect(self.open_settings)
        menu.addAction(settings_action)
        history_action = QAction("History", self)
        history_action.triggered.connect(self.open_history)
        menu.addAction(history_action)
        menu.addSeparator()
        quit_action = QAction("Exit", self)
        quit_action.triggered.connect(lambda: (self.save_final_usage(), QApplication.instance().quit()))
        menu.addAction(quit_action)
        tray.setContextMenu(menu)
            
        return tray

    # ------------- button style -------------
    def _button_style(self, width_style="font-size: 16px;", extra_color=None, icon=False, hover_color=None):
        padding = '5px' if icon else '8px'
        font_size = "22px" if icon else "14px"
        color = TEXT_COLOR

        if icon:
            base = f"qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 {self.ACCENT_START}90, stop:1 {self.ACCENT_END}90)"
            hover = f"qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #40ffff, stop:1 #40aaff)"
        elif extra_color:
            base = extra_color
            hover = hover_color if hover_color else f"#{QColor(extra_color).lighter(120).name()[1:]}"
        else:
            base = f"qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 {self.ACCENT_START}50, stop:1 {self.ACCENT_END}50)"
            hover = self.ACCENT_HOVER_BG

        return f"""
            QPushButton {{
                background: {base};
                border: 1px solid rgba(100, 100, 100, 50);
                border-radius: 10px;
                color: {color};
                font-size: {font_size};
                font-weight: 600;
                padding: {padding};
            }}
            QPushButton:hover {{
                background: {hover};
                border: 1px solid {self.ACCENT_END};
            }}
            QPushButton:pressed {{
                padding-top: {int(padding[:-2]) + 1}px;
                padding-bottom: {int(padding[:-2]) - 1}px;
            }}
        """

    # ------------- logic -------------
    def handle_suspend(self):
        try:
            self.is_asleep = True
            try:
                self.timer.stop()
            except Exception:
                pass
            log_wifi_usage(self.today_str, getattr(self, 'current_ssid', get_wifi_ssid()), self.total_today) 
            # Re-read network counters for pre-suspend baseline
            net_all = psutil.net_io_counters(pernic=True)
            if self.monitoring_interface == "Total" or self.monitoring_interface not in net_all:
                net = psutil.net_io_counters()
            else:
                net = net_all.get(self.monitoring_interface, psutil.net_io_counters())
                
            self.pre_suspend_net_total = net.bytes_recv + net.bytes_sent
            LOG(f"Suspend: Saved {self.convert(self.total_today)}")
        except Exception as e:
            LOG(f"ERROR [Suspend]: {e}", level='error')

    def handle_wake(self):
        try:
            time.sleep(1.5) 
            self.is_asleep = False
            
            self._initialize_monitoring_state() # Re-establish monitoring state
            
            try:
                self.timer.start(1000)
            except Exception:
                pass
            LOG(f"Wake: Rebuilt baseline. Today = {self.convert(self.total_today)}")
        except Exception as e:
            LOG(f"ERROR [Wake]: {e}", level='error')
            try:
                self.timer.start(1000)
            except Exception:
                pass

    def reset_today(self):
        # Confirm reset with user
        reply = QMessageBox.question(self, 'Confirm Reset', 
            "Are you sure you want to reset today's usage to 0?", 
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, 
            QMessageBox.StandardButton.No)
            
        if reply == QMessageBox.StandardButton.Yes:
            try:
                log_wifi_usage(self.today_str, getattr(self, 'current_ssid', get_wifi_ssid()), 0)
                self.total_today = 0
                
                # Reset baseline
                net_all = psutil.net_io_counters(pernic=True)
                if self.monitoring_interface == "Total" or self.monitoring_interface not in net_all:
                    net = psutil.net_io_counters()
                else:
                    net = net_all.get(self.monitoring_interface, psutil.net_io_counters())
                
                current = net.bytes_recv + net.bytes_sent
                self.start_total = current - self.total_today
                
                self._update_text()
                self.update()
                LOG("Reset: Daily usage reset.")
            except Exception as e:
                LOG(f"ERROR [Reset]: {e}", level='error')

    def get_net_stats(self):
        """Fetches stats for the selected interface/total."""
        net_all = psutil.net_io_counters(pernic=True)
        if self.monitoring_interface == "Total" or self.monitoring_interface not in net_all:
            return psutil.net_io_counters() # Global stats
        else:
            return net_all.get(self.monitoring_interface, psutil.net_io_counters())

    def update_usage(self):
        try:
            if self.is_asleep:
                return
            
            net = self.get_net_stats()
            
            # --- 1. Calculate Usage ---
            if self.limit_type == 'download':
                current_total_read_from_system = net.bytes_recv
                # Usage is based on how much the RECEIVE counter has increased since the baseline
                # The start_total for download only will be based only on bytes_recv.
                total = net.bytes_recv 
            else: # 'total'
                current_total_read_from_system = net.bytes_recv + net.bytes_sent
                total = net.bytes_recv + net.bytes_sent
                
            # total_today is the bytes used since the start of the day (or last reset)
            self.total_today = max(0, total - self.start_total)
            limit = self.daily_limit_gb * (1024 ** 3)
            self.target_pct = min(1.0, self.total_today / limit) if limit > 0 else 0.0

            # --- 2. Calculate Speed ---
            now = datetime.datetime.now()
            dt = (now - self.last_speed_time).total_seconds()
            if dt > 0:
                dl = max(0, net.bytes_recv - self.last_net.bytes_recv)
                ul = max(0, net.bytes_sent - self.last_net.bytes_sent)
                self.download_speed = dl / dt
                self.upload_speed = ul / dt
                self.last_net = net
                self.last_speed_time = now

            # --- 3. Periodic Logging ---
            if (now - self.last_log_time).total_seconds() >= 60:
                try:
                    log_wifi_usage(self.today_str, getattr(self, 'current_ssid', get_wifi_ssid()), self.total_today)
                    self.last_log_time = now
                except Exception as e:
                    LOG(f"ERROR [Periodic Log]: {e}", level='error')

            # --- 4. Animation and Notifications ---
            try:
                diff = self.target_pct - self.displayed_pct
                self.displayed_pct += diff * self.smooth_factor
            except Exception:
                pass
            
            # Notification logic using dynamic thresholds
            current_pct = self.target_pct * 100
            
            # Check if we should trigger the next notification state
            if self.notification_state < len(self.notif_thresholds):
                threshold = self.notif_thresholds[self.notification_state]
                
                if current_pct >= threshold:
                    level = "Warning" if self.notification_state == 0 else "Critical"
                    notify(f'Usage {level} ({threshold}%)', 
                           f'You have used {current_pct:.1f}% of your daily limit on {getattr(self, 'current_ssid', 'Unknown Network')}.')
                    self.notification_state += 1 # Advance to next threshold
            
            # Check for limit exceeded (which is always state X+)
            if self.notification_state == len(self.notif_thresholds) and current_pct >= 100:
                 notify('Usage Limit Exceeded', 
                       f'You have surpassed your daily limit of {self.daily_limit_gb} GB.')
                 self.notification_state += 1 # Prevents repeated notifications
            
            self._update_text()
            self.update()
        except Exception as e:
            LOG(f"ERROR [update_usage]: {e}", level='error')

    def _update_text(self):
        try:
            limit = self.daily_limit_gb * (1024 ** 3)
            used = self.total_today
            remaining = max(0, limit - used) if limit > 0 else 0
            pct = used / limit if limit > 0 else 0

            date_month = self.today.strftime("%b %d")

            if pct >= 1:
                status = f"<span style='color:{self.EXCEEDED_COLOR.name()}; font-weight: bold; font-size: 14px;'>🚨 LIMIT EXCEEDED ({date_month})</span>"
            # Check against the highest threshold (last element in the sorted list)
            elif pct * 100 >= self.notif_thresholds[-1]:
                 status = f"<span style='color:{self.WARNING_COLOR.name()}; font-weight: bold; font-size: 14px;'>⚠️ CRITICAL USAGE ({date_month})</span>"
            else:
                status = f"<span style='color:{SECONDARY_TEXT_COLOR}; font-size: 14px; font-weight: 500;'>WiFi: {getattr(self, 'current_ssid', 'Unknown Network')} ({date_month})</span>"

            used_val, used_unit = self.convert_split(used)
            limit_val, limit_unit = self.convert_split(limit)
            remaining_val, remaining_unit = self.convert_split(remaining)

            self.main_text.setText(
                f"{status}<br>"
                f"<span style='font-size: 38px; font-weight:900; color:{self.ACCENT_START};'>{used_val}</span>" # Reduced font size to fit
                f"<span style='font-size: 20px; color:{self.ACCENT_START}; font-weight: 600;'> {used_unit}</span>"
                f"<span style='font-size: 14px; color:#666666;'> / {limit_val} {limit_unit}</span><br>"
                f"<span style='font-size: 16px; font-weight: 600; color:{SECONDARY_TEXT_COLOR};'>Remaining: </span>"
                f"<span style='font-size: 16px; font-weight:bold; color:{self.NORMAL_COLOR.name()};'>{remaining_val} {remaining_unit}</span>"
            )
            
            # Update Speed Labels
            self.dl_speed_label.setText(f"DL: {self.convert_speed(self.download_speed)}")
            self.ul_speed_label.setText(f"UP: {self.convert_speed(self.upload_speed)}")

        except Exception as e:
            LOG(f"ERROR [_update_text]: {e}", level='error')

    def paintEvent(self, event):
        try:
            painter = QPainter(self)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)

            # Ring adjusted to fit left side of 480x260 window
            ring = QRectF(15, 20, 150, 150) 

            pct = getattr(self, "displayed_pct", 0.0)

            # Background ring
            painter.setPen(QPen(QColor(60, 70, 85, 160), RING_THICKNESS))
            painter.drawArc(ring, 0, 360 * 16)

            # Gradient
            gradient = QLinearGradient(ring.topLeft(), ring.bottomRight())
            gradient.setColorAt(0.0, QColor(self.ACCENT_START))
            gradient.setColorAt(1.0, QColor(self.ACCENT_END))

            if pct >= 1:
                gradient.setColorAt(0.0, self.EXCEEDED_COLOR)
                gradient.setColorAt(1.0, QColor("#aa0000"))
            elif pct * 100 >= self.notif_thresholds[-1]:
                gradient.setColorAt(0.0, self.WARNING_COLOR)
                gradient.setColorAt(1.0, QColor("#b39500"))

            # Foreground arc
            painter.setPen(QPen(QBrush(gradient), RING_THICKNESS,
                                Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            painter.drawArc(ring, 90 * 16, -int(pct * 360 * 16))

            # Glow
            glow_pen = QPen(QColor(self.ACCENT_START))
            glow_pen.setWidth(RING_THICKNESS * 2)
            glow_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            glow_pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            gc = QColor(self.ACCENT_START)
            gc.setAlpha(90)
            glow_pen.setColor(gc)
            painter.setPen(glow_pen)
            painter.drawArc(ring, 90 * 16, -int(pct * 360 * 16))

            # Percentage
            pct_rect = QRectF(ring.left(), ring.top() + 35, ring.width(), 60)
            painter.setFont(QFont(FONT_FAMILY, 38, QFont.Weight.Bold)) # Reduced font size to fit
            
            try:
                glow_col = QColor(self.ACCENT_START)
                glow_col.setAlpha(220)
                painter.setPen(QPen(glow_col))
                painter.drawText(pct_rect, Qt.AlignmentFlag.AlignCenter, f"{int(pct * 100)}%")
            except Exception:
                painter.setPen(QPen(QColor(self.ACCENT_START)))
                painter.drawText(pct_rect, Qt.AlignmentFlag.AlignCenter, f"{int(pct * 100)}%")

            # Used label
            used_rect = QRectF(ring.left(), ring.top() + 95, ring.width(), 40)
            painter.setFont(QFont(FONT_FAMILY, 15))
            painter.setPen(QPen(QColor("#00e0ff")))
            painter.drawText(used_rect, Qt.AlignmentFlag.AlignCenter, "USED")

            painter.end()
        except Exception as e:
            LOG(f"ERROR [paintEvent]: {e}", level='error')

    # Optimization: Static methods to reduce overhead
    @staticmethod
    def convert(x):
        try:
            x = float(x)
        except Exception:
            x = 0.0
        if x < 1024 ** 2:
            return f"{x / 1024:.2f} KB"
        if x < 1024 ** 3:
            return f"{x / (1024 ** 2):.2f} MB"
        return f"{x / (1024 ** 3):.2f} GB"

    @staticmethod
    def convert_split(x):
        try:
            x = float(x)
        except Exception:
            x = 0.0
        if x < 1024 ** 2:
            return f"{x / 1024:.2f}", "KB"
        if x < 1024 ** 3:
            return f"{x / (1024 ** 2):.2f}", "MB"
        return f"{x / (1024 ** 3):.2f}", "GB"

    @staticmethod
    def convert_speed(x):
        try:
            x = float(x)
        except Exception:
            x = 0.0
        if x < 1024:
            return f"{x:.1f} B/s"
        if x < 1024 ** 2:
            return f"{x / 1024:.1f} KB/s"
        return f"{x / (1024 ** 2):.1f} MB/s"

    def toggle_visibility(self):
        if self.isVisible():
            self.hide()
        else:
            self.show()
            self.raise_()
            self.activateWindow()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_pos = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()

    def mouseMoveEvent(self, event):
        if event.buttons() & Qt.MouseButton.LeftButton and self._drag_pos is not None:
            self.move(event.globalPosition().toPoint() - self._drag_pos)

    def open_settings(self):
        try:
            dlg = SettingsWindow(self)
            dlg.exec()
        except Exception as e:
            LOG(f"ERROR in open_settings: {e}", level='error')
            show_error(e)

    def open_history(self):
        try:
            dlg = HistoryWindow(self)
            dlg.exec()
        except Exception as e:
            LOG(f"ERROR in open_history: {e}", level='error')
            show_error(e)

    def save_final_usage(self):
        try:
            log_wifi_usage(self.today_str, getattr(self, 'current_ssid', get_wifi_ssid()), self.total_today)
            LOG("Final usage saved before shutdown.")
        except Exception as e:
            LOG(f"ERROR in save_final_usage: {e}", level='error')


# ---------------- RUN ----------------
if __name__ == "__main__":
    try:
        QApplication.setAttribute(Qt.ApplicationAttribute.AA_EnableHighDpiScaling, True)
        QApplication.setAttribute(Qt.ApplicationAttribute.AA_UseHighDpiPixmaps, True)
    except Exception:
        pass
    app = QApplication(sys.argv)

# Set application & taskbar icon
app_icon = QIcon(ICON_PATH)
app.setWindowIcon(app_icon)

# Force Windows taskbar icon (AUMID must match this)
try:
    ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("DataUsageMonitor.App")
except Exception:
    pass


    app.setFont(QFont(FONT_FAMILY))

    w = DataWidget()
    w.setWindowIcon(app_icon)
    w.show()

    sys.exit(app.exec())

# --- FIXED BLOCK BELOW ---
app.setQuitOnLastWindowClosed(False)
app.setFont(QFont(FONT_FAMILY))

w = DataWidget()
w.setWindowIcon(app_icon)
w.show()

sys.exit(app.exec())
