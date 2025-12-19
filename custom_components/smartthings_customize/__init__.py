"""Support for SmartThings Cloud."""
from __future__ import annotations

import asyncio
from collections.abc import Iterable
from http import HTTPStatus
import importlib
import logging
import homeassistant

from aiohttp.client_exceptions import ClientConnectionError, ClientResponseError
from .pysmartapp.event import EVENT_TYPE_DEVICE
from .pysmartthings import Attribute, Capability, SmartThings

from homeassistant.config_entries import SOURCE_IMPORT, ConfigEntry
from homeassistant.const import CONF_ACCESS_TOKEN, CONF_CLIENT_ID, CONF_CLIENT_SECRET, SERVICE_RELOAD
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import config_entry_oauth2_flow
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.dispatcher import (
    async_dispatcher_connect,
    async_dispatcher_send,
)
from homeassistant.helpers.entity import DeviceInfo, Entity
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.typing import ConfigType
from homeassistant.helpers.entity import async_generate_entity_id
from homeassistant.loader import async_get_loaded_integration
from homeassistant.setup import SetupPhases, async_pause_setup

from .common import *
from .device import SmartThings_custom

from homeassistant import config_entries, core

from .custom_api import async_get_app_info, async_remove_app_info

import yaml

from .config_flow import SmartThingsFlowHandler  # noqa: F401
from .const import (
    CONF_ACCESS_TOKEN,
    CONF_APP_ID,
    CONF_CLIENT_ID,
    CONF_CLIENT_SECRET,
    CONF_ENABLE_SYNTAX_PROPERTY,
    CONF_INSTALLED_APP_ID,
    CONF_LOCATION_ID,
    CONF_MANUAL_MODE,
    CONF_POLLING_INTERVAL,
    CONF_REFRESH_TOKEN,
    CONF_RESETTING_ENTITIES,
    CONF_USE_POLLING,
    CONF_USE_WEBHOOK,
    DATA_BROKERS,
    DATA_MANAGER,
    DOMAIN,
    EVENT_BUTTON,
    PLATFORMS,
    SIGNAL_SMARTTHINGS_UPDATE,
    TOKEN_REFRESH_INTERVAL,
)
from .smartapp import (
    format_unique_id,
    setup_smartapp,
    setup_smartapp_endpoint,
    smartapp_sync_subscriptions,
    unload_smartapp_endpoint,
    validate_installed_app,
    validate_webhook_requirements,
)

from homeassistant.helpers import (
    device_registry as dr,
    entity_platform,
    entity_registry as er,
)

_LOGGER = logging.getLogger(__name__)

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

ENTITY_ID_FORMAT = DOMAIN + ".{}"

 

