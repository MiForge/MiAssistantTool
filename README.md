<div align="center">

  <a href="https://github.com/MiForge/MiUnlockTool/releases/latest"><img src="https://img.shields.io/badge/MiAssistantTool-%230070FF?style=flat&logo=xiaomi&logoColor=%23FF6900" alt="MiAssistantTool" style="width: 200px; vertical-align: middle;" /> </a><br>

  It is compatible with all platforms.

  <img src="https://img.shields.io/github/v/release/MiForge/MiAssistantTool?style=flat&label=Version&labelColor=black&color=brightgreen" alt="Version" /><br>
  <br>
  
</div>

___

in Mi-Assistant mode, without unlocking bootloader:

- Read-Info
- Flash-Official-Recovery-ROM
- Format-Data

___

### [Download ](https://github.com/MiForge/MiAssistantTool/releases/latest)

### Building for other architectures

Not every architecture has a prebuilt binary. To build for yours, clone the repo and compile `miasst.c` + `tiny-json/tiny-json.c`, linking against `libusb-1.0`, `ssl`, `crypto`, and `curl`. See [`.github/workflows/build.yml`](.github/workflows/build.yml) for the exact commands used for each supported platform.