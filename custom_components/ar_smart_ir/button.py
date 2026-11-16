"""Custom command buttons.

Each custom command of an entry (Options > Custom commands) is a button of
the entry's device. Pressing it sends the command's code through the entry's
controller, repeated like any other command of the device: its own count when
it is listed under Repeat commands, else the device's count, with the
device's delay between sends.
"""

from __future__ import annotations

import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.helpers import entity_registry as er

from .const import (
    BUTTON_PLATFORM,
    CONF_CONTROLLER,
    CONF_CONTROLLER_DATA,
    CONF_CUSTOM_COMMANDS,
    CONF_DEFAULT_REPEAT_COUNT,
    CONF_DELAY,
    CONF_DEVICE_CODE,
    CONF_PLATFORM,
    DEFAULT_DELAY,
    DEFAULT_REPEAT_COUNT,
)
from .controller import get_controller
from .helpers import (
    async_load_device_data,
    custom_command_slug,
    device_info_for,
    parse_custom_commands,
)

_LOGGER = logging.getLogger(__name__)


def custom_button_unique_id(entry_unique_id: str, name: str) -> str:
    return f"{entry_unique_id}_custom_{custom_command_slug(name)}"


async def async_setup_entry(hass, entry, async_add_entities):
    config = {**entry.data, **entry.options}
    entry_unique_id = config.get("unique_id") or entry.entry_id
    commands = parse_custom_commands(config.get(CONF_CUSTOM_COMMANDS))

    device_info = None
    try:
        device_data = await async_load_device_data(
            config.get(CONF_DEVICE_CODE), config.get(CONF_PLATFORM)
        )
    except Exception as err:  # noqa: BLE001
        _LOGGER.warning("AR Smart IR: could not read the codeset for device info: %s", err)
    else:
        device_info = device_info_for(
            config.get("unique_id"),
            config.get("name"),
            device_data.get("manufacturer"),
            device_data.get("supportedModels"),
        )

    buttons = [
        CustomCommandButton(hass, config, entry_unique_id, command, device_info)
        for command in commands
        if command.get("code")
    ]

    # A command deleted in the options leaves its button behind in the entity
    # registry, shown as unavailable: remove those.
    wanted = {button.unique_id for button in buttons}
    registry = er.async_get(hass)
    for registry_entry in er.async_entries_for_config_entry(registry, entry.entry_id):
        if (
            registry_entry.domain == BUTTON_PLATFORM
            and registry_entry.unique_id not in wanted
        ):
            registry.async_remove(registry_entry.entity_id)

    async_add_entities(buttons)


class CustomCommandButton(ButtonEntity):
    """Sends one custom command."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:remote"

    def __init__(self, hass, config, entry_unique_id, command, device_info):
        self.hass = hass
        self._command = command
        self._attr_name = command["name"]
        self._attr_unique_id = custom_button_unique_id(entry_unique_id, command["name"])
        if device_info:
            self._attr_device_info = device_info
        self._controller = get_controller(
            hass,
            config.get(CONF_CONTROLLER),
            command.get("encoding", "Base64"),
            config.get(CONF_CONTROLLER_DATA),
            config.get(CONF_DELAY, DEFAULT_DELAY),
            config.get(CONF_DEFAULT_REPEAT_COUNT, DEFAULT_REPEAT_COUNT),
        )

    async def async_press(self) -> None:
        payload = self._command["code"]
        if self._command.get("repeat_count"):
            payload = {"code": payload, "repeat_count": self._command["repeat_count"]}
        await self._controller.send(payload)
