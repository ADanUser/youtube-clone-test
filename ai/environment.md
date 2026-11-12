# Среда Codex

Безопасный снимок фактической среды на этапе финальной документации:

- Codex CLI: `0.162.0-alpha.17.2`.
- Модель: `gpt-5.6-sol`; reasoning effort: `low`.
- Desktop: `followUpQueueMode = "steer"`, `conversationDetailMode = "STEPS_PROSE"`,
  ambient suggestions включены.
- Использованные при работе обвязки: `openai-docs` и
  `unified-computer-use`/`cua_repl`.
- Доступные локальные MCP-процессы: `cua_repl`, `node_repl`; внешние
  аккаунты и MCP-подключения проекта не использовались.

В сдачу намеренно не включены `~/.codex/auth.json`, SQLite-состояние, shell
snapshots, логи, кеши, переменные окружения MCP и абсолютные личные пути. Они не
нужны для воспроизведения проекта и могут содержать секреты или личные данные.
