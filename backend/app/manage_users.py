from __future__ import annotations

import argparse
import getpass

from app.auth import disable_user, enabled_user_ids, init_auth_db, provision_user, reset_password


def main() -> None:
    parser = argparse.ArgumentParser(description="Manage RAG users")
    sub = parser.add_subparsers(dest="command", required=True)
    create = sub.add_parser("create"); create.add_argument("username")
    disable = sub.add_parser("disable"); disable.add_argument("username")
    reset = sub.add_parser("reset-password"); reset.add_argument("username")
    sub.add_parser("list")
    args = parser.parse_args()
    init_auth_db()
    if args.command == "create":
        password = getpass.getpass("Password: ")
        confirm = getpass.getpass("Confirm password: ")
        if password != confirm:
            parser.error("passwords do not match")
        print(provision_user(args.username, password))
    elif args.command == "disable":
        disable_user(args.username)
    elif args.command == "reset-password":
        password = getpass.getpass("New password: ")
        if password != getpass.getpass("Confirm password: "):
            parser.error("passwords do not match")
        reset_password(args.username, password)
    else:
        print("\n".join(enabled_user_ids()))


if __name__ == "__main__":
    main()
