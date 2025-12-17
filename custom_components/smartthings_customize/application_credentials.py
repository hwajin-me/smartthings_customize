"""Application credentials platform for SmartThings."""

from homeassistant.components.application_credentials import AuthorizationServer
from homeassistant.core import HomeAssistant


async def async_get_authorization_server(hass: HomeAssistant) -> AuthorizationServer:
    """Return authorization server."""
    return AuthorizationServer(
        authorize_url="https://api.smartthings.com/oauth/authorize",
        token_url="https://auth-global.api.smartthings.com/oauth/token",
    )


async def async_get_description_placeholders(hass: HomeAssistant) -> dict[str, str]:
    """Return description placeholders for the credentials dialog."""
    return {
        "more_info_url": "https://developer.smartthings.com/",
        "oauth_url": "https://account.smartthings.com/tokens",
    }
