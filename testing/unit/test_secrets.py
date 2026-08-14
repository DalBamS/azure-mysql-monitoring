from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

COLLECTOR = Path(__file__).parents[2] / "mysql-internal" / "collector"
sys.path.insert(0, str(COLLECTOR))

from plan import EnvReference, KeyVaultReference  # noqa: E402
from secrets import SecretResolutionError, SecretResolver  # noqa: E402


class FakeResource:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class FakeClient(FakeResource):
    def __init__(self, values: dict[tuple[str, str], str]) -> None:
        super().__init__()
        self.values = values
        self.calls: list[tuple[str, str]] = []

    def get_secret(self, name: str, version: str = "") -> SimpleNamespace:
        self.calls.append((name, version))
        return SimpleNamespace(value=self.values[(name, version)])


class SecretResolverTests(unittest.TestCase):
    def test_environment_secret_preserves_significant_whitespace(self) -> None:
        resolver = SecretResolver(environ={"PASSWORD": " value with spaces "})
        self.assertEqual(
            resolver.resolve(EnvReference("PASSWORD"), strip=False),
            " value with spaces ",
        )

    def test_resolves_environment_reference_and_rejects_empty_value(self) -> None:
        resolver = SecretResolver(environ={"PRESENT": " value ", "EMPTY": " "})

        self.assertEqual(resolver.resolve(EnvReference("PRESENT")), "value")
        with self.assertRaisesRegex(SecretResolutionError, "EMPTY.*not set or is empty"):
            resolver.resolve(EnvReference("EMPTY"))

    def test_resolves_key_vault_version_with_one_cached_identity_and_client(self) -> None:
        credential = FakeResource()
        client = FakeClient({("password", "v2"): "secret-value", ("user", ""): "monitor"})
        credentials_created = 0
        clients_created = 0

        def credential_factory() -> FakeResource:
            nonlocal credentials_created
            credentials_created += 1
            return credential

        def client_factory(uri: str, supplied: FakeResource) -> FakeClient:
            nonlocal clients_created
            self.assertEqual(uri, "https://vault.vault.azure.net")
            self.assertIs(supplied, credential)
            clients_created += 1
            return client

        resolver = SecretResolver(
            environ={},
            credential_factory=credential_factory,
            client_factory=client_factory,
        )
        self.assertEqual(
            resolver.resolve(
                KeyVaultReference("https://vault.vault.azure.net", "password", "v2")
            ),
            "secret-value",
        )
        self.assertEqual(
            resolver.resolve(KeyVaultReference("https://vault.vault.azure.net", "user")),
            "monitor",
        )
        self.assertEqual(credentials_created, 1)
        self.assertEqual(clients_created, 1)
        self.assertEqual(client.calls, [("password", "v2"), ("user", "")])

        resolver.close()
        self.assertTrue(client.closed)
        self.assertTrue(credential.closed)

    def test_key_vault_failure_names_reference_without_leaking_other_values(self) -> None:
        class BrokenClient:
            def get_secret(self, _name: str) -> None:
                raise RuntimeError("managed identity denied")

        resolver = SecretResolver(
            environ={},
            credential_factory=lambda: object(),
            client_factory=lambda _uri, _credential: BrokenClient(),
        )
        with self.assertRaisesRegex(
            SecretResolutionError,
            "could not read Key Vault secret 'mysql-password'.*managed identity denied",
        ):
            resolver.resolve(
                KeyVaultReference("https://vault.vault.azure.net", "mysql-password"),
                path="Target orders password",
            )


if __name__ == "__main__":
    unittest.main()
