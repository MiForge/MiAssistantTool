import json
import os
import subprocess
import sys
import time

from miassistant import core

def prompt_choice():
    for n, (label, _) in core.ACTION_HANDLERS.items():
        print(f"  {n} > {label}")
    print()

    while True:
        raw = input("Choice: ").strip()
        if not raw.isdigit() or int(raw) not in core.ACTION_HANDLERS:
            print("Invalid choice, try again.")
            continue
        return int(raw)


def _relaunch_via_termux_usb(choice):
    subprocess.run(["pkill", "-9", "-f", "tcp"])

    while True:
        result = subprocess.run(["termux-usb", "-l"], capture_output=True, text=True)
        try:
            devices = json.loads(result.stdout)
        except json.JSONDecodeError:
            devices = []
        if devices:
            device = devices[0]
            result = subprocess.run(["termux-usb", "-r", device], capture_output=True, text=True)
            if "granted" in result.stdout.lower():
                break
            print("\nUSB permission not granted. Please allow access when prompted.")
        else:
            for i in range(4):
                print(f"\rNo USB devices connected {'.' * (i % 4)}", end="")
                time.sleep(0.5)

    env = os.environ.copy()
    env["MIASSISTANT_CHOICE"] = str(choice)
    subprocess.run(["termux-usb", "-E", "-e", "miasst", device], env=env)
    sys.exit()


def main():
    preset_choice = os.environ.get("MIASSISTANT_CHOICE")
    choice = int(preset_choice) if preset_choice else prompt_choice()

    if core.is_nonroot_termux() and "TERMUX_USB_FD" not in os.environ:
        _relaunch_via_termux_usb(choice)
        return

    _, handler = core.ACTION_HANDLERS[choice]
    handler()


if __name__ == "__main__":
    main()