"""Локальные проверки синтаксиса и примеров doctest. Не проверяют бизнес-требования.
Запуск: python -m pytest <этот файл> или python <этот файл>.
Примеры doctest исполняют код проекта при запуске тестов.
"""
import ast
import doctest
import importlib
import sys
import tokenize
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PATHS = ['main.py', 'app/__init__.py', 'app/agent_workbench.py', 'app/application.py', 'app/code_runner.py', 'app/controller.py', 'app/file_manager.py', 'app/main_window.py', 'app/project_handler.py', 'app/styling.py', 'app/top_bar.py', 'app/ui_manager.py', 'app/ui_scale.py', 'core/__init__.py', 'core/activity_log.py', 'core/adversary.py', 'core/analysis_cache.py', 'core/app_version.py', 'core/assembly.py', 'core/database_workspace.py', 'core/file_snapshot.py', 'core/ignore_rules.py', 'core/job_recovery.py', 'core/language_services.py', 'core/logging_config.py', 'core/project_intelligence.py', 'core/project_state.py', 'core/project_tasks.py', 'core/security.py', 'core/shell_integration.py', 'core/single_instance.py', 'core/subprocess_utils.py', 'core/text_file.py', 'core/workspace_index.py', 'core/analyzers/__init__.py', 'core/analyzers/binary.py', 'core/analyzers/polyglot.py', 'core/analyzers/registry.py', 'core/code_analysis/__init__.py', 'core/code_analysis/lsp_client.py', 'core/code_analysis/openapi_bridge.py', 'core/code_analysis/registry.py', 'core/code_analysis/service.py', 'core/code_analysis/syntax_treesitter.py', 'core/code_analysis/text_analyzer.py', 'core/code_analysis/treesitter_pack.py', 'design/__init__.py', 'design/colors.py', 'design/icons.py', 'design/metrics.py', 'design/themes.py', 'design/typography.py', 'extensions/__init__.py', 'extensions/api.py', 'extensions/manager.py', 'extensions/examples/completion_demo/extension.py', 'services/__init__.py', 'services/agent_delegation.py', 'services/agent_edits.py', 'services/agent_harness.py', 'services/agent_team.py', 'services/agent_tools.py', 'services/ai_commands.py', 'services/ai_context.py', 'services/ai_providers.py', 'services/ai_service.py', 'services/background.py', 'services/browser_settings.py', 'services/chat_attachments.py', 'services/code_intelligence_service.py', 'services/github_service.py', 'services/ide_mcp.py', 'services/mcp_client.py', 'services/office_reader.py', 'services/offline_tests.py', 'services/process_runner.py', 'services/state_store.py', 'services/student_mentor_service.py', 'services/team_cache.py', 'services/team_optimizer.py', 'services/team_scheduler.py', 'services/updater.py', 'services/workbench_documents.py', 'services/workbench_tools.py', 'tests/conftest.py', 'tests/test_adversary.py', 'tests/test_agent_harness.py', 'tests/test_agent_planner_delete.py', 'tests/test_agent_team.py', 'tests/test_agent_tools.py', 'tests/test_ai_and_scale.py', 'tests/test_ai_tls.py', 'tests/test_app_structure.py', 'tests/test_chat_attachments.py', 'tests/test_chat_render.py', 'tests/test_chat_transcript_fixes.py', 'tests/test_code_analysis.py', 'tests/test_database_open.py', 'tests/test_editor_and_terminal.py', 'tests/test_hex_viewer.py', 'tests/test_ignore_rules.py', 'tests/test_job_recovery.py', 'tests/test_mcp_client.py', 'tests/test_pdf_viewer.py', 'tests/test_project_intelligence.py', 'tests/test_project_manager.py', 'tests/test_project_tasks.py', 'tests/test_release_workflow.py', 'tests/test_subprocess_window.py', 'tests/test_table_viewer.py', 'tests/test_team_cache.py', 'tests/test_team_optimizer.py', 'tests/test_team_scheduler.py', 'tests/test_todo_panel.py', 'tests/test_ui_responsiveness.py', 'tests/test_updates_and_shell.py', 'tools/diplom_postgres.py', 'tools/generate_icons.py', 'tools/push_github.py', 'ui/__init__.py', 'ui/titlebar.py', 'utils/CSharpCompiler.py', 'utils/CodeAnalyzer.py', 'utils/FabricRunCode.py', 'utils/WorkspaceIndex.py', 'utils/__init__.py', 'utils/programs.py', 'utils/settings_manager.py', 'utils/tls.py', 'utils/toolchain_detector.py', 'utils/utils_manager.py', 'widgets/AIAgentPanel.py', 'widgets/AboutDialog.py', 'widgets/ActivityBar.py', 'widgets/AgentGraph.py', 'widgets/AgentPlanner.py', 'widgets/AgentTeamTab.py', 'widgets/AnalysisProfilesDialog.py', 'widgets/ChangeCapsuleDialog.py', 'widgets/ChatMessage.py', 'widgets/CodeExplainerDialog.py', 'widgets/CodeLensBar.py', 'widgets/CodeRadarWidget.py', 'widgets/CommandPalette.py', 'widgets/ConsoleWidget.py', 'widgets/DatabaseWorkspace.py', 'widgets/DiagramViewerWidget.py', 'widgets/Dialog.py', 'widgets/DialogSave.py', 'widgets/DocumentViewerWidget.py', 'widgets/EditorRunControls.py', 'widgets/Explorer.py', 'widgets/ExtensionsWidget.py', 'widgets/FindReplaceBar.py', 'widgets/GitPanel.py', 'widgets/HexViewer.py', 'widgets/KeyboardShortcutsDialog.py', 'widgets/LoadingSpinner.py', 'widgets/LogShareDialog.py', 'widgets/MarkdownViewerWidget.py', 'widgets/PDFViewerWidget.py', 'widgets/ProjectAnalyzerDialog.py', 'widgets/ProjectDialog.py', 'widgets/ProjectIntelligenceDashboard.py', 'widgets/ProjectIntelligenceWidget.py', 'widgets/ProjectManager.py', 'widgets/ProjectTaskManager.py', 'widgets/PushButtons.py', 'widgets/QArgsEditor.py', 'widgets/QCodeEditor.py', 'widgets/QNewFileDialog.py', 'widgets/QuickOpenDialog.py', 'widgets/SearchPanel.py', 'widgets/SettingsWidget.py', 'widgets/ShellIntegrationDialog.py', 'widgets/StudentExplainerDialog.py', 'widgets/SymbolNavigationDialog.py', 'widgets/TabWidget.py', 'widgets/TableViewerWidget.py', 'widgets/TerminalWidget.py', 'widgets/TestResultsPanel.py', 'widgets/TodoPanel.py', 'widgets/UpdateDialog.py', 'widgets/WebBrowserWidget.py', 'widgets/WelcomeWidget.py', 'widgets/__init__.py', 'widgets/animations.py', 'widgets/terminal_output.py', 'widgets/terminal_process.py']

