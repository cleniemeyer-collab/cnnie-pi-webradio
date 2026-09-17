from pathlib import Path
from getpass import getpass
from web_auth import AuthConfig

auth = AuthConfig(Path("web_auth_config.json"))
username = input("Benutzername: ").strip()
password = getpass("Passwort: ")
auth.add_user(username, password)
print(f"Benutzer {username!r} wurde angelegt.")

