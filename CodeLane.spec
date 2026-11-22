# -*- mode: python ; coding: utf-8 -*-
# CodeLane PyInstaller Specification
# Builds a self-contained, windowless Windows desktop IDE executable and bundle.

from pathlib import Path
import sys
from PyInstaller.utils.hooks import collect_data_files, collect_delvewheel_libs_directory, collect_submodules, copy_metadata

ROOT = Path(SPECPATH)

datas = [
    (str(ROOT / "src"), "src"),
    (str(ROOT / "design" / "theme.qss"), "design"),
    (str(ROOT / "design" / "fonts"), "design/fonts"),
    (str(ROOT / "design" / "icons"), "design/icons"),
    (str(ROOT / "config" / "settings.default.json"), "config"),
    (str(ROOT / "USER_GUIDE.md"), "."),
    (str(ROOT / "VERSION"), "."),
    (str(ROOT / "third_party" / "assembly" / "nasm"), "third_party/assembly/nasm"),
    (str(ROOT / "third_party" / "assembly" / "fasm"), "third_party/assembly/fasm"),
]
datas += collect_data_files("jedi") + collect_data_files("parso")
# The Mozilla CA bundle is data, not code: without it a frozen build falls
# back to the (sometimes trimmed) Windows root store and AI requests fail with
# CERTIFICATE_VERIFY_FAILED.
import certifi
certifi_bundle = Path(certifi.where())
if not certifi_bundle.is_file():
    raise FileNotFoundError(f"certifi CA bundle is missing: {certifi_bundle}")
datas.append((str(certifi_bundle), "certifi"))
datas += copy_metadata("bandit")
datas, database_binaries = collect_delvewheel_libs_directory("psycopg_binary", datas=datas)

hiddenimports = [
    # PyQt6 Core and GUI
    "PyQt6.QtCore",
    "PyQt6.QtGui",
    "PyQt6.QtWidgets",
    "PyQt6.QtPrintSupport",
    "PyQt6.QtWebEngineCore",
    "PyQt6.QtWebEngineWidgets",
    "PyQt6.sip",
    # System and Process inspection
    "psutil",
    "pyte",
    # CA bundle for outbound HTTPS (utils/tls.py)
    "certifi",
    # Syntax highlighting & themes
    "pygments",
    "pygments.lexers",
    "pygments.styles",
    "pygments.formatters",
    # Application modular subsystems
    "app.application",
    "app.main_window",
    "app.controller",
    "app.top_bar",
    "app.ui_manager",
    "app.file_manager",
    "app.project_handler",
    "app.code_runner",
    "app.styling",
    # Core analyzers & services
    "core.logging_config",
    "core.single_instance",
    "core.project_intelligence",
    "core.workspace_index",
    "sqlalchemy.dialects.sqlite.pysqlite",
    "sqlalchemy.dialects.postgresql.psycopg",
    "sqlalchemy.dialects.mysql.pymysql",
    "sqlalchemy.dialects.mssql.pyodbc",
    "psycopg_binary",
    "core.security",
    "core.analyzers.registry",
    "core.analyzers.polyglot",
    "core.analyzers.binary",
    "services.code_intelligence_service",
    "services.github_service",
    "services.process_runner",
    "services.state_store",
    # Widgets
    "widgets.ActivityBar",
    "widgets.GitPanel",
    "widgets.ProjectIntelligenceDashboard",
    "widgets.ChangeCapsuleDialog",
    "widgets.HexViewer",
    "widgets.PDFViewerWidget",
    "widgets.MarkdownViewerWidget",
    "widgets.DocumentViewerWidget",
    "widgets.ConsoleWidget",
    "widgets.TerminalWidget",
    "widgets.Explorer",
    "widgets.QCodeEditor",
    "widgets.AboutDialog",
    "widgets.SettingsWidget",
    "widgets.FindReplaceBar",
    "widgets.QuickOpenDialog",
    "widgets.CommandPalette",
    "widgets.KeyboardShortcutsDialog",
    "widgets.AnalysisProfilesDialog",
    "widgets.ProjectManager",
    "widgets.ProjectDialog",
    "widgets.EditorRunControls",
    "widgets.SearchPanel",
    "widgets.TabWidget",
    "widgets.WelcomeWidget",
    # Utilities
    "utils.programs",
    "utils.settings_manager",
    "utils.toolchain_detector",
]


a = Analysis(
    [str(ROOT / "main.py")],
    pathex=[str(ROOT)],
    binaries=database_binaries,
    datas=datas,
    hiddenimports=hiddenimports + [module
        for package in ("bandit.plugins", "bandit.blacklists", "bandit.formatters")
        for module in collect_submodules(package)],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "unittest", "test"],
    noarchive=False,
    optimize=0,
)

if sys.platform == "win32":
    # Qt imports the Windows ICU API. A third-party ICU on the build machine's
    # PATH can have the same DLL name but incompatible versioned exports.
    a.binaries = [
        entry for entry in a.binaries
        if entry[0].lower() not in {"icuuc.dll", "icuin.dll", "icu.dll"}
    ]

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="CodeLane",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(ROOT / "src" / "PyScribe.ico"),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="CodeLane",
)
