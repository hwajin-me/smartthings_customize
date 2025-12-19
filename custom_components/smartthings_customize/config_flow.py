"""Config flow to configure SmartThings."""

from collections.abc import Mapping
from http import HTTPStatus
import logging
from typing import Any

from aiohttp import ClientResponseError
from .pysmartthings import APIResponseError, SmartThings
import voluptuous as vol

from homeassistant.config_entries import (
    SOURCE_REAUTH,
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_ACCESS_TOKEN
from homeassistant.helpers import config_entry_oauth2_flow
from homeassistant.helpers.aiohttp_client import async_get_clientsession
import homeassistant.helpers.config_validation as cv
from homeassistant.core import callback

from .const import (
    CONF_INSTALLED_APP_ID,
    CONF_LOCATION_ID,
    CONF_MANUAL_MODE,
    CONF_USE_POLLING,
    CONF_POLLING_INTERVAL,
    CONF_USE_WEBHOOK,
    DOMAIN,
    REQUESTED_SCOPES,
    CONF_RESETTING_ENTITIES,
    CONF_ENABLE_SYNTAX_PROPERTY,
)
from .smartapp import (
    format_unique_id,
    setup_smartapp_endpoint,
    validate_webhook_requirements,
)

_LOGGER = logging.getLogger(__name__)


class SmartThingsFlowHandler(
    config_entry_oauth2_flow.AbstractOAuth2FlowHandler, domain=DOMAIN
):
    """Handle configuration of SmartThings integrations."""

    VERSION = 2
    DOMAIN = DOMAIN

    location_id: str
    manual_mode: bool = False

    def __init__(self) -> None:
        """Create a new instance of the flow handler."""
        super().__init__()
        self.endpoints_initialized = False

    @property
    def logger(self) -> logging.Logger:
        """Return logger."""
        return _LOGGER

    @property
    def extra_authorize_data(self) -> dict[str, Any]:
        """Extra data that needs to be appended to the authorize url."""
        return {"scope": " ".join(REQUESTED_SCOPES)}

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle a flow initialized by the user."""
        if not self.endpoints_initialized:
            self.endpoints_initialized = True
            await setup_smartapp_endpoint(
                self.hass, len(self._async_current_entries()) == 0
            )

        # Check if webhook is available for automatic mode
        has_webhook = validate_webhook_requirements(self.hass)

        # Check OAuth availability
        has_cloud = "cloud" in self.hass.config.components
        has_my = "my" in self.hass.config.components
        has_external_url = bool(self.hass.config.external_url)

        # OAuth is available if either:
        # 1. Cloud is available (uses my.home-assistant.io)
        # 2. external_url is configured and my integration is disabled
        oauth_available = has_cloud or (has_external_url and not has_my)

        if user_input is not None:
            if user_input.get("auth_method") == "manual":
                self.manual_mode = True
                return await self.async_step_manual_auth()

            # Check if OAuth is possible before proceeding
            if not oauth_available:
                return self.async_abort(
                    reason="oauth_not_available",
                    description_placeholders={
                        "error": "OAuth requires either Home Assistant Cloud or external_url configuration. Please disable 'my' integration if using external_url, or use Manual authentication."
                    }
                )

            # Continue with OAuth2 flow
            return await self.async_step_pick_implementation()

        # Determine OAuth status message
        if has_cloud:
            oauth_status = "OAuth2 (Available via Cloud)"
        elif has_external_url and not has_my:
            oauth_status = "OAuth2 (Available via external_url)"
        elif has_external_url and has_my:
            oauth_status = "OAuth2 (Unavailable: disable 'my' integration)"
        else:
            oauth_status = "OAuth2 (Unavailable: configure external_url)"

        # Show choice between OAuth2 and manual
        data_schema = vol.Schema(
            {
                vol.Required("auth_method", default="oauth2" if oauth_available else "manual"): vol.In(
                    {
                        "oauth2": oauth_status,
                        "manual": "Manual (for restricted networks)",
                    }
                )
            }
        )

        # Prepare description placeholders
        placeholders = {
            "webhook_available": "Yes" if has_webhook else "No (Manual mode required)",
        }

        # Add OAuth setup instructions if not available
        if not oauth_available:
            if not has_external_url:
                placeholders["oauth_help"] = (
                    "To use OAuth2, configure external_url in configuration.yaml:\n"
                    "homeassistant:\n"
                    "  external_url: 'https://your-domain.com'\n"
                    "Then register redirect URI: https://your-domain.com/auth/external/callback"
                )
            elif has_my:
                placeholders["oauth_help"] = (
                    "To use local OAuth2, disable the 'my' integration.\n"
                    "Then register redirect URI: https://your-domain.com/auth/external/callback"
                )
        else:
            if has_cloud:
                placeholders["oauth_help"] = (
                    "Using OAuth2 via Home Assistant Cloud.\n"
                    "Register redirect URI: https://my.home-assistant.io/redirect/oauth"
                )
            else:
                placeholders["oauth_help"] = (
                    "Using OAuth2 via external_url.\n"
                    f"Register redirect URI: {self.hass.config.external_url}/auth/external/callback"
                )

        return self.async_show_form(
            step_id="user",
            data_schema=data_schema,
            description_placeholders=placeholders,
        )

    async def async_step_manual_auth(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle manual authentication with installed app credentials."""
        errors: dict[str, str] = {}

        if user_input is not None:
            # Validate the installed app credentials
            try:
                installed_app_id = user_input[CONF_INSTALLED_APP_ID]
                access_token = user_input[CONF_ACCESS_TOKEN]

                api = SmartThings(async_get_clientsession(self.hass), access_token)

                # Validate installed app
                from .smartapp import validate_installed_app
                installed_app = await validate_installed_app(api, installed_app_id)

                self.location_id = installed_app.location_id

                # Get location name for title
                location = await api.location(self.location_id)

                # Create entry with manual mode enabled
                return self.async_create_entry(
                    title=location.name,
                    data={
                        CONF_INSTALLED_APP_ID: installed_app_id,
                        CONF_ACCESS_TOKEN: access_token,
                        CONF_LOCATION_ID: self.location_id,
                        CONF_MANUAL_MODE: True,
                        CONF_USE_POLLING: True,
                    },
                )

            except ClientResponseError as ex:
                if ex.status in (HTTPStatus.UNAUTHORIZED, HTTPStatus.FORBIDDEN):
                    errors["base"] = "invalid_access_token"
                else:
                    errors["base"] = "cannot_connect"
                _LOGGER.exception("Error validating installed app")
            except Exception as ex:
                _LOGGER.exception("Unexpected error during manual auth")
                errors["base"] = "unknown"

        return self.async_show_form(
            step_id="manual_auth",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_INSTALLED_APP_ID): str,
                    vol.Required(CONF_ACCESS_TOKEN): str,
                }
            ),
            errors=errors,
            description_placeholders={
                "manual_instructions": (
                    "1. Create a SmartThings app at https://developer.smartthings.com/\n"
                    "2. Install the app in your SmartThings location\n"
                    "3. Get your installed_app_id and access_token\n"
                    "4. Enter them below"
                ),
            },
        )

    async def async_oauth_create_entry(self, data: dict[str, Any]) -> ConfigFlowResult:
        """Create an entry for the flow, or update existing entry."""
        token = data["token"]

        # Validate token has required scopes
        if not set(token.get("scope", "").split()) >= set(REQUESTED_SCOPES):
            return self.async_abort(
                reason="missing_scopes",
                description_placeholders={"scope": ", ".join(REQUESTED_SCOPES)},
            )

        # Get installed app ID from token
        installed_app_id = token.get(CONF_INSTALLED_APP_ID)
        if not installed_app_id:
            return self.async_abort(reason="missing_installed_app_id")

        # Get location for this installed app
        api = SmartThings(
            async_get_clientsession(self.hass), token[CONF_ACCESS_TOKEN]
        )

        try:
            installed_app = await api.installed_app(installed_app_id)
            self.location_id = installed_app.location_id

            # Check for existing entry with same location
            await self.async_set_unique_id(
                format_unique_id(installed_app.app_id, self.location_id)
            )
            self._abort_if_unique_id_configured()

            # Get location name
            location = await api.location(self.location_id)

            return self.async_create_entry(
                title=location.name,
                data={
                    **data,
                    CONF_INSTALLED_APP_ID: installed_app_id,
                    CONF_LOCATION_ID: self.location_id,
                    CONF_MANUAL_MODE: False,
                    CONF_USE_POLLING: False,  # Use webhook for OAuth2
                },
            )

        except ClientResponseError as ex:
            if ex.status in (HTTPStatus.UNAUTHORIZED, HTTPStatus.FORBIDDEN):
                return self.async_abort(reason="oauth_error")
            raise

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Handle reauthorization request."""
        entry = self.hass.config_entries.async_get_entry(self.context["entry_id"])

        if entry and entry.data.get(CONF_MANUAL_MODE):
            # Manual mode reauth
            return await self.async_step_reauth_manual()

        # OAuth2 reauth
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm reauth dialog."""
        if user_input is None:
            return self.async_show_form(step_id="reauth_confirm")
        return await self.async_step_user()

    async def async_step_reauth_manual(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle manual reauth."""
        errors: dict[str, str] = {}
        entry = self.hass.config_entries.async_get_entry(self.context["entry_id"])

        if user_input is not None:
            try:
                access_token = user_input[CONF_ACCESS_TOKEN]
                api = SmartThings(async_get_clientsession(self.hass), access_token)

                # Validate the token
                installed_app_id = entry.data[CONF_INSTALLED_APP_ID]
                await api.installed_app(installed_app_id)

                # Update entry with new token
                return self.async_update_reload_and_abort(
                    entry,
                    data_updates={CONF_ACCESS_TOKEN: access_token},
                )

            except ClientResponseError:
                errors["base"] = "invalid_access_token"
            except Exception:
                _LOGGER.exception("Unexpected error during manual reauth")
                errors["base"] = "unknown"

        return self.async_show_form(
            step_id="reauth_manual",
            data_schema=vol.Schema({vol.Required(CONF_ACCESS_TOKEN): str}),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Handle a option flow."""
        return OptionsFlowHandler(config_entry)


class OptionsFlowHandler(OptionsFlow):
    """Handles options flow for the component."""

    def __init__(self, config_entry: ConfigEntry) -> None:
        """Initialize options flow."""
        self.config_entry = config_entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle options flow."""
        if user_input is not None:
            return self.async_create_entry(data=user_input)

        options = self.config_entry.options
        data = self.config_entry.data

        # Check if this is manual mode or OAuth2 mode
        manual_mode = data.get(CONF_MANUAL_MODE, False)
        oauth2_mode = not manual_mode and "app_id" not in data

        # Build schema based on mode
        schema_dict = {
            vol.Optional(
                CONF_ENABLE_SYNTAX_PROPERTY,
                default=options.get(CONF_ENABLE_SYNTAX_PROPERTY, False),
            ): cv.boolean,
            vol.Optional(
                CONF_RESETTING_ENTITIES,
                default=options.get(CONF_RESETTING_ENTITIES, False),
            ): cv.boolean,
        }

        # Add webhook option for OAuth2 mode
        if oauth2_mode:
            schema_dict[vol.Optional(
                CONF_USE_WEBHOOK,
                default=options.get(CONF_USE_WEBHOOK, True),
            )] = cv.boolean

        # Add polling options (for all modes)
        schema_dict[vol.Optional(
            CONF_USE_POLLING,
            default=options.get(CONF_USE_POLLING, manual_mode or not oauth2_mode),
        )] = cv.boolean

        schema_dict[vol.Optional(
            CONF_POLLING_INTERVAL,
            default=options.get(CONF_POLLING_INTERVAL, 30),
        )] = vol.All(vol.Coerce(int), vol.Range(min=5, max=300))

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(schema_dict),
            description_placeholders={
                "mode": "OAuth2" if oauth2_mode else ("Manual" if manual_mode else "Webhook"),
                "polling_info": "Polling interval in seconds (5-300). Lower values mean more frequent updates but higher API usage.",
                "webhook_info": "Enable webhook for real-time updates (requires HTTPS). If disabled, polling will be used.",
            },
        )
