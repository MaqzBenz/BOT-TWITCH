"""Contrat commun des modules (plugins) : état activable à chaud + configuration validée."""
from pydantic import BaseModel


class BaseModule:
    name: str = ""
    label: str = ""
    description: str = ""
    ConfigModel: type[BaseModel] = BaseModel

    def __init__(self, service) -> None:
        self.service = service          # TwitchService
        self.enabled = False
        self.config: BaseModel = self.ConfigModel()

    async def on_start(self) -> None:
        """Appelé à l'activation (tâches de fond, chargement de cache...)."""

    async def on_stop(self) -> None:
        """Appelé à la désactivation (annuler les tâches de fond)."""

    async def on_config_changed(self) -> None:
        """Appelé après modification de la configuration."""

    async def handle_message(self, msg) -> bool:
        """Traite un message du chat. Retourner True pour stopper la chaîne de modules."""
        return False
