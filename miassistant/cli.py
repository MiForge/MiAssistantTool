import json
import os
import subprocess
import sys
import time

from miassistant import core

from rich.console import Console
from rich.prompt import IntPrompt

console = Console()

def prompt_choice():
    console.print()
    for n, (label, _) in core.ACTION_HANDLERS.items():
        console.print(f"  [bold color(208)]{n}[/]  {label}")
    console.print()

    choices = [str(n) for n in core.ACTION_HANDLERS]
    return int(IntPrompt.ask("Choice", choices=choices, show_choices=False))


def _relaunch_via_termux_usb(choice):
    subprocess.run(["pkill", "-9", "-f", "tcp"])

    console.print()
    dots = 0
    while True:
        result = subprocess.run(["termux-usb", "-l"], capture_output=True, text=True)
        try:
            devices = json.loads(result.stdout)
        except json.JSONDecodeError:
            devices = []
        if devices:
            sys.stdout.write("\r" + " " * 40 + "\r")
            sys.stdout.flush()
            device = devices[0]
            result = subprocess.run(["termux-usb", "-r", device], capture_output=True, text=True)
            if "granted" in result.stdout.lower():
                break
            console.print("[bold red]USB permission not granted.[/] Please allow access when prompted.")
        else:
            line = f"No USB devices connected{'.' * (dots % 4)}"
            sys.stdout.write("\r" + line.ljust(40))
            sys.stdout.flush()
            dots += 1
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