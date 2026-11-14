import json
import os
import shutil
import subprocess
import sys
import time

USB_FD_ENV = "TERMUX_USB_FD"
CHOICE_ENV = "MIASSISTANT_CHOICE"

LIST_TIMEOUT = 15
PERMISSION_TIMEOUT = 90
MAX_PERMISSION_ATTEMPTS = 5

_console = None


def get_console():
    global _console
    if _console is None:
        from rich.console import Console

        _console = Console()
    return _console


def running_in_termux_usb_callback():
    return USB_FD_ENV in os.environ


def attach_terminal():
    if not running_in_termux_usb_callback():
        return

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except (OSError, ValueError):
            pass

    try:
        tty_fd = os.open("/dev/tty", os.O_RDWR | getattr(os, "O_NOCTTY", 0))
    except OSError as error:
        os.write(
            2,
            (
                f"miasst: cannot open /dev/tty ({error}).\n"
                "Run it from an interactive Termux session "
                "(not from a background job or a non-tty SSH command).\n"
            ).encode(),
        )
        raise SystemExit(1)

    try:
        for target in (0, 1, 2):
            os.dup2(tty_fd, target)
    finally:
        if tty_fd > 2:
            os.close(tty_fd)

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(line_buffering=True)
        except (AttributeError, ValueError):
            pass


def prompt_choice(core):
    console = get_console()
    from rich.prompt import IntPrompt

    console.print()
    for number, (label, _) in core.ACTION_HANDLERS.items():
        console.print(f"  [bold color(208)]{number}[/]  {label}")
    console.print()

    choices = [str(number) for number in core.ACTION_HANDLERS]
    return int(IntPrompt.ask("Choice", choices=choices, show_choices=False))


def get_usb_devices():
    try:
        result = subprocess.run(
            ["termux-usb", "-l"],
            capture_output=True,
            text=True,
            check=False,
            timeout=LIST_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        get_console().print(
            "[bold red]termux-usb -l timed out.[/] "
            "Is the Termux:API app installed and not frozen by Android?"
        )
        return []
    except FileNotFoundError:
        get_console().print(
            "[bold red]termux-usb not found.[/] Run: pkg install termux-api"
        )
        raise SystemExit(1)

    if result.returncode != 0:
        message = (result.stderr or result.stdout).strip()
        get_console().print(
            f"[bold red]termux-usb -l failed:[/] {message or result.returncode}"
        )
        return []

    try:
        devices = json.loads(result.stdout)
    except json.JSONDecodeError:
        return []
    return devices if isinstance(devices, list) else []


def request_usb_permission(device):
    try:
        result = subprocess.run(
            ["termux-usb", "-r", device],
            capture_output=True,
            text=True,
            check=False,
            timeout=PERMISSION_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return False

    output = (result.stdout + "\n" + result.stderr).lower()
    return result.returncode == 0 and "granted" in output


def wait_for_usb_device():
    console = get_console()
    denied = 0

    with console.status("Waiting for a USB device...", spinner="dots") as status:
        while True:
            devices = get_usb_devices()

            if devices:
                device = devices[0]
                status.update("Requesting USB permission...")

                if request_usb_permission(device):
                    return device

                denied += 1
                if denied >= MAX_PERMISSION_ATTEMPTS:
                    console.print(
                        "[bold red]USB permission was not granted "
                        f"after {denied} attempts.[/]"
                    )
                    raise SystemExit(1)
                console.print(
                    "[bold red]USB permission not granted.[/] "
                    "Please allow access when prompted."
                )
                status.update("Waiting for a USB device...")
                time.sleep(1.0)
                continue

            time.sleep(0.5)


def build_callback_command():
    entry = shutil.which("miasst")
    if entry and not any(ch.isspace() for ch in entry):
        return entry
    if not any(ch.isspace() for ch in sys.executable):
        return f"{sys.executable} -m miassistant.cli"
    raise SystemExit("miasst: cannot build a termux-usb callback (path contains spaces).")


def relaunch_via_termux_usb(choice):
    device = wait_for_usb_device()

    environment = os.environ.copy()
    environment[CHOICE_ENV] = str(choice)

    command = ["termux-usb", "-r", "-E", "-e", build_callback_command(), device]

    get_console().print()
    sys.stdout.flush()
    sys.stderr.flush()
    try:
        os.execvpe(command[0], command, environment)
    except OSError as error:
        raise SystemExit(f"miasst: failed to start termux-usb: {error}")


def resolve_choice(core):
    preset = os.environ.get(CHOICE_ENV)
    if preset is None:
        return prompt_choice(core)
    try:
        choice = int(preset)
    except ValueError:
        raise SystemExit(f"miasst: invalid {CHOICE_ENV}={preset!r}")
    if choice not in core.ACTION_HANDLERS:
        raise SystemExit(f"miasst: unknown action {choice}")
    return choice


def set_wake_lock(enabled):
    command = "termux-wake-lock" if enabled else "termux-wake-unlock"
    if shutil.which(command) is None:
        return
    try:
        subprocess.run(
            [command],
            capture_output=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        pass


def main():
    attach_terminal()

    from miassistant import core, deps

    try:
        deps.ensure_libusb()
        choice = resolve_choice(core)

        if core.is_nonroot_termux() and not running_in_termux_usb_callback():
            relaunch_via_termux_usb(choice)
            return

        _, handler = core.ACTION_HANDLERS[choice]
        set_wake_lock(True)
        try:
            handler()
        finally:
            set_wake_lock(False)

    except KeyboardInterrupt:
        sys.stderr.write("\nInterrupted.\n")
        raise SystemExit(130)


if __name__ == "__main__":
    main()