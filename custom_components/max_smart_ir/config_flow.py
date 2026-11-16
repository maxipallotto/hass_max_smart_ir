from __future__ import annotations

import asyncio
import json
import logging
from typing import Any
import uuid

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.const import CONF_NAME
from homeassistant.data_entry_flow import section
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import selector

from .const import (
    CONF_COMMAND_OVERRIDES,
    CONF_CONTROLLER,
    CONF_CONTROLLER_DATA,
    CONF_CONTROLLER_ENTITY,
    CONF_CUSTOM_COMMANDS,
    CONF_DEFAULT_REPEAT_COUNT,
    CONF_DELAY,
    CONF_DEVICE_CLASS,
    CONF_DEVICE_CODE,
    CONF_GO_BACK,
    CONF_HUMIDITY_SENSOR,
    CONF_INFRARED_ENTITY,
    CONF_PLATFORM,
    CONF_PASSIVE_MODE,
    CONF_POWER_SENSOR,
    CONF_POWER_SENSOR_RESTORE_STATE,
    CONF_SOURCE_NAMES,
    CONF_TEMPERATURE_SENSOR,
    CONF_TEMPERATURE_UNIT,
    TEMPERATURE_UNIT_AUTO,
    TEMPERATURE_UNIT_OPTIONS,
    CONF_TEST_COMMAND,
    CONF_TEST_DEVICE,
    DEFAULT_DEVICE_CLASS,
    DEFAULT_DELAY,
    DEFAULT_REPEAT_COUNT,
    DOMAIN,
    PLATFORM_TITLES,
    PLATFORMS,
)
from .helpers import (
    async_load_device_data,
    clear_all_command_repeats,
    command_path_to_key,
    command_repeat_list,
    detect_ir_code_encoding,
    flatten_command_paths,
    get_command_value_at_path,
    infer_title,
    get_manufacturers,
    get_models_for_manufacturer,
    normalize_ir_code,
    parse_custom_commands,
    set_command_repeat_at_path,
)
from .controller import get_controller

_LOGGER = logging.getLogger(__name__)

CONTROLLERS = [
    "Broadlink",
    "LinkNLink",
    "Xiaomi",
    "MQTT",
    "LOOKin",
    "ESPHome",
    "Infrared",
    "Tuya",
    "UFOR11",
]

TEST_COMMAND_PRIORITIES = (
    ("off", "Power off"),
    ("power_off", "Power off"),
    ("power", "Power toggle"),
    ("toggle", "Power toggle"),
    ("on", "Power on"),
    ("power_on", "Power on"),
)


RAW_BASED_CONTROLLERS = {
    "Xiaomi",
    "MQTT",
    "LOOKin",
    "ESPHome",
    "Infrared",
    "Tuya",
    "UFOR11",
}


def _temperature_unit_selector():
    """Let the user force a codeset's temperature unit (issue #33)."""
    return selector.SelectSelector(
        selector.SelectSelectorConfig(
            options=TEMPERATURE_UNIT_OPTIONS,
            mode=selector.SelectSelectorMode.DROPDOWN,
            translation_key=CONF_TEMPERATURE_UNIT,
        )
    )


def _temperature_sensor_selector():
    return selector.EntitySelector(
        selector.EntitySelectorConfig(
            filter=[
                {
                    "domain": "sensor",
                    "device_class": "temperature",
                }
            ],
            multiple=False,
        )
    )


def _humidity_sensor_selector():
    return selector.EntitySelector(
        selector.EntitySelectorConfig(
            filter=[
                {
                    "domain": "sensor",
                    "device_class": "humidity",
                }
            ],
            multiple=False,
        )
    )


def _entity_selector():
    return selector.EntitySelector(
        selector.EntitySelectorConfig(multiple=False)
    )


def _infrared_entity_selector():
    return selector.EntitySelector(
        selector.EntitySelectorConfig(
            filter=[
                {
                    "domain": "infrared",
                }
            ],
            multiple=False,
        )
    )


def _optional_entity_field(config_key: str, data: dict[str, Any]):
    if data.get(config_key):
        return vol.Optional(config_key, default=data.get(config_key))
    return vol.Optional(config_key)


def _optional_entity_key(config_key: str, value: Any):
    # An EntitySelector rejects an empty-string default ("Entity is neither a
    # valid entity ID nor a valid UUID"), so only set a default when we have a
    # real value; otherwise leave the field truly optional/empty.
    if isinstance(value, str):
        value = value.strip()
    if value:
        return vol.Optional(config_key, default=value)
    return vol.Optional(config_key)


# Which kind of target each controller accepts.
#   - REMOTE: a Home Assistant remote.* entity (remote.send_command).
#   - TEXT:   free text — an MQTT "/set" topic, a service name, an IP, or JSON.
# Tuya is in both: a UFO-R11 is a Tuya device you can drive either via a
# remote.* entity OR by publishing to its Zigbee2MQTT topic, so it gets both
# inputs and the user fills whichever applies. Infrared is handled separately
# via its own infrared.* entity field.
REMOTE_TARGET_CONTROLLERS = ("Broadlink", "LinkNLink", "Xiaomi", "Tuya")
TEXT_TARGET_CONTROLLERS = ("MQTT", "LOOKin", "ESPHome", "Tuya", "UFOR11")


def _remote_entity_selector():
    return selector.EntitySelector(
        selector.EntitySelectorConfig(domain="remote")
    )


def _controller_data_field(controller: str):
    # Free-text box: accepts an MQTT "/set" topic, a service name / JSON config
    # (ESPHome), an IP address (LOOKin), or a JSON config (Tuya). An explicit
    # TextSelector is used instead of a bare `str` schema type so the box
    # always renders in the frontend (issue #24).
    return selector.TextSelector(
        selector.TextSelectorConfig(multiline=False)
    )


def _looks_like_remote_entity(value: Any) -> bool:
    return isinstance(value, str) and value.strip().startswith("remote.")


