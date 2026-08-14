"""Resolve Collection Plan secret references without exposing their values."""

from __future__ import annotations

import os
import logging
import threading
from collections.abc import Callable, Mapping
from typing import Any

from plan import EnvReference, KeyVaultReference, SecretReference

log = logging.getLogger(__name__)


class SecretResolutionError(RuntimeError):
    """Raised when a referenced value cannot be resolved."""


class SecretResolver:
    """Resolve environment and Azure Key Vault references.

    Azure SDK imports are lazy so environment-only plans need only the core dependencies.
    ``DefaultAzureCredential`` uses managed identity in Azure and the existing developer
    credential chain when the collector is run locally.
    """

    def __init__(
        self,
        *,
        environ: Mapping[str, str] | None = None,
        credential_factory: Callable[[], Any] | None = None,
        client_factory: Callable[[str, Any], Any] | None = None,
    ) -> None:
        self._environ = os.environ if environ is None else environ
        self._credential_factory = credential_factory
        self._client_factory = client_factory
        self._credential: Any | None = None
        self._clients: dict[str, Any] = {}
        self._lock = threading.Lock()

    def resolve(
        self,
        reference: SecretReference,
        *,
        path: str = "secret",
        strip: bool = True,
    ) -> str:
        if isinstance(reference, EnvReference):
            value = self._environ.get(reference.variable, "")
            if not value.strip():
                raise SecretResolutionError(
                    f"{path}: environment variable {reference.variable} is not set or is empty"
                )
            return value.strip() if strip else value

        if not isinstance(reference, KeyVaultReference):
            raise SecretResolutionError(f"{path}: unsupported secret reference")

        client = self._key_vault_client(reference.vault_uri, path=path)
        try:
            secret = (
                client.get_secret(reference.secret, reference.version)
                if reference.version
                else client.get_secret(reference.secret)
            )
            value = getattr(secret, "value", None)
        except Exception as exc:  # noqa: BLE001 - add actionable reference context
            raise SecretResolutionError(
                f"{path}: could not read Key Vault secret {reference.secret!r} "
                f"from {reference.vault_uri}: {exc}"
            ) from exc
        if not isinstance(value, str) or not value:
            raise SecretResolutionError(
                f"{path}: Key Vault secret {reference.secret!r} in "
                f"{reference.vault_uri} has no string value"
            )
        return value.strip() if strip else value

    def _key_vault_client(self, vault_uri: str, *, path: str) -> Any:
        with self._lock:
            existing = self._clients.get(vault_uri)
            if existing is not None:
                return existing

            credential_factory = self._credential_factory
            client_factory = self._client_factory
            if credential_factory is None or client_factory is None:
                try:
                    from azure.identity import DefaultAzureCredential
                    from azure.keyvault.secrets import SecretClient
                except ImportError as exc:
                    raise SecretResolutionError(
                        f"{path}: Azure Key Vault references require the ADX/Key Vault extras; "
                        "install mysql-internal/collector/requirements-adx.txt"
                    ) from exc
                credential_factory = credential_factory or (
                    lambda: DefaultAzureCredential(
                        exclude_interactive_browser_credential=False
                    )
                )
                client_factory = client_factory or (
                    lambda uri, credential: SecretClient(
                        vault_url=uri, credential=credential
                    )
                )

            try:
                if self._credential is None:
                    self._credential = credential_factory()
                client = client_factory(vault_uri, self._credential)
            except Exception as exc:  # noqa: BLE001 - make identity/setup failures explicit
                raise SecretResolutionError(
                    f"{path}: could not initialise managed-identity access to "
                    f"Azure Key Vault {vault_uri}: {exc}"
                ) from exc
            self._clients[vault_uri] = client
            return client

    def close(self) -> None:
        """Close Azure SDK clients when they expose a close method."""

        with self._lock:
            resources = [*self._clients.values()]
            if self._credential is not None:
                resources.append(self._credential)
            self._clients.clear()
            self._credential = None
        for resource in resources:
            close = getattr(resource, "close", None)
            if callable(close):
                try:
                    close()
                except Exception as exc:  # noqa: BLE001 - report but complete shutdown
                    log.warning("could not close secret-resolution resource: %s", exc)
