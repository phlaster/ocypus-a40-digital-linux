# Ocypus A40 LCD Driver for Linux/Proxmox

A Python-based driver for controlling the LCD display on **Ocypus Iota A40** and **Ocypus Gamma A40 Digital** CPU coolers in Linux environments, including Proxmox.

## ⚠️ Important Disclaimers

- **Hardware Compatibility**: This project supports **Ocypus Iota A40** and **Ocypus Gamma A40 Digital** coolers. Other models are not tested and may not work.
- **Limited Testing**: The driver has been tested on specific hardware configurations. Your mileage may vary.
- **Hardware Detection**: The device appears in `lsusb` as:
  ```
  ID 1a2c:434d China Resource Semico Co., Ltd USB Gaming Keyboard
  Manufacturer: SEMICO
  Product: USB Gaming Keyboard
  ```
- **Use at Your Own Risk**: While the driver is designed to be safe, use it at your own discretion. Incorrect USB HID operations can potentially cause device instability.
- **Community Contribution**: This is a community-created project, not officially supported by Ocypus.

## Development Note

This driver was developed by the community for Ocypus A40 series coolers. The hardware identification shows as a "USB Gaming Keyboard" from China Resource Semico Co., Ltd, which is the actual manufacturer of the LCD controller used in these coolers.

## Features

- **Dual Model Support**: Switch between Iota and Gamma protocols via `--model` argument
- **Auto CPU Detection**: Automatically selects `coretemp` (Intel) or `k10temp` (AMD) sensor based on your processor
- **Auto-detection**: Automatically detects and connects to working HID interfaces
- **Temperature Display**: Shows real-time CPU temperature on the cooler's LCD
- **Dual Units**: Supports both Celsius (°C) and Fahrenheit (°F) temperature display
- **Sensor Flexibility**: Works with any psutil-compatible temperature sensor; manual override available
- **Keep-alive**: Maintains display connection with periodic updates
- **Systemd Integration**: Built-in command to generate and install systemd service
- **Robust Design**: Object-oriented architecture with proper error handling

## Requirements

- Python 3.6+
- Linux operating system (tested on Ubuntu, Debian, Proxmox, CachyOS)
- Root privileges (required for HID device access)
- Dependencies:
  - `hidapi`
  - `psutil`

## Installation

1. **Clone the repository:**
   ```bash
   git clone https://github.com/moyunkz/ocypus-a40-digital-linux.git
   cd ocypus-a40-digital-linux
   ```

2. **Install Python dependencies:**
   ```bash
   pip3 install -r requirements.txt
   ```

3. **Make the script executable:**
   ```bash
   chmod +x ocypus-control.py
   ```

## Usage

### Basic Commands

**List available Ocypus devices:**
```bash
sudo ./ocypus-control.py list
```

**Turn on temperature display (auto-detect CPU, Iota protocol):**
```bash
sudo ./ocypus-control.py on
```

**Turn on temperature display (Gamma A40 Digital protocol):**
```bash
sudo ./ocypus-control.py on --model gamma
```

**Turn on temperature display (Fahrenheit):**
```bash
sudo ./ocypus-control.py on -u f
```

**Turn off/blank the display:**
```bash
sudo ./ocypus-control.py off
```

### Advanced Options

**Specify a custom sensor (override auto-detection):**
```bash
sudo ./ocypus-control.py on -s "coretemp" -u c
```

**Set custom refresh rate (in seconds):**
```bash
sudo ./ocypus-control.py on -r 2.0
```

**Full command with all options:**
```bash
sudo ./ocypus-control.py on -u f -s "k10temp" -r 1.5 -m gamma
```

### Model Selection

| Model | Command | Description |
|-------|---------|-------------|
| Iota A40 | `--model iota` (default) | Original protocol: 65-byte reports, feature reports, 2-digit temp |
| GAMMA A40 DIGITAL | `--model gamma` | Updated protocol: 64-byte reports, output reports, 3-digit temp, magic bytes |

### Systemd Service Installation

**Install as a systemd service (auto-detect settings):**
```bash
sudo ./ocypus-control.py install-service
```

**Install with custom settings for GAMMA model:**
```bash
sudo ./ocypus-control.py install-service --model gamma -u f -s "coretemp" -r 2.0 --name my-ocypus
```

**Enable and start the service:**
```bash
sudo systemctl enable --now ocypus-lcd.service
```