def _add_controller_target_fields(
    schema: dict[Any, Any], controller: str, values: dict[str, Any]
) -> None:
    """Add the controller-data input(s) for ``controller`` into ``schema``.

    Broadlink / LinkNLink / Xiaomi get a remote.* entity picker; MQTT / UFOR11 /
    ESPHome / LOOKin get a text box; Tuya gets both. The two Tuya inputs are
    folded back into a single ``controller_data`` value by
    ``_normalize_controller_target`` on submit. Infrared is handled by the
    caller and never reaches here.
    """
    wants_remote = controller in REMOTE_TARGET_CONTROLLERS
    wants_text = controller in TEXT_TARGET_CONTROLLERS
    if not wants_remote and not wants_text:
        wants_text = True  # safe default for any unknown controller

    entity_default = (values.get(CONF_CONTROLLER_ENTITY) or "").strip()
    data_default = (values.get(CONF_CONTROLLER_DATA) or "").strip()

    # On first load / edit there is only the merged ``controller_data``. If it
    # is actually a remote entity, surface it in the picker rather than the box.
    if wants_remote and not entity_default and _looks_like_remote_entity(data_default):
        entity_default = data_default
        data_default = ""

    if wants_remote:
        schema[
            _optional_entity_key(CONF_CONTROLLER_ENTITY, entity_default)
        ] = _remote_entity_selector()

    if wants_text:
        schema[
            vol.Optional(CONF_CONTROLLER_DATA, default=data_default)
        ] = _controller_data_field(controller)


def _controller_target_conflict(values: dict[str, Any]) -> bool:
    """True when the user filled both the remote entity and the text box."""
    return bool(
        (values.get(CONF_CONTROLLER_ENTITY) or "").strip()
        and (values.get(CONF_CONTROLLER_DATA) or "").strip()
    )


def _normalize_controller_target(data: dict[str, Any]) -> None:
    if data.get(CONF_CONTROLLER) == "Infrared":
        infrared_entity = data.get(CONF_INFRARED_ENTITY) or data.get(CONF_CONTROLLER_DATA)
        data[CONF_INFRARED_ENTITY] = infrared_entity
        data[CONF_CONTROLLER_DATA] = infrared_entity
        return

    # Fold the optional remote-entity picker into the single controller_data
    # value the rest of the integration uses. A genuine both-filled case is
    # blocked earlier by _controller_target_conflict(); text wins as a fallback.
    entity = (data.pop(CONF_CONTROLLER_ENTITY, "") or "").strip()
    text = (data.get(CONF_CONTROLLER_DATA, "") or "").strip()
    data[CONF_CONTROLLER_DATA] = text or entity


def _controller_target_error_key(controller: str) -> str:
    if controller == "Infrared":
        return CONF_INFRARED_ENTITY
    wants_remote = controller in REMOTE_TARGET_CONTROLLERS
    wants_text = controller in TEXT_TARGET_CONTROLLERS
    if wants_remote and wants_text:
        return "base"  # either field satisfies (Tuya)
    if wants_remote:
        return CONF_CONTROLLER_ENTITY
    return CONF_CONTROLLER_DATA


def _source_names_default(value: Any) -> str:
    if not value:
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=True)


def _build_compatibility_message(
    selected_controller: str,
    device_controller: str,
    command_encoding: str,
) -> str:
    # Broadlink and LinkNLink share the exact same base64 wire format, so a
    # code authored for one needs no adaptation for the other.
    broadlink_family = {"Broadlink", "LinkNLink"}
    if selected_controller in broadlink_family and device_controller in broadlink_family:
        return "No test sent yet."

    if selected_controller == "Infrared":
        return (
            "This code will be converted to native Home Assistant infrared raw "
            "timings. Test a command before saving."
        )

    if (
        selected_controller in RAW_BASED_CONTROLLERS
        and device_controller in broadlink_family
        and command_encoding in {"Base64", "Hex", "Pronto"}
    ):
        return (
            "This code was authored for Broadlink and will be converted to raw "
            f"format for {selected_controller}. Test a command before saving."
        )

    if selected_controller != device_controller:
        return (
            f"This code was authored for {device_controller} and will be adapted "
            f"for {selected_controller}. Test a command before saving."
        )

    return "No test sent yet."


# Where a controller sends from, per controller. Each text kind has its own
# options key so the form can label it ("MQTT topic", "LOOKin address", ...);
# all of them are stored as controller_data.
REMOTE_ENTITY_CONTROLLERS = ("Broadlink", "LinkNLink", "Xiaomi")
TEXT_TARGET_FIELDS = {
    "MQTT": "mqtt_topic",
    "UFOR11": "mqtt_topic",
    "LOOKin": "lookin_address",
    "ESPHome": "esphome_service",
    "Tuya": "tuya_target",
}
# Integrations whose remote entity can record IR codes.
LEARNING_PLATFORMS = ("broadlink", "linknlink")
RECORD_TIMEOUT = 30

# Options form sections and the lists inside them.
SECTION_REPEAT = "repeat_commands"
SECTION_CUSTOM = "custom_commands"
SECTION_SENSORS = "sensors"
SECTION_ADVANCED = "advanced"
FIELD_COMMAND_REPEATS = "command_repeats"
FIELD_CUSTOM_LIST = "commands"

# Settings the setup wizard (and Reconfigure) asks for. When Reconfigure saves,
# these are dropped from the options so the values just entered are used.
WIZARD_SETTINGS = (
    CONF_NAME,
    CONF_CONTROLLER,
    CONF_CONTROLLER_DATA,
    CONF_CONTROLLER_ENTITY,
    CONF_INFRARED_ENTITY,
    CONF_DELAY,
    CONF_TEMPERATURE_SENSOR,
    CONF_HUMIDITY_SENSOR,
    CONF_POWER_SENSOR,
    CONF_POWER_SENSOR_RESTORE_STATE,
    CONF_PASSIVE_MODE,
    CONF_TEMPERATURE_UNIT,
    CONF_DEVICE_CLASS,
    CONF_SOURCE_NAMES,
)


def _repeat_count_selector():
    # A box, not a slider: a slider does not show the number it is set to.
    return selector.NumberSelector(
        selector.NumberSelectorConfig(
            min=1,
            max=20,
            step=1,
            mode=selector.NumberSelectorMode.BOX,
        )
    )


def _delay_selector():
    return selector.NumberSelector(
        selector.NumberSelectorConfig(
            min=0,
            max=30,
            step=0.1,
            mode=selector.NumberSelectorMode.BOX,
            unit_of_measurement="s",
        )
    )


