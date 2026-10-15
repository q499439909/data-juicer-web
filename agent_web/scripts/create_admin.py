from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from agent_web.auth.passwords import hash_password
from agent_web.config import Settings
from agent_web.db.store import AppStore
from agent_web.workspaces import WorkspaceManager


def main() -> int:
    parser = argparse.ArgumentParser(description="Create a Data-Juicer Agent Web administrator")
    parser.add_argument("username")
    parser.add_argument("--display-name", default="管理员")
    parser.add_argument("--password-stdin", action="store_true", help="Read one password line from stdin (for provisioning)")
    args = parser.parse_args()
    settings = Settings.from_env()
    store = AppStore(settings.database_path)
    store.initialize()
    if store.find_user_by_username(args.username):
        print("User already exists", file=sys.stderr)
        return 2
    password = sys.stdin.readline().rstrip("\r\n") if args.password_stdin else getpass.getpass("Password: ")
    confirm = password if args.password_stdin else getpass.getpass("Confirm password: ")
    if password != confirm:
        print("Passwords do not match", file=sys.stderr)
        return 2
    try:
        encoded = hash_password(password)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    user = store.create_user(args.username, args.display_name, encoded, role="admin")
    WorkspaceManager(settings.workspace_root).ensure_user_workspace(user["id"])
    print(f"Created administrator: {user['username']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