**Check service status:**
```bash
systemctl status ocypus-lcd.service
```

**View service logs:**
```bash
journalctl -u ocypus-lcd.service -f
```

## Command Reference

### Main Commands

| Command | Description |
|---------|-------------|
| `list` | List all found Ocypus cooler devices |
| `on` | Turn on display and stream temperature |
| `off` | Turn off (blank) the display |
| `install-service` | Install systemd unit for background operation |

### Options for `on` command

| Option | Short | Default | Description |
|--------|-------|---------|-------------|
| `--unit` | `-u` | `c` | Temperature unit: `c` for Celsius, `f` for Fahrenheit |
| `--sensor` | `-s` | *auto* | Substring of psutil sensor to use (auto-detects based on CPU) |
| `--rate` | `-r` | `1.0` | Update interval in seconds |
| `--model` | `-m` | `iota` | Cooler model: `iota` or `gamma` |

### Options for `install-service` command

| Option | Default | Description |
|--------|---------|-------------|
| `--unit` | `c` | Temperature unit for the service |
| `--sensor` | *auto* | Sensor substring for the service (auto-detects) |
| `--rate` | `1.0` | Update interval for the service |
| `--model` | `iota` | Cooler model protocol for the service |
| `--name` | `ocypus-lcd` | Name for the systemd unit file |

## Technical Details

### Hardware Compatibility
- **Vendor ID**: `0x1a2c`
- **Product ID**: `0x434d`
- **Interface**: HID (Human Interface Device)

### Protocol Differences

| Parameter | Iota (default) | Gamma |
|-----------|---------------|-------|
| `REPORT_LENGTH` | 65 bytes | 64 bytes |
| Send Method | `send_feature_report()` | `write()` (Output Report) |
| Magic Bytes | None | `0xff` at positions 1, 2 |
| Temperature Format | 2 digits (tens, ones) at bytes 5, 6 | 3 digits (hundreds, tens, ones) at bytes 3, 4, 5 |
| Unit Flag | Byte 7 (`0x00`/`0x01`) | Not used (conversion done in software) |

### Temperature Display
- **Format**: 2-digit (Iota) or 3-digit (Gamma) display with temperature unit indicator
- **Units**: Supports both Celsius (°C) and Fahrenheit (°F)

### Sensor Auto-Detection
The script automatically detects your CPU vendor and selects the appropriate sensor:
- **Intel processors** (`GenuineIntel`): Uses `coretemp`
- **AMD processors** (`AuthenticAMD`): Uses `k10temp`

Manual override is always available via the `-s` / `--sensor` option.

Common sensor names:
- `k10temp` (AMD Ryzen, EPYC, Threadripper)
- `coretemp` (Intel Core, Xeon, Pentium, Celeron)
- `acpi` (ACPI thermal zones, fallback)

## Troubleshooting

### Permission Issues
```
Error: No working Ocypus interface found
```
**Solution**: Ensure you're running the script with `sudo` privileges. HID device access requires root.

### Sensor Not Found
```
Sensor containing 'k10temp' not found
```
**Solution**: 
1. List available sensors: 
   ```bash
   python3 -c "import psutil; print(psutil.sensors_temperatures().keys())"
   ```
2. Use the correct sensor name with `-s` option:
   ```bash
   sudo ./ocypus-control.py on -s "acpi"
   ```

### Device Not Detected
```
No Ocypus cooler found
```
**Solution**:
1. Check USB connection and cable integrity
2. Verify device appears in `lsusb` with ID `1a2c:434d`
3. Try different USB ports (preferably USB 2.0)
4. Ensure no other software is locking the HID interface

### Display Shows No Values
**Solution**: 
1. Ensure you're using `--model gamma` for Gamma A40 Digital, `--model iota` (default) for Iota A40
2. Try different USB interfaces if multiple are detected
2. Verify the protocol constants match your hardware revision
3. Check that the display is not in a locked state (try `off` then `on`)

## Contributing

Contributions are welcome! If you have:
- Fixes for other Ocypus models
- Improvements to protocol handling
- Better error reporting or logging

Please feel free to submit issues, feature requests, or pull requests.

## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.

## Acknowledgments

- Built for the Ocypus A40 cooler community
- Uses `psutil` for cross-platform temperature monitoring
- Implements HID communication via `hidapi` for direct hardware control
- Protocol reverse-engineered by community contributors