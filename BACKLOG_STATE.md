# Backlog State

## Completed
- [x] **Story 8 (P0)**: Integration tests — `test_integration.py` with 29 tests covering fresh translation, incremental, cache hits, deleted strings, CDATA, HTML, format specs, plurals, string-arrays, overrides
- [x] **Story NEW (P1)**: Manual override dictionary — `overrides.json` + `--overrides` CLI option, case-sensitive token protection via placeholder system, 12 unit tests + 5 integration tests

- [x] **Story 7 (P1)**: XML output validation — parse output after write, restore backup on failure, 3 integration tests

- [x] **Story 9 (P1)**: Specific error handling — custom exceptions, classified catch blocks, error counting summary, 8 tests
- [x] **Story 2 (P1)**: Preserve XML comments & attributes — CommentedTreeBuilder parser, 7 integration tests

- [x] **Story 3 (P1)**: File logging — `--log-file` CLI option, logger with timestamps + DEBUG level, 4 integration tests

- [x] **Story 4 (P1)**: Regional language variants — `zh-CN`→`values-zh-rCN`, `pt-BR`→`values-pt-rBR` Android folder mapping, 7 integration tests

- [x] **Story 1 (P2)**: Dry-run mode — `--dry-run` CLI flag, skip file write + validation, 3 integration tests

- [x] **Story 11 (P2)**: `--only` language filter — filter by ISO codes, case-insensitive, 3 integration tests

- [x] **Story 10 (P2)**: Translation report JSON — `--report` CLI option, per-language pass/fail/counts, 11 integration tests

- [x] **Story 12 (P2)**: Cache management CLI — `--cache stats/clear`, `--cache-clear-lang`, stats/clear/clear_language methods, 8 unit tests

## Backlog (P2)
- [ ] **Story 5**: Config file support
