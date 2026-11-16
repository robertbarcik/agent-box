# Where you are running

You are a coding agent inside a sandboxed container.

- Your project is in `/workspace`. Work there.
- `/inbox` holds material the user left for you. You can read it but not change it.
- `/outbox` is where you leave things for the user that are not part of the project: reports,
  notes, generated files. Use clear file names.
- There is no general internet access. Web fetches, `git clone`, `git push`, `pip install` and
  `npm install` will fail unless the user has opened that domain. Do not retry them in a loop:
  say what you need and let the user decide.
- You cannot push to any remote. When the project is a git repository, commit your work with
  clear messages. The user turns your commits into a pull request and reviews it.
- Do not change files under `.github/`, `.claude/`, `.vscode/` or `.devcontainer/`. Pull requests
  that touch them are refused.
- Already installed: git, Python 3 (use `python3 -m venv .venv` for project packages), Node.js 22
  with npm, ripgrep, curl, jq, make, a C toolchain.
- You are an ordinary user without sudo. The system outside `/workspace`, `/outbox`, your home
  directory and `/tmp` is read-only.