async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Initialize the SmartThings platform."""
    # Check if any entry needs webhook (not in OAuth2-only mode)
    # OAuth2-only entries don't have app_id and don't need webhooks
    entries = hass.config_entries.async_entries(DOMAIN)
    needs_webhook = any(
        CONF_APP_ID in entry.data for entry in entries
    )

    # Only setup webhook endpoint if needed
    if needs_webhook:
        await setup_smartapp_endpoint(hass, False)

    # async def _handle_reload(service):
    #     """Handle reload service call."""

    #     current_entries = hass.config_entries.async_entries(DOMAIN)

    #     reload_tasks = [
    #         hass.config_entries.async_reload(entry.entry_id)
    #         for entry in current_entries
    #     ]

    #     await asyncio.gather(*reload_tasks)

        # entries = hass.config_entries.async_entries(DOMAIN)
        # for entry in entries:
        #     _LOGGER.error("_handle_reload entry id : " + str(entry.entry_id))
        #     config_entry = hass.config_entries.async_get_entry(entry.entry_id)
        #     _LOGGER.error("config entry : " + str(config_entry))
        #     await update_listener(hass, config_entry)

    # hass.helpers.service.async_register_admin_service(
    #     DOMAIN,
    #     SERVICE_RELOAD,
    #     _handle_reload,
    # )

    return True


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Handle migration of a previous version config entry.

    A config entry created under a previous version must go through the
    integration setup again so we can properly retrieve the needed data
    elements. Force this by removing the entry and triggering a new flow.
    """
    # Remove the entry which will invoke the callback to delete the app.
    hass.async_create_task(hass.config_entries.async_remove(entry.entry_id))
    # only create new flow if there isn't a pending one for SmartThings.
    if not hass.config_entries.flow.async_progress_by_handler(DOMAIN):
        hass.async_create_task(
            hass.config_entries.flow.async_init(
                DOMAIN, context={"source": SOURCE_IMPORT}
            )
        )

    # Return False because it could not be migrated.
    return False

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Initialize config entry which represents an installed SmartApp."""
    entry.update_listeners.clear()

    # Check if manual mode or OAuth2 mode
    manual_mode = entry.data.get(CONF_MANUAL_MODE, False)

    # Detect OAuth2 mode: not manual mode and no app_id (means using OAuth2 cloud auth)
    oauth2_mode = not manual_mode and CONF_APP_ID not in entry.data

    # Get options for webhook and polling
    use_webhook = entry.options.get(CONF_USE_WEBHOOK, True)  # Default to webhook for OAuth2
    use_polling = entry.options.get(CONF_USE_POLLING, manual_mode or not use_webhook)
    polling_interval = entry.options.get(CONF_POLLING_INTERVAL, 30)  # Default 30 seconds

    # Get access token based on mode
    if manual_mode:
        # Manual mode: use access token directly
        access_token = entry.data[CONF_ACCESS_TOKEN]
    else:
        # OAuth2 mode: use OAuth2 session for token management
        try:
            implementation = await config_entry_oauth2_flow.async_get_config_entry_implementation(
                hass, entry
            )
        except (ValueError, KeyError) as err:
            _LOGGER.error("OAuth2 implementation not found or auth_implementation missing: %s", err)
            raise ConfigEntryAuthFailed from err

        oauth_session = config_entry_oauth2_flow.OAuth2Session(hass, entry, implementation)

        try:
            await oauth_session.async_ensure_token_valid()
        except Exception as err:
            _LOGGER.error("Token refresh failed: %s", err)
            raise ConfigEntryAuthFailed from err

        access_token = oauth_session.token[CONF_ACCESS_TOKEN]

    # For backwards compat
    if entry.unique_id is None and not manual_mode:
        hass.config_entries.async_update_entry(
            entry,
            unique_id=format_unique_id(
                entry.data.get(CONF_APP_ID, "unknown"), entry.data[CONF_LOCATION_ID]
            ),
        )

    # Skip webhook validation if not using webhook
    if use_webhook and not manual_mode and not validate_webhook_requirements(hass):
        _LOGGER.warning(
            "The 'base_url' of the 'http' integration must be configured and start with"
            " 'https://'. Webhook is disabled, using polling instead."
        )
        use_webhook = False
        use_polling = True

    api = SmartThings_custom(async_get_clientsession(hass), access_token)

    # Ensure platform modules are loaded since the DeviceBroker will
    # import them below and we want them to be cached ahead of time
    # so the integration does not do blocking I/O in the event loop
    # to import the modules.
    await async_get_loaded_integration(hass, DOMAIN).async_get_platforms(PLATFORMS)

    settings = SettingManager()
    settings.init(hass, await api.location(entry.data[CONF_LOCATION_ID]))
    await hass.async_add_executor_job(settings.load_setting)
    SettingManager().set_options(entry.options)

    remove_entry = False

    # Setup SmartApp (only for manual mode with app_id)
    smart_app = None
    if manual_mode and CONF_APP_ID in entry.data:
        app = await async_get_app_info(hass, entry.data[CONF_APP_ID], access_token)

        try:
            # See if the app is already setup
            manager = hass.data[DOMAIN][DATA_MANAGER]
            smart_app = manager.smartapps.get(entry.data[CONF_APP_ID])
            if not smart_app:
                smart_app = setup_smartapp(hass, app)
        except Exception:
            _LOGGER.warning("Failed to setup smartapp, continuing without it")

    try:
        # Validate and retrieve the installed app
        installed_app = await validate_installed_app(
            api, entry.data[CONF_INSTALLED_APP_ID]
        )

        # Get scenes
        scenes = await async_get_entry_scenes(entry, api)

        # Token refresh for manual mode only
        if manual_mode and CONF_CLIENT_ID in entry.data:
            try:
                token = await api.generate_tokens(
                    entry.data[CONF_CLIENT_ID],
                    entry.data[CONF_CLIENT_SECRET],
                    entry.data[CONF_REFRESH_TOKEN],
                )
                hass.config_entries.async_update_entry(
                    entry, data={**entry.data,
                    CONF_ACCESS_TOKEN: token.access_token,
                    CONF_REFRESH_TOKEN: token.refresh_token
                    }
                )
                access_token = token.access_token
                api = SmartThings_custom(async_get_clientsession(hass), access_token)
            except Exception:
                _LOGGER.warning("Token refresh failed in manual mode, continuing with existing token")

        # Get devices and their current status
        devices = await api.devices(location_ids=[installed_app.location_id])

        async def retrieve_device_status(device):
            try:
                await device.status.refresh()
            except ClientResponseError:
                _LOGGER.debug(
                    (
                        "Unable to update status for device: %s (%s), the device will"
                        " be excluded"
                    ),
                    device.label,
                    device.device_id,
                    exc_info=True,
                )
                devices.remove(device)

        await asyncio.gather(*(retrieve_device_status(d) for d in devices.copy()))

        # Sync device subscriptions (only if webhook is enabled)
        if use_webhook and not manual_mode:
            await smartapp_sync_subscriptions(
                hass,
                token.access_token,
                installed_app.location_id,
                installed_app.installed_app_id,
                devices,
            )
            _LOGGER.info(
                "Webhook enabled for app '%s' - device events will be pushed in real-time",
                installed_app.installed_app_id,
            )
        else:
            if manual_mode:
                _LOGGER.info(
                    "Manual mode enabled - using polling (interval: %s seconds) for app '%s'",
                    polling_interval,
                    installed_app.installed_app_id,
                )
            else:
                _LOGGER.info(
                    "Webhook disabled - using API polling (interval: %s seconds) for app '%s'",
                    polling_interval,
                    installed_app.installed_app_id,
                )

        # Setup device broker
        #broker = DeviceBroker(hass, entry, token, smart_app, devices, scenes)
        with async_pause_setup(hass, SetupPhases.WAIT_IMPORT_PLATFORMS):
            # DeviceBroker has a side effect of importing platform
            # modules when its created. In the future this should be
            # refactored to not do this.
            broker = await hass.async_add_import_executor_job(
                DeviceBroker, hass, entry, token, smart_app, devices, scenes, use_polling, polling_interval, api
            )
        broker.connect()
        hass.data[DOMAIN][DATA_BROKERS][entry.entry_id] = broker

    except ClientResponseError as ex:
        if ex.status in (HTTPStatus.UNAUTHORIZED, HTTPStatus.FORBIDDEN):
            _LOGGER.exception(
                (
                    "Unable to setup configuration entry '%s' - please reconfigure the"
                    " integration"
                ),
                entry.title,
            )
            remove_entry = True
        else:
            _LOGGER.debug(ex, exc_info=True)
            raise ConfigEntryNotReady from ex
    except (ClientConnectionError, RuntimeWarning) as ex:
        _LOGGER.debug(ex, exc_info=True)
        raise ConfigEntryNotReady from ex

    if remove_entry:
        hass.async_create_task(hass.config_entries.async_remove(entry.entry_id))
        # only create new flow if there isn't a pending one for SmartThings.
        if not hass.config_entries.flow.async_progress_by_handler(DOMAIN):
            hass.async_create_task(
                hass.config_entries.flow.async_init(
                    DOMAIN, context={"source": SOURCE_IMPORT}
                )
            )
        return False

    if SettingManager.resetting_entities():
        entity_registry = homeassistant.helpers.entity_registry.async_get(
                hass)
        entities = homeassistant.helpers.entity_registry.async_entries_for_config_entry(
            entity_registry, entry.entry_id)
        for e in entities:
            entity_registry.async_remove(e.entity_id)

        device_registry = homeassistant.helpers.device_registry.async_get(hass)
        devices = homeassistant.helpers.device_registry.async_entries_for_config_entry(
        device_registry, entry.entry_id)
        for d in devices:
            device_registry.async_update_device(d.id, remove_config_entry_id=entry.entry_id)

    hass.config_entries.async_update_entry(
            entry,
            options={
                    #CONF_ENABLE_DEFAULT_ENTITIES:entry.options.get(CONF_ENABLE_DEFAULT_ENTITIES, False),
                    CONF_ENABLE_SYNTAX_PROPERTY:entry.options.get(CONF_ENABLE_SYNTAX_PROPERTY, False),
                    CONF_RESETTING_ENTITIES:False,
                    }
            )

        # if DOMAIN in list(d.identifiers)[0]:
        #     _LOGGER.debug("remove device, identifiers" + str(d.identifiers))
        #     device_registry.async_remove_device(d.id)

    entry.add_update_listener(update_listener)

    hass.data[DOMAIN]["listener"] = []
    #PLATFORMS.different_update(SettingManager.ignore_platforms())
    _LOGGER.debug("enable platforms : " + str(SettingManager().get_enable_platforms()))
    await hass.config_entries.async_forward_entry_setups(entry, SettingManager().get_enable_platforms())
    return True

async def update_listener(
    hass: core.HomeAssistant, config_entry: config_entries.ConfigEntry
):
    """Handle options update."""
    config_entry.update_listeners.clear()
    await hass.config_entries.async_reload(config_entry.entry_id)


async def async_get_entry_scenes(entry: ConfigEntry, api):
    """Get the scenes within an integration."""
    try:
        return await api.scenes(location_id=entry.data[CONF_LOCATION_ID])
    except ClientResponseError as ex:
        if ex.status == HTTPStatus.FORBIDDEN:
            _LOGGER.exception(
                (
                    "Unable to load scenes for configuration entry '%s' because the"
                    " access token does not have the required access"
                ),
                entry.title,
            )
        else:
            raise
    return []


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    for listener in hass.data[DOMAIN]["listener"]:
        listener()

    broker = hass.data[DOMAIN][DATA_BROKERS].pop(entry.entry_id, None)
    if broker:
        broker.disconnect()

    return await hass.config_entries.async_unload_platforms(entry, SettingManager().get_enable_platforms())


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Perform clean-up when entry is being removed."""
    api = SmartThings(async_get_clientsession(hass), entry.data[CONF_ACCESS_TOKEN])

    # Remove the installed_app, which if already removed raises a HTTPStatus.FORBIDDEN error.
    installed_app_id = entry.data[CONF_INSTALLED_APP_ID]
    try:
        await api.delete_installed_app(installed_app_id)
    except ClientResponseError as ex:
        if ex.status == HTTPStatus.FORBIDDEN:
            _LOGGER.debug(
                "Installed app %s has already been removed",
                installed_app_id,
                exc_info=True,
            )
        else:
            raise
    _LOGGER.debug("Removed installed app %s", installed_app_id)

    # Remove the app if not referenced by other entries, which if already
    # removed raises a HTTPStatus.FORBIDDEN error.
    all_entries = hass.config_entries.async_entries(DOMAIN)
    app_id = entry.data[CONF_APP_ID]
    app_count = sum(1 for entry in all_entries if entry.data[CONF_APP_ID] == app_id)
    if app_count > 1:
        _LOGGER.debug(
            (
                "App %s was not removed because it is in use by other configuration"
                " entries"
            ),
            app_id,
        )
        return
    # Remove the app
    try:
        await api.delete_app(app_id)
    except ClientResponseError as ex:
        if ex.status == HTTPStatus.FORBIDDEN:
            _LOGGER.debug("App %s has already been removed", app_id, exc_info=True)
        else:
            raise
    _LOGGER.debug("Removed app %s", app_id)

    if len(all_entries) == 1:
        await unload_smartapp_endpoint(hass)

