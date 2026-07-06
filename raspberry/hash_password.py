#!/usr/bin/env python3
"""
Jelszó hash generátor — futtasd ezt ha új jelszót akarsz beállítani.
A kimenetét másold be a config.json "password_hash" mezőjébe.

Használat:
    python3 hash_password.py
"""
import bcrypt
import getpass

password = getpass.getpass("Új jelszó: ")
confirm  = getpass.getpass("Mégegyszer: ")

if password != confirm:
    print("A két jelszó nem egyezik.")
else:
    hashed = bcrypt.hashpw(password.encode(), bcrypt.gensalt(12))
    print(f"\nMásold be a config.json-ba:\n{hashed.decode()}")