class OfflineProjectTests(unittest.TestCase):
    def test_python_syntax(self):
        for relative in PATHS:
            with self.subTest(path=relative):
                with tokenize.open(ROOT / relative) as stream:
                    compile(stream.read(), relative, "exec", dont_inherit=True)

    def test_documented_examples(self):
        sys.path.insert(0, str(ROOT))
        try:
            found = 0
            for relative in PATHS:
                with tokenize.open(ROOT / relative) as stream:
                    tree = ast.parse(stream.read(), filename=relative)
                docs = [ast.get_docstring(node) or "" for node in ast.walk(tree)
                        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))]
                if not any(">>>" in doc for doc in docs):
                    continue
                parts = list(Path(relative).with_suffix("").parts)
                if parts[-1] == "__init__":
                    parts.pop()
                if not parts or not all(part.isidentifier() for part in parts):
                    continue
                with self.subTest(path=relative):
                    module = importlib.import_module(".".join(parts))
                    self.assertEqual(Path(module.__file__).resolve(), (ROOT / relative).resolve())
                    result = doctest.testmod(module, optionflags=doctest.ELLIPSIS)
                    found += result.attempted
                    self.assertEqual(result.failed, 0, relative)
            if not found:
                self.skipTest("В проекте нет исполняемых примеров doctest")
        finally:
            sys.path.remove(str(ROOT))

if __name__ == "__main__":
    unittest.main()
