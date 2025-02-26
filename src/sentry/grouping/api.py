from __future__ import annotations

import re
from collections.abc import MutableMapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, NotRequired, TypedDict

import sentry_sdk

from sentry import options
from sentry.grouping.component import (
    AppGroupingComponent,
    BaseGroupingComponent,
    DefaultGroupingComponent,
    SystemGroupingComponent,
)
from sentry.grouping.enhancer import LATEST_VERSION, Enhancements
from sentry.grouping.enhancer.exceptions import InvalidEnhancerConfig
from sentry.grouping.strategies.base import DEFAULT_GROUPING_ENHANCEMENTS_BASE, GroupingContext
from sentry.grouping.strategies.configurations import CONFIGURATIONS
from sentry.grouping.utils import (
    expand_title_template,
    hash_from_values,
    is_default_fingerprint_var,
    resolve_fingerprint_values,
)
from sentry.grouping.variants import (
    BaseVariant,
    BuiltInFingerprintVariant,
    ChecksumVariant,
    ComponentVariant,
    CustomFingerprintVariant,
    FallbackVariant,
    HashedChecksumVariant,
    SaltedComponentVariant,
)
from sentry.models.grouphash import GroupHash

if TYPE_CHECKING:
    from sentry.eventstore.models import Event
    from sentry.grouping.fingerprinting import FingerprintingRules, FingerprintRuleJSON
    from sentry.grouping.strategies.base import StrategyConfiguration
    from sentry.models.project import Project

HASH_RE = re.compile(r"^[0-9a-f]{32}$")


class FingerprintInfo(TypedDict):
    client_fingerprint: NotRequired[list[str]]
    matched_rule: NotRequired[FingerprintRuleJSON]


@dataclass
class GroupHashInfo:
    config: GroupingConfig
    variants: dict[str, BaseVariant]
    hashes: list[str]
    grouphashes: list[GroupHash]
    existing_grouphash: GroupHash | None


NULL_GROUPING_CONFIG: GroupingConfig = {"id": "", "enhancements": ""}
NULL_GROUPHASH_INFO = GroupHashInfo(NULL_GROUPING_CONFIG, {}, [], [], None)


class GroupingConfigNotFound(LookupError):
    pass


class GroupingConfig(TypedDict):
    id: str
    enhancements: str


class GroupingConfigLoader:
    """Load a grouping config based on global or project options"""

    cache_prefix: str  # Set in subclasses

    def get_config_dict(self, project: Project) -> GroupingConfig:
        return {
            "id": self._get_config_id(project),
            "enhancements": self._get_enhancements(project),
        }

    def _get_enhancements(self, project) -> str:
        enhancements = project.get_option("sentry:grouping_enhancements")

        config_id = self._get_config_id(project)
        enhancements_base = CONFIGURATIONS[config_id].enhancements_base

        # Instead of parsing and dumping out config here, we can make a
        # shortcut
        from sentry.utils.cache import cache
        from sentry.utils.hashlib import md5_text

        cache_prefix = self.cache_prefix
        cache_prefix += f"{LATEST_VERSION}:"
        cache_key = cache_prefix + md5_text(f"{enhancements_base}|{enhancements}").hexdigest()
        rv = cache.get(cache_key)
        if rv is not None:
            return rv

        try:
            rv = Enhancements.from_config_string(enhancements, bases=[enhancements_base]).dumps()
        except InvalidEnhancerConfig:
            rv = get_default_enhancements()
        cache.set(cache_key, rv)
        return rv

    def _get_config_id(self, project):
        raise NotImplementedError


class ProjectGroupingConfigLoader(GroupingConfigLoader):
    option_name: str  # Set in subclasses

    def _get_config_id(self, project):
        return project.get_option(
            self.option_name,
            validate=lambda x: x in CONFIGURATIONS,
        )


class PrimaryGroupingConfigLoader(ProjectGroupingConfigLoader):
    """The currently active grouping config"""

    option_name = "sentry:grouping_config"
    cache_prefix = "grouping-enhancements:"


class SecondaryGroupingConfigLoader(ProjectGroupingConfigLoader):
    """Secondary config to find old groups after config change"""

    option_name = "sentry:secondary_grouping_config"
    cache_prefix = "secondary-grouping-enhancements:"


class BackgroundGroupingConfigLoader(GroupingConfigLoader):
    """Does not affect grouping, runs in addition to measure performance impact"""

    cache_prefix = "background-grouping-enhancements:"

    def _get_config_id(self, project):
        return options.get("store.background-grouping-config-id")


