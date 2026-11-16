# Changelog

All notable changes to this fork are documented here, starting from where
we took over maintenance. For history before that, see the original
[marsh4200/ar_smart_ir](https://github.com/marsh4200/ar_smart_ir).

## Unreleased

- **Options are one form, in sections**, for the device they belong to:
  - At the top, the device (type, brand, model and code; changed with
    Reconfigure), its name and its controller. The controller's target has
    its own label: Infrared emitter, Remote entity, MQTT topic, LOOKin
    address, ESPHome service or Tuya topic or JSON.
  - **Repeat commands**: how many times every command is sent, one delay
    between sends (also used between the codes of a command), and a list of
    commands with their own count, edited in place. Commands are picked
    from a searchable list; `cool/low/24` is accepted for
    `cool / low / 24`.
  - **Custom commands**: your own commands (name and code), edited in
    place. Each one is a button of the device. A code can be pasted
    (Broadlink Base64, Pronto or Raw, checked when you save) or left empty
    to be recorded with the original remote after Submit, through the
    Broadlink or LinkNLink remote next to the emitter.
  - **Sensors** and **Advanced**, collapsed, with what applies to the
    device type.
  - Nothing is saved until Submit.
- A device-wide repeat count: every command that does not have its own
  count is sent that many times. Codesets that set `num_repeats` keep
  theirs. The default is 1, so nothing changes until it is raised.
- One delay: "Delay between sends" replaces "Delay between commands" and is
  also the delay between repeats. A delay saved for a single command is
  dropped.
- Each entry now has a device (named after it, with the codeset's brand and
  model), so its entity and its custom command buttons show together.
- Reconfigure: what it sets (controller, name, sensors, delay) now replaces
  the same settings saved in the options. Before, the options won and a
  controller changed through Reconfigure seemed to have no effect.
- Learning a code with the `learn_command` action no longer pins the command
  to a single send; repeat settings the command had are kept.
- Labels: the integration is called AR Smart IR everywhere, and fields that
  showed their raw key (`controller`, `power_sensor`,
  `power_sensor_restore_state`, `device_class`, `source_names`) have
  labels.
- Removed: the options menu and the per-command screens of the previous
  build, and replacing a codeset command's code from the options. Codes
  already learned that way keep working.

## 1.8.1

- Fix reconfigure flow: `async_step_reconfigure` was copying the entire
  existing entry's data into `self._data`, including `controller_data`,
  which then survived untouched through the whole wizard. A controller
  that only needs the entity picker (e.g. Broadlink) would submit a fresh
  `controller_entity`, and the leftover `controller_data` would still be
  there too - falsely tripping the "enter a remote entity or an MQTT
  topic, not both" conflict check even though only one field was shown.

## 1.8.0

- Add a reconfigure flow - swap manufacturer/model/controller on an
  existing device (e.g. try a different codeset) without deleting and
  recreating it. Reachable via the "Reconfigure" action on the config
  entry; each step defaults to the entry's current value.

## 1.7.5

- First release under this fork. No functional changes from upstream
  v1.7.4 - links, badges, and the "archived" banner updated to point at
  this fork instead of the original.
