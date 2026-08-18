# Serial Protocol Translator for Windows — v0.2.0

This release provides the Windows installer for the GasWorks ↔ ProLab Serial
Translator.

The application bridges GasWorks and a ProLab analyzer over serial ports. It
converts a GasWorks command terminated with `CR` (`0D`) to `CR+LF` (`0D 0A`)
for ProLab, without adding a second line feed when one is already present.
ProLab responses are sent back to GasWorks unchanged.

## Install

1. Download `Serial-Protocol-Translator-Setup.exe` from this release.
2. Run the installer and follow the prompts.
3. Launch **Serial Protocol Translator** from the Start menu or optional
   desktop shortcut.
4. Select the GasWorks and ProLab ports and their serial settings, then choose
   **Start Bridge**.

## Windows COM-port setup

When GasWorks and the translator run on the same PC, create a virtual null-
modem COM-port pair with [com0com](https://sourceforge.net/projects/com0com/)
or a compatible product. Configure GasWorks and the translator to use opposite
ends of that pair. The ProLab side of the translator should use the analyzer's
physical COM port.

Example topology:

```text
GasWorks (COM5) ↔ virtual pair ↔ Translator source (COM6)
Translator destination (COM4) ↔ ProLab analyzer
```

Do not configure GasWorks and the translator to open the same COM port.

## Included capabilities

- Separate port and serial settings for GasWorks and ProLab
- Automatic reconnect after a disconnected or unavailable port
- Optional start-at-sign-in and start-minimized behavior
- Live traffic display with bounded, rotating diagnostic logs
- A raw serial console with text and hexadecimal-byte send modes
- A Windows test-bench workflow for validating a virtual-COM setup

## Notes

- The installer does not install or configure com0com.
- Saved settings and logs are kept in the current user's application-settings
  directory, not beside the installed application.
- Windows 7 is supported only through a compatible Python 3.7 build; it is a
  legacy operating system and should be isolated where practical.

For full configuration, virtual-port topology, and troubleshooting guidance,
see the project's `README.md`.
