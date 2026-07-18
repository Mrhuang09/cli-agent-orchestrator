"""Recovery of live terminal runtimes after an API server restart."""

from unittest.mock import MagicMock

from cli_agent_orchestrator.services import terminal_service


def test_recover_existing_terminal_rebuilds_runtime_without_sending_input(monkeypatch):
    backend = MagicMock()
    backend.session_exists.return_value = True
    backend.supports_event_inbox.return_value = False
    backend.get_history.return_value = "settled agent screen"
    provider = MagicMock()

    monkeypatch.setattr(terminal_service, "get_backend", lambda: backend)
    monkeypatch.setattr(
        terminal_service,
        "list_all_terminals",
        lambda: [{"id": "terminal-1"}],
    )
    monkeypatch.setattr(
        terminal_service,
        "get_terminal_metadata",
        lambda _terminal_id: {
            "id": "terminal-1",
            "provider": "codex",
            "tmux_session": "session-1",
            "tmux_window": "window-1",
            "agent_profile": "project-director",
            "allowed_tools": ["*"],
            "shell_command": "bash",
        },
    )
    monkeypatch.setattr(
        terminal_service.provider_manager,
        "create_provider",
        MagicMock(return_value=provider),
    )
    monkeypatch.setattr(terminal_service.fifo_manager, "create_reader", MagicMock())
    monkeypatch.setattr(terminal_service.bus, "publish", MagicMock())

    assert terminal_service.recover_existing_terminal_runtimes() == 1

    backend.configure_server_interaction_defaults.assert_called_once_with()
    terminal_service.provider_manager.create_provider.assert_called_once_with(
        "codex",
        "terminal-1",
        "session-1",
        "window-1",
        "project-director",
        ["*"],
    )
    assert provider.shell_baseline == "bash"
    terminal_service.fifo_manager.create_reader.assert_called_once_with("terminal-1")
    backend.pipe_pane.assert_called_once()
    backend.send_special_key.assert_not_called()
    terminal_service.bus.publish.assert_called_once_with(
        "terminal.terminal-1.output", {"data": "settled agent screen"}
    )


def test_recover_existing_terminal_skips_missing_tmux_session(monkeypatch):
    backend = MagicMock()
    backend.session_exists.return_value = False
    monkeypatch.setattr(terminal_service, "get_backend", lambda: backend)
    monkeypatch.setattr(
        terminal_service,
        "list_all_terminals",
        lambda: [{"id": "terminal-1"}],
    )
    monkeypatch.setattr(
        terminal_service,
        "get_terminal_metadata",
        lambda _terminal_id: {
            "id": "terminal-1",
            "tmux_session": "gone",
        },
    )
    create_provider = MagicMock()
    monkeypatch.setattr(
        terminal_service.provider_manager, "create_provider", create_provider
    )

    assert terminal_service.recover_existing_terminal_runtimes() == 0
    backend.configure_server_interaction_defaults.assert_called_once_with()
    create_provider.assert_not_called()