@sentry_sdk.tracing.trace
def get_grouping_config_dict_for_project(project) -> GroupingConfig:
    """Fetches all the information necessary for grouping from the project
    settings.  The return value of this is persisted with the event on
    ingestion so that the grouping algorithm can be re-run later.

    This is called early on in normalization so that everything that is needed
    to group the project is pulled into the event.
    """
    loader = PrimaryGroupingConfigLoader()
    return loader.get_config_dict(project)


def get_grouping_config_dict_for_event_data(data, project) -> GroupingConfig:
    """Returns the grouping config for an event dictionary."""
    return data.get("grouping_config") or get_grouping_config_dict_for_project(project)


def get_default_enhancements(config_id=None) -> str:
    base: str | None = DEFAULT_GROUPING_ENHANCEMENTS_BASE
    if config_id is not None:
        base = CONFIGURATIONS[config_id].enhancements_base
    return Enhancements.from_config_string("", bases=[base]).dumps()


def get_projects_default_fingerprinting_bases(
    project: Project, config_id: str | None = None
) -> Sequence[str] | None:
    """Returns the default built-in fingerprinting bases (i.e. sets of rules) for a project."""
    from sentry.projectoptions.defaults import DEFAULT_GROUPING_CONFIG

    config_id = (
        config_id
        # TODO: add fingerprinting config to GroupingConfigLoader and use that here
        or PrimaryGroupingConfigLoader()._get_config_id(project)
        or DEFAULT_GROUPING_CONFIG
    )

    bases = CONFIGURATIONS[config_id].fingerprinting_bases
    return bases


def get_default_grouping_config_dict(config_id=None) -> GroupingConfig:
    """Returns the default grouping config."""
    if config_id is None:
        from sentry.projectoptions.defaults import DEFAULT_GROUPING_CONFIG

        config_id = DEFAULT_GROUPING_CONFIG
    return {"id": config_id, "enhancements": get_default_enhancements(config_id)}


def load_grouping_config(config_dict=None) -> StrategyConfiguration:
    """Loads the given grouping config."""
    if config_dict is None:
        config_dict = get_default_grouping_config_dict()
    elif "id" not in config_dict:
        raise ValueError("Malformed configuration dictionary")
    config_dict = dict(config_dict)
    config_id = config_dict.pop("id")
    if config_id not in CONFIGURATIONS:
        raise GroupingConfigNotFound(config_id)
    return CONFIGURATIONS[config_id](**config_dict)


def load_default_grouping_config() -> StrategyConfiguration:
    return load_grouping_config(config_dict=None)


def get_fingerprinting_config_for_project(
    project: Project, config_id: str | None = None
) -> FingerprintingRules:
    """
    Returns the fingerprinting rules for a project.
    Merges the project's custom fingerprinting rules (if any) with the default built-in rules.
    """

    from sentry.grouping.fingerprinting import FingerprintingRules, InvalidFingerprintingConfig

    bases = get_projects_default_fingerprinting_bases(project, config_id=config_id)
    rules = project.get_option("sentry:fingerprinting_rules")
    if not rules:
        return FingerprintingRules([], bases=bases)

    from sentry.utils.cache import cache
    from sentry.utils.hashlib import md5_text

    cache_key = "fingerprinting-rules:" + md5_text(rules).hexdigest()
    rv = cache.get(cache_key)
    if rv is not None:
        return FingerprintingRules.from_json(rv, bases=bases)

    try:
        rv = FingerprintingRules.from_config_string(rules, bases=bases)
    except InvalidFingerprintingConfig:
        rv = FingerprintingRules([], bases=bases)
    cache.set(cache_key, rv.to_json())
    return rv


def apply_server_fingerprinting(
    event: MutableMapping[str, Any], config: FingerprintingRules, allow_custom_title: bool = True
) -> None:
    fingerprint_info = {}

    client_fingerprint = event.get("fingerprint", [])
    client_fingerprint_is_default = len(client_fingerprint) == 1 and is_default_fingerprint_var(
        client_fingerprint[0]
    )
    if client_fingerprint and not client_fingerprint_is_default:
        fingerprint_info["client_fingerprint"] = client_fingerprint

    rv = config.get_fingerprint_values_for_event(event)
    if rv is not None:
        rule, new_fingerprint, attributes = rv

        # A custom title attribute is stored in the event to override the
        # default title.
        if "title" in attributes and allow_custom_title:
            event["title"] = expand_title_template(attributes["title"], event)
        event["fingerprint"] = new_fingerprint

        # Persist the rule that matched with the fingerprint in the event
        # dictionary for later debugging.
        fingerprint_info["matched_rule"] = rule.to_json()

    if fingerprint_info:
        event["_fingerprint_info"] = fingerprint_info


