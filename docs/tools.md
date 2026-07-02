# Tools Reference

## Overview

All tools are available to the AI during conversation and execution. Tools are sandboxed to the workspace root — the AI cannot read or write files outside it. Write tools produce diff previews that require user approval (unless auto-approve is enabled).

## Circuit Breaker

Each tool call goes through a circuit breaker that tracks consecutive timeouts or severe errors. After 3 consecutive failures, the tool is blocked for 60 seconds. This prevents cascading failures from a stuck API or filesystem issue.

## Read Tools

| Tool                | Description                                                       | Parameters                                                   |
|---------------------|-------------------------------------------------------------------|--------------------------------------------------------------|
| `read_file`         | Read a UTF-8 text file (capped at 200 KB)                         | `path`                                                       |
| `read_files`        | Batch read multiple files (capped at 500 KB total)                | `paths`                                                      |
| `list_directory`    | List files and subdirectories (excludes .git, __pycache__, etc.)  | `path`                                                       |
| `glob`              | Recursive file matching with glob patterns (capped at 200 matches) | `pattern`                                                   |
| `read_file_outline` | Structural outline via AST (class names, functions, imports)       | `path`                                                       |
| `grep_search`       | String or regex search across files                               | `pattern`, `regex_mode`, `case_sensitive`, `max_results`, `include_pattern` |
| `find_usages`       | Word-boundary symbol search for refactoring safety                | `symbol`, `include_pattern`, `max_results`                   |
| `search_codebase`   | BM25 semantic search across the project index                     | `query`, `top_k`                                             |

`grep_search` is for discovery: finding candidate files and line locations across the workspace. For exact verification of known edited content, use `read_file` or `read_file_range`.

## Write Tools

| Tool               | Description                                                      | Parameters                                      |
|--------------------|------------------------------------------------------------------|-------------------------------------------------|
| `write_file`       | Write full file content (creates directories if needed)          | `target`, `content`                             |
| `edit_file`        | String-replacement edit with old/new search block                | `target`, `old_str`, `new_str`                  |
| `edit_symbol`      | AST-aware structured edit of a Python symbol                     | `target`, `symbol_name`, `old_body`, `new_body` |
| `edit_symbol` (fields mode) | Add/modify/remove dataclass fields on a symbol        | `target`, `symbol_name`, `action`, `fields`     |
| `edit_symbol` (replace mode) | Replace a symbol's full body                           | `target`, `symbol_name`, `new_body`             |

`edit_symbol` uses Python AST parsing to safely locate and modify specific symbols. It supports three modes: body replacement, body editing (old_body → new_body replacement within the symbol), and field manipulation for dataclasses. Falls back gracefully to `edit_file` if AST parsing fails.

## Git Tools

| Tool              | Description                                        | Parameters                      |
|-------------------|----------------------------------------------------|---------------------------------|
| `git_status`      | Working tree status (branch, staged/unstaged files) | —                               |
| `git_diff`        | Working tree or staged diff (capped at 200 KB)     | `staged`, `path`                |
| `git_log`         | Recent commit history                              | `max_count`, `path`             |
| `git_show`        | Full diff and metadata for a specific commit        | `commit_sha`                    |
| `git_log_file`    | Commit history for a single file (follows renames)  | `path`, `max_count`             |
| `git_branch_list` | List branches                                      | —                               |
| `git_stash_list`  | List stash entries                                 | —                               |
| `git_stash_show`  | Show changes in a stash entry                      | `stash_index`                   |

## Terminal

| Tool                    | Description                                  | Parameters                            |
|-------------------------|----------------------------------------------|---------------------------------------|
| `run_terminal_command`  | Execute a read-only diagnostic command       | `command`, `timeout`, `description`    |

This tool is available to the Planner and Drones. Worker may also use it. Output is truncated at 100 KB. Rejects mutating or dangerous commands.

## Worker Display

| Tool                 | Description                                  | Parameters |
|----------------------|----------------------------------------------|------------|
| `update_worker_todo` | (Worker only) Publish the live TODO snapshot | `items`    |

The TODO snapshot is display-only. It does not gate execution, decide completion, or persist as the Worker receipt.

## Dispatch

| Tool                 | Description                                           | Parameters                                                   |
|----------------------|-------------------------------------------------------|--------------------------------------------------------------|
| `dispatch_to_worker` | (Planner only) Dispatch a spec to the Worker          | `goal`, `files`, `spec`, `acceptance`, `summary`, plus optional structured fields |

Only available to the Planner agent. Triggers the full Worker cycle with its own tool budget.
