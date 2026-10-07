"""Point d'entrée conteneur : lance uvicorn sur le port configuré."""
import uvicorn

from .config import settings

if __name__ == "__main__":
    uvicorn.run("app.main:app", host="0.0.0.0", port=settings.app_port,
                log_level=settings.log_level.lower(), access_log=False)