def _get_calculated_grouping_variants_for_event(
    event: Event, context: GroupingContext
) -> dict[str, AppGroupingComponent | SystemGroupingComponent | DefaultGroupingComponent]:
    winning_strategy: str | None = None
    precedence_hint: str | None = None
    per_variant_components: dict[str, list[BaseGroupingComponent]] = {}

    for strategy in context.config.iter_strategies():
        # Defined in src/sentry/grouping/strategies/base.py
        rv = strategy.get_grouping_component_variants(event, context=context)
        for variant, component in rv.items():
            per_variant_components.setdefault(variant, []).append(component)

            if winning_strategy is None:
                if component.contributes:
                    winning_strategy = strategy.name
                    variants_hint = "/".join(sorted(k for k, v in rv.items() if v.contributes))
                    precedence_hint = "{} take{} precedence".format(
                        (
                            f"{strategy.name} of {variants_hint}"
                            if variant != "default"
                            else strategy.name
                        ),
                        "" if strategy.name.endswith("s") else "s",
                    )
            elif component.contributes and winning_strategy != strategy.name:
                component.update(contributes=False, hint=precedence_hint)

    rv = {}
    for variant, components in per_variant_components.items():
        component_class_by_variant = {
            "app": AppGroupingComponent,
            "default": DefaultGroupingComponent,
            "system": SystemGroupingComponent,
        }
        component = component_class_by_variant[variant](values=components)
        if not component.contributes and precedence_hint:
            component.update(hint=precedence_hint)
        rv[variant] = component

    return rv


# This is called by the Event model in get_grouping_variants()
def get_grouping_variants_for_event(
    event: Event, config: StrategyConfiguration | None = None
) -> dict[str, BaseVariant]:
    """Returns a dict of all grouping variants for this event."""
    # If a checksum is set the only variant that comes back from this
    # event is the checksum variant.
    #
    # TODO: Is there a reason we don't treat a checksum like a custom fingerprint, and run the other
    # strategies but mark them as non-contributing, with explanations why?
    #
    # TODO: In the case where we have to hash the checksum to get a value in the right format, we
    # store the raw value as well (provided it's not so long that it will overflow the DB field).
    # Even when we do this, though, we don't set the raw value as non-cotributing, and we don't add
    # an "ignored because xyz" hint on the variant, which we should.
    checksum = event.data.get("checksum")
    if checksum:
        if HASH_RE.match(checksum):
            return {"checksum": ChecksumVariant(checksum)}

        rv: dict[str, BaseVariant] = {
            "hashed_checksum": HashedChecksumVariant(hash_from_values(checksum), checksum),
        }

        # The legacy code path also supported arbitrary values here but
        # it will blow up if it results in more than 32 bytes of data
        # as this cannot be inserted into the database.  (See GroupHash.hash)
        if len(checksum) <= 32:
            rv["checksum"] = ChecksumVariant(checksum)

        return rv

    # Otherwise we go to the various forms of fingerprint handling.  If the event carries
    # a materialized fingerprint info from server side fingerprinting we forward it to the
    # variants which can export additional information about them.
    fingerprint = event.data.get("fingerprint") or ["{{ default }}"]
    fingerprint_info = event.data.get("_fingerprint_info", {})
    defaults_referenced = sum(1 if is_default_fingerprint_var(d) else 0 for d in fingerprint)

    if config is None:
        config = load_default_grouping_config()
    context = GroupingContext(config)

    # At this point we need to calculate the default event values.  If the
    # fingerprint is salted we will wrap it.
    components = _get_calculated_grouping_variants_for_event(event, context)

    # If no defaults are referenced we produce a single completely custom
    # fingerprint and mark all other variants as non-contributing
    if defaults_referenced == 0:
        rv = {}
        for key, component in components.items():
            component.update(
                contributes=False,
                hint="custom fingerprint takes precedence",
            )
            rv[key] = ComponentVariant(component, context.config)

        fingerprint = resolve_fingerprint_values(fingerprint, event.data)
        if fingerprint_info.get("matched_rule", {}).get("is_builtin") is True:
            rv["built_in_fingerprint"] = BuiltInFingerprintVariant(fingerprint, fingerprint_info)
        else:
            rv["custom_fingerprint"] = CustomFingerprintVariant(fingerprint, fingerprint_info)

    # If only the default is referenced, we can use the variants as is
    elif defaults_referenced == 1 and len(fingerprint) == 1:
        rv = {}
        for key, component in components.items():
            rv[key] = ComponentVariant(component, context.config)

    # Otherwise we need to "salt" our variants with the custom fingerprint value(s)
    else:
        rv = {}
        fingerprint = resolve_fingerprint_values(fingerprint, event.data)
        for key, component in components.items():
            rv[key] = SaltedComponentVariant(
                fingerprint, component, context.config, fingerprint_info
            )

    # Ensure we have a fallback hash if nothing else works out
    if not any(x.contributes for x in rv.values()):
        rv["fallback"] = FallbackVariant()

    return rv