def _device_class_selector():
    return selector.SelectSelector(
        selector.SelectSelectorConfig(
            options=["tv", "receiver", "speaker"],
            mode=selector.SelectSelectorMode.DROPDOWN,
            translation_key="device_class",
        )
    )


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _command_key(typed: Any, keys: list[str]) -> str | None:
    """The command a typed value names, or None.

    "cool/low/24" is accepted for "cool / low / 24", unless a command is
    really named with a slash (e.g. "play/pause").
    """
    text = str(typed or "").strip()
    if text in keys:
        return text
    spaced = " / ".join(part.strip() for part in text.split("/"))
    return spaced if spaced in keys else None


def _learning_remote(hass, controller: str, target: str) -> str | None:
    """The remote entity that can record codes for this entry, if any.

    A Broadlink or LinkNLink controller records with its own remote. With the
    Infrared controller, it is the Broadlink or LinkNLink remote on the same
    device as the emitter (one RM4 exposes both).
    """
    if not target:
        return None
    registry = er.async_get(hass)
    if controller in ("Broadlink", "LinkNLink") and target.startswith("remote."):
        return target
    if controller != "Infrared":
        return None
    emitter = registry.async_get(target)
    if emitter is None or emitter.device_id is None:
        return None
    for entity in er.async_entries_for_device(registry, emitter.device_id):
        if entity.domain == "remote" and entity.platform in LEARNING_PLATFORMS:
            return entity.entity_id
    return None


class MaxSmartIRConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}
        self._test_status: str = ""
        self._pending_name_input: dict[str, Any] = {}

    async def _get_test_command_options(self) -> list[selector.SelectOptionDict]:
        device_data = await async_load_device_data(
            self._data[CONF_DEVICE_CODE],
            self._data[CONF_PLATFORM],
        )
        command_paths = flatten_command_paths(device_data.get("commands", {}))

        return [
            selector.SelectOptionDict(
                value=command_path_to_key(path),
                label=self._label_for_test_path(path),
            )
            for path in command_paths
        ]

    async def _get_default_test_command(self) -> str:
        options = await self._get_test_command_options()
        if not options:
            return ""

        by_value = {option["value"]: option for option in options}
        normalized = {
            value.casefold().replace(" ", "").replace("_", ""): value
            for value in by_value
        }

        for preferred, _label in TEST_COMMAND_PRIORITIES:
            match = normalized.get(preferred.casefold().replace("_", ""))
            if match:
                return match

        return options[0]["value"]

    def _label_for_test_path(self, path: tuple[str, ...]) -> str:
        key = command_path_to_key(path)
        leaf = path[-1].casefold().replace("_", "")

        for preferred, label in TEST_COMMAND_PRIORITIES:
            if leaf == preferred.casefold().replace("_", ""):
                return f"{label} ({key})"

        return key

    async def _async_test_selected_command(self, data: dict[str, Any]) -> str:
        device_data = await async_load_device_data(
            data[CONF_DEVICE_CODE],
            data[CONF_PLATFORM],
        )
        commands = device_data.get("commands", {})

        selected_key = data.get(CONF_TEST_COMMAND) or await self._get_default_test_command()
        if not selected_key:
            raise ValueError("No testable commands were found for this code.")

        command_path = tuple(selected_key.split(" / "))
        command = get_command_value_at_path(commands, command_path)
        if command is None:
            raise ValueError("The selected test command could not be found in this code.")

        controller = get_controller(
            self.hass,
            data[CONF_CONTROLLER],
            device_data["commandsEncoding"],
            data[CONF_CONTROLLER_DATA],
            float(data.get(CONF_DELAY, DEFAULT_DELAY)),
        )
        await controller.send(command)

        return selected_key

    async def _async_show_name_form(
        self,
        user_input: dict[str, Any] | None = None,
        errors: dict[str, str] | None = None,
    ):
        platform = self._data[CONF_PLATFORM]
        code = self._data[CONF_DEVICE_CODE]
        controller = self._data[CONF_CONTROLLER]
        device_data = await async_load_device_data(code, platform)

        default_name = infer_title(
            {
                "platform": platform,
                "device_code": code,
            }
        )
        test_options = await self._get_test_command_options()
        default_test_command = await self._get_default_test_command()

        current_values = {**self._pending_name_input}
        if user_input is not None:
            current_values.update(user_input)

        data_schema: dict[Any, Any] = {
            vol.Required(
                CONF_NAME,
                default=current_values.get(CONF_NAME, default_name),
            ): str,
        }

        if controller == "Infrared":
            infrared_default = (
                current_values.get(CONF_INFRARED_ENTITY)
                or current_values.get(CONF_CONTROLLER_DATA)
            )
            data_schema[
                _optional_entity_key(CONF_INFRARED_ENTITY, infrared_default)
            ] = _infrared_entity_selector()
        else:
            _add_controller_target_fields(data_schema, controller, current_values)

        if platform == "climate":
            data_schema[
                _optional_entity_field(CONF_TEMPERATURE_SENSOR, current_values)
            ] = _temperature_sensor_selector()
            data_schema[
                _optional_entity_field(CONF_HUMIDITY_SENSOR, current_values)
            ] = _humidity_sensor_selector()
            data_schema[
                _optional_entity_field(CONF_POWER_SENSOR, current_values)
            ] = _entity_selector()
            data_schema[
                vol.Optional(
                    CONF_POWER_SENSOR_RESTORE_STATE,
                    default=current_values.get(CONF_POWER_SENSOR_RESTORE_STATE, False),
                )
            ] = bool
            data_schema[
                vol.Optional(
                    CONF_PASSIVE_MODE,
                    default=current_values.get(CONF_PASSIVE_MODE, False),
                )
            ] = bool
            data_schema[
                vol.Optional(
                    CONF_TEMPERATURE_UNIT,
                    default=current_values.get(
                        CONF_TEMPERATURE_UNIT, TEMPERATURE_UNIT_AUTO
                    ),
                )
            ] = _temperature_unit_selector()

        if platform in {"fan", "light", "media_player"}:
            data_schema[
                _optional_entity_field(CONF_POWER_SENSOR, current_values)
            ] = _entity_selector()

        if platform == "media_player":
            data_schema[
                vol.Optional(
                    CONF_DEVICE_CLASS,
                    default=current_values.get(CONF_DEVICE_CLASS, DEFAULT_DEVICE_CLASS),
                )
            ] = str
            data_schema[
                vol.Optional(
                    CONF_SOURCE_NAMES,
                    default=_source_names_default(current_values.get(CONF_SOURCE_NAMES)),
                )
            ] = selector.TextSelector(
                selector.TextSelectorConfig(
                    multiline=True,
                )
            )

        data_schema[
            vol.Optional(
                CONF_DELAY,
                default=current_values.get(CONF_DELAY, DEFAULT_DELAY),
            )
        ] = vol.Coerce(float)

        if test_options:
            data_schema[
                vol.Optional(
                    CONF_TEST_COMMAND,
                    default=current_values.get(CONF_TEST_COMMAND, default_test_command),
                )
            ] = selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=test_options,
                    mode=selector.SelectSelectorMode.DROPDOWN,
                )
            )
            data_schema[
                vol.Optional(
                    CONF_TEST_DEVICE,
                    default=False,
                )
            ] = bool

        data_schema[vol.Optional(CONF_GO_BACK, default=False)] = bool

        return self.async_show_form(
            step_id="name",
            data_schema=vol.Schema(data_schema),
            errors=errors or {},
            description_placeholders={
                "status": self._test_status
                or _build_compatibility_message(
                    controller,
                    device_data["supportedController"],
                    device_data["commandsEncoding"],
                ),
            },
        )

    async def async_step_user(self, user_input=None):
        if user_input is not None:
            self._data[CONF_PLATFORM] = user_input[CONF_PLATFORM]
            return await self.async_step_manufacturer()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_PLATFORM): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=[
                                selector.SelectOptionDict(
                                    value=p,
                                    label=PLATFORM_TITLES[p],
                                )
                                for p in PLATFORMS
                            ],
                            mode=selector.SelectSelectorMode.DROPDOWN,
                        )
                    ),
                }
            ),
        )

    async def async_step_manufacturer(self, user_input=None):
        platform = self._data[CONF_PLATFORM]
        manufacturers = get_manufacturers(platform)

        if user_input is not None:
            self._data["manufacturer"] = user_input["manufacturer"]
            return await self.async_step_model()

        manufacturer_field = vol.Required("manufacturer")
        current_manufacturer = self._data.get("manufacturer")
        if current_manufacturer in manufacturers:
            manufacturer_field = vol.Required(
                "manufacturer", default=current_manufacturer
            )

        return self.async_show_form(
            step_id="manufacturer",
            data_schema=vol.Schema(
                {
                    manufacturer_field: selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=manufacturers,
                            mode=selector.SelectSelectorMode.DROPDOWN,
                        )
                    )
                }
            ),
        )

    async def async_step_model(self, user_input=None):
        platform = self._data[CONF_PLATFORM]
        manufacturer = self._data["manufacturer"]

        models = get_models_for_manufacturer(platform, manufacturer)

        if user_input is not None:
            if user_input.get(CONF_GO_BACK):
                return await self.async_step_manufacturer()
            self._data[CONF_DEVICE_CODE] = int(user_input[CONF_DEVICE_CODE])
            return await self.async_step_controller()

        options = [
            selector.SelectOptionDict(
                value=item["code"],
                label=item["label"],
            )
            for item in models
        ]

        current_code = self._data.get(CONF_DEVICE_CODE)
        matching_default = next(
            (
                item["code"]
                for item in models
                if str(item["code"]) == str(current_code)
            ),
            None,
        )
        device_code_field = vol.Required(CONF_DEVICE_CODE)
        if matching_default is not None:
            device_code_field = vol.Required(
                CONF_DEVICE_CODE, default=matching_default
            )

        return self.async_show_form(
            step_id="model",
            data_schema=vol.Schema(
                {
                    device_code_field: selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=options,
                            mode=selector.SelectSelectorMode.DROPDOWN,
                        )
                    ),
                    vol.Optional(CONF_GO_BACK, default=False): bool,
                }
            ),
        )

    async def async_step_controller(self, user_input=None):
        if user_input is not None:
            if user_input.get(CONF_GO_BACK):
                return await self.async_step_model()
            self._data[CONF_CONTROLLER] = user_input[CONF_CONTROLLER]
            return await self.async_step_name()

        controller_field = vol.Required(CONF_CONTROLLER)
        current_controller = self._data.get(CONF_CONTROLLER)
        if current_controller in CONTROLLERS:
            controller_field = vol.Required(
                CONF_CONTROLLER, default=current_controller
            )

        return self.async_show_form(
            step_id="controller",
            data_schema=vol.Schema(
                {
                    controller_field: selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=CONTROLLERS,
                            mode=selector.SelectSelectorMode.DROPDOWN,
                        )
                    ),
                    vol.Optional(CONF_GO_BACK, default=False): bool,
                }
            ),
        )

    async def async_step_reconfigure(self, user_input=None):
        """Entry point for the "Reconfigure" action on an existing entry.

        Re-walks the same manufacturer -> model -> controller -> name wizard
        used when adding a device, prefilled from the current entry, but
        updates it in place instead of creating a new one - lets a wrong
        codeset be swapped without deleting and recreating the device.
        """
        reconfigure_entry = self._get_reconfigure_entry()
        full_data = dict(reconfigure_entry.data)
        wizard_keys = (CONF_PLATFORM, "manufacturer", CONF_DEVICE_CODE, CONF_CONTROLLER)

        # Only the fields the manufacturer/model/controller steps themselves
        # set belong in self._data - matching what a fresh (non-reconfigure)
        # flow accumulates by the time it reaches the name step. Everything
        # else (controller_data, sensors, etc.) must live only in
        # _pending_name_input and be re-derived fresh at each render;
        # otherwise a stale field like controller_data survives untouched
        # through the whole wizard and collides with a freshly submitted
        # controller_entity for controllers that don't show the text box
        # (e.g. Broadlink), wrongly tripping the "can't set both" check.
        self._data = {
            key: value for key, value in full_data.items() if key in wizard_keys
        }
        self._pending_name_input = {
            key: value
            for key, value in full_data.items()
            if key not in (*wizard_keys, "unique_id")
        }
        return await self.async_step_manufacturer()

    async def async_step_name(self, user_input=None):
        controller = self._data[CONF_CONTROLLER]

        if user_input is not None:
            data = {**self._data, **user_input}
            self._pending_name_input = {
                key: value
                for key, value in user_input.items()
                if key != CONF_TEST_DEVICE
            }

            if user_input.get(CONF_GO_BACK):
                return await self.async_step_controller()

            data[CONF_DEVICE_CODE] = int(data[CONF_DEVICE_CODE])
            data[CONF_DELAY] = float(data.get(CONF_DELAY, DEFAULT_DELAY))

            data["controller"] = controller

            if _controller_target_conflict(data):
                return await self._async_show_name_form(
                    user_input,
                    errors={"base": "controller_target_conflict"},
                )

            _normalize_controller_target(data)

            if not data.get(CONF_CONTROLLER_DATA):
                error_key = _controller_target_error_key(controller)
                return await self._async_show_name_form(
                    user_input,
                    errors={error_key: "required"},
                )

            if user_input.get(CONF_TEST_DEVICE):
                try:
                    tested_command = await self._async_test_selected_command(data)
                except Exception as err:  # noqa: BLE001
                    self._test_status = (
                        "Test failed: "
                        f"{err}"
                    )
                    return await self._async_show_name_form(
                        user_input,
                        errors={"base": "test_failed"},
                    )
                else:
                    self._test_status = (
                        "Test command sent: "
                        f"{tested_command}. Confirm the device reacted, then save."
                    )
                    return await self._async_show_name_form(user_input)

            data.pop(CONF_GO_BACK, None)
            data.pop(CONF_TEST_DEVICE, None)
            data.pop(CONF_TEST_COMMAND, None)

            if self.source == config_entries.SOURCE_RECONFIGURE:
                reconfigure_entry = self._get_reconfigure_entry()
                data["unique_id"] = reconfigure_entry.data.get(
                    "unique_id", reconfigure_entry.unique_id
                )
                # The options win over the entry data, so drop the settings
                # this wizard sets from them: the values just entered are used.
                options = {
                    key: value
                    for key, value in reconfigure_entry.options.items()
                    if key not in data and key not in WIZARD_SETTINGS
                }
                return self.async_update_reload_and_abort(
                    reconfigure_entry,
                    title=data[CONF_NAME],
                    data=data,
                    options=options,
                )

            data["unique_id"] = uuid.uuid4().hex
            await self.async_set_unique_id(data["unique_id"])
            self._abort_if_unique_id_configured()

            return self.async_create_entry(
                title=data[CONF_NAME],
                data=data,
            )

        return await self._async_show_name_form()

    @staticmethod
    def async_get_options_flow(config_entry):
        return MaxSmartIROptionsFlow(config_entry)


