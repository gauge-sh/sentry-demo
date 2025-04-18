from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from django.db import models
from django.utils import timezone

from bitfield import TypedClassBitField
from sentry.backup.dependencies import NormalizedModelName, get_model_name
from sentry.backup.sanitize import SanitizableField, Sanitizer
from sentry.backup.scopes import RelocationScope
from sentry.db.models import BoundedPositiveIntegerField, control_silo_model, sane_repr
from sentry.db.models.fields.hybrid_cloud_foreign_key import HybridCloudForeignKey
from sentry.db.models.fields.jsonfield import JSONField
from sentry.hybridcloud.models.outbox import ControlOutbox
from sentry.hybridcloud.outbox.base import ReplicatedControlModel
from sentry.hybridcloud.outbox.category import OutboxCategory, OutboxScope
from sentry.types.region import find_regions_for_orgs

logger = logging.getLogger("sentry.authprovider")

SCIM_INTERNAL_INTEGRATION_OVERVIEW = (
    "This internal integration was auto-generated during the installation process of your SCIM "
    "integration. It is needed to provide the token used to provision members and teams. If this integration is "
    "deleted, your SCIM integration will stop working!"
)


@control_silo_model
class AuthProvider(ReplicatedControlModel):
    __relocation_scope__ = RelocationScope.Global
    category = OutboxCategory.AUTH_PROVIDER_UPDATE

    organization_id = HybridCloudForeignKey("sentry.Organization", on_delete="cascade", unique=True)
    provider = models.CharField(max_length=128)
    config: models.Field[dict[str, Any], dict[str, Any]] = JSONField()

    date_added = models.DateTimeField(default=timezone.now)
    sync_time = BoundedPositiveIntegerField(null=True)
    last_sync = models.DateTimeField(null=True)

    default_role = BoundedPositiveIntegerField(default=50)
    default_global_access = models.BooleanField(default=True)

    def handle_async_replication(self, region_name: str, shard_identifier: int) -> None:
        from sentry.auth.services.auth.serial import serialize_auth_provider
        from sentry.hybridcloud.services.replica.service import region_replica_service

        serialized = serialize_auth_provider(self)
        region_replica_service.upsert_replicated_auth_provider(
            auth_provider=serialized, region_name=region_name
        )

    @classmethod
    def handle_async_deletion(
        cls,
        identifier: int,
        region_name: str,
        shard_identifier: int,
        payload: Mapping[str, Any] | None,
    ) -> None:
        from sentry.hybridcloud.services.replica.service import region_replica_service

        region_replica_service.delete_replicated_auth_provider(
            auth_provider_id=identifier, region_name=region_name
        )

    class flags(TypedClassBitField):
        # WARNING: Only add flags to the bottom of this list
        # bitfield flags are dependent on their order and inserting/removing
        # flags from the middle of the list will cause bits to shift corrupting
        # existing data.

        # Grant access to members who have not linked SSO accounts.
        allow_unlinked: bool
        # Enable SCIM for member and team provisioning and syncing.
        scim_enabled: bool

        bitfield_default = 0

    class Meta:
        app_label = "sentry"
        db_table = "sentry_authprovider"

    __repr__ = sane_repr("organization_id", "provider")

    def __str__(self):
        return self.provider

    def get_provider(self):
        from sentry.auth import manager

        return manager.get(self.provider, **self.config)

    @property
    def provider_name(self) -> str:
        return self.get_provider().name

    def get_scim_token(self):
        return get_scim_token(self.flags.scim_enabled, self.organization_id, self.provider)

    def enable_scim(self, user):
        from sentry.sentry_apps.logic import SentryAppCreator
        from sentry.sentry_apps.models.sentry_app_installation import SentryAppInstallation
        from sentry.sentry_apps.models.sentry_app_installation_for_provider import (
            SentryAppInstallationForProvider,
        )

        if (
            not self.get_provider().can_use_scim(self.organization_id, user)
            or self.flags.scim_enabled is True
        ):
            logger.warning(
                "SCIM already enabled",
                extra={"organization_id": self.organization_id},
            )
            return

        # check if we have a scim app already

        if SentryAppInstallationForProvider.objects.filter(
            organization_id=self.organization_id, provider="okta_scim"
        ).exists():
            logger.warning(
                "SCIM installation already exists",
                extra={"organization_id": self.organization_id},
            )
            return

        sentry_app = SentryAppCreator(
            name="SCIM Internal Integration",
            author="Auto-generated by Sentry",
            organization_id=self.organization_id,
            overview=SCIM_INTERNAL_INTEGRATION_OVERVIEW,
            is_internal=True,
            verify_install=False,
            scopes=[
                "member:read",
                "member:write",
                "member:admin",
                "team:write",
                "team:admin",
            ],
        ).run(user=user)
        sentry_app_installation = SentryAppInstallation.objects.get(sentry_app=sentry_app)
        SentryAppInstallationForProvider.objects.create(
            sentry_app_installation=sentry_app_installation,
            organization_id=self.organization_id,
            provider=f"{self.provider}_scim",
        )
        self.flags.scim_enabled = True

    def outboxes_for_reset_idp_flags(self) -> list[ControlOutbox]:
        return [
            ControlOutbox(
                shard_scope=OutboxScope.ORGANIZATION_SCOPE,
                shard_identifier=self.organization_id,
                category=OutboxCategory.RESET_IDP_FLAGS,
                object_identifier=self.organization_id,
                region_name=region_name,
            )
            for region_name in find_regions_for_orgs([self.organization_id])
        ]

    def disable_scim(self):
        from sentry import deletions
        from sentry.sentry_apps.models.sentry_app_installation_for_provider import (
            SentryAppInstallationForProvider,
        )

        if self.flags.scim_enabled:
            # Only one SCIM installation allowed per organization. So we can reset the idp flags for the orgs
            # We run this update before the app is uninstalled to avoid ending up in a situation where there are
            # members locked out because we failed to drop the IDP flag
            for outbox in self.outboxes_for_reset_idp_flags():
                outbox.save()
            try:
                # Provider : Installation links aren't guaranteed to be around all the time.
                # Customers can remove the SCIM sentry app before the auth provider
                install = SentryAppInstallationForProvider.objects.get(
                    organization_id=self.organization_id, provider=f"{self.provider}_scim"
                )
                sentry_app = install.sentry_app_installation.sentry_app
                assert (
                    sentry_app.is_internal
                ), "scim sentry apps should always be internal, thus deleting them without triggering InstallationNotifier is correct."
                deletions.exec_sync(sentry_app)
            except SentryAppInstallationForProvider.DoesNotExist:
                pass
            self.flags.scim_enabled = False

    def get_audit_log_data(self):
        provider = self.provider
        # NOTE(isabella): for both standard fly SSO and fly-non-partner SSO, we should record the
        # provider as "fly" in the audit log entry data; the only difference between the two is
        # that the latter can be disabled by customers
        if "fly" in self.provider:
            provider = "fly"
        return {"provider": provider, "config": self.config}

    def outboxes_for_mark_invalid_sso(self, user_id: int) -> list[ControlOutbox]:
        return [
            ControlOutbox(
                shard_scope=OutboxScope.ORGANIZATION_SCOPE,
                shard_identifier=self.organization_id,
                category=OutboxCategory.MARK_INVALID_SSO,
                object_identifier=user_id,
                region_name=region_name,
            )
            for region_name in find_regions_for_orgs([self.organization_id])
        ]

    @classmethod
    def sanitize_relocation_json(
        cls, json: Any, sanitizer: Sanitizer, model_name: NormalizedModelName | None = None
    ) -> None:
        model_name = get_model_name(cls) if model_name is None else model_name
        super().sanitize_relocation_json(json, sanitizer, model_name)

        sanitizer.set_json(json, SanitizableField(model_name, "config"), {})
        sanitizer.set_string(json, SanitizableField(model_name, "provider"))


def get_scim_token(scim_enabled: bool, organization_id: int, provider: str) -> str | None:
    from sentry.sentry_apps.services.app import app_service

    if scim_enabled:
        return app_service.get_installation_token(
            organization_id=organization_id, provider=f"{provider}_scim"
        )
    else:
        logger.warning(
            "SCIM disabled but tried to access token",
            extra={"organization_id": organization_id},
        )
        return None