class DeviceBroker:
    """Manages an individual SmartThings config entry."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        token,
        smart_app,
        devices: Iterable,
        scenes: Iterable,
        use_polling: bool = False,
        polling_interval: int = 30,
        api = None,
    ) -> None:
        """Create a new instance of the DeviceBroker."""
        self._hass = hass
        self._entry = entry
        self._installed_app_id = entry.data[CONF_INSTALLED_APP_ID]
        self._smart_app = smart_app
        self._token = token
        self._event_disconnect = None
        self._regenerate_token_remove = None
        self._polling_remove = None
        self._use_polling = use_polling
        self._polling_interval = polling_interval
        self._api = api
        self._assignments = self._assign_capabilities(devices)
        self.devices = {device.device_id: device for device in devices}
        self.scenes = {scene.scene_id: scene for scene in scenes}
        self._created_entities = []

    def add_valid_entity(self, entity_id):
        self._created_entities.append(entity_id)

    def is_valid_entity(self, entity_id):
        return entity_id in self._created_entities

    def build_capability(self, device) -> dict:
        capabilities = {}
        capabilities["main"] = device.capabilities
        for key, value in device.components.items():
            capabilities[key] = value
        return capabilities

    def _assign_capabilities(self, devices: Iterable):
        """Assign platforms to capabilities."""
        assignments = {}
        for device in devices:
            capabilities = device.capabilities.copy()
            slots = {}
            for platform in PLATFORMS:
                platform_module = importlib.import_module(
                    f".{platform}", self.__module__
                )
                if not hasattr(platform_module, "get_capabilities"):
                    continue
                assigned = platform_module.get_capabilities(capabilities)
                if not assigned:
                    continue
                # Draw-down capabilities and set slot assignment
                for capability in assigned:
                    if capability not in capabilities:
                        continue
                    capabilities.remove(capability)
                    slots[capability] = platform
            assignments[device.device_id] = slots
        return assignments

    def connect(self):
        """Connect handlers/listeners for device/lifecycle events."""

        if self._use_polling:
            # Use API polling instead of webhooks
            from datetime import timedelta

            async def poll_device_status(now):
                """Poll device status from SmartThings API."""
                try:
                    # Get the location ID from the entry
                    location_id = self._entry.data.get(CONF_LOCATION_ID)
                    if not location_id:
                        _LOGGER.error("No location_id found for polling")
                        return

                    # Fetch updated device list
                    devices = await self._api.devices(location_ids=[location_id])

                    # Update device statuses
                    updated_devices = set()
                    for device in devices:
                        if device.device_id in self.devices:
                            try:
                                await device.status.refresh()
                                # Copy the updated status to our device
                                self.devices[device.device_id].status = device.status
                                updated_devices.add(device.device_id)
                            except Exception as err:
                                _LOGGER.debug(
                                    "Error refreshing status for device %s: %s",
                                    device.device_id,
                                    err,
                                )

                    # Send update signal if any devices were updated
                    if updated_devices:
                        async_dispatcher_send(
                            self._hass,
                            SIGNAL_SMARTTHINGS_UPDATE,
                            updated_devices,
                            None  # No event object in polling mode
                        )
                        _LOGGER.debug(
                            "Polled and updated %s devices for app '%s'",
                            len(updated_devices),
                            self._installed_app_id,
                        )

                except Exception as err:
                    _LOGGER.error(
                        "Error polling device status for app '%s': %s",
                        self._installed_app_id,
                        err,
                    )

            # Poll at configured interval
            self._polling_remove = async_track_time_interval(
                self._hass, poll_device_status, timedelta(seconds=self._polling_interval)
            )
            _LOGGER.info(
                "Started API polling (interval: %s seconds) for app '%s'",
                self._polling_interval,
                self._installed_app_id,
            )
        else:
            # Webhook mode: use traditional event-based updates
            # Setup interval to regenerate the refresh token on a periodic basis.
            # Tokens expire in 30 days and once expired, cannot be recovered.
            async def regenerate_refresh_token(now):
                """Generate a new refresh token and update the config entry."""
                await self._token.refresh(
                    self._entry.data[CONF_CLIENT_ID],
                    self._entry.data[CONF_CLIENT_SECRET],
                )
                self._hass.config_entries.async_update_entry(
                    self._entry,
                    data={
                        **self._entry.data,
                        CONF_ACCESS_TOKEN: self._token.access_token,
                        CONF_REFRESH_TOKEN: self._token.refresh_token,
                    },
                )
                for id, device in self.devices.items():
                    device.status._api._token = self._token.access_token

                _LOGGER.debug(
                    "Regenerated refresh token for installed app: %s",
                    self._installed_app_id,
                )

            self._regenerate_token_remove = async_track_time_interval(
                self._hass, regenerate_refresh_token, TOKEN_REFRESH_INTERVAL
            )

            # Connect handler to incoming device events (if smart_app is available)
            if self._smart_app:
                self._event_disconnect = self._smart_app.connect_event(self._event_handler)

    def disconnect(self):
        """Disconnects handlers/listeners for device/lifecycle events."""
        if self._regenerate_token_remove:
            self._regenerate_token_remove()
        if self._event_disconnect:
            self._event_disconnect()
        if self._polling_remove:
            self._polling_remove()

    def get_assigned(self, device_id: str, platform: str):
        """Get the capabilities assigned to the platform."""
        slots = self._assignments.get(device_id, {})
        return [key for key, value in slots.items() if value == platform]

    def any_assigned(self, device_id: str, platform: str):
        """Return True if the platform has any assigned capabilities."""
        slots = self._assignments.get(device_id, {})
        return any(value for value in slots.values() if value == platform)

    async def _event_handler(self, req, resp, app):
        """Broker for incoming events."""
        # Do not process events received from a different installed app
        # under the same parent SmartApp (valid use-scenario)
        if req.installed_app_id != self._installed_app_id:
            return

        updated_devices = set()
        for evt in req.events:
            if evt.event_type != EVENT_TYPE_DEVICE:
                continue
            if not (device := self.devices.get(evt.device_id)):
                continue
            device.status.apply_attribute_update(
                evt.component_id,
                evt.capability,
                evt.attribute,
                evt.value,
                data=evt.data,
            )

            # Fire events for buttons
            if (
                evt.capability == Capability.button
                and evt.attribute == Attribute.button
            ):
                data = {
                    "component_id": evt.component_id,
                    "device_id": evt.device_id,
                    "location_id": evt.location_id,
                    "value": evt.value,
                    "name": device.label,
                    "data": evt.data,
                }
                self._hass.bus.async_fire(EVENT_BUTTON, data)
                _LOGGER.debug("Fired button event: %s", data)
            else:
                data = {
                    "location_id": evt.location_id,
                    "device_id": evt.device_id,
                    "component_id": evt.component_id,
                    "capability": evt.capability,
                    "attribute": evt.attribute,
                    "value": evt.value,
                    "data": evt.data,
                }
                _LOGGER.debug("Push update received: %s", data)

            updated_devices.add(device.device_id)

        async_dispatcher_send(self._hass, SIGNAL_SMARTTHINGS_UPDATE, updated_devices, evt)
