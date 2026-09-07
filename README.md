<div align="center">

  <a href="https://github.com/MiForge/MiAssistantTool"><img src="https://img.shields.io/badge/MiAssistantTool-%230070FF?style=flat&logo=xiaomi&logoColor=%23FF6900" alt="MiAssistantTool" style="width: 200px; vertical-align: middle;" /></a><br>

  Windows, macOS, Linux, and Termux.

  [![Version](https://img.shields.io/pypi/v/miasst?label=Version&labelColor=black&color=brightgreen)](https://pypi.org/project/miasst/)
  [![Changelog](https://img.shields.io/badge/Changelog-blue?style=flat)](CHANGELOG.md)
  [![License](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](./LICENSE)
  ___

</div>

In Mi Assistant mode, without unlocking the bootloader:

- Read Info
- Flash Official Recovery ROM
- ROMs that can be flashed
- Format Data / Cache / Storage / Data + Storage / FRP
- Wipe Data Storage / EFS
- Reboot / Reboot to recovery / fastboot / bootloader
- Shutdown

___

### Dependencies

* Linux: `sudo apt install libusb-1.0-0`
* macOS: `brew install libusb`
* Termux: `pkg install libusb`
* Windows: No extra steps required (uses standard USB drivers).

### Install

```bash
pip install miasst
```

### Usage

```bash
miasst
```

## Notes

On Termux (without root) you'll need the [Termux:API](https://github.com/termux/termux-api/releases/latest) app, and `pkg install termux-api`.

### Quick Installation (for Termux):

```sh
curl -sS https://raw.githubusercontent.com/MiForge/MiAssistantTool/main/install.sh | bash
```
