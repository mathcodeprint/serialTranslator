# Serial Translator Project Instructions

## Project purpose

This project bridges GasWorks and a ProLab analyzer over serial connections.
The primary protocol invariant is:

- GasWorks commands with a CR (`0D`) terminator must reach ProLab as CR+LF
  (`0D 0A`).
- GasWorks commands that already contain CR+LF must not gain an extra LF.
- ProLab-to-GasWorks traffic must be forwarded byte-for-byte unchanged.

Treat serial data as arbitrary bytes. Do not decode, strip, reframe, or
otherwise alter it outside of the documented GasWorks CR normalization.

## Code layout

- `translator.py`: the single application source file. It contains the CLI
  bridge, main Tkinter UI, embedded test clients, and shared port/settings
  helpers.
- Launch the main UI with `--gui`; without that flag, it runs the CLI bridge.
  `--simulate-traffic` provides a no-hardware text protocol demonstration.
- `test_translator.py`: unit tests for normalization and CLI validation.

## Development rules

- Preserve separate configuration for the GasWorks and ProLab ports, including
  their baud rates.
- Keep serial I/O off the Tkinter UI thread.
- Use the shared port/settings helpers in `translator.py`; do not duplicate
  platform-specific discovery behavior.
- On Linux, the Simulate Traffic action owns its two `socat` PTY pairs and the
  four `/tmp` aliases. It must refuse to overwrite existing aliases and clean
  up only relays and aliases it created.
- Keep the main window and its test-client windows compact; prioritize
  connection, simulation, status, and traffic controls over decorative
  whitespace.
- Apply the same compact layout standard to the main translator window while
  keeping the port and serial-settings controls immediately accessible.
- Handle serial disconnects and write failures cleanly; always close open ports.
- Keep diagnostic logs bounded. The bridge uses rotating logs (5 MiB per file,
  three retained backups) rather than unbounded traffic files.
- Close bridge log handlers during shutdown so a subsequent run can immediately
  reopen the log file, particularly on Windows.
- Do not log sensitive customer, instrument, or sample data beyond what is
  necessary for diagnostic traffic logging.
- Avoid relying on private attributes across modules. Expose a small public API
  when a component needs an extension point.
- Do not change default COM-port mappings without updating the README and the
  fake-client topology documentation.

## Verification

For changes to Python code, run at least:

```bash
python3 -m py_compile translator.py test_translator.py
python3 -m unittest discover -v
```

For protocol changes, add or update automated tests covering:

- CR-only commands;
- existing CR+LF commands;
- CR and LF split across separate reads;
- binary/non-ASCII payloads; and
- unchanged ProLab responses.

When serial hardware is available, validate the bridge against the connected
GasWorks and ProLab equipment before delivery.

On POSIX, retain the PTY end-to-end test: it must prove that a CR-only
GasWorks command becomes CR+LF and that a ProLab response is unchanged.

## Documentation and packaging

- Keep `README.md` setup, launch, topology, and test instructions accurate.
- Update dependency and packaging metadata whenever imports or runtime
  requirements change.
- Build the Windows GUI with `scripts/build-windows.bat` on Windows. The
  current distribution target is a PyInstaller `--onedir --windowed` bundle;
  do not try to cross-compile it from Linux.
- GUI logs and saved settings must stay in the user's application-settings
  directory, not the installed application directory.
- Treat generated logs, bytecode, local settings, and packaged executables as
  untracked artifacts; do not commit them unless explicitly requested.
