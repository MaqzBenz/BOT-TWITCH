"""Utilitaire : génère un hash Argon2 pour ADMIN_PASSWORD_HASH."""
import getpass

from argon2 import PasswordHasher

pw = getpass.getpass("Mot de passe admin (>= 12 caractères) : ")
if len(pw) < 12:
    raise SystemExit("Trop court.")
if pw != getpass.getpass("Confirmer : "):
    raise SystemExit("Les mots de passe diffèrent.")
h = PasswordHasher().hash(pw)
print("\nADMIN_PASSWORD_HASH (entourer de quotes simples dans .env à cause des '$') :\n")
print(f"'{h}'")