class MaxSmartIROptionsFlow(config_entries.OptionsFlow):
    """Options: one form, in sections.

    Name and controller at the top, then Repeat commands (the device's count,
    its delay and the commands with their own count), Custom commands (the
    user's own commands, one button each), Sensors and Advanced. The two lists
    are edited in place; nothing is saved until Submit. Custom commands
    without a code are recorded after Submit, one by one.
    """

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self._config_entry = config_entry
        self._controller_shown: str | None = None
        self._pending: dict[str, Any] = {}
        self._queue: list[str] = []
        self._queue_pos = 0
        self._recorded: dict[str, str] = {}
        self._previous_codes: dict[str, dict[str, Any]] = {}
        self._learner: str | None = None
        self._record_task: asyncio.Task | None = None

    def _config(self) -> dict[str, Any]:
        return {**self._config_entry.data, **self._config_entry.options}

    async def _async_codeset(self, config: dict[str, Any]) -> tuple[list[str], str]:
        """The codeset's command keys, and the line describing the device."""
        platform = config.get(CONF_PLATFORM)
        device_data = await async_load_device_data(config.get(CONF_DEVICE_CODE), platform)
        keys = [
            command_path_to_key(path)
            for path in flatten_command_paths(device_data.get("commands", {}))
        ]
        models = device_data.get("supportedModels") or []
        model = models[0] if isinstance(models, list) and models else ""
        device = " · ".join(
            part
            for part in (
                PLATFORM_TITLES.get(platform, str(platform)),
                f"{device_data.get('manufacturer', '')} {model}".strip(),
            )
            if part
        )
        return keys, f"{device} (code {config.get(CONF_DEVICE_CODE)})"

    # ── values shown in the form ──────────────────────────────────────────────

    def _initial_values(self) -> dict[str, Any]:
        config = self._config()
        customs = parse_custom_commands(config.get(CONF_CUSTOM_COMMANDS))
        repeats = command_repeat_list(config.get(CONF_COMMAND_OVERRIDES)) + [
            {"command": command["name"], "times": command["repeat_count"]}
            for command in customs
            if command.get("repeat_count")
        ]
        controller = config.get(CONF_CONTROLLER, CONTROLLERS[0])
        if controller == "Infrared":
            target = config.get(CONF_INFRARED_ENTITY) or config.get(CONF_CONTROLLER_DATA)
        else:
            target = config.get(CONF_CONTROLLER_DATA)
        return {
            CONF_NAME: config.get(CONF_NAME, self._config_entry.title),
            CONF_CONTROLLER: controller,
            "target": target or "",
            CONF_DEFAULT_REPEAT_COUNT: _as_int(config.get(CONF_DEFAULT_REPEAT_COUNT), DEFAULT_REPEAT_COUNT),
            CONF_DELAY: _as_float(config.get(CONF_DELAY), DEFAULT_DELAY),
            FIELD_COMMAND_REPEATS: repeats,
            FIELD_CUSTOM_LIST: [
                {"name": command["name"], "code": command["code"]} for command in customs
            ],
            CONF_TEMPERATURE_SENSOR: config.get(CONF_TEMPERATURE_SENSOR),
            CONF_HUMIDITY_SENSOR: config.get(CONF_HUMIDITY_SENSOR),
            CONF_POWER_SENSOR: config.get(CONF_POWER_SENSOR),
            CONF_POWER_SENSOR_RESTORE_STATE: bool(config.get(CONF_POWER_SENSOR_RESTORE_STATE, False)),
            CONF_PASSIVE_MODE: bool(config.get(CONF_PASSIVE_MODE, False)),
            CONF_TEMPERATURE_UNIT: config.get(CONF_TEMPERATURE_UNIT, TEMPERATURE_UNIT_AUTO),
            CONF_DEVICE_CLASS: config.get(CONF_DEVICE_CLASS) or DEFAULT_DEVICE_CLASS,
            CONF_SOURCE_NAMES: _source_names_default(config.get(CONF_SOURCE_NAMES)),
        }

    def _values_from_input(self, user_input: dict[str, Any]) -> dict[str, Any]:
        controller = user_input.get(CONF_CONTROLLER) or CONTROLLERS[0]
        values: dict[str, Any] = {
            CONF_NAME: str(user_input.get(CONF_NAME) or "").strip(),
            CONF_CONTROLLER: controller,
            "tuya_conflict": False,
        }
        if controller == "Infrared":
            target = user_input.get(CONF_INFRARED_ENTITY)
        elif controller in REMOTE_ENTITY_CONTROLLERS:
            target = user_input.get(CONF_CONTROLLER_ENTITY)
        elif controller == "Tuya":
            entity = str(user_input.get(CONF_CONTROLLER_ENTITY) or "").strip()
            text = str(user_input.get(TEXT_TARGET_FIELDS["Tuya"]) or "").strip()
            values["tuya_conflict"] = bool(entity and text)
            target = text or entity
        else:
            target = user_input.get(TEXT_TARGET_FIELDS.get(controller, CONF_CONTROLLER_DATA))
        values["target"] = str(target or "").strip()

        repeat = user_input.get(SECTION_REPEAT) or {}
        values[CONF_DEFAULT_REPEAT_COUNT] = _as_int(
            repeat.get(CONF_DEFAULT_REPEAT_COUNT), DEFAULT_REPEAT_COUNT
        )
        values[CONF_DELAY] = _as_float(repeat.get(CONF_DELAY), DEFAULT_DELAY)
        values[FIELD_COMMAND_REPEATS] = list(repeat.get(FIELD_COMMAND_REPEATS) or [])

        custom = user_input.get(SECTION_CUSTOM) or {}
        values[FIELD_CUSTOM_LIST] = list(custom.get(FIELD_CUSTOM_LIST) or [])

        sensors = user_input.get(SECTION_SENSORS) or {}
        advanced = user_input.get(SECTION_ADVANCED) or {}
        values[CONF_TEMPERATURE_SENSOR] = sensors.get(CONF_TEMPERATURE_SENSOR) or None
        values[CONF_HUMIDITY_SENSOR] = sensors.get(CONF_HUMIDITY_SENSOR) or None
        values[CONF_POWER_SENSOR] = sensors.get(CONF_POWER_SENSOR) or None
        values[CONF_POWER_SENSOR_RESTORE_STATE] = bool(
            sensors.get(CONF_POWER_SENSOR_RESTORE_STATE, False)
        )
        values[CONF_PASSIVE_MODE] = bool(advanced.get(CONF_PASSIVE_MODE, False))
        values[CONF_TEMPERATURE_UNIT] = advanced.get(CONF_TEMPERATURE_UNIT, TEMPERATURE_UNIT_AUTO)
        values[CONF_DEVICE_CLASS] = advanced.get(CONF_DEVICE_CLASS) or DEFAULT_DEVICE_CLASS
        values[CONF_SOURCE_NAMES] = advanced.get(CONF_SOURCE_NAMES) or ""
        return values

    # ── the form ──────────────────────────────────────────────────────────────

    def _build_schema(
        self,
        values: dict[str, Any],
        platform: str | None,
        command_keys: list[str],
    ) -> vol.Schema:
        controller = values[CONF_CONTROLLER]
        target = values.get("target") or ""
        schema: dict[Any, Any] = {
            vol.Required(CONF_NAME, default=values[CONF_NAME]): selector.TextSelector(),
            vol.Required(CONF_CONTROLLER, default=controller): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=CONTROLLERS,
                    mode=selector.SelectSelectorMode.DROPDOWN,
                )
            ),
        }
        if controller == "Infrared":
            schema[_optional_entity_key(CONF_INFRARED_ENTITY, target)] = _infrared_entity_selector()
        elif controller in REMOTE_ENTITY_CONTROLLERS:
            schema[_optional_entity_key(CONF_CONTROLLER_ENTITY, target)] = _remote_entity_selector()
        elif controller == "Tuya":
            remote_target = target if _looks_like_remote_entity(target) else ""
            schema[_optional_entity_key(CONF_CONTROLLER_ENTITY, remote_target)] = _remote_entity_selector()
            schema[
                vol.Optional(
                    TEXT_TARGET_FIELDS["Tuya"],
                    default="" if remote_target else target,
                )
            ] = _controller_data_field(controller)
        else:
            schema[
                vol.Optional(
                    TEXT_TARGET_FIELDS.get(controller, CONF_CONTROLLER_DATA),
                    default=target,
                )
            ] = _controller_data_field(controller)

        repeat_schema = {
            vol.Required(
                CONF_DEFAULT_REPEAT_COUNT, default=values[CONF_DEFAULT_REPEAT_COUNT]
            ): _repeat_count_selector(),
            vol.Required(CONF_DELAY, default=values[CONF_DELAY]): _delay_selector(),
            vol.Optional(
                FIELD_COMMAND_REPEATS, default=values[FIELD_COMMAND_REPEATS]
            ): selector.ObjectSelector(
                selector.ObjectSelectorConfig(
                    fields={
                        "command": {
                            "selector": {
                                "select": {
                                    "options": command_keys,
                                    "custom_value": True,
                                    "mode": "dropdown",
                                    "sort": False,
                                }
                            },
                            "required": True,
                        },
                        "times": {
                            "selector": {
                                "number": {"min": 1, "max": 20, "step": 1, "mode": "box"}
                            },
                            "required": True,
                            "default": 2,
                        },
                    },
                    multiple=True,
                    label_field="command",
                    description_field="times",
                    translation_key=FIELD_COMMAND_REPEATS,
                )
            ),
        }
        schema[vol.Required(SECTION_REPEAT)] = section(
            vol.Schema(repeat_schema), {"collapsed": False}
        )

        custom_schema = {
            vol.Optional(
                FIELD_CUSTOM_LIST, default=values[FIELD_CUSTOM_LIST]
            ): selector.ObjectSelector(
                selector.ObjectSelectorConfig(
                    fields={
                        "name": {"selector": {"text": {}}, "required": True},
                        "code": {"selector": {"text": {"multiline": True}}, "required": False},
                    },
                    multiple=True,
                    label_field="name",
                    description_field="code",
                    translation_key=SECTION_CUSTOM,
                )
            ),
        }
        schema[vol.Required(SECTION_CUSTOM)] = section(
            vol.Schema(custom_schema), {"collapsed": False}
        )

        sensors_schema: dict[Any, Any] = {}
        if platform == "climate":
            sensors_schema[
                vol.Optional(
                    CONF_TEMPERATURE_SENSOR,
                    description={"suggested_value": values[CONF_TEMPERATURE_SENSOR]},
                )
            ] = _temperature_sensor_selector()
            sensors_schema[
                vol.Optional(
                    CONF_HUMIDITY_SENSOR,
                    description={"suggested_value": values[CONF_HUMIDITY_SENSOR]},
                )
            ] = _humidity_sensor_selector()
        sensors_schema[
            vol.Optional(
                CONF_POWER_SENSOR,
                description={"suggested_value": values[CONF_POWER_SENSOR]},
            )
        ] = _entity_selector()
        if platform == "climate":
            sensors_schema[
                vol.Required(
                    CONF_POWER_SENSOR_RESTORE_STATE,
                    default=values[CONF_POWER_SENSOR_RESTORE_STATE],
                )
            ] = bool
        schema[vol.Required(SECTION_SENSORS)] = section(
            vol.Schema(sensors_schema), {"collapsed": True}
        )

        advanced_schema: dict[Any, Any] = {}
        if platform == "climate":
            advanced_schema[
                vol.Required(CONF_PASSIVE_MODE, default=values[CONF_PASSIVE_MODE])
            ] = bool
            advanced_schema[
                vol.Required(CONF_TEMPERATURE_UNIT, default=values[CONF_TEMPERATURE_UNIT])
            ] = _temperature_unit_selector()
        if platform == "media_player":
            advanced_schema[
                vol.Required(CONF_DEVICE_CLASS, default=values[CONF_DEVICE_CLASS])
            ] = _device_class_selector()
            advanced_schema[
                vol.Optional(CONF_SOURCE_NAMES, default=values[CONF_SOURCE_NAMES])
            ] = selector.TextSelector(selector.TextSelectorConfig(multiline=True))
        if advanced_schema:
            schema[vol.Required(SECTION_ADVANCED)] = section(
                vol.Schema(advanced_schema), {"collapsed": True}
            )

        return vol.Schema(schema)

    async def _async_show_form(
        self,
        values: dict[str, Any],
        errors: dict[str, str],
    ):
        config = self._config()
        command_keys, device = await self._async_codeset(config)
        custom_names = [
            str(item.get("name") or "").strip()
            for item in values[FIELD_CUSTOM_LIST]
            if str(item.get("name") or "").strip()
        ]
        return self.async_show_form(
            step_id="init",
            data_schema=self._build_schema(
                values, config.get(CONF_PLATFORM), command_keys + custom_names
            ),
            errors=errors,
            description_placeholders={
                "name": values[CONF_NAME] or self._config_entry.title,
                "device": device,
            },
        )

    # ── step: init (the form) ─────────────────────────────────────────────────

    async def async_step_init(self, user_input=None):
        if user_input is None:
            values = self._initial_values()
            self._controller_shown = values[CONF_CONTROLLER]
            return await self._async_show_form(values, {})

        values = self._values_from_input(user_input)

        if values[CONF_CONTROLLER] != self._controller_shown:
            # Another controller sends from another kind of target: show the
            # form again with that controller's field.
            self._controller_shown = values[CONF_CONTROLLER]
            values["target"] = ""
            return await self._async_show_form(values, {"base": "controller_changed"})

        config = self._config()
        codeset_keys, _device = await self._async_codeset(config)
        error = self._validate(values, codeset_keys)
        if error:
            return await self._async_show_form(values, {"base": error})

        options, queue = self._build_options(values, config, codeset_keys)
        if not queue:
            return self.async_create_entry(title="", data=options)

        self._learner = _learning_remote(
            self.hass, values[CONF_CONTROLLER], values["target"]
        )
        if self._learner is None:
            return await self._async_show_form(values, {"base": "no_learning_remote"})

        self._pending = options
        self._queue = queue
        self._queue_pos = 0
        self._recorded = {}
        self._previous_codes = {
            command["name"]: command
            for command in parse_custom_commands(config.get(CONF_CUSTOM_COMMANDS))
            if command.get("code")
        }
        return await self.async_step_record()

    def _validate(self, values: dict[str, Any], codeset_keys: list[str]) -> str | None:
        """The error key for the first problem in the form, or None."""
        if not values[CONF_NAME]:
            return "name_required"
        if values.get("tuya_conflict"):
            return "controller_target_conflict"
        if not values["target"]:
            return "target_required"

        lowered_codeset = {key.lower() for key in codeset_keys}
        custom_names: list[str] = []
        for item in values[FIELD_CUSTOM_LIST]:
            name = str(item.get("name") or "").strip()
            code = str(item.get("code") or "").strip()
            if not name:
                return "custom_name_missing"
            if "/" in name:
                return "custom_name_slash"
            if name.lower() in (known.lower() for known in custom_names):
                return "custom_name_duplicate"
            if name.lower() in lowered_codeset:
                return "custom_name_taken"
            if code and detect_ir_code_encoding(code) is None:
                return "custom_code_invalid"
            custom_names.append(name)

        seen: set[str] = set()
        for item in values[FIELD_COMMAND_REPEATS]:
            key = _command_key(item.get("command"), codeset_keys + custom_names)
            if key is None:
                return "repeat_command_unknown"
            if key in seen:
                return "repeat_command_duplicate"
            times = _as_int(item.get("times"), 0)
            if not 1 <= times <= 20:
                return "repeat_times_invalid"
            seen.add(key)
        return None

    def _build_options(
        self,
        values: dict[str, Any],
        config: dict[str, Any],
        codeset_keys: list[str],
    ) -> tuple[dict[str, Any], list[str]]:
        """The options to save, and the custom commands still to record."""
        controller = values[CONF_CONTROLLER]
        target = values["target"]
        target_fields: dict[str, Any] = {CONF_CONTROLLER: controller}
        if controller == "Infrared":
            target_fields[CONF_INFRARED_ENTITY] = target
        elif controller in REMOTE_ENTITY_CONTROLLERS:
            target_fields[CONF_CONTROLLER_ENTITY] = target
        else:
            target_fields[CONF_CONTROLLER_DATA] = target
        _normalize_controller_target(target_fields)

        platform = config.get(CONF_PLATFORM)
        options: dict[str, Any] = {
            CONF_NAME: values[CONF_NAME],
            **target_fields,
            CONF_DEFAULT_REPEAT_COUNT: values[CONF_DEFAULT_REPEAT_COUNT],
            CONF_DELAY: values[CONF_DELAY],
            # Cleared fields are saved as None so they also clear what the
            # setup wizard stored.
            CONF_POWER_SENSOR: values[CONF_POWER_SENSOR],
        }
        if platform == "climate":
            options[CONF_TEMPERATURE_SENSOR] = values[CONF_TEMPERATURE_SENSOR]
            options[CONF_HUMIDITY_SENSOR] = values[CONF_HUMIDITY_SENSOR]
            options[CONF_POWER_SENSOR_RESTORE_STATE] = bool(
                values[CONF_POWER_SENSOR] and values[CONF_POWER_SENSOR_RESTORE_STATE]
            )
            options[CONF_PASSIVE_MODE] = values[CONF_PASSIVE_MODE]
            options[CONF_TEMPERATURE_UNIT] = values[CONF_TEMPERATURE_UNIT]
        if platform == "media_player":
            options[CONF_DEVICE_CLASS] = values[CONF_DEVICE_CLASS]
            options[CONF_SOURCE_NAMES] = values[CONF_SOURCE_NAMES]

        custom_names = [
            str(item.get("name") or "").strip() for item in values[FIELD_CUSTOM_LIST]
        ]
        repeats: dict[str, int] = {}
        for item in values[FIELD_COMMAND_REPEATS]:
            key = _command_key(item.get("command"), codeset_keys + custom_names)
            if key is not None:
                repeats[key] = _as_int(item.get("times"), DEFAULT_REPEAT_COUNT)

        # One delay for everything: a command's own delay is not kept.
        overrides = clear_all_command_repeats(config.get(CONF_COMMAND_OVERRIDES))
        for key, times in repeats.items():
            if key in codeset_keys:
                overrides = set_command_repeat_at_path(
                    overrides, tuple(key.split(" / ")), times
                )
        options[CONF_COMMAND_OVERRIDES] = overrides

        customs: list[dict[str, Any]] = []
        queue: list[str] = []
        for item in values[FIELD_CUSTOM_LIST]:
            name = str(item.get("name") or "").strip()
            code = str(item.get("code") or "").strip()
            command: dict[str, Any] = {"name": name}
            if code:
                encoding = detect_ir_code_encoding(code) or "Base64"
                command["code"] = normalize_ir_code(code, encoding)
                command["encoding"] = encoding
            else:
                command["code"] = ""
                command["encoding"] = "Base64"
                queue.append(name)
            if name in repeats:
                command["repeat_count"] = repeats[name]
            customs.append(command)
        options[CONF_CUSTOM_COMMANDS] = customs
        return options, queue

    # ── steps: record the custom commands that have no code ──────────────────

    def _record_placeholders(self) -> dict[str, str]:
        state = self.hass.states.get(self._learner) if self._learner else None
        return {
            "name": self._queue[self._queue_pos] if self._queue_pos < len(self._queue) else "",
            "remote": state.name if state is not None else (self._learner or ""),
            "position": str(self._queue_pos + 1),
            "total": str(len(self._queue)),
        }

    async def async_step_record(self, user_input=None):
        if self._record_task is None:
            from . import _async_broadlink_learn

            self._record_task = self.hass.async_create_task(
                _async_broadlink_learn(self.hass, self._learner, RECORD_TIMEOUT)
            )
        if not self._record_task.done():
            return self.async_show_progress(
                step_id="record",
                progress_action="record",
                progress_task=self._record_task,
                description_placeholders=self._record_placeholders(),
            )

        task, self._record_task = self._record_task, None
        try:
            code = task.result()
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("MAX Smart IR: recording a custom command failed: %s", err)
            return self.async_show_progress_done(next_step_id="record_failed")

        self._recorded[self._queue[self._queue_pos]] = code
        self._queue_pos += 1
        if self._queue_pos < len(self._queue):
            return self.async_show_progress_done(next_step_id="record")
        return self.async_show_progress_done(next_step_id="record_finish")

    async def async_step_record_failed(self, user_input=None):
        return self.async_show_menu(
            step_id="record_failed",
            menu_options=["record", "record_skip"],
            description_placeholders=self._record_placeholders(),
        )

    async def async_step_record_skip(self, user_input=None):
        self._queue_pos += 1
        if self._queue_pos < len(self._queue):
            return await self.async_step_record()
        return await self.async_step_record_finish()

    async def async_step_record_finish(self, user_input=None):
        customs = []
        for command in self._pending.get(CONF_CUSTOM_COMMANDS, []):
            if not command.get("code"):
                name = command["name"]
                if name in self._recorded:
                    command = {**command, "code": self._recorded[name], "encoding": "Base64"}
                elif name in self._previous_codes:
                    # Skipped while re-recording: keep the code it had.
                    previous = self._previous_codes[name]
                    command = {**command, "code": previous["code"], "encoding": previous["encoding"]}
                else:
                    # A new command that was skipped is not saved.
                    continue
            customs.append(command)
        self._pending[CONF_CUSTOM_COMMANDS] = customs
        return self.async_create_entry(title="", data=self._pending)
